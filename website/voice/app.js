// Rosey wake-word PWA
// =====================================================================
// Flow:  ASLEEP (waiting for wake) -> wake -> mint a configured OpenAI session
//          -> open WebRTC Realtime session (gpt-realtime-2) -> wait for tools
//          -> converse, with Rosey's tools attached as a remote MCP server
//          -> graceful end (VAD silence + follow-up window + hard timeout)
//          -> close session -> ASLEEP.
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
    "log_feed", "amend_last_feed", "list_feeds",
  ],
  // All of these are low-risk household actions, so auto-run them (no approval
  // prompt) for a smooth hands-free experience. When you add genuinely sensitive
  // tools later (e.g. send_message), leave them OUT of AUTO_RUN so they gate.
  MCP_AUTO_RUN_TOOLS: [
    "get_household", "remember",
    "list_grocery_items", "add_grocery_item",
    "list_reminders", "add_reminder",
    "log_feed", "amend_last_feed", "list_feeds",
  ],

  // Lifecycle timings
  FOLLOWUP_WINDOW_MS: 8000,   // after Rosey replies, keep listening this long for a follow-up
  HARD_TIMEOUT_MS: 90000,     // absolute cap on a single wake session (cost insurance)
  READY_TIMEOUT_MS: 5000,     // don't stay muted forever if tool setup fails
  VAD_SILENCE_MS: 650,        // fallback only; token server owns the real value
  WAKE_TRANSCRIPT_RE: /\b(rosey|rosie|rozey|rosy)\b/i,
  WAKE_WORD_ONLY_RE: /\b(rosey|rosie|rozey|rosy)\b/gi,

  SYSTEM_PROMPT:
    "You are Rosey, a warm, concise household assistant for this family. " +
    "For tool-backed questions, call the needed tool silently before speaking. " +
    "Do not say 'let me check', 'one moment', or similar filler as a standalone reply. " +
    "Fetch or change the thing, then answer in the same turn. " +
    "To record a baby feeding, call log_feed (NOT remember): give the type (breast " +
    "left/right or bottle) and a measure — ounces for a bottle, minutes for a breastfeed. " +
    "If the type or amount/duration is missing, ASK a brief question to confirm before " +
    "logging; never log a half-empty feed. The time defaults to now. To fix the feed you " +
    "just logged (e.g. 'there was poop too'), call amend_last_feed, don't log again. " +
    "For ANY question about feeds (when, how much, daily totals) call list_feeds — you " +
    "CAN see the feed log through it, so never say you can't. " +
    "Memory files can be long logs with timestamps and status notes. Do NOT read them " +
    "verbatim. Extract only what was asked: if asked for tomorrow's reminders, read just " +
    "tomorrow's, as a short spoken list of the task text (skip ids, timestamps, ack/escalation " +
    "metadata). Keep replies short and spoken-friendly, and finish your sentences fully.",
};

// ---------------------------------------------------------------------
// State
// ---------------------------------------------------------------------
const STATE = { ASLEEP: "asleep", LISTENING: "listening", THINKING: "thinking", ERROR: "error" };
let state = "boot";
let porcupine = null;
let pc = null;              // RTCPeerConnection for the active session
let dc = null;             // data channel for realtime events
let micTrack = null;
let remoteAudioEl = null;
let followupTimer = null;
let hardTimer = null;
let readyTimer = null;
let started = false;
let wakeLock = null;
let realtimeReady = false;
let expectMcpTools = false;
let serverConfiguredSession = false;
let responseActive = false;
let wakeInterruptSent = false;
let pendingResponseAfterCancel = false;
let inputTranscripts = new Map();
let respondedInputItems = new Set();

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
  setState(STATE.THINKING, "Waking Rosey…");
  setTranscript("");
  realtimeReady = false;
  expectMcpTools = false;
  serverConfiguredSession = false;
  responseActive = false;
  wakeInterruptSent = false;
  pendingResponseAfterCancel = false;
  respondedInputItems.clear();

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
    const session = secret.session || {};
    const sessionTools = session.tools || [];
    serverConfiguredSession = !!session.instructions || sessionTools.length > 0;
    expectMcpTools = sessionTools.some((t) => t.type === "mcp") || mcpConfigured();

    // 2) Capture mic and set up the peer connection.
    // Echo cancellation is critical: without it, on a device with speaker+mic
    // Rosey hears her own voice, server VAD treats it as you interrupting, and
    // cuts her off mid-sentence. (Headphones also eliminate this entirely.)
    const mic = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    micTrack = mic.getAudioTracks()[0];
    micTrack.enabled = false;
    pc = new RTCPeerConnection();
    pc.addTrack(micTrack, mic);

    remoteAudioEl = remoteAudioEl || new Audio();
    remoteAudioEl.autoplay = true;
    pc.ontrack = (e) => { remoteAudioEl.srcObject = e.streams[0]; };

    // 3) Data channel for realtime events.
    dc = pc.createDataChannel("oai-events");
    dc.onopen = () => {
      if (serverConfiguredSession) {
        setState(STATE.THINKING, expectMcpTools ? "Loading Rosey's tools…" : "Almost ready…");
        if (!expectMcpTools) markRealtimeReady("data-channel-open");
      } else {
        configureSession();
      }
      armReadyTimeout();
    };
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
      // Fallback for older token servers. The deployed token server now creates
      // sessions with this config already attached, which avoids first-turn tool
      // loading races.
      audio: {
        input: {
          turn_detection: {
            type: "server_vad",
            silence_duration_ms: CONFIG.VAD_SILENCE_MS,
            create_response: false,
            interrupt_response: false,
          },
          transcription: {
            model: "gpt-4o-mini-transcribe",
            language: "en",
            prompt: "Listen for the wake word Rosey, also commonly transcribed as Rosie.",
          },
        },
      },
      tools,
    },
  });
}

function send(obj) { if (dc && dc.readyState === "open") dc.send(JSON.stringify(obj)); }

function setMicEnabled(enabled) {
  if (micTrack) micTrack.enabled = enabled;
}

function markRealtimeReady(reason) {
  if (realtimeReady) return;
  realtimeReady = true;
  clearTimeout(readyTimer);
  setMicEnabled(true);
  console.log("realtime ready:", reason);
  setState(STATE.LISTENING, "Listening…");
}

function armReadyTimeout() {
  clearTimeout(readyTimer);
  readyTimer = setTimeout(() => {
    if (!realtimeReady) {
      console.warn("Realtime setup still pending; enabling mic so the session is not stuck.");
      markRealtimeReady("ready-timeout");
    }
  }, CONFIG.READY_TIMEOUT_MS);
}

function isTranscriptionPromptEcho(transcript) {
  return /^\s*listen for the wake word rosey\b/i.test(transcript || "");
}

function hasWakeWord(transcript) {
  return CONFIG.WAKE_TRANSCRIPT_RE.test(transcript || "");
}

function commandAfterWakeWord(transcript) {
  return (transcript || "")
    .replace(CONFIG.WAKE_WORD_ONLY_RE, " ")
    .replace(/\b(hey|hi|hello|okay|ok)\b/gi, " ")
    .replace(/[^\w\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function createResponse(reason) {
  if (responseActive || !realtimeReady) return false;
  console.log("creating response:", reason);
  send({ type: "response.create" });
  return true;
}

function maybeWakeInterrupt(transcript) {
  if (!responseActive || wakeInterruptSent || !transcript) return false;
  if (isTranscriptionPromptEcho(transcript) || !hasWakeWord(transcript)) return false;

  wakeInterruptSent = true;
  pendingResponseAfterCancel = commandAfterWakeWord(transcript).length > 0;
  clearTimeout(followupTimer);
  console.log("wake-word interrupt:", transcript);
  send({ type: "response.cancel" });
  setTranscript("");
  setState(STATE.LISTENING, "Listening…");
  return true;
}

function rememberInputTranscript(evt) {
  const itemId = evt.item_id || "_latest";
  const next = evt.transcript || ((inputTranscripts.get(itemId) || "") + (evt.delta || ""));
  inputTranscripts.set(itemId, next);
  if (evt.type === "conversation.item.input_audio_transcription.delta") {
    maybeWakeInterrupt(next);
  }
  return { itemId, transcript: next };
}

function maybeRespondToCompletedInput(evt) {
  const { itemId, transcript } = rememberInputTranscript(evt);
  if (respondedInputItems.has(itemId) || isTranscriptionPromptEcho(transcript)) return;

  if (responseActive) {
    if (maybeWakeInterrupt(transcript)) respondedInputItems.add(itemId);
    return;
  }

  if (!commandAfterWakeWord(transcript)) return;
  if (createResponse(`input:${itemId}`)) respondedInputItems.add(itemId);
}

// ---------------------------------------------------------------------
// Realtime event handling
// ---------------------------------------------------------------------
function handleEvent(evt) {
  switch (evt.type) {
    case "session.updated":
      if (!expectMcpTools) markRealtimeReady("session-updated");
      break;

    case "input_audio_buffer.speech_started":
      clearTimeout(followupTimer);
      inputTranscripts.clear();
      setState(STATE.LISTENING, "Listening…");
      break;

    case "response.created":
      // A new response is starting (often the second response that reads the
      // tool result). Cancel any pending follow-up/sleep timer so we don't
      // end the session mid-answer during a tool round-trip.
      clearTimeout(followupTimer);
      responseActive = true;
      wakeInterruptSent = false;
      setState(STATE.THINKING, "…");
      break;

    // Live transcript of Rosey's spoken reply.
    case "response.output_audio_transcript.delta":
      setTranscript((els.transcript.textContent || "") + (evt.delta || ""));
      break;

    // A turn finished. Open a short follow-up window; if the user stays
    // silent past it, gracefully end.
    case "response.done":
      responseActive = false;
      if (pendingResponseAfterCancel) {
        pendingResponseAfterCancel = false;
        createResponse("wake-word-after-cancel");
        break;
      }
      setState(STATE.LISTENING, "Anything else?");
      armFollowupWindow();
      break;

    case "conversation.item.input_audio_transcription.delta":
      rememberInputTranscript(evt);
      break;

    case "conversation.item.input_audio_transcription.completed":
      maybeRespondToCompletedInput(evt);
      break;

    // --- MCP lifecycle logging (so tool problems are visible, not silent) ---
    case "mcp_list_tools.in_progress":
      console.log("[mcp] importing tools from", CONFIG.MCP_SERVER_URL, "…");
      break;
    case "mcp_list_tools.completed":
      console.log("[mcp] tools imported OK");
      markRealtimeReady("mcp-tools-loaded");
      break;
    case "mcp_list_tools.failed":
      console.error("[mcp] TOOL IMPORT FAILED — server unreachable or bad /mcp endpoint:", evt);
      markRealtimeReady("mcp-tools-failed");
      break;
    case "response.mcp_call_arguments.done":
      console.log("[mcp] calling tool, args:", evt.arguments);
      break;
    case "response.mcp_call.in_progress":
      // Tool is running — keep the session alive; the answer comes after.
      clearTimeout(followupTimer);
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
  clearTimeout(readyTimer);
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
  pc = null; dc = null; micTrack = null;
  realtimeReady = false;
  expectMcpTools = false;
  serverConfiguredSession = false;
  responseActive = false;
  wakeInterruptSent = false;
  pendingResponseAfterCancel = false;
  inputTranscripts.clear();
  respondedInputItems.clear();

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
