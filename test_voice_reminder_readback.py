"""Tests for the co-located MCP server's reminder readback (mcp_server.py).

The voice path used to dump the entire reminders.md log (every fired/acked/
escalated entry, ~12KB) when asked "what are my reminders" — unusable to read
aloud. `list_reminders` now returns ONLY pending, unacked items as clean task
text, grouped by day, with an optional `when` filter.

Covers:
  1. `when="tomorrow"` returns just tomorrow's pending items, time-ordered,
     with all machine tags (@mentions, from:, id:, urg:, esc:, repeat:)
     stripped, and excludes fired / acked / missed history.
  2. `when="today"` narrows to today's items.
  3. `when=""` (full list) groups Today / Tomorrow / overdue, surfaces an
     undated checkbox item, and still excludes fired / acked / missed.
  4. A date with nothing scheduled returns a clean "no reminders" line.
  5. An empty reminders file returns a clean empty-state line.

Dates are computed relative to the server's own clock so the test is stable
on any day. Run with: PYTHONPATH=. MEMORY_ROOT=/tmp/rosey-mcptest \
    python3 test_voice_reminder_readback.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# mcp_server reads MEMORY_ROOT at import time and creates <root>/memories.
# Point it at a throwaway dir before importing.
_TMP = tempfile.mkdtemp(prefix="rosey-mcptest-")
os.environ["MEMORY_ROOT"] = _TMP
os.environ.setdefault("SCHEDULER_TZ", "UTC")

sys.path.insert(0, str(Path(__file__).parent))

import mcp_server  # noqa: E402

MEM = mcp_server.ROOT  # <_TMP>/memories
REMINDERS = MEM / "reminders.md"

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
results: list[tuple[bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    results.append((condition, name))
    marker = PASS if condition else FAIL
    extra = f"  ({detail})" if detail and not condition else ""
    print(f"{marker} {name}{extra}")


# Anchor everything to the server's own notion of "now".
NOW = mcp_server._local_now()
TODAY = NOW.date()
TOMORROW = TODAY + timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)


def _write_full_fixture() -> None:
    """A realistic reminders.md: pending head with tags + lifecycle sections."""
    REMINDERS.write_text(
        "# Reminders\n"
        "\n"
        # --- pending (head) ---
        f"- [{TOMORROW} 18:00] call the pediatrician @Sunanda "
        "from:tg:8637121285 urg:normal id:aaa111bbb222\n"
        f"- [{TOMORROW} 09:00] give Siya her vitamin D drops 💧 @Ankit @Sunanda "
        "from:wa:group:120363408351089900@g.us urg:high id:ccc333 esc:5m repeat:daily\n"
        f"- [{TODAY} 14:00] move the laundry @Ankit from:tg:8600355980 urg:low\n"
        f"- [{YESTERDAY} 08:00] renew car registration @Ankit "
        "from:tg:8600355980 urg:normal id:ddd444\n"
        # acked-but-still-in-head → must be excluded
        f"- [{TOMORROW} 07:00] take out recycling @Ankit from:tg:8600355980 "
        "id:eee555 (acked by Ankit at 2026-05-19 07:05)\n"
        # undated voice-added checkbox → surfaced under "No set time"
        "- [ ] water the plants\n"
        # checked-off checkbox → excluded
        "- [x] already did this\n"
        "\n"
        "## Fired\n"
        f"- [{YESTERDAY} 09:00] drink water 💧 @Ankit from:tg:8600355980 "
        "id:fff666 (fired at " + str(YESTERDAY) + " 09:00 chat:tg:8600355980 msg:1) "
        "(acked by Ankit at " + str(YESTERDAY) + " 09:10)\n"
        "\n"
        "## Missed\n"
        f"- [{YESTERDAY} 12:00] pick up dry cleaning @Sunanda from:tg:8637121285 "
        "id:ggg777 (missed at " + str(YESTERDAY) + " 14:00 — no ack)\n",
        encoding="utf-8",
    )


def test_tomorrow_filter() -> None:
    _write_full_fixture()
    out = mcp_server.list_reminders("tomorrow")

    check("tomorrow: labelled with Tomorrow", "Tomorrow" in out, detail=out)
    check("tomorrow: includes the 9am drops item",
          "give Siya her vitamin D drops 💧" in out, detail=out)
    check("tomorrow: includes the 6pm pediatrician item",
          "call the pediatrician" in out, detail=out)
    # Time-ordered: 9:00 AM should come before 6:00 PM.
    check("tomorrow: items in time order (9am before 6pm)",
          out.find("9:00 AM") < out.find("6:00 PM"), detail=out)
    # Machine tags fully stripped.
    for tag in ("from:", "urg:", "id:", "esc:", "repeat:", "@Ankit", "@Sunanda"):
        check(f"tomorrow: tag stripped — {tag!r} absent", tag not in out, detail=out)
    # History + acked excluded.
    check("tomorrow: excludes acked head item",
          "take out recycling" not in out, detail=out)
    check("tomorrow: excludes ## Fired content",
          "drink water" not in out, detail=out)
    check("tomorrow: excludes today's item", "move the laundry" not in out, detail=out)
    # Sanity: this is a short readback, not a 12KB dump.
    check("tomorrow: concise output (<400 chars)", len(out) < 400,
          detail=f"len={len(out)}")


def test_today_filter() -> None:
    _write_full_fixture()
    out = mcp_server.list_reminders("today")
    check("today: includes today's laundry item",
          "move the laundry" in out, detail=out)
    check("today: labelled Today", "Today" in out, detail=out)
    check("today: excludes tomorrow's items",
          "call the pediatrician" not in out, detail=out)


def test_full_list() -> None:
    _write_full_fixture()
    out = mcp_server.list_reminders()
    check("all: groups Today", "Today (" in out, detail=out)
    check("all: groups Tomorrow", "Tomorrow (" in out, detail=out)
    check("all: marks the overdue item",
          "overdue" in out.lower() and "renew car registration" in out, detail=out)
    check("all: surfaces undated checkbox under 'No set time'",
          "No set time:" in out and "water the plants" in out, detail=out)
    check("all: excludes checked-off checkbox",
          "already did this" not in out, detail=out)
    check("all: excludes ## Fired", "drink water" not in out, detail=out)
    check("all: excludes ## Missed", "pick up dry cleaning" not in out, detail=out)
    check("all: excludes acked head item",
          "take out recycling" not in out, detail=out)


def test_empty_day() -> None:
    _write_full_fixture()
    out = mcp_server.list_reminders("2099-01-01")
    check("empty day: clean 'no reminders' line",
          out == "You have no reminders for 2099-01-01.", detail=repr(out))


def test_empty_file() -> None:
    REMINDERS.write_text("", encoding="utf-8")
    out = mcp_server.list_reminders()
    check("empty file: clean empty-state line",
          out == "You have no reminders set.", detail=repr(out))


# Wednesday 2026-05-20, 10:00 — fixed anchor so parsing is deterministic.
_NOW = datetime(2026, 5, 20, 10, 0)


def test_parse_when() -> None:
    f = lambda w: mcp_server._parse_when(w, _NOW)
    cases: list[tuple[str, datetime | None]] = [
        ("tomorrow 9am",       datetime(2026, 5, 21, 9, 0)),
        ("today 6pm",          datetime(2026, 5, 20, 18, 0)),
        ("in 30 minutes",      datetime(2026, 5, 20, 10, 30)),
        ("in 2 hours",         datetime(2026, 5, 20, 12, 0)),
        ("in 3 days",          datetime(2026, 5, 23, 10, 0)),
        ("20 minutes ago",     datetime(2026, 5, 20, 9, 40)),
        ("2 hours ago",        datetime(2026, 5, 20, 8, 0)),
        ("2026-12-25 07:30",   datetime(2026, 12, 25, 7, 30)),
        ("2026-07-04",         datetime(2026, 7, 4, 9, 0)),    # date only → 9am
        ("3pm",                datetime(2026, 5, 20, 15, 0)),  # bare time, still ahead
        ("9:30am",             datetime(2026, 5, 21, 9, 30)),  # bare time, past → tomorrow
        ("14:45",              datetime(2026, 5, 20, 14, 45)), # 24-hour
        ("noon",               datetime(2026, 5, 20, 12, 0)),
        ("tonight",            datetime(2026, 5, 20, 20, 0)),
        ("friday 8am",         datetime(2026, 5, 22, 8, 0)),
        ("wednesday 9am",      datetime(2026, 5, 27, 9, 0)),   # same weekday → NEXT week
        ("tomorrow",           datetime(2026, 5, 21, 9, 0)),   # day, no time → 9am
        ("",                   None),
        ("whenever you can",   None),
    ]
    for raw, expected in cases:
        got = f(raw)
        # Compare on wall-clock fields so a tz-aware result still matches.
        ok = (got is None and expected is None) or (
            got is not None and expected is not None
            and got.replace(tzinfo=None) == expected
        )
        check(f"parse_when: {raw!r} → {expected}", ok, detail=f"got {got!r}")


def test_add_reminder_roundtrip() -> None:
    REMINDERS.write_text("# Reminders\n\n", encoding="utf-8")
    now = mcp_server._local_now()
    tomorrow = now.date() + timedelta(days=1)

    msg = mcp_server.add_reminder("call the dentist", "tomorrow 2pm")
    raw = REMINDERS.read_text(encoding="utf-8")

    expected_line = f"- [{tomorrow} 14:00] call the dentist urg:normal"
    check("add: wrote canonical scheduler line",
          expected_line in raw, detail=raw)

    from reminder_format import LINE_RE
    written = next((l for l in raw.splitlines() if l.startswith("- [")), "")
    check("add: line is LINE_RE-valid (won't be quarantined as malformed)",
          bool(LINE_RE.match(written)), detail=repr(written))

    check("add: confirmation reads back the time",
          "2:00 PM" in msg and "call the dentist" in msg, detail=msg)

    back = mcp_server.list_reminders("tomorrow")
    check("add: shows up cleanly in tomorrow's readback",
          "call the dentist" in back and "2:00 PM" in back, detail=back)
    check("add: no machine tags leak into readback",
          "urg:" not in back, detail=back)


def test_log_feed() -> None:
    from reminder_format import LINE_RE
    feed = MEM / "knowledge" / "baby_feed_log.md"
    feed.parent.mkdir(parents=True, exist_ok=True)
    feed.write_text("", encoding="utf-8")
    notes = MEM / "notes.md"
    notes.write_text("", encoding="utf-8")

    now = mcp_server._local_now()

    # No time given → defaults to now, lands in the FEED log (not notes.md).
    msg = mcp_server.log_feed(amount="1.5 oz", kind="bottle")
    content = feed.read_text(encoding="utf-8")
    check("log_feed: wrote to knowledge/baby_feed_log.md",
          "bottle, 1.5 oz" in content, detail=content)
    written = next((l for l in content.splitlines() if l.startswith("- [")), "")
    check("log_feed: entry is a dated, parseable line",
          bool(LINE_RE.match(written)), detail=repr(written))
    check("log_feed: did NOT land in notes.md",
          "1.5 oz" not in notes.read_text(encoding="utf-8"), detail=notes.read_text())
    check("log_feed: confirmation names what was logged",
          "bottle, 1.5 oz" in msg, detail=msg)

    # Explicit fields + time.
    feed.write_text("", encoding="utf-8")
    mcp_server.log_feed(amount="1 oz", kind="BF-L", duration="25 mins", when="today 1:30pm")
    line = feed.read_text(encoding="utf-8").strip()
    expected = f"- [{now.date()} 13:30] BF-L, 1 oz, 25 mins"
    check("log_feed: composes kind/amount/duration with the given time",
          line == expected, detail=repr(line))


def test_add_reminder_no_time() -> None:
    REMINDERS.write_text("# Reminders\n\n", encoding="utf-8")
    before = REMINDERS.read_text(encoding="utf-8")
    msg = mcp_server.add_reminder("water the plants", "whenever")
    after = REMINDERS.read_text(encoding="utf-8")
    check("add (no time): asks for a time instead of guessing",
          "couldn't work out a time" in msg, detail=msg)
    check("add (no time): writes nothing (no ghost/malformed entry)",
          before == after, detail=repr(after))


if __name__ == "__main__":
    print("─" * 60)
    print(" Voice reminder readback (mcp_server.list_reminders)")
    print("─" * 60)

    test_tomorrow_filter()
    test_today_filter()
    test_full_list()
    test_empty_day()
    test_empty_file()
    test_parse_when()
    test_log_feed()
    test_add_reminder_roundtrip()
    test_add_reminder_no_time()

    print("─" * 60)
    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    if passed == total:
        print(f"{PASS} {passed}/{total} checks passed")
        sys.exit(0)
    else:
        failed = [name for ok, name in results if not ok]
        print(f"{FAIL} {passed}/{total} checks passed — {total - passed} failed:")
        for name in failed:
            print(f"   - {name}")
        sys.exit(1)
