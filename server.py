"""Multi-channel HTTP server entrypoint for Rosey on Fly.

Replaces python-telegram-bot's built-in webhook server (tornado) with a
Quart app that hosts both /telegram and /alexa under a single port.
Reason: PTB's `Application.run_webhook(...)` doesn't expose a way to add
arbitrary URL routes alongside its `url_path`. Once we wanted Alexa as a
second channel, we needed to own the HTTP layer ourselves so we can route
multiple paths to the same process.

Architecture:
    Telegram webhook  ─▶  POST /telegram  ─▶  app.process_update(Update)
    Alexa skill       ─▶  POST /alexa     ─▶  alexa_handler.handle(env)
                          GET  /health    ─▶  "ok" (Fly TCP check)

PTB is initialized but its built-in webhook server is NOT started — we
just use it as a smart update-router library. We register the Telegram
webhook URL with the Bot API once at boot via `bot.set_webhook(...)`.

Local dev: this entrypoint can also run, but the `python -m telegram_bot`
polling-mode entrypoint stays available for offline testing where you
don't want to deal with public webhook URLs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from dotenv import load_dotenv
from quart import Quart, Response, request

import alexa_handler
import scheduler as reminder_scheduler
import whatsapp_handler

# Reuse all the existing handler functions from telegram_bot.py — no
# need to duplicate the trigger gating, ack lookup, fuzzy gate, etc.
from telegram_bot import (
    _on_start,
    _on_status_command,
    _on_text,
    _on_voice,
)

log = logging.getLogger("rosey.server")

# Quart app is module-global so it can be the ASGI app hypercorn finds
# when invoked as `hypercorn server:asgi_app`. The PTB Application is
# attached during async startup.
asgi_app = Quart(__name__)
_ptb_app = None  # populated by _startup()


@asgi_app.before_serving
async def _startup():
    """Run once when the ASGI server boots. Initializes the PTB
    Application, starts the reminder scheduler, and registers the
    Telegram webhook URL.
    """
    from telegram import Update
    from telegram.ext import (
        Application,
        CommandHandler,
        MessageHandler,
        filters,
    )

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        log.error("TELEGRAM_BOT_TOKEN not set — refusing to start")
        sys.exit(1)

    # Persistent reminder scheduler (DateTrigger jobs in SQLite jobstore).
    reminder_scheduler.start()
    reminder_scheduler.reconcile()
    # Pick up reminders written out-of-band by the co-located voice MCP server
    # (a separate process that can't trigger the per-turn reconcile).
    reminder_scheduler.schedule_periodic_reconcile()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(CommandHandler("status", _on_status_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, _on_voice))

    await app.initialize()
    await app.start()

    # Register the webhook with Telegram. Idempotent — repeating it on
    # every boot is fine, Telegram just records the latest URL+secret.
    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL")
    secret_token = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if webhook_url:
        if not secret_token:
            log.warning(
                "TELEGRAM_WEBHOOK_SECRET unset — webhook is unauthenticated. "
                "Anyone who guesses the URL can spoof updates. Set it in production."
            )
        full_webhook_url = webhook_url.rstrip("/") + "/telegram"
        await app.bot.set_webhook(
            url=full_webhook_url,
            secret_token=secret_token,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info(
            "telegram webhook registered: %s (auth=%s)",
            full_webhook_url, "on" if secret_token else "OFF",
        )
    else:
        log.warning(
            "TELEGRAM_WEBHOOK_URL unset — Telegram /telegram route will not "
            "receive any updates (Telegram doesn't know where to deliver)."
        )

    global _ptb_app
    _ptb_app = app
    asgi_app.config["TELEGRAM_WEBHOOK_SECRET"] = secret_token
    log.info("server ready — routes: /telegram /alexa /health")


@asgi_app.after_serving
async def _shutdown():
    """Graceful PTB shutdown so background tasks (job queue, etc.) stop
    cleanly when the container is told to exit.
    """
    if _ptb_app is not None:
        try:
            await _ptb_app.stop()
            await _ptb_app.shutdown()
            log.info("ptb application stopped cleanly")
        except Exception:
            log.exception("ptb shutdown raised")
    try:
        reminder_scheduler.shutdown(wait=False)
    except Exception:
        log.exception("scheduler shutdown raised")


@asgi_app.route("/telegram", methods=["POST"])
async def telegram_route():
    """Receive a Telegram update via webhook, hand it to PTB. Verifies
    the X-Telegram-Bot-Api-Secret-Token header so random internet
    traffic to /telegram can't spoof messages.
    """
    secret = asgi_app.config.get("TELEGRAM_WEBHOOK_SECRET")
    if secret:
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided != secret:
            log.warning("telegram: rejected request with bad secret token")
            return Response("forbidden", status=403)

    if _ptb_app is None:
        log.error("telegram: ptb application not initialized")
        return Response("not ready", status=503)

    body = await request.get_json(force=True, silent=True)
    if body is None:
        return Response("bad request", status=400)

    from telegram import Update
    update = Update.de_json(body, _ptb_app.bot)
    # process_update handles routing through CommandHandlers, MessageHandlers,
    # etc. Same path that PTB's built-in webhook server would have used.
    await _ptb_app.process_update(update)
    return Response("", status=200)


@asgi_app.route("/alexa", methods=["POST"])
async def alexa_route():
    """Receive an Alexa request envelope, hand to alexa_handler. Returns
    the Alexa response envelope as JSON.
    """
    body = await request.get_json(force=True, silent=True)
    if body is None:
        return Response("bad request", status=400)
    response = await alexa_handler.handle(body)
    return response  # Quart serializes dict to JSON with the right Content-Type


@asgi_app.route("/whatsapp", methods=["GET"])
async def whatsapp_verify_route():
    """Meta's webhook verification handshake. Meta hits us once during
    setup with `?hub.mode=subscribe&hub.verify_token=<our_secret>
    &hub.challenge=<random>`; we echo back the challenge if the token
    matches. Without a successful 200+challenge response, Meta refuses
    to register the webhook.
    """
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")
    body, status = whatsapp_handler.verify_webhook(mode, token, challenge)
    return Response(body, status=status)


@asgi_app.route("/whatsapp", methods=["POST"])
async def whatsapp_event_route():
    """Receive an inbound WhatsApp event from Meta. Always return 200 OK
    so Meta doesn't retry — the handler logs and recovers internally on
    any agent crash.
    """
    body = await request.get_json(force=True, silent=True)
    if body is None:
        return Response("bad request", status=400)
    await whatsapp_handler.handle_event(body)
    return Response("", status=200)


@asgi_app.route("/whatsapp-baileys", methods=["POST"])
async def whatsapp_baileys_route():
    """Receive an inbound WhatsApp message from the Baileys sidecar.

    Schema is what `baileys/index.js` POSTs (NOT Meta's nested envelope):
    { message_id, sender_phone, sender_jid, chat_jid, is_group, text, ... }

    Auth: X-Bridge-Secret header must match BAILEYS_BRIDGE_SECRET. The
    sidecar runs in the same container so loopback is the only path here,
    but defense in depth — a misconfigured deployment that exposed
    127.0.0.1:8080 wouldn't be open to spoofed inbound traffic.
    """
    secret = os.environ.get("BAILEYS_BRIDGE_SECRET", "")
    provided = request.headers.get("X-Bridge-Secret", "")
    if not secret or provided != secret:
        log.warning("baileys-inbound: rejected request with bad bridge secret")
        return Response("forbidden", status=403)

    body = await request.get_json(force=True, silent=True)
    if body is None:
        return Response("bad request", status=400)
    await whatsapp_handler.handle_baileys_event(body)
    return Response("", status=200)


@asgi_app.route("/health", methods=["GET"])
async def health_route():
    return Response("ok", mimetype="text/plain")


def main() -> int:
    """Optional CLI entrypoint that runs hypercorn for us. In production
    we invoke hypercorn directly via the Dockerfile CMD, so this is mostly
    for local testing (`python -m server`).
    """
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    config = Config()
    config.bind = [f"0.0.0.0:{os.environ.get('PORT', '8080')}"]
    config.accesslog = "-"  # stdout — folds into fly logs
    config.errorlog = "-"

    log.info("starting hypercorn on %s", config.bind[0])

    # asyncio.run handles the event loop; serve() blocks until SIGINT/SIGTERM.
    try:
        asyncio.run(serve(asgi_app, config))
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
