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
import feed_format as ff  # noqa: E402

MEM = mcp_server.ROOT  # <_TMP>/memories
REMINDERS = MEM / "reminders.md"
FEED = MEM / "knowledge" / "baby_feed_log.md"

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


def test_list_feeds() -> None:
    FEED.parent.mkdir(parents=True, exist_ok=True)
    FEED.write_text(
        "## Individual Feeds\n"
        "| Date | Time | Type | Amount | Duration | Pee | Poop | Notes |\n"
        "|------|------|------|--------|----------|-----|------|-------|\n"
        "| 2026-05-19 | 8:00 am  | Bottle | 1.5 oz         |         |   |   |   |\n"
        "| 2026-05-19 | 10:40 pm | Bottle | ~35 mL + 15 mL |         |   |   |   |\n"
        "| 2026-05-20 | 4:38 am  | BF-L   | ~1 oz          | 17 mins |   |   |   |\n"
        "| 2026-05-20 | 10:58 am | Bottle | 15 mL          |         |   |   |   |\n"
        "| 2026-05-20 | 3:00 pm  | Bottle | 0.5 oz         |         |   |   |   |\n"
        "| 2026-05-20 | 5:20 pm  | —      | —              | —       | ✓ |   |   |\n",
        encoding="utf-8",
    )

    day = mcp_server.list_feeds("2026-05-20")
    check("list_feeds(day): count excludes the diaper row", "3 feeds" in day, detail=day)
    check("list_feeds(day): day total ~2 oz (1 + 0.5 + 15mL)", "~2 oz" in day, detail=day)
    check("list_feeds(day): still lists the diaper event", "diaper" in day.lower(), detail=day)

    tot = mcp_server.list_feeds()
    check("list_feeds(): daily-totals header", "daily feed totals" in tot.lower(), detail=tot)
    check("list_feeds(): 5/19 sums oz + mL to ~3.2 oz", "3.2 oz" in tot, detail=tot)
    check("list_feeds(): most recent day listed first",
          tot.find("May 20") < tot.find("May 19"), detail=tot)

    check("list_feeds(missing day): clean message",
          "no feeds logged for" in mcp_server.list_feeds("2099-01-01").lower(), detail="")


def test_add_reminder_no_time() -> None:
    REMINDERS.write_text("# Reminders\n\n", encoding="utf-8")
    before = REMINDERS.read_text(encoding="utf-8")
    msg = mcp_server.add_reminder("water the plants", "whenever")
    after = REMINDERS.read_text(encoding="utf-8")
    check("add (no time): asks for a time instead of guessing",
          "couldn't work out a time" in msg, detail=msg)
    check("add (no time): writes nothing (no ghost/malformed entry)",
          before == after, detail=repr(after))


def _seed_feed_log() -> None:
    FEED.parent.mkdir(parents=True, exist_ok=True)
    FEED.write_text(
        "# Siya Feed Log\n\n## Individual Feeds\n"
        "| Date       | Time     | Type      | Amount   | Duration  | Pee          | Poop | Notes |\n"
        "|------------|----------|-----------|----------|-----------|--------------|------|-------|\n"
        "| 2026-05-20 | 5:20 pm  | BF-R      | ~1 oz    | 20 mins   | ✓            |      |       |\n",
        encoding="utf-8",
    )


def test_feed_format_module() -> None:
    row = {"Date": "2026-05-20", "Time": "5:20 pm", "Type": "BF-R", "Amount": "~1 oz",
           "Duration": "20 mins", "Pee": "✓", "Poop": "", "Notes": ""}
    parsed = ff.parse_row(ff.render_row(row))
    check("feed_format: render/parse round-trip",
          parsed["Type"] == "BF-R" and parsed["Amount"] == "~1 oz" and parsed["Pee"] == "✓",
          detail=repr(parsed))

    _seed_feed_log()
    new = ff.append_feed(FEED.read_text(), {
        "Date": "2026-05-20", "Time": "6:00 pm", "Type": "Bottle", "Amount": "2 oz",
        "Duration": "", "Pee": "", "Poop": "", "Notes": ""})
    _, last = ff.last_feed(new)
    check("feed_format: append adds a new last row",
          last["Type"] == "Bottle" and last["Amount"] == "2 oz", detail=new)
    check("feed_format: stays a table (no stray '- [' lines)", "- [" not in new, detail=new)

    res = ff.amend_last_feed(new, {"Poop": "✓"})
    check("feed_format: amend merges into last row",
          res is not None and res[1]["Poop"] == "✓" and res[1]["Type"] == "Bottle",
          detail=str(res))


def test_parse_when_past() -> None:
    now = datetime(2026, 5, 20, 22, 0)  # 10:00 pm
    f = lambda w: mcp_server._parse_when(w, now, prefer_past=True)
    cases = [
        ("9:00 pm",     datetime(2026, 5, 20, 21, 0)),   # earlier today
        ("11:00 pm",    datetime(2026, 5, 19, 23, 0)),   # future tonight → yesterday
        ("10:01 pm",    datetime(2026, 5, 20, 22, 1)),   # within grace → today
        ("8 hours ago", datetime(2026, 5, 20, 14, 0)),
    ]
    for raw, exp in cases:
        got = f(raw)
        ok = got is not None and got.replace(tzinfo=None) == exp
        check(f"parse_when(past): {raw!r} → {exp}", ok, detail=f"got {got!r}")


def test_log_feed_table() -> None:
    today = str(mcp_server._local_now().date())

    _seed_feed_log()
    before = FEED.read_text()
    msg = mcp_server.log_feed(kind="bottle")  # no amount → clarify
    check("log_feed: bottle w/o amount asks for ounces", "ounce" in msg.lower(), detail=msg)
    check("log_feed: clarify writes nothing", FEED.read_text() == before, detail="file changed")

    check("log_feed: empty asks what to log",
          "feed or a diaper" in mcp_server.log_feed().lower(), detail="")

    mcp_server.log_feed(kind="bottle", amount="3 oz")
    _, last = ff.last_feed(FEED.read_text())
    check("log_feed: bottle row written as a table row",
          last["Type"] == "Bottle" and last["Amount"] == "3 oz" and last["Date"] == today,
          detail=str(last))
    check("log_feed: no malformed bracket lines", "- [" not in FEED.read_text(),
          detail=FEED.read_text())

    _seed_feed_log()
    cl = mcp_server.log_feed(kind="BF-L")  # no duration → clarify
    check("log_feed: BF w/o duration asks minutes", "minute" in cl.lower(), detail=cl)
    mcp_server.log_feed(kind="BF-L", duration="20 mins")
    _, last = ff.last_feed(FEED.read_text())
    check("log_feed: BF row defaults amount to ~1 oz",
          last["Type"] == "BF-L" and last["Duration"] == "20 mins" and last["Amount"] == "~1 oz",
          detail=str(last))

    _seed_feed_log()
    mcp_server.log_feed(pee="yes", poop="yes")  # diaper-only
    _, last = ff.last_feed(FEED.read_text())
    check("log_feed: diaper-only row uses — for feed columns",
          last["Type"] == "—" and last["Pee"] == "✓" and last["Poop"] == "✓", detail=str(last))


def test_amend_last_feed_tool() -> None:
    _seed_feed_log()
    mcp_server.log_feed(kind="bottle", amount="3 oz")
    msg = mcp_server.amend_last_feed(pee="yes", poop="yes")
    _, last = ff.last_feed(FEED.read_text())
    check("amend_last_feed: edits last row in place",
          last["Poop"] == "✓" and last["Pee"] == "✓" and last["Type"] == "Bottle",
          detail=str(last))
    check("amend_last_feed: no duplicate row added",
          FEED.read_text().count("Bottle") == 1, detail=FEED.read_text())
    check("amend_last_feed: confirmation says updated", "updated" in msg.lower(), detail=msg)


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
    test_parse_when_past()
    test_feed_format_module()
    test_log_feed_table()
    test_amend_last_feed_tool()
    test_list_feeds()
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
