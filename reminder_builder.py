"""Deterministic reminder-line construction.

For a long time the chat agent created reminders by hand-writing lines into
reminders.md as free LLM text. That produced fragile files: stray section
headers (e.g. a bogus "## Pending"), the slots of an "N times a day" request
split across the file, past timestamps, and wrong first-occurrence dates. The
durable fix is to take the LLM out of the date-math and formatting entirely:
the model decides WHAT and WHEN (the times of day), and this module turns that
structured request into correctly-formatted lines.

Used by:
  - agent.py      → the `create_reminders` tool (WhatsApp / Telegram)
  - mcp_server.py → the `add_recurring_reminder` voice tool

Design rules baked in here so callers can't get them wrong:
  * Never emit a past timestamp. Each time-of-day maps to its FIRST FUTURE
    occurrence — today if that time is still ahead, otherwise tomorrow.
  * One line per occurrence, all carrying the same `repeat:` tag, so a "5×/day
    daily" request becomes five independent daily chains (matching how the
    scheduler rolls recurrences: one pending line per slot at a time).
  * Canonical tag order/format identical to what scheduler.py parses. No `id:`
    (the reconciler assigns a stable hex id) and never a stray section header.

Pure stdlib, no project imports → trivially unit-testable.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

VALID_URGENCY = ("low", "normal", "high")

# Same recurrence grammar as reminder_format.REPEAT_RE (kept in sync by tests).
_REPEAT_RE = re.compile(r"^(daily|weekly|hourly|\d+[mhd])$")

# The ONLY headers the scheduler treats as section boundaries. Anything else is
# pending-head content. We must split on exactly these when inserting new lines
# so we never drop a line below a real section (or invent a new header).
_SECTION_HEADERS = ("## Fired", "## Missed", "## Malformed", "## Failed_Delivery")


class ReminderError(ValueError):
    """Raised for invalid input; callers surface the message to the user."""


def normalize_repeat(repeat: str | None) -> str | None:
    """Return a canonical repeat token, None for 'no repeat', or raise."""
    if not repeat:
        return None
    r = repeat.strip().lower()
    if r in ("", "none", "off", "once", "no", "never"):
        return None
    if _REPEAT_RE.match(r):
        return r
    raise ReminderError(
        f"Unsupported repeat {repeat!r}. Use daily, weekly, hourly, or a span "
        "like 2h / 30m / 3d (or leave it off for a one-time reminder)."
    )


def parse_time_of_day(s: str) -> tuple[int, int]:
    """Parse a clock time → (hour, minute) in 24h.

    Accepts: '9', '9am', '9:30am', '0900', '09:00', '15:00', '5pm', '5:45 pm',
    'noon', 'midnight'. Raises ReminderError on anything it can't read.
    """
    t = (s or "").strip().lower().replace(".", "")
    if t in ("noon", "12 noon", "12noon", "midday"):
        return (12, 0)
    if t in ("midnight",):
        return (0, 0)
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?$", t)
    if not m:
        raise ReminderError(f"Couldn't read the time {s!r}.")
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "am":
        if hour == 12:
            hour = 0
    elif ap == "pm":
        if hour != 12:
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ReminderError(f"Time out of range: {s!r}.")
    return (hour, minute)


def first_future_occurrence(hour: int, minute: int, now: datetime) -> datetime:
    """The next datetime at (hour, minute) that is strictly after `now`:
    today if that time is still ahead, otherwise tomorrow."""
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand


def format_line(
    dt: datetime,
    text: str,
    *,
    recipients: list[str] | None = None,
    origin_chat: str | None = None,
    urgency: str = "normal",
    repeat: str | None = None,
) -> str:
    """One canonical reminders.md line. Tag order matches scheduler examples:
    `- [ts] text @Name from:<chat> urg:<tier> repeat:<interval>`. No id: — the
    reconciler assigns a stable hex one on its next pass."""
    body = " ".join((text or "").split())
    if not body:
        raise ReminderError("Reminder text is required.")
    parts = [f"- [{dt.strftime('%Y-%m-%d %H:%M')}]", body]
    for r in recipients or []:
        name = r.lstrip("@").strip()
        if name:
            parts.append(f"@{name}")
    if origin_chat:
        parts.append(f"from:{origin_chat}")
    u = (urgency or "normal").strip().lower()
    if u not in VALID_URGENCY:
        u = "normal"
    parts.append(f"urg:{u}")
    rep = normalize_repeat(repeat)
    if rep:
        parts.append(f"repeat:{rep}")
    return " ".join(parts)


def build_occurrences(
    text: str,
    times: list[str],
    *,
    now: datetime,
    repeat: str | None = None,
    recipients: list[str] | None = None,
    origin_chat: str | None = None,
    urgency: str = "normal",
) -> tuple[list[str], list[datetime]]:
    """Build reminders.md lines for one reminder at one or more times of day.

    Each time → its first future occurrence (today if ahead, else tomorrow), so
    no past timestamps. With `repeat`, every line carries it (each slot becomes
    its own chain). Duplicate resulting slots are de-duped. Returns
    (lines, sorted_occurrence_datetimes).
    """
    if not (text and text.strip()):
        raise ReminderError("Reminder text is required.")
    if not times:
        raise ReminderError("At least one time is required.")
    rep = normalize_repeat(repeat)
    pairs: list[tuple[datetime, str]] = []
    seen: set[str] = set()
    for t in times:
        hh, mm = parse_time_of_day(t)
        dt = first_future_occurrence(hh, mm, now)
        key = dt.strftime("%Y-%m-%d %H:%M")
        if key in seen:
            continue
        seen.add(key)
        line = format_line(
            dt, text, recipients=recipients, origin_chat=origin_chat,
            urgency=urgency, repeat=rep,
        )
        pairs.append((dt, line))
    pairs.sort(key=lambda p: p[0])
    return [ln for _, ln in pairs], [dt for dt, _ in pairs]


def _fmt_time(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {ampm}"


def _day_label(d, today) -> str:
    rel = (d - today).days
    if rel == 0:
        return "today"
    if rel == 1:
        return "tomorrow"
    return d.strftime("%a %b %-d") if hasattr(d, "strftime") else str(d)


def summarize(occurrences: list[datetime], now: datetime, repeat: str | None) -> str:
    """A short spoken-friendly confirmation grouped by day, e.g.
    'today 3:00 PM, 5:00 PM; tomorrow 9:00 AM, 11:00 AM, 1:00 PM (repeating daily)'."""
    if not occurrences:
        return "nothing scheduled"
    today = now.date()
    by_day: dict = {}
    for dt in occurrences:
        by_day.setdefault(dt.date(), []).append(dt)
    chunks = []
    for d in sorted(by_day):
        times = ", ".join(_fmt_time(x) for x in sorted(by_day[d]))
        chunks.append(f"{_day_label(d, today)} {times}")
    out = "; ".join(chunks)
    rep = normalize_repeat(repeat)
    if rep:
        out += f" (repeating {rep})"
    return out


# ---------------------------------------------------------------------------
# File writing — insert lines into the pending head without disturbing history.
# ---------------------------------------------------------------------------
def _split_head_tail(content: str) -> tuple[list[str], str]:
    lines = content.splitlines()
    for i, raw in enumerate(lines):
        s = raw.strip()
        if any(s == h or s.startswith(h + " ") for h in _SECTION_HEADERS):
            return lines[:i], "\n".join(lines[i:])
    return lines, ""


def write_lines_to_head(reminders_path, lines: list[str]) -> None:
    """Append `lines` to the pending head of reminders.md (before any Fired/
    Missed/Malformed/Failed_Delivery section), creating the file if needed.
    Atomic-ish: full rewrite via a temp file + replace."""
    import os
    from pathlib import Path

    path = Path(reminders_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not lines:
        return
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Reminders\n"
    head_lines, tail = _split_head_tail(existing)
    head_text = "\n".join(head_lines).rstrip()
    new_head = (head_text + "\n" + "\n".join(lines)) if head_text else "\n".join(lines)
    if tail.strip():
        content = new_head.rstrip() + "\n\n" + tail.strip() + "\n"
    else:
        content = new_head.rstrip() + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
