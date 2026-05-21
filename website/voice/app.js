// Rosey wake-word PWA
// =====================================================================
// Flow:  ASLEEP (waiting for wake) -> wake -> open WebRTC Realtime session
//          (gpt-realtime-2) -> converse, with Rosey's tools attached as a
//          remote MCP server -> graceful end (VAD silence + follow-up window +
//          end_conversation tool + hard timeout) -> close session -> ASLEEP.
//
// The paid OpenAI session only spans wake -> graceful-close (~20-60s).
//
// WAKE ENGINE — choose how Rosey wakes (CONFIG.WAKE_ENGINE):
//   "tap"        Tap the orb to wake. Zero dependencies, fully private.
//                Best for first tests and demos. Always available as override.
//   "webspeech"  Say "Rosey". Uses the browser SpeechRecognition API — works
//                today with no signup, BUT streams ambient audio to the
//                browser's STT (Google on Chrome). NOT on-device; a stopgap,
//                not for the privacy-first brand story.
//   "porcupine"  Say "Rosey", fully on-device. Requires a Picovoice AccessKey
//                + Rosey.ppn (their console is gated behind approval as of now).
//                Kept ready: set the key + drop the model files, flip the flag.
//
// SETUP:
//   1. SESSION_ENDPOINT: the Fly token server (mints OpenAI client secrets so
//      the API key never lives in the browser). Already deployed.
//   2. MCP_SERVER_URL: your Rosey MCP adapter (memory/reminders/grocery). Until
//      set, Rosey talks but can't touch household state.
// =====================================================================

const CONFIG = {
  // "tap" | "webspeech" | "porcupine"
  WAKE_ENGINE: "tap",

  WAKE_WORD_LABEL: "Rosey",
  PICOVOICE_ACCESS_KEY: "<YOUR_PICOVOICE_ACCESS_KEY>",
  PORCUPINE_KEYWORD_PATH: "./models/Rosey.ppn",     // custom keyword from Picovoice console
  PORCUPINE_MODEL_PATH: "./models/porcupine_params.pv",

  // Backend that mints a short-lived OpenAI realtime client secret.
  // NEVER put your OpenAI API key in this file. See voice-token-server/.
  // Option A: the token server runs on its own small Fly app, so this is an
  // absolute cross-origin URL (the server sends CORS headers for rosey.family).
  // Update the app name here if you name the Fly app something else.
  SESSION_ENDPOINT: "https://rosey-voice-token.fly.dev/session",

  REALTIME_MODEL: "gpt-realtime-2",
  // OpenAI WebRTC SDP exchange endpoint for voice-agent sessions.
  // Confirm against the WebRTC guide before shipping.
  REALTIME_CALLS_URL: "https://api.openai.com/v1/realtime/calls",

  // Rosey's tools, exposed as a remote MCP server (rosey-mcp/). Tool names
  // must match server.py. Keep the surface narrow.
  MCP_SERVER_LABEL: "rosey",
  // Co-located MCP endpoint on the rosey engine app (shared household memory:
  // same /data/memories the WhatsApp/Telegram bot uses). On standard 443 because
  // OpenAI's realtime backend only connects to MCP servers on 443.
  // (The standalone rosey-mcp.fly.dev/mcp is the isolated-memory variant.)
  MCP_SERVER_URL: "https://rosey.fly.dev/mcp",
  MCP_ALLOWED_TOOLS: [
    "get_household", "remember",
    "list_grocery_items", "add_grocery_item",
    "list_reminders", "add_reminder",
  ],
  // All of these are low-risk household actions, so auto-run them (no approval
  // prompt) for a smooth hands-free experience. When you add genuinely sensitive
  // tools later (e.g. send_message), leave them OUT of AUTO_RUN so they gate.
  MCP_AUTO_RUN_TOOLS: [
    "get_household", "remember",
    "list_grocery_items", "add_grocery_item",
    "list_reminders", "add_reminder",
  ],

  // Lifecycle timings
  FOLLOWUP_WINDOW_MS: 8000,   // after Rosey replies, keep listening this long for a follow-up
  HARD_TIMEOUT_MS: 90000,     // absolute cap on a single wake session (cost insurance)
  VAD_SILENCE_MS: 1500,       // server VAD: silence that ends a user turn

  SYSTEM_PROMPT:
    "You are Rosey, a warm, concise household assistant for this family. " +
    "Always use your tools to actually fetch or change things before replying — " +
    "if asked what's on the grocery list, CALL list_grocery_items and read back the " +
    "real result; never say you'll 'check' or 'look into it' without calling the tool. " +
    "Keep replies short and spoken-friendly. Finish your sentences fully; the user " +
    "can ask a follow-up after you're done.",
};

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------
const STATE = { ASLEEP: "asleep", LISTENING: "listening", THINKING: "thinking", ERROR: "error" };
let state = "boot";
let porcupine = null;
let pc = null;              // RTCPeerConnection for the active session
let dc = null;             // data channel for realtime events
let remoteAudioEl = null;
let followupTimer = null;
let hardTimer = null;
let started = false;
let wakeLock = null;

const els = {
  body: document.body,
  status: document.getElementById("status"),
  transcript: document.getElementById("transcript"),
  startBtn: document.getElementById("start"),
};

function setState(s, statusText) {
  state = s;
  els.body.dataset.state = s;
  if (statusText !== undefined) els.status.textContent = statusText;
}

function setTranscript(t) { els.transcript.textContent = t || ""; }

// ---------------------------------------------------------------------
// Chimes (simple WebAudio tones so we don't ship audio files)
// ---------------------------------------------------------------------
let audioCtx = null;
function chime(freq, durationMs) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = freq;
    osc.type = "sine";
    gain.gain.setValueAtTime(0.0001, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.2, audioCtx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + durationMs / 1000);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + durationMs / 1000);
  } catch (e) { /* non-fatal */ }
}
const wakeChime = () => chime(660, 180);
const sleepChime = () => chime(330, 220);

// ---------------------------------------------------------------------
// Screen wake lock — keep the screen on so the mic isn't suspended.
// ---------------------------------------------------------------------
async function acquireWakeLock() {
  try {
    if ("wakeLock" in navigator) {
      wakeLock = await navigator.wakeLock.request("screen");
      wakeLock.addEventListener("release", () => { wakeLock = null; });
    }
  } catch (e) { console.warn("wakeLock failed", e); }
}
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !wakeLock) acquireWakeLock();
});

// ---------------------------------------------------------------------
// Wake — dispatches on CONFIG.WAKE_ENGINE. The rest of the app only cares
// about onWake(); swapping engines never touches realtime/lifecycle code.
// ---------------------------------------------------------------------
let speechRec = null;   // Web Speech recognizer (webspeech engine)
let wvp = null;         // Picovoice WebVoiceProcessor (porcupine engine)
let wakeActive = false; // armed to detect a wake?

async function startWakeWord() {
  wakeActive = true;
  if (CONFIG.WAKE_ENGINE === "porcupine") return startPorcupine();
  if (CONFIG.WAKE_ENGINE === "webspeech") return startWebSpeech();
  // "tap": nothing to listen for — tapping the orb calls onWake() (see boot).
  setState(STATE.ASLEEP, "Tap the circle to talk to Rosey");
}

async function stopWakeWord() {
  wakeActive = false;
  if (CONFIG.WAKE_ENGINE === "porcupine") return stopPorcupine();
  if (CONFIG.WAKE_ENGINE === "webspeech") return stopWebSpeech();
}

// --- webspeech engine: browser SpeechRecognition (NOT on-device) ---
function startWebSpeech() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { setState(STATE.ERROR, "No SpeechRecognition here — use tap mode."); return; }
  speechRec = new SR();
  speechRec.continuous = true;
  speechRec.interimResults = true;
  speechRec.lang = "en-US";
  speechRec.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const t = e.results[i][0].transcript.toLowerCase();
      if (t.includes(CONFIG.WAKE_WORD_LABEL.toLowerCase())) { onWake(); return; }
    }
  };
  // SpeechRecognition stops itself periodically; restart while armed.
  speechRec.onend = () => {
    if (wakeActive && CONFIG.WAKE_ENGINE === "webspeech") {
      try { speechRec.start(); } catch (e) {}
    }
  };
  speechRec.onerror = (e) => {
    if (e.error === "not-allowed") setState(STATE.ERROR, "Mic blocked — allow mic & reload.");
  };
  try { speechRec.start(); } catch (e) {}
  setState(STATE.ASLEEP, 'Say "Rosey" — or tap the circle');
}

function stopWebSpeech() {
  try { if (speechRec) { speechRec.onend = null; speechRec.stop(); } } catch (e) {}
  speechRec = null;
}

// --- porcupine engine: on-device keyword (needs Picovoice approval) ---
async function startPorcupine() {
  const [{ PorcupineWorker }, { WebVoiceProcessor }] = await Promise.all([
    import("https://cdn.jsdelivr.net/npm/@picovoice/porcupine-web@3.0.3/dist/iife/index.js"),
    import("https://cdn.jsdelivr.net/npm/@picovoice/web-voice-processor@4.0.9/dist/iife/index.js"),
  ]);
  wvp = WebVoiceProcessor;
  porcupine = await PorcupineWorker.create(
    CONFIG.PICOVOICE_ACCESS_KEY,
    { label: CONFIG.WAKE_WORD_LABEL, publicPath: CONFIG.PORCUPINE_KEYWORD_PATH },
    onWake,
    { publicPath: CONFIG.PORCUPINE_MODEL_PATH }
  );
  await wvp.subscribe(porcupine);
  setState(STATE.ASLEEP, 'Say "Rosey" to wake me');
}

async function stopPorcupine() {
  try { if (wvp && porcupine) await wvp.unsubscribe(porcupine); } catch (e) {}
  try { porcupine && porcupine.release(); } catch (e) {}
  porcupine = null;
}

// Any engine (or an orb tap) calls this to wake.
async function onWake() {
  if (state === STATE.LISTENING || state === STATE.THINKING) return; // already awake
  wakeChime();
  await stopWakeWord();   // release the mic before WebRTC grabs it
  await openSession();
}

// ---------------------------------------------------------------------
// Realtime session (WebRTC -> gpt-realtime-2)
// ---------------------------------------------------------------------
async function openSession() {
  setState(STATE.LISTENING, "Listening…");
  setTranscript("");

  try {
    // 1) Mint a short-lived client secret from our backend.
    const secretResp = await fetch(CONFIG.SESSION_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: CONFIG.REALTIME_MODEL }),
    });
    if (!secretResp.ok) throw new Error("session endpoint failed: " + secretResp.status);
    // The client-secrets endpoint returns the ephemeral key at top-level `value`
    // (current shape: { value, expires_at, session }), but older shapes nested
    // it under client_secret. Handle both so we don't pass an undefined token.
    const secret = await secretResp.json();
    const ephemeralKey =
      secret.value || secret.client_secret?.value || secret.client_secret;
    if (!ephemeralKey) throw new Error("no ephemeral key in /session response");

    // 2) Capture mic and set up the peer connection.
    // Echo cancellation is critical: without it, on a device with speaker+mic
    // Rosey hears her own voice, server VAD treats it as you interrupting, and
    // cuts her off mid-sentence. (Headphones also eliminate this entirely.)
    const mic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    pc = new RTCPeerConnection();
    pc.addTrack(mic.getAudioTracks()[0], mic);

    remoteAudioEl = remoteAudioEl || new Audio();
    remoteAudioEl.autoplay = true;
    pc.ontrack = (e) => { remoteAudioEl.srcObject = e.streams[0]; };

    // 3) Data channel for realtime events.
    dc = pc.createDataChannel("oai-events");
    dc.onopen = () => configureSession();
    dc.onmessage = (e) => handleEvent(JSON.parse(e.data));

    // 4) SDP offer/answer with OpenAI.
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const sdpResp = await fetch(`${CONFIG.REALTIME_CALLS_URL}?model=${CONFIG.REALTIME_MODEL}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${ephemeralKey}`, "Content-Type": "application/sdp" },
      body: offer.sdp,
    });
    if (!sdpResp.ok) throw new Error("realtime SDP failed: " + sdpResp.status);
    await pc.setRemoteDescription({ type: "answer", sdp: await sdpResp.text() });

    // 5) Safety net: hard cap on session length.
    hardTimer = setTimeout(() => endSession("hard-timeout"), CONFIG.HARD_TIMEOUT_MS);
  } catch (err) {
    console.error(err);
    setState(STATE.ERROR, "Couldn't connect. Going back to sleep.");
    setTimeout(() => endSession("connect-error"), 1500);
  }
}

// Configure the session once the data channel is open: instructions, VAD,
// and tools. The Rosey MCP server is attached ONLY when MCP_SERVER_URL is set
// to a real URL — so you can test the talk->sleep loop first, then wire MCP
// later without touching this file's structure.
function mcpConfigured() {
  const u = CONFIG.MCP_SERVER_URL || "";
  return u.startsWith("http") && !u.includes("YOUR_ROSEY_MCP_SERVER_URL");
}

function configureSession() {
  // No end_conversation tool: it was firing mid-reply and cutting Rosey off.
  // Sessions end naturally on the silence follow-up window (or hard timeout).
  const tools = [];

  if (mcpConfigured()) {
    const requireApproval =
      CONFIG.MCP_AUTO_RUN_TOOLS.length === CONFIG.MCP_ALLOWED_TOOLS.length
        ? "never"
        : { never: { tool_names: CONFIG.MCP_AUTO_RUN_TOOLS } };
    tools.unshift({
      type: "mcp",
      server_label: CONFIG.MCP_SERVER_LABEL,
      server_url: CONFIG.MCP_SERVER_URL,
      allowed_tools: CONFIG.MCP_ALLOWED_TOOLS,
      require_approval: requireApproval,
    });
  } else {
    console.warn("MCP_SERVER_URL not set — running without Rosey tools (talk-only).");
  }

  send({
    type: "session.update",
    session: {
      type: "realtime",
      instructions: CONFIG.SYSTEM_PROMPT,
      // turn_detection lives under audio.input in the current realtime schema
      // (confirmed by the /session response shape), not at the top of session.
      audio: {
        input: {
          // interrupt_response:false — don't let detected speech (including echo of
      // Rosey's own voice on speaker+mic setups) cut off her reply mid-sentence.
      turn_detection: {
        type: "server_vad",
        silence_duration_ms: CONFIG.VAD_SILENCE_MS,
        interrupt_response: false,
      },
        },
      },
      tools,
    },
  });
}

function send(obj) { if (dc && dc.readyState === "open") dc.send(JSON.stringify(obj)); }

// ---------------------------------------------------------------------
// Realtime event handling
// ---------------------------------------------------------------------
function handleEvent(evt) {
  switch (evt.type) {
    case "input_audio_buffer.speech_started":
      clearTimeout(followupTimer);
      setState(STATE.LISTENING, "Listening…");
      break;

    case "response.created":
      setState(STATE.THINKING, "…");
      break;

    // Live transcript of Rosey's spoken reply.
    case "response.output_audio_transcript.delta":
      setTranscript((els.transcript.textContent || "") + (evt.delta || ""));
      break;

    // A turn finished. Open a short follow-up window; if the user stays
    // silent past it, gracefully end.
    case "response.done":
      setState(STATE.LISTENING, "Anything else?");
      armFollowupWindow();
      break;

    // --- MCP lifecycle logging (so tool problems are visible, not silent) ---
    case "mcp_list_tools.in_progress":
      console.log("[mcp] importing tools from", CONFIG.MCP_SERVER_URL, "…");
      break;
    case "mcp_list_tools.completed":
      console.log("[mcp] tools imported OK");
      break;
    case "mcp_list_tools.failed":
      console.error("[mcp] TOOL IMPORT FAILED — server unreachable or bad /mcp endpoint:", evt);
      break;
    case "response.mcp_call_arguments.done":
      console.log("[mcp] calling tool, args:", evt.arguments);
      break;
    case "response.mcp_call.in_progress":
      console.log("[mcp] tool running…");
      break;
    case "response.mcp_call.failed":
      console.error("[mcp] TOOL CALL FAILED:", evt);
      break;

    case "conversation.item.done":
      // Which tools actually loaded?
      if (evt.item?.type === "mcp_list_tools") {
        const names = (evt.item.tools || []).map((t) => t.name).join(", ");
        console.log("[mcp] available tools:", names || "(NONE — that's the problem)");
      }
      // Approval gate for any tool not in MCP_AUTO_RUN_TOOLS.
      if (evt.item?.type === "mcp_approval_request") {
        send({
          type: "conversation.item.create",
          item: { type: "mcp_approval_response", approval_request_id: evt.item.id, approve: true },
        });
      }
      break;

    case "response.output_item.done":
      if (evt.item?.type === "mcp_call") {
        console.log(`[mcp] ${evt.item.server_label}.${evt.item.name} ->`, evt.item.output);
      }
      break;

    case "error":
      console.error("realtime error", evt);
      break;
  }
}

function armFollowupWindow() {
  clearTimeout(followupTimer);
  followupTimer = setTimeout(() => endSession("no-followup"), CONFIG.FOLLOWUP_WINDOW_MS);
}

// ---------------------------------------------------------------------
// Graceful end -> back to sleep
// ---------------------------------------------------------------------
async function endSession(reason) {
  clearTimeout(followupTimer);
  clearTimeout(hardTimer);
  console.log("ending session:", reason);
  sleepChime();
  setTranscript("");

  try { dc && dc.close(); } catch (e) {}
  try {
    if (pc) {
      pc.getSenders().forEach((s) => s.track && s.track.stop());
      pc.close();
    }
  } catch (e) {}
  pc = null; dc = null;

  // Resume free on-device wake-word listening.
  await startWakeWord();
}

// ---------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------
els.startBtn.addEventListener("click", async () => {
  if (started) return;
  started = true;
  els.startBtn.hidden = true;
  setState(STATE.ASLEEP, "Starting…");
  await acquireWakeLock();
  try {
    await startWakeWord();
  } catch (err) {
    console.error(err);
    setState(STATE.ERROR, "Mic/setup failed. Check config & permissions.");
    started = false;
    els.startBtn.hidden = false;
  }
});

// Tap-to-talk: tapping the orb wakes Rosey from sleep. This is the entire
// interaction in "tap" mode, and a manual override in the wake-word modes.
// The tap itself is the user gesture that unlocks mic capture. Ignored mid-call.
document.getElementById("orb").addEventListener("click", () => {
  if (started && state === STATE.ASLEEP) onWake();
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("./sw.js").catch((e) => console.warn("SW reg failed", e));
}
