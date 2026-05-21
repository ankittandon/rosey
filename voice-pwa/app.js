// Rosey wake-word PWA
// =====================================================================
// Flow:  ASLEEP (Porcupine listens on-device, free)
//          -> hears "Rosey" -> open WebRTC Realtime session (gpt-realtime-2)
//          -> converse, with Rosey's tools attached as a remote MCP server
//          -> graceful end (VAD silence + follow-up window + end_conversation
//             tool + hard timeout) -> close session -> ASLEEP again.
//
// The paid OpenAI session only spans wake -> graceful-close (~20-60s).
// Everything before/after is free on-device wake-word listening.
//
// SETUP (fill these in — see CONFIG below):
//   1. Picovoice access key + a custom "Rosey" keyword file (Rosey.ppn).
//      Generate at https://console.picovoice.ai  (free tier).
//      Drop Rosey.ppn and porcupine_params.pv into voice-pwa/models/.
//   2. SESSION_ENDPOINT: deploy server/session_server.py somewhere (e.g. your
//      Fly app) so the OpenAI API key never lives in the browser.
//   3. MCP_SERVER_URL: your Rosey MCP server (the adapter that exposes
//      memory/reminders/grocery as MCP tools). allowed_tools narrows the surface.
// =====================================================================

import { PorcupineWorker } from "https://cdn.jsdelivr.net/npm/@picovoice/porcupine-web@3.0.3/dist/iife/index.js";
import { WebVoiceProcessor } from "https://cdn.jsdelivr.net/npm/@picovoice/web-voice-processor@4.0.9/dist/iife/index.js";

const CONFIG = {
  WAKE_WORD_LABEL: "Rosey",
  PICOVOICE_ACCESS_KEY: "<YOUR_PICOVOICE_ACCESS_KEY>",
  PORCUPINE_KEYWORD_PATH: "./models/Rosey.ppn",     // custom keyword from Picovoice console
  PORCUPINE_MODEL_PATH: "./models/porcupine_params.pv",

  // Backend that mints a short-lived OpenAI realtime client secret.
  // NEVER put your OpenAI API key in this file. See server/session_server.py.
  SESSION_ENDPOINT: "/session",

  REALTIME_MODEL: "gpt-realtime-2",
  // OpenAI WebRTC SDP exchange endpoint for voice-agent sessions.
  // Confirm against the WebRTC guide before shipping.
  REALTIME_CALLS_URL: "https://api.openai.com/v1/realtime/calls",

  // Rosey's tools, exposed as a remote MCP server. Keep the surface narrow.
  MCP_SERVER_LABEL: "rosey",
  MCP_SERVER_URL: "<YOUR_ROSEY_MCP_SERVER_URL>",
  MCP_ALLOWED_TOOLS: ["memory_read", "memory_write", "add_grocery_item", "add_reminder", "list_reminders"],
  // Auto-run reads; gate writes/sends. Tools NOT listed here default to requiring approval.
  MCP_AUTO_RUN_TOOLS: ["memory_read", "list_reminders"],

  // Lifecycle timings
  FOLLOWUP_WINDOW_MS: 4000,   // after Rosey replies, keep listening this long for a follow-up
  HARD_TIMEOUT_MS: 90000,     // absolute cap on a single wake session (cost insurance)
  VAD_SILENCE_MS: 1500,       // server VAD: silence that ends a user turn

  SYSTEM_PROMPT:
    "You are Rosey, a warm, concise household assistant for this family. " +
    "Answer from the household's shared memory via your tools. Keep replies short and spoken-friendly. " +
    "When the user is clearly done (says thanks/bye, or there's nothing left to do), call end_conversation.",
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
// Wake-word listening (on-device, free)
// ---------------------------------------------------------------------
async function startWakeWord() {
  porcupine = await PorcupineWorker.create(
    CONFIG.PICOVOICE_ACCESS_KEY,
    { label: CONFIG.WAKE_WORD_LABEL, publicPath: CONFIG.PORCUPINE_KEYWORD_PATH },
    onWake,
    { publicPath: CONFIG.PORCUPINE_MODEL_PATH }
  );
  await WebVoiceProcessor.subscribe(porcupine);
  setState(STATE.ASLEEP, 'Say "Rosey" to wake me');
}

async function stopWakeWord() {
  try { await WebVoiceProcessor.unsubscribe(porcupine); } catch (e) {}
  try { porcupine && porcupine.release(); } catch (e) {}
  porcupine = null;
}

// Porcupine fires this when it hears "Rosey".
async function onWake() {
  if (state === STATE.LISTENING || state === STATE.THINKING) return; // already awake
  wakeChime();
  // Release the mic from Porcupine before WebRTC grabs it (avoid contention).
  await stopWakeWord();
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
    const { client_secret } = await secretResp.json();
    const ephemeralKey = client_secret?.value || client_secret;

    // 2) Capture mic and set up the peer connection.
    const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
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
// and tools (the Rosey MCP server + a local end_conversation function tool).
function configureSession() {
  const requireApproval =
    CONFIG.MCP_AUTO_RUN_TOOLS.length === CONFIG.MCP_ALLOWED_TOOLS.length
      ? "never"
      : { never: { tool_names: CONFIG.MCP_AUTO_RUN_TOOLS } };

  send({
    type: "session.update",
    session: {
      type: "realtime",
      instructions: CONFIG.SYSTEM_PROMPT,
      turn_detection: { type: "server_vad", silence_duration_ms: CONFIG.VAD_SILENCE_MS },
      tools: [
        {
          type: "mcp",
          server_label: CONFIG.MCP_SERVER_LABEL,
          server_url: CONFIG.MCP_SERVER_URL,
          allowed_tools: CONFIG.MCP_ALLOWED_TOOLS,
          require_approval: requireApproval,
        },
        {
          type: "function",
          name: "end_conversation",
          description: "End the conversation and go back to sleep when the user is done.",
          parameters: { type: "object", properties: {}, required: [] },
        },
      ],
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

    // Local function tool: explicit goodbye.
    case "response.function_call_arguments.done":
      if (evt.name === "end_conversation") endSession("goodbye-tool");
      break;

    // MCP approval gate for any tool not in MCP_AUTO_RUN_TOOLS.
    case "conversation.item.done":
      if (evt.item?.type === "mcp_approval_request") {
        // For an unattended kitchen device, auto-approve known tools; otherwise
        // you'd surface a confirm UI. Here we approve the allowed surface.
        send({
          type: "conversation.item.create",
          item: { type: "mcp_approval_response", approval_request_id: evt.item.id, approve: true },
        });
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

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("./sw.js").catch((e) => console.warn("SW reg failed", e));
}
