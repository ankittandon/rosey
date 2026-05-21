"""Ephemeral client-secret minter for the Rosey voice PWA.

The browser must NOT hold your OpenAI API key. Instead, the PWA POSTs to this
endpoint, which uses your secret key (server-side, from the OPENAI_API_KEY env
var) to mint a short-lived realtime *client secret*. The browser uses that
ephemeral key to open the WebRTC realtime session directly.

Run locally:
    pip install flask requests --break-system-packages
    OPENAI_API_KEY=sk-... python server/session_server.py

Deploy: this is tiny enough to run alongside Rosey's existing Fly app, or as a
single serverless function. In production, lock CORS down to your PWA origin
(e.g. https://rosey.family) instead of "*".

NOTE: confirm the client-secrets endpoint + request shape against the current
WebRTC guide (https://developers.openai.com/api/docs/guides/realtime-webrtc).
OpenAI has been iterating on the realtime session-creation payload.
"""
import os
import requests
from flask import Flask, request, jsonify, make_response

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CLIENT_SECRETS_URL = "https://api.openai.com/v1/realtime/client_secrets"
DEFAULT_MODEL = os.environ.get("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
TRANSCRIPTION_MODEL = os.environ.get("OPENAI_REALTIME_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe")
VAD_SILENCE_MS = int(os.environ.get("OPENAI_REALTIME_VAD_SILENCE_MS", "650"))

MCP_SERVER_LABEL = os.environ.get("ROSEY_MCP_SERVER_LABEL", "rosey")
MCP_SERVER_URL = os.environ.get("ROSEY_MCP_SERVER_URL", "https://rosey.fly.dev/mcp")
MCP_ALLOWED_TOOLS = [
    "get_household", "remember",
    "list_grocery_items", "add_grocery_item",
    "list_reminders", "add_reminder",
    "log_feed",
]
MCP_AUTO_RUN_TOOLS = MCP_ALLOWED_TOOLS

SYSTEM_PROMPT = (
    "You are Rosey, a warm, concise household assistant for this family. "
    "For tool-backed questions, call the needed tool silently before speaking. "
    "Do not say 'let me check', 'one moment', or similar filler as a standalone reply. "
    "Fetch or change the thing, then answer in the same turn. "
    "To record a baby feeding, call log_feed (NOT remember) so it lands in the shared "
    "feed log; default the time to now unless the user gives one. "
    "Memory files can be long logs with timestamps and status notes. Do NOT read them "
    "verbatim. Extract only what was asked: if asked for tomorrow's reminders, read just "
    "tomorrow's, as a short spoken list of the task text, skipping ids, timestamps, "
    "acknowledgement/escalation metadata, and other machine tags. Keep replies short, "
    "spoken-friendly, and finish your sentences fully."
)

# Lock this to your PWA origin in production.
ALLOWED_ORIGIN = os.environ.get("ROSEY_PWA_ORIGIN", "*")


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


@app.route("/session", methods=["OPTIONS"])
def session_preflight():
    return _cors(make_response("", 204))


@app.route("/session", methods=["POST"])
def create_session():
    if not OPENAI_API_KEY:
        return _cors(jsonify({"error": "OPENAI_API_KEY not set on the server"})), 500

    body = request.get_json(silent=True) or {}
    model = body.get("model", DEFAULT_MODEL)

    tools = []
    if MCP_SERVER_URL:
        tools.append({
            "type": "mcp",
            "server_label": MCP_SERVER_LABEL,
            "server_url": MCP_SERVER_URL,
            "allowed_tools": MCP_ALLOWED_TOOLS,
            "require_approval": "never"
            if len(MCP_AUTO_RUN_TOOLS) == len(MCP_ALLOWED_TOOLS)
            else {"never": {"tool_names": MCP_AUTO_RUN_TOOLS}},
        })

    # Mint the ephemeral client secret with the full session config already
    # attached. This lets Realtime start loading MCP tools before the browser's
    # first audio turn, avoiding the "let me check" then silence race.
    payload = {
        "session": {
            "type": "realtime",
            "model": model,
            "instructions": SYSTEM_PROMPT,
            "audio": {
                "input": {
                    "transcription": {
                        "model": TRANSCRIPTION_MODEL,
                        "language": "en",
                        "prompt": "Listen for the wake word Rosey, also commonly transcribed as Rosie.",
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "silence_duration_ms": VAD_SILENCE_MS,
                        "create_response": False,
                        # Alexa-style barge-in is handled client-side: regular
                        # speech does not interrupt, but the wake word "Rosey"
                        # cancels the current response and queues the next one.
                        "interrupt_response": False,
                    },
                },
            },
            "tools": tools,
            # Optionally pin a safety identifier (hashed household/user id):
            # "safety_identifier": "household-<hashed-id>",
        }
    }

    try:
        r = requests.post(
            CLIENT_SECRETS_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
    except requests.RequestException as e:
        return _cors(jsonify({"error": f"upstream request failed: {e}"})), 502

    if r.status_code >= 400:
        return _cors(jsonify({"error": "openai error", "detail": r.text})), r.status_code

    # Returns { "client_secret": { "value": "...", "expires_at": ... }, ... }
    return _cors(jsonify(r.json()))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    app.run(host="0.0.0.0", port=port)
