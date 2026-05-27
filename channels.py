"""Outbound message dispatch by identifier prefix.

Identifier scheme:
    tg:NNN          → Telegram chat_id
    wa:+E164        → WhatsApp phone number (e.g. wa:+15551234567)

A note on rich text: Telegram supports `parse_mode="HTML"` (or
"MarkdownV2") and renders things like `<a href="tg://user?id=NNN">N</a>`
mention links. WhatsApp's Cloud API has no equivalent — any HTML in the
body is shown literally. So a reminder body crafted for Telegram with
HTML mentions would render as gibberish in WhatsApp ("<a href=...">N</a>"
shown verbatim). We handle this by stripping HTML in `send_whatsapp`
before posting to Meta, so callers can send the same Telegram-flavored
body to multi-channel recipients without per-channel branching.

To add a new channel: implement `send_<channel>(target, body) -> ...`,
add a branch in `send_returning_msg_id()`, and teach `roster.py` to
recognize the prefix.

Two return-type variants:
  send(...) -> bool               legacy callers; True/False on success
  send_returning_msg_id(...)      returns the platform message_id, or
                                   None on failure. Needed by the
                                   reminder lifecycle for reply-to-bot
                                   ack lookup (Telegram only — WhatsApp
                                   ack works via natural-language path).
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.error
import urllib.request

log = logging.getLogger("rosey.channels")

# Regex stripper for HTML tags. We don't need a full HTML parser — the
# bodies coming through here only ever contain the simple subset Telegram
# accepts (<a>, <b>, <i>, <code>, <pre>). A bracket-stripping regex plus
# entity-unescape is sufficient and keeps channels.py dependency-free.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

TELEGRAM_TEXT_LIMIT = 4096
WHATSAPP_TEXT_LIMIT = 4096
WHATSAPP_API_BASE = "https://graph.facebook.com/v25.0"


def send(identifier: str, body: str, parse_mode: str | None = None,
         mentions: list[str] | None = None) -> bool:
    """Dispatch outbound message by identifier prefix.

    Returns True on success, False if creds are missing or the API
    call failed. Errors are logged, not raised — fan-out callers should
    keep going for the other recipients.
    """
    return send_returning_msg_id(
        identifier, body, parse_mode=parse_mode, mentions=mentions,
    ) is not None


def send_returning_msg_id(
    identifier: str, body: str, parse_mode: str | None = None,
    mentions: list[str] | None = None,
) -> int | str | None:
    """Like send() but returns the platform message_id on success.

    `mentions` (WhatsApp only): a list of JIDs to @-ping; the body must contain
    the matching `@<user-part>` token(s). Ignored for Telegram.

    Return type varies by channel:
      - Telegram: int (Telegram's numeric `message.message_id`)
      - WhatsApp: str  (Meta's wamid string, e.g. "wamid.HBgM...")
      - None     on failure or unknown channel

    `parse_mode` is Telegram-specific; ignored for other channels.
    """
    if identifier.startswith("tg:"):
        return send_telegram(identifier[len("tg:"):], body, parse_mode=parse_mode)
    if identifier.startswith("wa:"):
        # Two flavors of wa: identifier:
        #   wa:+15551234567       → DM to that phone number
        #   wa:group:120363xx@g.us → message into that group (Baileys only)
        # For DMs we strip the leading `+` (Cloud API wants raw digits;
        # Baileys's normalizer accepts either). For groups we leave the
        # `group:<jid>` form intact and Baileys's toJid() handles it.
        rest = identifier[len("wa:"):]
        if rest.startswith("group:"):
            return send_whatsapp(rest, body, mentions=mentions)  # pass through
        return send_whatsapp(rest.lstrip("+"), body, mentions=mentions)
    log.warning("unknown identifier scheme: %s", identifier)
    return None


def send_telegram(
    chat_id: str, body: str, parse_mode: str | None = None,
) -> int | None:
    """Stateless POST to Telegram bot API. No SDK dependency.

    `chat_id` is the numeric ID as a string OR int (we coerce). Body is
    truncated to Telegram's 4096-char hard limit. Returns Telegram's
    `message.message_id` on 200, None otherwise.

    `parse_mode`, when set ("HTML" or "MarkdownV2"), enables Telegram's
    rich rendering. Most importantly: HTML lets us emit
    `<a href="tg://user?id=N">Name</a>` mention links that ping the user
    even when they don't have a public @username.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot send to tg:%s", chat_id)
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload_dict: dict = {
        "chat_id": int(chat_id),
        "text": body[:TELEGRAM_TEXT_LIMIT],
    }
    if parse_mode:
        payload_dict["parse_mode"] = parse_mode
    payload = json.dumps(payload_dict).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not data.get("ok"):
                log.warning("telegram sendMessage non-ok response: %s", data)
                return None
            return data.get("result", {}).get("message_id")
    except urllib.error.HTTPError as e:
        log.error(
            "telegram send failed to tg:%s status=%s body=%s",
            chat_id, e.code,
            e.read().decode("utf-8", errors="replace")[:300],
        )
        return None
    except Exception:
        log.exception("telegram send failed to tg:%s", chat_id)
        return None


def send_whatsapp(phone: str, body: str,
                  mentions: list[str] | None = None) -> str | None:
    """Route a WhatsApp send to either Baileys (if BAILEYS_MODE is on)
    or Meta's Cloud API (default). Returns the message id on success,
    None on failure. Caller doesn't need to know which transport; the
    semantics are the same from the outside.

    `mentions` (Baileys only): JIDs to @-ping; the body must contain the
    matching `@<user-part>` token(s). Ignored by the Cloud API path.

    `phone` for Cloud API is E.164 *without* leading `+`. For Baileys
    it can be either a phone number (we normalize) or a full JID
    including a `group:<jid>` prefix for group destinations — Baileys
    speaks the WhatsApp wire protocol natively and supports both.

    Strips Telegram-flavored HTML before dispatch. WhatsApp (both Cloud
    API and Baileys/MultiDevice) renders HTML literally — `<a href=…>` etc.
    show up as gibberish. Telegram-flavored callers can pass HTML
    bodies blindly and this strip handles the WhatsApp side.
    """
    plain_body = html.unescape(_HTML_TAG_RE.sub("", body))
    if _baileys_mode_enabled():
        return _send_via_baileys(phone, plain_body, mentions=mentions)
    return _send_via_cloud_api(phone, plain_body)


def _baileys_mode_enabled() -> bool:
    val = os.environ.get("BAILEYS_MODE", "").strip().lower()
    return val in ("on", "1", "true", "yes")


def _send_via_baileys(target: str, body: str,
                      mentions: list[str] | None = None) -> str | None:
    """POST to the local Baileys sidecar's /send endpoint. The Node
    process forwards via Baileys, returns the WhatsApp message id.

    `target` may be a bare E.164 phone (1:1 DM), or a `group:<JID>`
    string for group chats. Baileys handles JID resolution itself.
    """
    bridge_port = os.environ.get("BAILEYS_BRIDGE_PORT", "3001")
    bridge_secret = os.environ.get("BAILEYS_BRIDGE_SECRET", "")
    if not bridge_secret:
        log.warning(
            "BAILEYS_MODE=on but BAILEYS_BRIDGE_SECRET not set — refusing to "
            "call the sidecar; set the secret on both processes."
        )
        return None
    url = f"http://127.0.0.1:{bridge_port}/send"
    payload_obj = {
        "to": target,
        "text": body[:WHATSAPP_TEXT_LIMIT],
    }
    if mentions:
        payload_obj["mentions"] = mentions
    payload = json.dumps(payload_obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={
            "Content-Type": "application/json",
            "X-Bridge-Secret": bridge_secret,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            msg_id = data.get("id")
            if not msg_id:
                log.warning("baileys send to %s — no message id in response: %s",
                            target, data)
            return msg_id
    except urllib.error.HTTPError as e:
        log.error(
            "baileys send failed to %s status=%s body=%s",
            target, e.code, e.read().decode("utf-8", errors="replace")[:300],
        )
        return None
    except Exception:
        log.exception("baileys send failed to %s", target)
        return None


def _send_via_cloud_api(phone: str, body: str) -> str | None:
    """POST to Meta's WhatsApp Cloud API. Returns the wamid (message ID
    as a string) on success, None on failure.

    `phone` is E.164 *without* the leading `+` (Meta's API expectation).

    Required environment:
      WHATSAPP_ACCESS_TOKEN      — long-lived or temporary EAA... token
      WHATSAPP_PHONE_NUMBER_ID   — the numeric ID of the sending phone
                                   number (NOT the phone number itself)

    Note on test mode: free-tier sends only deliver to recipients you've
    pre-verified in the Meta developer console, AND only within the
    24-hour conversation window. Group sends are not supported on
    standard accounts (requires Official Business Account / blue tick).
    """
    token = os.environ.get("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if not token or not phone_number_id:
        log.warning(
            "whatsapp creds missing — set WHATSAPP_ACCESS_TOKEN and "
            "WHATSAPP_PHONE_NUMBER_ID; cannot send to wa:%s", phone,
        )
        return None

    # Body is already HTML-stripped by `send_whatsapp` upstream.
    url = f"{WHATSAPP_API_BASE}/{phone_number_id}/messages"
    payload = json.dumps({
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"body": body[:WHATSAPP_TEXT_LIMIT]},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            messages = data.get("messages") or []
            if messages:
                return messages[0].get("id")
            log.warning("whatsapp send to %s returned no messages: %s", phone, data)
            return None
    except urllib.error.HTTPError as e:
        log.error(
            "whatsapp send failed to wa:%s status=%s body=%s",
            phone, e.code,
            e.read().decode("utf-8", errors="replace")[:400],
        )
        return None
    except Exception:
        log.exception("whatsapp send failed to wa:%s", phone)
        return None
