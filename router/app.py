"""Router app: Telegram webhook entry point.

For each inbound Telegram update, dispatch one of:
  - **new_chat_members event** → the bot was just added to a group. Look
    up the inviter, link the group's chat_id to their household, post a
    welcome message in the group.
  - **group message** (chat.id < 0) → look up the household by the
    group's chat_id, forward to that household's VM.
  - **DM from a known member** → forward to that household's VM.
  - **DM from an unknown sender** → hand to the Telegram-native
    onboarding FSM (v1 single-question OR v2 six-step, picked by the
    /start payload).

Identifier scheme: ``tg:NNN`` where NNN is the Telegram chat_id (or
sender's user_id for group routing).
"""
from __future__ import annotations

import base64
import hmac
import json as json_mod
import logging
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv
from flask import Flask, Response, abort, request

from . import db, notifications, provisioning, telegram_onboarding

FEEDBACK_PREFIXES = ("/feedback", "/contact", "/support")

load_dotenv()
log = logging.getLogger("rosey.router")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = Flask(__name__)
engine = db.get_engine()
db.init_db(engine)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def _is_feedback(body: str) -> bool:
    lower = body.lower().lstrip()
    return any(lower == p or lower.startswith(p + " ") for p in FEEDBACK_PREFIXES)


def _strip_feedback_prefix(body: str) -> str:
    lower = body.lower().lstrip()
    for p in FEEDBACK_PREFIXES:
        if lower.startswith(p):
            return body.lstrip()[len(p):].strip()
    return body.strip()


def _send_feedback(member: dict, sender_id: str, message: str) -> None:
    """Forward a /feedback message from a household member to the operator
    via Telegram. Operator chat_id configured via env var.
    """
    operator_id = os.environ.get("ROSEY_OPERATOR_TELEGRAM_ID")
    if not operator_id:
        log.warning("feedback dropped — ROSEY_OPERATOR_TELEGRAM_ID not set")
        return

    text = (
        f"📝 Rosey feedback from {member['name']} ({sender_id})\n"
        f"household {member['household_id'][:8]}\n\n"
        f"{message}"
    )
    if notifications.send_text(int(operator_id), text):
        log.info("feedback forwarded to operator from=%s len=%d", sender_id, len(message))
    else:
        log.warning("feedback forward failed from=%s", sender_id)


def _post_to_household(fly_app_name: str, path: str, payload: dict) -> bool:
    """POST JSON to a household VM endpoint over 6PN. Returns True on 2xx."""
    base = os.environ.get("ROSEY_HOUSEHOLD_BASE_URL")
    if base:
        url = f"{base.rstrip('/')}{path}"
    else:
        url = f"http://{fly_app_name}.internal:8080{path}"
    token = os.environ.get("ROSEY_INTERNAL_TOKEN")
    headers = {
        "X-Rosey-Internal-Token": token or "",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        log.info("posted %s to %s status=%d", path, fly_app_name, r.status_code)
        return 200 <= r.status_code < 300
    except Exception:
        log.exception("post %s to %s failed", path, fly_app_name)
        return False


def _forward_to_household_telegram(fly_app_name: str, payload: dict) -> None:
    """POST a parsed Telegram message to the household VM's /telegram endpoint."""
    _post_to_household(fly_app_name, "/telegram", payload)


# ---------------------------------------------------------------------------
# Telegram payload parsing
# ---------------------------------------------------------------------------

@dataclass
class TelegramUpdate:
    """Parsed view of one Telegram update — only the bits the router uses.

    All fields default to None / empty so individual branches can check
    just what they need without nested ``get`` calls everywhere.
    """
    chat_id: Optional[int] = None
    chat_type: str = ""              # "private", "group", "supergroup", "channel"
    text: str = ""
    start_payload: str = ""           # parsed from "/start <payload>"
    is_start_command: bool = False    # True if original text was "/start" or "/start ..."
    from_user_id: Optional[int] = None
    from_username: str = ""           # lowercased, no "@"
    first_name: str = ""
    photo_file_id: Optional[str] = None
    new_chat_members: list = None
    is_bot_added_to_group: bool = False  # convenience: bot was in new_chat_members


def _telegram_extract(update: dict) -> TelegramUpdate:
    """Parse the bits of a Telegram update we care about."""
    out = TelegramUpdate(new_chat_members=[])
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return out

    chat = msg.get("chat") or {}
    out.chat_id = chat.get("id")
    out.chat_type = chat.get("type") or ""

    sender = msg.get("from") or {}
    out.from_user_id = sender.get("id")
    out.from_username = (sender.get("username") or "").lower()
    out.first_name = sender.get("first_name") or ""

    text = (msg.get("text") or msg.get("caption") or "").strip()
    out.is_start_command = text.lower().startswith("/start")
    out.text, out.start_payload = _parse_start_payload(text)

    # Photo: forward the largest variant to the household VM
    photo = msg.get("photo") or []
    out.photo_file_id = photo[-1]["file_id"] if photo else None

    # new_chat_members fires when the bot (or anyone) is added to a group.
    out.new_chat_members = msg.get("new_chat_members") or []
    bot_username = (os.environ.get("TELEGRAM_BOT_USERNAME") or "").lower().lstrip("@")
    if out.new_chat_members and bot_username:
        out.is_bot_added_to_group = any(
            (m.get("is_bot") and (m.get("username") or "").lower() == bot_username)
            for m in out.new_chat_members
        )

    return out


def _parse_start_payload(text: str) -> tuple:
    """Split ``/start <payload>`` into ('', payload). For any other text,
    returns (text, ''). Plain ``/start`` returns ('', '')."""
    stripped = text.strip()
    if stripped == "/start":
        return "", ""
    if stripped.lower().startswith("/start "):
        payload = stripped[len("/start "):].strip()
        return "", payload
    return text, ""


def _download_telegram_photo(file_id: str) -> tuple:
    """Resolve a Telegram file_id → (bytes, mime). Returns (None, None) on
    failure. Telegram serves photos as JPEG.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot download photo")
        return None, None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/getFile",
            json={"file_id": file_id},
            timeout=10,
        )
        r.raise_for_status()
        file_path = r.json().get("result", {}).get("file_path")
        if not file_path:
            log.warning("getFile returned no file_path")
            return None, None
        r = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}",
            timeout=30,
        )
        r.raise_for_status()
        return r.content, "image/jpeg"
    except Exception:
        log.exception("photo download failed for file_id=%s", file_id)
        return None, None


def _validate_telegram_secret() -> None:
    """Telegram lets us register a webhook with a secret_token; it sends it
    back as X-Telegram-Bot-Api-Secret-Token. Use it as a cheap auth check."""
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        log.warning("TELEGRAM_WEBHOOK_SECRET not set — skipping telegram auth")
        return
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not provided or not hmac.compare_digest(provided, expected):
        log.warning("rejected telegram webhook with bad secret_token")
        abort(403)


# ---------------------------------------------------------------------------
# Telegram inbound webhook — dispatcher
# ---------------------------------------------------------------------------

@app.post("/telegram")
def telegram_webhook() -> Response:
    _validate_telegram_secret()

    update = request.get_json(silent=True) or {}
    parsed = _telegram_extract(update)

    if parsed.chat_id is None:
        return Response("", status=200)

    # Branch 1: bot was added to a group → link group to household.
    if parsed.is_bot_added_to_group:
        _handle_bot_added_to_group(parsed)
        return Response("", status=200)

    # Branch 2: group message (the bot is already a member, someone said
    # something in the chat). Route by group's chat_id.
    if parsed.chat_type in ("group", "supergroup") and parsed.chat_id < 0:
        _handle_group_message(parsed)
        return Response("", status=200)

    # Branch 3: DM. The chat_id IS the user's user_id for private chats.
    if (
        not parsed.text
        and not parsed.photo_file_id
        and not parsed.start_payload
        and not parsed.is_start_command
    ):
        # Voice notes, stickers, edited messages we don't care about, etc.
        return Response("", status=200)

    _handle_dm(parsed)
    return Response("", status=200)


# ---------------------------------------------------------------------------
# Branch handlers
# ---------------------------------------------------------------------------

def _handle_bot_added_to_group(parsed: TelegramUpdate) -> None:
    """Bot was just added to a group. Link the group to a household via
    the inviter (Telegram doesn't echo the startgroup payload back to the
    bot, so we identify the household by who invited us).
    """
    inviter_id = f"tg:{parsed.from_user_id}"
    inviter = db.lookup_household(engine, inviter_id)
    group_chat_id = parsed.chat_id

    if not inviter:
        # Random group adds the bot without ever onboarding → say so and leave.
        log.info(
            "bot added to group %s by unknown user %s — leaving",
            group_chat_id, inviter_id,
        )
        notifications.send_text(
            group_chat_id,
            "Hi! I'm Rosey, but I need an onboarded household to function. "
            "Anyone here can DM @"
            + (os.environ.get("TELEGRAM_BOT_USERNAME") or "the bot")
            + " to start a free household first. Bye for now 👋",
        )
        notifications.leave_chat(group_chat_id)
        return

    existing = inviter.get("group_chat_id")
    if existing and str(existing) != str(group_chat_id):
        log.info(
            "bot added to NEW group %s for household %s (was %s) — relinking",
            group_chat_id, inviter["household_id"], existing,
        )

    db.set_group_chat_id(engine, inviter["household_id"], group_chat_id)
    threading.Thread(
        target=_post_to_household,
        args=(
            inviter["fly_app_name"],
            "/admin/link-group",
            {"group_chat_id": group_chat_id},
        ),
        daemon=True,
    ).start()
    notifications.send_text(
        group_chat_id,
        "Hi everyone — I'm Rosey 👋\n\n"
        "Anyone here can text me to add to the shopping list, set a reminder, "
        "ask what's on the calendar, or share what's going on. I learn over time.\n\n"
        "Try \"add milk to the list\" or \"remind me Friday at 9am to call Mom\".",
    )


def _handle_group_message(parsed: TelegramUpdate) -> None:
    """A message in a group the bot is a member of. Look up the household
    by the group's chat_id and forward."""
    household = db.lookup_household_by_group(engine, parsed.chat_id)
    if household is None:
        log.warning(
            "group message in unknown group %s from %s — ignoring",
            parsed.chat_id, parsed.from_user_id,
        )
        return

    payload = {
        "chat_id": parsed.chat_id,           # the group's chat_id
        "from_user_id": parsed.from_user_id,  # who actually said it
        "text": parsed.text,
        "name": parsed.first_name,
        "from_username": parsed.from_username,
        "is_group": True,
    }
    if parsed.photo_file_id:
        img_bytes, img_mime = _download_telegram_photo(parsed.photo_file_id)
        if img_bytes:
            payload["image_b64"] = base64.b64encode(img_bytes).decode("ascii")
            payload["image_mime"] = img_mime
    threading.Thread(
        target=_forward_to_household_telegram,
        args=(household["fly_app_name"], payload),
        daemon=True,
    ).start()


def _handle_dm(parsed: TelegramUpdate) -> None:
    """A private-chat message from one human to the bot. Known sender →
    forward to household. Unknown sender → onboarding FSM."""
    chat_id = parsed.chat_id
    sender_id = f"tg:{chat_id}"

    member = db.lookup_household(engine, sender_id)
    if member:
        _handle_known_member_dm(parsed, member)
        return

    _handle_unknown_dm(parsed)


def _handle_known_member_dm(parsed: TelegramUpdate, member: dict) -> None:
    """DM from an active household member. Handle /invite, /feedback,
    otherwise forward to household VM."""
    chat_id = parsed.chat_id
    sender_id = f"tg:{chat_id}"
    text = parsed.text

    # /invite <name> — admin command to generate an invite code
    if text.lower().lstrip().startswith("/invite"):
        invitee = text.lstrip()[len("/invite"):].strip()
        if not invitee:
            notifications.send_text(
                chat_id,
                "Send: /invite <their name> — I'll give you a code to share with them.",
            )
            return
        code = telegram_onboarding.generate_invite_code(engine, member, invitee)
        bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "the Rosey bot")
        notifications.send_text(
            chat_id,
            f"Share this with {invitee}:\n\n"
            f"Open @{bot_username} and send: {code}\n\n"
            "(Expires in 7 days.)",
        )
        return

    # /feedback intercept (text-only)
    if text and _is_feedback(text):
        note = _strip_feedback_prefix(text)
        if not note:
            notifications.send_text(
                chat_id,
                "Send /feedback followed by your message — I'll pass it on.",
            )
            return
        threading.Thread(
            target=_send_feedback,
            args=(member, sender_id, note),
            daemon=True,
        ).start()
        notifications.send_text(chat_id, "Got it — passed your message along. 🙏")
        return

    # Forward to household VM
    payload = {
        "chat_id": chat_id,
        "from_user_id": parsed.from_user_id,
        "text": text,
        "name": parsed.first_name,
        "from_username": parsed.from_username,
        "is_group": False,
    }
    if parsed.photo_file_id:
        img_bytes, img_mime = _download_telegram_photo(parsed.photo_file_id)
        if img_bytes:
            payload["image_b64"] = base64.b64encode(img_bytes).decode("ascii")
            payload["image_mime"] = img_mime
    threading.Thread(
        target=_forward_to_household_telegram,
        args=(member["fly_app_name"], payload),
        daemon=True,
    ).start()


def _handle_unknown_dm(parsed: TelegramUpdate) -> None:
    """DM from someone not in any household. Hand to onboarding FSM."""
    chat_id = parsed.chat_id
    sender_id = f"tg:{chat_id}"

    reply = telegram_onboarding.handle(
        engine,
        chat_id,
        parsed.first_name,
        parsed.text,
        payload=parsed.start_payload,
    )

    # If the reply implies a member was just added (invite redemption),
    # propagate to the household VM's household.md.
    if reply.text.startswith("🎉"):
        added = db.lookup_household(engine, sender_id)
        if added:
            threading.Thread(
                target=_post_to_household,
                args=(
                    added["fly_app_name"],
                    "/admin/add-member",
                    {"name": added["name"], "identifier": sender_id},
                ),
                daemon=True,
            ).start()

    if reply.trigger_provisioning:
        provisioning.kick_off(engine, sender_id)

    notifications.send_text(chat_id, reply.text)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> Response:
    return Response("ok", mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8081)))
