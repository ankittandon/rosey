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
DEFAULT_MODEL = "gpt-realtime-2"

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

    # Mint the ephemeral client secret. The session's tools/instructions can be
    # set here, or (as the PWA does) via session.update once the data channel
    # opens. Keeping it minimal here; the PWA configures tools client-side.
    payload = {
        "session": {
            "type": "realtime",
            "model": model,
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
