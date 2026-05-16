"""Tests for router.app — Telegram webhook dispatcher.

Patches notifications + provisioning so we don't make real HTTP calls.
Uses an in-memory router DB for state.

The router's forward + welcome paths use threading.Thread(daemon=True) to
keep the webhook response fast. For tests we replace Thread with a
synchronous shim so we can assert call shapes deterministically.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

# Set BEFORE importing router.app so db.init_db doesn't try to create a
# real router.db on disk.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from router import db, app as app_module  # noqa: E402


class _SyncThread:
    """Drop-in replacement for threading.Thread: ``.start()`` runs the
    target immediately instead of spawning a thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=False):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------

class TestParseStartPayload(unittest.TestCase):
    def test_bare_start(self):
        self.assertEqual(app_module._parse_start_payload("/start"), ("", ""))

    def test_start_with_payload(self):
        text, payload = app_module._parse_start_payload("/start signup")
        self.assertEqual(text, "")
        self.assertEqual(payload, "signup")

    def test_start_with_multi_word_payload(self):
        # Telegram allows base64-ish payloads up to 64 chars
        text, payload = app_module._parse_start_payload("/start beta_cohort_xyz")
        self.assertEqual(payload, "beta_cohort_xyz")

    def test_non_start_text_unchanged(self):
        text, payload = app_module._parse_start_payload("hi there")
        self.assertEqual(text, "hi there")
        self.assertEqual(payload, "")

    def test_start_inside_text_not_a_command(self):
        text, payload = app_module._parse_start_payload("let's start things")
        self.assertEqual(text, "let's start things")
        self.assertEqual(payload, "")


class TestTelegramExtract(unittest.TestCase):
    def test_dm_text(self):
        update = {
            "message": {
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100, "first_name": "Ankit", "username": "AnkitT"},
                "text": "hello",
            }
        }
        out = app_module._telegram_extract(update)
        self.assertEqual(out.chat_id, 100)
        self.assertEqual(out.chat_type, "private")
        self.assertEqual(out.text, "hello")
        self.assertEqual(out.start_payload, "")
        self.assertEqual(out.from_username, "ankitt")
        self.assertEqual(out.first_name, "Ankit")
        self.assertFalse(out.is_bot_added_to_group)

    def test_start_payload_extracted(self):
        update = {
            "message": {
                "chat": {"id": 100, "type": "private"},
                "from": {"id": 100, "first_name": "Ankit"},
                "text": "/start signup",
            }
        }
        out = app_module._telegram_extract(update)
        self.assertEqual(out.text, "")
        self.assertEqual(out.start_payload, "signup")

    def test_group_chat_detected(self):
        update = {
            "message": {
                "chat": {"id": -100200, "type": "group"},
                "from": {"id": 999, "first_name": "Ankit"},
                "text": "hi everyone",
            }
        }
        out = app_module._telegram_extract(update)
        self.assertEqual(out.chat_id, -100200)
        self.assertEqual(out.chat_type, "group")

    def test_new_chat_members_with_bot(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_USERNAME": "RoseyHouseholdBot"}):
            update = {
                "message": {
                    "chat": {"id": -100200, "type": "group"},
                    "from": {"id": 999, "first_name": "Ankit"},
                    "new_chat_members": [
                        {"id": 12345, "is_bot": True, "username": "RoseyHouseholdBot"},
                    ],
                }
            }
            out = app_module._telegram_extract(update)
            self.assertTrue(out.is_bot_added_to_group)
            self.assertEqual(len(out.new_chat_members), 1)

    def test_new_chat_members_without_bot(self):
        with patch.dict("os.environ", {"TELEGRAM_BOT_USERNAME": "RoseyHouseholdBot"}):
            update = {
                "message": {
                    "chat": {"id": -100200, "type": "group"},
                    "from": {"id": 999, "first_name": "Ankit"},
                    "new_chat_members": [
                        {"id": 12346, "is_bot": False, "first_name": "Sarah"},
                    ],
                }
            }
            out = app_module._telegram_extract(update)
            self.assertFalse(out.is_bot_added_to_group)

    def test_empty_update(self):
        out = app_module._telegram_extract({})
        self.assertIsNone(out.chat_id)


# ---------------------------------------------------------------------------
# Branch handlers — patched I/O
# ---------------------------------------------------------------------------

def _swap_engine():
    """Replace the module-global engine with a fresh in-memory DB.
    Returns the new engine so the test can assert against it."""
    eng = db.get_engine("sqlite:///:memory:")
    db.init_db(eng)
    app_module.engine = eng
    return eng


class TestHandleBotAddedToGroup(unittest.TestCase):
    @patch("router.app.notifications")
    @patch("router.app._post_to_household")
    def test_known_inviter_links_group(self, mock_post, mock_notif):
        eng = _swap_engine()
        hid = db.create_household(eng, "rosey-h-test")
        db.add_member(eng, "tg:100", hid, "Ankit")

        parsed = app_module.TelegramUpdate(
            chat_id=-100200,
            chat_type="group",
            from_user_id=100,
            first_name="Ankit",
            new_chat_members=[{"id": 99, "is_bot": True, "username": "RoseyHouseholdBot"}],
            is_bot_added_to_group=True,
        )
        app_module._handle_bot_added_to_group(parsed)

        # group_chat_id stored on household
        h = db.get_household(eng, hid)
        self.assertEqual(h["group_chat_id"], "-100200")

        # Welcome message sent
        mock_notif.send_text.assert_called()
        args, _ = mock_notif.send_text.call_args
        self.assertEqual(args[0], -100200)
        self.assertIn("Rosey", args[1])
        # leave_chat NOT called
        mock_notif.leave_chat.assert_not_called()

    @patch("router.app.notifications")
    def test_unknown_inviter_bot_leaves(self, mock_notif):
        _swap_engine()
        parsed = app_module.TelegramUpdate(
            chat_id=-100201,
            chat_type="group",
            from_user_id=9999,
            first_name="Random",
            new_chat_members=[{"id": 99, "is_bot": True, "username": "RoseyHouseholdBot"}],
            is_bot_added_to_group=True,
        )
        app_module._handle_bot_added_to_group(parsed)
        mock_notif.leave_chat.assert_called_with(-100201)


class TestHandleGroupMessage(unittest.TestCase):
    @patch("router.app.threading.Thread", _SyncThread)
    @patch("router.app._forward_to_household_telegram")
    def test_routes_to_linked_household(self, mock_fwd):
        eng = _swap_engine()
        hid = db.create_household(eng, "rosey-h-grp")
        db.add_member(eng, "tg:100", hid, "Ankit")
        db.set_group_chat_id(eng, hid, -100300)

        parsed = app_module.TelegramUpdate(
            chat_id=-100300,
            chat_type="group",
            text="hi rosey",
            from_user_id=100,
            from_username="ankit_t",
            first_name="Ankit",
            new_chat_members=[],
        )
        raw_update = {
            "update_id": 42,
            "message": {
                "message_id": 1,
                "chat": {"id": -100300, "type": "group"},
                "from": {"id": 100, "first_name": "Ankit", "username": "ankit_t"},
                "text": "hi rosey",
            },
        }
        app_module._handle_group_message(parsed, raw_update)
        mock_fwd.assert_called_once()
        args, _ = mock_fwd.call_args
        self.assertEqual(args[0], "rosey-h-grp")
        # Router now forwards the RAW Telegram update so the household VM's
        # python-telegram-bot Update.de_json can parse it cleanly.
        forwarded_payload = args[1]
        self.assertEqual(forwarded_payload["update_id"], 42)
        self.assertEqual(forwarded_payload["message"]["chat"]["id"], -100300)
        self.assertEqual(forwarded_payload["message"]["text"], "hi rosey")

    @patch("router.app.threading.Thread", _SyncThread)
    @patch("router.app._forward_to_household_telegram")
    def test_unknown_group_ignored(self, mock_fwd):
        _swap_engine()
        parsed = app_module.TelegramUpdate(
            chat_id=-100999,
            chat_type="group",
            text="hi",
            from_user_id=100,
            from_username="",
            new_chat_members=[],
        )
        app_module._handle_group_message(parsed, {"update_id": 1})
        mock_fwd.assert_not_called()


class TestHandleUnknownDM(unittest.TestCase):
    @patch("router.app.notifications")
    @patch("router.app.provisioning")
    def test_start_signup_routes_to_v2(self, mock_prov, mock_notif):
        _swap_engine()
        parsed = app_module.TelegramUpdate(
            chat_id=200,
            chat_type="private",
            text="",
            start_payload="signup",
            is_start_command=True,
            from_user_id=200,
            first_name="Ankit",
            new_chat_members=[],
        )
        app_module._handle_unknown_dm(parsed)
        mock_notif.send_text.assert_called()
        # Reply should be the v2 opening — mentions "household called"
        args, _ = mock_notif.send_text.call_args
        self.assertIn("household called", args[1].lower())

    @patch("router.app.notifications")
    @patch("router.app.provisioning")
    def test_plain_start_routes_to_v1(self, mock_prov, mock_notif):
        _swap_engine()
        parsed = app_module.TelegramUpdate(
            chat_id=201,
            chat_type="private",
            text="",
            start_payload="",
            is_start_command=True,
            from_user_id=201,
            first_name="Ankit",
            new_chat_members=[],
        )
        app_module._handle_unknown_dm(parsed)
        mock_notif.send_text.assert_called()
        args, _ = mock_notif.send_text.call_args
        self.assertIn("starting a new household", args[1])


if __name__ == "__main__":
    unittest.main()
