"""Co-located Rosey MCP server.

Runs INSIDE the household's engine container (alongside the Telegram/WhatsApp
bot) and reads/writes the SAME /data/memories volume the engine uses — so the
household's memory is ONE shared store that every channel (Telegram, WhatsApp,
the voice PWA, and anything future) reads and writes through.

It resolves the memory dir via paths.memories_dir(), the same helper the agent
uses, so the two never disagree. Served as MCP Streamable HTTP at /mcp on
MCP_PORT (default 8089); scripts/start.sh launches it as a supervised sibling
process and fly.toml exposes the port so the OpenAI Realtime backend can reach it.

This generalizes to the hosted fleet: every per-household VM that ships this
module serves its own household's memory at its own /mcp endpoint.

------------------------------------------------------------------------------
SECURITY — READ THIS BEFORE EXPOSING REAL DATA
------------------------------------------------------------------------------
This serves the household's REAL private memory (names, schedule, etc.) over a
PUBLIC endpoint, because the OpenAI Realtime backend reaches it over the
internet. It is currently UNAUTHENTICATED. In the realtime-MCP topology the
server_url/authorization are set client-side in the PWA, so any token there is
visible to users — strong, invisible auth isn't possible here without a more
involved per-session credential flow (mint an ephemeral MCP credential
server-side, like the realtime token, scoped to one session). Until that
exists, treat this as a PERSONAL-DEMO posture (obscure host/port) and do not
rely on it for anyone else's data. ROSEY_MCP_TOKEN is reserved for that gate.
"""
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo  # 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from mcp.server.fastmcp import FastMCP

import feed_format
import reminder_builder
from paths import memories_dir
from reminder_format import (
    ACKED_RE,
    ESC_RE,
    FB_RE,
    FROM_RE,
    ID_RE,
    LINE_RE,
    MENTION_RE,
    MISS_RE,
    REPEAT_RE,
    URG_RE,
    _strip_to_user_message,
)

ROOT = memories_dir()
ROOT.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("MCP_PORT", "8089"))
mcp = FastMCP("rosey", host="0.0.0.0", port=PORT)


def _safe(rel: str) -> Path:
    """Resolve `rel` under ROOT, refusing traversal outside the memory dir."""
    p = (ROOT / rel).resolve()
    if not p.is_relative_to(ROOT.resolve()):
        raise ValueError("path escapes memory root")
    return p


def _read(rel: str) -> str:
    p = _safe(rel)
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _append(rel: str, line: str) -> None:
    p = _safe(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def _write(rel: str, content: str) -> None:
    p = _safe(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# --- discovery tools (robust to whatever the real memory layout is) ----------
@mcp.tool()
def list_memory_files() -> str:
    """List every file in the household's shared memory so you can find what you
    need (the grocery list, reminders, household profile, etc.)."""
    files = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file())
    return "\n".join(files) or "(memory is empty)"


@mcp.tool()
def read_memory_file(path: str) -> str:
    """Read a memory file by its path relative to the memory root (see
    list_memory_files). Use when the convenience tools don't cover the ask."""
    try:
        return _read(path) or f"(empty or missing: {path})"
    except ValueError:
        return "Invalid path."


# --- convenience tools -------------------------------------------------------
@mcp.tool()
def get_household() -> str:
    """Return the household roster and durable facts (who lives here)."""
    return _read("household.md") or "No household profile saved yet."


@mcp.tool()
def list_grocery_items() -> str:
    """Return the current shared grocery / shopping list."""
    return _read("groceries/list.md") or "The grocery list is empty."


@mcp.tool()
def add_grocery_item(item: str) -> str:
    """Add an item to the shared grocery list."""
    _append("groceries/list.md", f"- {item}")
    return f"Added '{item}' to the grocery list."


@mcp.tool()
def get_current_time() -> str:
    """Return the current local date and time. This reads the SAME clock the
    scheduler uses to fire reminders, so it always matches when things go off.

    Call this for ANY 'what time is it', 'what's the date', or 'what day is it'
    question — you do not have a clock of your own, so check here instead of
    guessing or saying you can't tell."""
    now = _local_now()
    return f"It's {_fmt_time(now)} on {now.strftime('%A')}, {now.strftime('%B')} {now.day}, {now.year}."


# --- reminder readback helpers ----------------------------------------------
# reminders.md is an append-only lifecycle log: pending items live at the top
# ("head"), and the scheduler moves fired/missed/etc. lines down into
# "## Fired" / "## Missed" / ... sections, accreting machine tags (from:, id:,
# urg:, …) and parenthetical annotations ((fired …), (acked …)) as they go.
# Reading the raw file aloud is unusable — it can be ~12KB of history. These
# helpers extract just the still-pending, unacked items as clean task text.

def _local_now() -> datetime:
    """Now, in the same timezone the scheduler uses, so 'today'/'tomorrow'
    line up with when reminders actually fire."""
    tz_name = os.environ.get("SCHEDULER_TZ", "UTC")
    tz = ZoneInfo(tz_name) if ZoneInfo else None
    return datetime.now(tz=tz)


# The scheduler (scheduler.py._split_sections) treats ONLY these as section
# boundaries; every other "## ..." line is ordinary content inside the pending
# head. The readback MUST use the same definition — otherwise a stray header the
# agent might write (e.g. "## Pending") truncates the head here while the
# scheduler keeps firing the lines below it, so reminders silently vanish from
# "list/update/delete" even though they still go off. Match the scheduler exactly.
_SECTION_HEADERS = ("## Fired", "## Missed", "## Malformed", "## Failed_Delivery")


def _is_section_header(line: str) -> bool:
    s = line.strip()
    return any(s == h or s.startswith(h + " ") for h in _SECTION_HEADERS)


def _pending_head(content: str) -> str:
    """The 'head' of reminders.md — everything before the first RECOGNIZED
    section header (Fired/Missed/Malformed/Failed_Delivery). The head is the set
    of not-yet-fired (pending) reminders. Unknown '## ...' lines are treated as
    inline content, exactly as the scheduler does, so they can't hide pending
    reminders the scheduler is still firing."""
    out: list[str] = []
    for raw in content.splitlines():
        if _is_section_header(raw):
            break
        out.append(raw)
    return "\n".join(out)


def _fmt_time(dt: datetime) -> str:
    """12-hour clock without platform-specific strftime directives."""
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{hour}:{dt.minute:02d} {ampm}"


def _date_label(d: date, today: date) -> str:
    rel = (d - today).days
    md = f"{d.strftime('%a %b')} {d.day}"
    if rel == 0:
        return f"Today ({md})"
    if rel == 1:
        return f"Tomorrow ({md})"
    if rel == -1:
        return f"Yesterday — overdue ({md})"
    if rel < 0:
        return f"Overdue — {md}"
    return f"{d.strftime('%A')} ({md})"


def _clean_undated(text: str) -> str:
    """Tidy a non-timestamped bullet (e.g. a voice-added '- [ ] …' item):
    drop @mentions and machine tags, but KEEP parentheticals so an inline
    '(when: tomorrow 9am)' note survives for the listener."""
    s = MENTION_RE.sub("", text)
    s = FROM_RE.sub("", s)
    s = ID_RE.sub("", s)
    s = FB_RE.sub("", s)
    return " ".join(s.split())


def _resolve_when(when: str, today: date) -> tuple[bool, date | None, str]:
    """Map a `when` argument to (is_filtered, target_date, label)."""
    norm = (when or "").strip().lower()
    if not norm:
        return False, None, ""
    if norm in ("today",):
        return True, today, "today"
    if norm in ("tomorrow", "tmrw"):
        return True, today + timedelta(days=1), "tomorrow"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", norm):
        try:
            return True, datetime.strptime(norm, "%Y-%m-%d").date(), norm
        except ValueError:
            pass
    # Unrecognized → don't filter; fall back to the full upcoming list.
    return False, None, ""


# --- natural-language time parsing (for add_reminder) ------------------------
# `dateutil` is NOT a declared dependency, so this is a small, dependency-free
# parser covering the phrasings a voice user actually produces. It turns a
# `when` expression into a concrete datetime; add_reminder then writes the
# scheduler's canonical "- [YYYY-MM-DD HH:MM] …" line so the reminder is real
# (parseable, schedulable) instead of a "- [ ] …" note the reconciler would
# quarantine as malformed.
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_NAMED_TIMES = {
    "midnight": (0, 0), "noon": (12, 0), "midday": (12, 0),
    "morning": (9, 0), "afternoon": (15, 0),
    "evening": (18, 0), "tonight": (20, 0), "night": (20, 0),
}
# "3pm" / "3:30 pm"  OR  "14:30" (24-hour, no meridiem).
_CLOCK_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]\.?\s?m\.?)\b|\b(\d{1,2}):(\d{2})\b")
_REL_RE = re.compile(r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|m|h|d)\b")
_AGO_RE = re.compile(r"\b(\d+)\s*(minutes?|mins?|hours?|hrs?|days?|m|h|d)\s+ago\b")
_ISO_DT_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})[ tT](\d{1,2}):(\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _parse_clock(s: str) -> tuple[int, int] | None:
    """Return (hour, minute) for a time-of-day found in `s`, else None."""
    for name, hm in _NAMED_TIMES.items():
        if name in s:
            return hm
    m = _CLOCK_RE.search(s)
    if not m:
        return None
    if m.group(1) is not None:  # meridiem form, e.g. "3pm" / "3:30 pm"
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ap = m.group(3).replace(".", "").replace(" ", "").lower()
        if ap == "pm" and hour != 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
    else:  # 24-hour form, e.g. "14:30"
        hour = int(m.group(4))
        minute = int(m.group(5))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _parse_when(when: str, now: datetime, prefer_past: bool = False) -> datetime | None:
    """Best-effort parse of a `when` expression into a concrete datetime.
    Returns None when no time can be determined (caller should ask the user).

    `prefer_past` flips the bare-clock heuristic: reminders assume a bare time
    is the NEXT occurrence (future), but logged events (feeds) assume the most
    recent PAST one — so "11:04 pm" at 11:04 pm logs today, not tomorrow."""
    s = (when or "").strip().lower()
    if not s:
        return None

    # Explicit "YYYY-MM-DD HH:MM" (or with 'T'/'t').
    m = _ISO_DT_RE.search(s)
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}",
                "%Y-%m-%d %H:%M",
            )
        except ValueError:
            return None

    # Relative-past: "30 minutes ago", "2 hours ago" (handy for logging a feed
    # that already happened). Checked before the forward "in N" form.
    m = _AGO_RE.search(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)[0]
        base = now.replace(second=0, microsecond=0)
        if unit == "m":
            return base - timedelta(minutes=n)
        if unit == "h":
            return base - timedelta(hours=n)
        if unit == "d":
            return base - timedelta(days=n)

    # Relative: "in 30 minutes", "in 2 hours", "in 3 days".
    m = _REL_RE.search(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)[0]
        base = now.replace(second=0, microsecond=0)
        if unit == "m":
            return base + timedelta(minutes=n)
        if unit == "h":
            return base + timedelta(hours=n)
        if unit == "d":
            return base + timedelta(days=n)

    # Day anchor: today / tomorrow / explicit date / weekday name.
    base_date: date | None = None
    if "tomorrow" in s or "tmrw" in s:
        base_date = now.date() + timedelta(days=1)
    elif "today" in s or "tonight" in s:
        base_date = now.date()
    else:
        m = _ISO_DATE_RE.search(s)
        if m:
            try:
                base_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                base_date = None
        if base_date is None:
            for name, wd in _WEEKDAYS.items():
                if re.search(rf"\b{name}\b", s):
                    ahead = (wd - now.weekday()) % 7 or 7  # next such weekday
                    base_date = now.date() + timedelta(days=ahead)
                    break

    clock = _parse_clock(s)

    if base_date is not None:
        hour, minute = clock if clock else (9, 0)  # default to 9am if a day but no time
        return datetime(base_date.year, base_date.month, base_date.day, hour, minute)

    if clock is not None:
        cand = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if prefer_past:
            # Most recent past occurrence; small grace so a just-now time that
            # reads a minute ahead (clock skew / rounding) still counts as today.
            if cand > now + timedelta(minutes=2):
                cand -= timedelta(days=1)
        else:
            # Next occurrence (default, for reminders).
            if cand <= now:
                cand += timedelta(days=1)
        return cand

    return None


@mcp.tool()
def list_reminders(when: str = "") -> str:
    """List the household's UPCOMING reminders, cleaned up for reading aloud.

    Returns only reminders that are still pending — fired, acknowledged, and
    missed history is excluded, and internal tags (@mentions, ids, urgency,
    etc.) are stripped — so you get plain task text grouped by day, soonest
    first.

    Optional `when` narrows the result:
      - "" (default) → every pending reminder
      - "today" / "tomorrow" → just that day's reminders
      - "YYYY-MM-DD" → reminders on that specific date
    """
    content = _read("reminders.md")
    if not content:
        return "You have no reminders set."

    now = _local_now()
    today = now.date()

    dated: list[tuple[datetime, str]] = []
    undated: list[str] = []

    for raw in _pending_head(content).splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue
        if ACKED_RE.search(line):
            continue  # already handled

        m = LINE_RE.match(line)
        if m:
            ts_str = m.group(1).replace("T", " ")
            text = _strip_to_user_message(m.group(2))
            if not text:
                continue
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
                dated.append((dt, text))
            except ValueError:
                undated.append(text)
            continue

        # Non-timestamped bullet (e.g. a voice-added "- [ ] …" checkbox item).
        body = line[2:]
        cb = re.match(r"\[([ xX])\]\s*", body)
        if cb:
            if cb.group(1).lower() == "x":
                continue  # checked off → done
            body = body[cb.end():]
        text = _clean_undated(body)
        if text:
            undated.append(text)

    filtered, target, label = _resolve_when(when, today)

    if filtered:
        sel = sorted(dt_t for dt_t in dated if dt_t[0].date() == target)
        if not sel:
            return f"You have no reminders for {label}."
        out = [f"{_date_label(target, today)}:"]
        out += [f"- {_fmt_time(dt)} — {text}" for dt, text in sel]
        return "\n".join(out)

    if not dated and not undated:
        return "You have no pending reminders."

    out: list[str] = []
    current_day: date | None = None
    for dt, text in sorted(dated):
        d = dt.date()
        if d != current_day:
            if current_day is not None:
                out.append("")
            out.append(f"{_date_label(d, today)}:")
            current_day = d
        out.append(f"- {_fmt_time(dt)} — {text}")

    if undated:
        if out:
            out.append("")
        out.append("No set time:")
        out += [f"- {text}" for text in undated]

    return "\n".join(out).strip()


@mcp.tool()
def add_reminder(text: str, when: str = "") -> str:
    """Add a reminder for the household, scheduled to fire at a specific time.

    `when` is when it should go off — give it as concretely as you can. Accepts
    things like 'YYYY-MM-DD HH:MM', 'today 6pm', 'tomorrow 9am', 'friday 8am',
    '9:30am', 'in 30 minutes', 'in 2 hours', 'tonight', 'noon'. When it fires it
    reaches everyone in the household on whatever channel they use.

    If you can't work out a time, ask the user for one before calling this — a
    reminder with no time can't be scheduled.
    """
    body = " ".join((text or "").split())
    if not body:
        return "What should I remind everyone about?"

    now = _local_now()
    dt = _parse_when(when, now)
    if dt is None:
        hint = f' from "{when}"' if when else ""
        return (
            f"I couldn't work out a time{hint}. When should this go off? "
            "For example 'tomorrow at 9am' or 'in 2 hours'."
        )

    ts = dt.strftime("%Y-%m-%d %H:%M")
    # Canonical scheduler line: no @mention/from: tag, so the reconciler fans it
    # out to the whole household (the right default for a shared appliance).
    _append("reminders.md", f"- [{ts}] {body} urg:normal")
    return f'Added "{body}" for {_date_label(dt.date(), now.date())} at {_fmt_time(dt)}.'


# --- editing / cancelling existing reminders ---------------------------------
# update_reminder / delete_reminder operate on the pending "head" of
# reminders.md, the same source of truth add_reminder appends to. They rewrite
# the matched line (or drop it) and leave the ## Fired / ## Missed / ...
# sections below untouched. The engine's scheduler reconciles the change on its
# next pass: a retimed or text-changed line gets a fresh id (we strip the old
# one, mirroring scheduler.py's own recurrence roll) so the stale job ladder is
# retired; a deleted line is orphaned out, and any fire that races reconcile
# self-skips because the id is gone. A recurring reminder is just a pending line
# carrying a `repeat:` tag, so deleting it stops the chain and clearing the tag
# lets the next occurrence fire without rescheduling.

# Trailing machine tokens the agent appends after the message text. Used to
# split a line's "user text" from its "tags" so we can edit one without losing
# the other (recipients, urgency, recurrence, …).
_TAG_RES = (MENTION_RE, FROM_RE, ID_RE, ESC_RE, MISS_RE, URG_RE, FB_RE, REPEAT_RE)


def _split_head_tail(content: str) -> tuple[list[str], str]:
    """Split reminders.md into (head_lines, tail). Head is the pending region
    before the first RECOGNIZED section header (Fired/Missed/Malformed/
    Failed_Delivery); tail is that header onward, preserved verbatim so we never
    disturb fired/missed/failed history. Uses the same header set as the
    scheduler so a stray '## ...' line can't hide pending reminders from
    update/delete."""
    lines = content.splitlines()
    for i, raw in enumerate(lines):
        if _is_section_header(raw):
            return lines[:i], "\n".join(lines[i:])
    return lines, ""


def _write_reminders(head_lines: list[str], tail: str) -> None:
    head = "\n".join(head_lines).strip()
    if tail.strip():
        content = (head + "\n\n" + tail.strip() + "\n") if head else (tail.strip() + "\n")
    else:
        content = (head + "\n") if head else ""
    _write("reminders.md", content)


def _split_message_tags(message: str) -> tuple[str, str]:
    """Split a reminder's message portion into (user_text, tags). The agent
    writes free text first, then tokens (@mentions, from:, urg:, repeat:, …), so
    we cut at the earliest token (or '(' annotation) and keep the rest verbatim."""
    starts = [mm.start() for rx in _TAG_RES for mm in rx.finditer(message)]
    paren = message.find("(")
    if paren != -1:
        starts.append(paren)
    if not starts:
        return message.strip(), ""
    cut = min(starts)
    return message[:cut].strip(), message[cut:].strip()


def _match_pending(content: str, match: str) -> list[dict]:
    """Pending head reminders whose user-facing text contains `match`
    (case-insensitive). Each item: {idx into head_lines, line, text, dt|None}."""
    head_lines, _ = _split_head_tail(content)
    needle = " ".join((match or "").lower().split())
    out: list[dict] = []
    for idx, raw in enumerate(head_lines):
        line = raw.strip()
        if not line.startswith("- ") or ACKED_RE.search(line):
            continue
        m = LINE_RE.match(line)
        if m:
            ts_str = m.group(1).replace("T", " ")
            text = _strip_to_user_message(m.group(2))
            try:
                dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
            except ValueError:
                dt = None
        else:
            text = _clean_undated(line[2:])
            dt = None
        if not text:
            continue
        if needle and needle not in text.lower():
            continue
        out.append({"idx": idx, "line": raw, "text": text, "dt": dt})
    return out


def _describe_reminder(hit: dict) -> str:
    if hit["dt"] is not None:
        now = _local_now()
        return f'"{hit["text"]}" ({_date_label(hit["dt"].date(), now.date())} at {_fmt_time(hit["dt"])})'
    return f'"{hit["text"]}"'


@mcp.tool()
def update_reminder(match: str, new_text: str = "", when: str = "", repeat: str = "") -> str:
    """Change an existing PENDING reminder — its time, its wording, and/or how it
    repeats. Identify which one with `match`: a few distinctive words from the
    reminder's text (case-insensitive). At least one of new_text/when/repeat must
    be given.

      new_text — replace the wording. Recipients and other tags are kept.
      when     — reschedule it. Same phrasings as add_reminder: 'tomorrow 9am',
                 'friday 8am', 'in 2 hours', '6pm', 'YYYY-MM-DD HH:MM'.
      repeat   — change the recurrence: 'daily', 'weekly', 'hourly', or a span
                 like '2h', '30m', '3d'. Use 'none' (or 'stop'/'off') to STOP it
                 repeating: the next occurrence still happens but it won't
                 reschedule after that. To cancel it outright, use delete_reminder.

    If `match` hits more than one reminder, this lists them so you can ask which.
    """
    content = _read("reminders.md")
    hits = _match_pending(content, match)
    if not hits:
        if not match:
            return "Which reminder should I change? Tell me a few words from it."
        return (f'I don\'t see a pending reminder matching "{match}". '
                "Say 'list my reminders' to hear what's set.")
    if len(hits) > 1:
        listing = "; ".join(_describe_reminder(h) for h in hits)
        return f'I found a few reminders matching "{match}": {listing}. Which one?'
    if not (new_text.strip() or when.strip() or repeat.strip()):
        return "What should I change — the time, the wording, or how often it repeats?"

    hit = hits[0]
    line = hit["line"].strip()
    m = LINE_RE.match(line)
    if m:
        ts_str = m.group(1).replace("T", " ")
        message = m.group(2)
    else:
        ts_str = ""
        message = line[2:]

    text_part, tags_part = _split_message_tags(message)
    # Drop the old id so the reconciler assigns a fresh one and retires the old
    # job ladder — same convention scheduler.py uses when it rolls a recurrence.
    tags_part = ID_RE.sub("", tags_part).strip()

    if new_text.strip():
        text_part = " ".join(new_text.split())

    if repeat.strip():
        r = repeat.strip().lower()
        if r in ("none", "off", "stop", "no", "never", "once"):
            tags_part = REPEAT_RE.sub("", tags_part).strip()
        elif re.fullmatch(r"daily|weekly|hourly|\d+[mhd]", r):
            if REPEAT_RE.search(tags_part):
                tags_part = REPEAT_RE.sub(f"repeat:{r}", tags_part)
            else:
                tags_part = (tags_part + f" repeat:{r}").strip()
        else:
            return (f'I didn\'t understand the repeat "{repeat}". Try daily, weekly, '
                    "hourly, or a span like 2h or 30m — or 'none' to stop repeating.")

    now = _local_now()
    dt: datetime | None
    if when.strip():
        dt = _parse_when(when, now)
        if dt is None:
            return (f'I couldn\'t work out a new time from "{when}". '
                    "Try 'tomorrow at 9am' or 'in 2 hours'.")
        ts_str = dt.strftime("%Y-%m-%d %H:%M")
    elif ts_str:
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M")
        except ValueError:
            dt = None
    else:
        dt = None

    new_message = " ".join((text_part + " " + tags_part).split())
    new_line = f"- [{ts_str}] {new_message}".rstrip() if ts_str else f"- {new_message}".rstrip()

    head_lines, tail = _split_head_tail(content)
    head_lines[hit["idx"]] = new_line
    _write_reminders(head_lines, tail)

    clean = _strip_to_user_message(new_message) or text_part
    when_str = f" — {_date_label(dt.date(), now.date())} at {_fmt_time(dt)}" if dt else ""
    rep = REPEAT_RE.search(new_message)
    rep_str = f", repeating {rep.group(1)}" if rep else ""
    return f'Updated "{clean}"{when_str}{rep_str}.'


@mcp.tool()
def delete_reminder(match: str) -> str:
    """Cancel/remove a PENDING reminder entirely. Identify it with `match`: a few
    distinctive words from its text (case-insensitive). This also stops a
    recurring reminder for good — it removes the pending occurrence that would
    otherwise reschedule. If `match` hits more than one, this lists them so you
    can ask which to cancel."""
    content = _read("reminders.md")
    hits = _match_pending(content, match)
    if not hits:
        if not match:
            return "Which reminder should I cancel? Tell me a few words from it."
        return f'I don\'t see a pending reminder matching "{match}".'
    if len(hits) > 1:
        listing = "; ".join(_describe_reminder(h) for h in hits)
        return f'I found a few matching "{match}": {listing}. Which one should I cancel?'

    hit = hits[0]
    head_lines, tail = _split_head_tail(content)
    del head_lines[hit["idx"]]
    _write_reminders(head_lines, tail)

    extra = " It won't repeat anymore." if REPEAT_RE.search(hit["line"]) else ""
    when_str = ""
    if hit["dt"] is not None:
        now = _local_now()
        when_str = f" ({_date_label(hit['dt'].date(), now.date())} at {_fmt_time(hit['dt'])})"
    return f'Cancelled "{hit["text"]}"{when_str}.{extra}'


@mcp.tool()
def add_recurring_reminder(
    text: str,
    times: str,
    repeat: str = "daily",
    recipients: str = "",
    urgency: str = "normal",
) -> str:
    """Create a reminder that fires at one or MORE times of day, optionally
    recurring. Use this for 'N times a day', 'morning and night', or any
    multi-time / repeating reminder (for a single one-off time, add_reminder is
    fine). Each time is scheduled at its next future occurrence — today if it's
    still ahead, otherwise tomorrow — so nothing is ever set in the past.

      times      — comma-separated clock times, e.g. "9am, 11am, 1pm, 3pm, 5pm"
                   or "8:00, 20:00".
      repeat     — "daily" (default), "weekly", "hourly", or a span like "2h" /
                   "30m" / "3d". Pass "none" for one-time reminders at those times.
      recipients — comma-separated household names responsible, e.g. "Sunanda".
                   Omit to remind the whole household.
      urgency    — low | normal | high (default normal).
    """
    now = _local_now()
    time_list = [t.strip() for t in (times or "").replace(";", ",").split(",") if t.strip()]
    recip_list = [r.strip() for r in (recipients or "").replace(";", ",").split(",") if r.strip()]
    rep = None if (repeat or "").strip().lower() in ("none", "off", "once", "") else repeat
    try:
        lines, occ = reminder_builder.build_occurrences(
            text, time_list, now=now, repeat=rep,
            recipients=recip_list, origin_chat=None, urgency=urgency,
        )
    except reminder_builder.ReminderError as e:
        return str(e)
    if not lines:
        return "I couldn't work out any times for that. Try something like '9am, 1pm, 5pm'."
    content = _read("reminders.md")
    head_lines, tail = _split_head_tail(content)
    head_lines.extend(lines)
    _write_reminders(head_lines, tail)
    body = " ".join((text or "").split())
    return f'Set "{body}": {reminder_builder.summarize(occ, now, rep)}.'


@mcp.tool()
def remember(note: str) -> str:
    """Save a durable, general note to the household's shared memory. For a baby
    feeding, use `log_feed` instead so it lands in the feed log."""
    _append("notes.md", f"- {datetime.now().date().isoformat()}: {note}")
    return "Saved to household memory."


_FEED_LOG = "knowledge/baby_feed_log.md"


def _normalize_feed_type(kind: str) -> str:
    """Map a spoken feed type to the log's canonical Type value."""
    k = (kind or "").strip().lower().replace(" ", "")
    if not k:
        return ""
    if "bottle" in k or "formula" in k:
        return "Bottle"
    if k in ("bf-l+r", "bflr", "both", "bothbreasts") or ("left" in k and "right" in k):
        return "BF-L+R"
    if "left" in k or k in ("bf-l", "bfl"):
        return "BF-L"
    if "right" in k or k in ("bf-r", "bfr"):
        return "BF-R"
    if "breast" in k or k.startswith("bf") or k == "nursing":
        return "BF"
    return kind.strip()


def _feed_summary(row: dict) -> str:
    """Short spoken-friendly description of a feed row."""
    parts = [row.get(c, "") for c in ("Type", "Amount", "Duration")]
    body = ", ".join(p for p in parts if p and p != "—") or "diaper"
    extras = []
    if row.get("Pee"):
        extras.append("pee")
    if row.get("Poop"):
        extras.append("poop")
    if extras:
        body += " (" + " + ".join(extras) + ")"
    return body


@mcp.tool()
def log_feed(
    amount: str = "",
    kind: str = "",
    duration: str = "",
    when: str = "",
    pee: str = "",
    poop: str = "",
    notes: str = "",
) -> str:
    """Record a baby feeding in the household's shared FEED LOG
    (knowledge/baby_feed_log.md) — the SAME Markdown table the chat assistant
    reads when asked "when was the last feed?". Use this (not `remember`) for any
    feeding so it shows up across every channel.

    Required to record a feed: the type, and a measure (oz for a bottle, minutes
    for a breastfeed). If something required is missing, this returns a question
    to ask the user — do that and call again; don't log a half-empty entry.

      kind     — "bottle", "breastfeed left"/"BF-L", "breastfeed right"/"BF-R",
                 "BF" (unspecified breast), "BF-L+R" (both)
      amount   — how much, e.g. "1.5 oz", "15 mL", "~1 oz"
      duration — how long for a breastfeed, e.g. "20 mins"
      when     — DEFAULTS TO NOW. Accepts "now", "1:30pm", "today 2pm",
                 "20 minutes ago", "YYYY-MM-DD HH:MM". Never logs in the future.
      pee/poop — "yes" if there was one (or a volume like "✓ ~2 oz")
      notes    — anything else worth recording.
    """
    type_norm = _normalize_feed_type(kind)
    is_bottle = type_norm == "Bottle"
    is_bf = type_norm.startswith("BF")
    has_feed = bool(type_norm) or bool(amount.strip()) or bool(duration.strip())
    has_diaper = bool(feed_format.diaper_cell(pee)) or bool(feed_format.diaper_cell(poop))

    # Clarify required info instead of writing an incomplete entry.
    if not has_feed and not has_diaper:
        return ("I can log that — is it a feed or a diaper change? For a feed, "
                "tell me breast or bottle, and how much (oz) or how long (mins).")
    if has_feed:
        if not type_norm:
            return "Was that a breastfeed or a bottle?"
        if is_bottle and not amount.strip():
            return "How much was the bottle — how many ounces (or mL)?"
        if is_bf and not duration.strip() and not amount.strip():
            return "How long was the feed — about how many minutes?"

    now = _local_now()
    dt = _parse_when(when, now, prefer_past=True) if when.strip() else now
    if dt is None:
        dt = now

    amt = amount.strip()
    if is_bf and not amt:
        amt = "~1 oz"  # log convention: BF sessions assumed ~1 oz unless noted

    diaper_only = has_diaper and not has_feed
    fields = {
        "Date": dt.strftime("%Y-%m-%d"),
        "Time": feed_format.fmt_time(dt),
        "Type": type_norm or ("—" if diaper_only else ""),
        "Amount": amt or ("—" if diaper_only else ""),
        "Duration": duration.strip() or ("—" if diaper_only else ""),
        "Pee": feed_format.diaper_cell(pee),
        "Poop": feed_format.diaper_cell(poop),
        "Notes": notes.strip(),
    }
    _write(_FEED_LOG, feed_format.append_feed(_read(_FEED_LOG), fields))
    return f"Logged: {_feed_summary(fields)} at {fields['Time']} on {fields['Date']}."


@mcp.tool()
def amend_last_feed(
    amount: str = "",
    kind: str = "",
    duration: str = "",
    pee: str = "",
    poop: str = "",
    notes: str = "",
) -> str:
    """Correct or add detail to the MOST RECENT feed entry, in place — instead of
    logging a duplicate. Use this when the user fixes the feed they just logged,
    e.g. "actually there was poop too" or "it was 2 oz, not 1". Only the fields
    you pass change."""
    updates: dict = {}
    t = _normalize_feed_type(kind)
    if t:
        updates["Type"] = t
    if amount.strip():
        updates["Amount"] = amount.strip()
    if duration.strip():
        updates["Duration"] = duration.strip()
    if feed_format.diaper_cell(pee):
        updates["Pee"] = feed_format.diaper_cell(pee)
    if feed_format.diaper_cell(poop):
        updates["Poop"] = feed_format.diaper_cell(poop)
    if notes.strip():
        updates["Notes"] = notes.strip()
    if not updates:
        return "What should I change about the last feed?"

    result = feed_format.amend_last_feed(_read(_FEED_LOG), updates)
    if result is None:
        return "There's no feed entry yet to amend."
    new_content, row = result
    _write(_FEED_LOG, new_content)
    return f"Updated the last feed ({row.get('Date','')} {row.get('Time','')}): {_feed_summary(row)}."


_OZ_PER_ML = 1 / 29.5735


def _amount_oz(amount: str, ftype: str) -> float:
    """Best-effort ounces for a feed row. Sums every 'N oz' / 'N mL' token
    (so '~35 mL + 15 mL' works); falls back to ~1 oz for a breastfeed with no
    stated volume, per the log's own convention."""
    s = (amount or "").lower()
    total = 0.0
    found = False
    for num, unit in re.findall(r"([\d.]+)\s*(oz|ml)", s):
        try:
            v = float(num)
        except ValueError:
            continue
        found = True
        total += v if unit == "oz" else v * _OZ_PER_ML
    if not found:
        return 1.0 if ftype.startswith("BF") else 0.0
    return total


def _round_oz(x: float):
    r = round(x, 1)
    return int(r) if r == int(r) else r


def _pretty_date(d: date, today: date) -> str:
    rel = (d - today).days
    md = f"{d.strftime('%a %b')} {d.day}"
    if rel == 0:
        return f"Today ({md})"
    if rel == -1:
        return f"Yesterday ({md})"
    if rel == 1:
        return f"Tomorrow ({md})"
    return md


def _feed_day_target(day: str, today: date):
    d = (day or "").strip().lower()
    if not d:
        return None
    if d == "today":
        return today
    if d in ("yesterday", "yday"):
        return today - timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", d)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", d)  # M/D, assume current year
    if m:
        try:
            return date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _feed_line(row: dict) -> str:
    if row.get("Type", "") in ("", "—"):
        marks = [m for m, v in (("pee", row.get("Pee")), ("poop", row.get("Poop"))) if v]
        return f"- {row.get('Time','')} — diaper" + (f" ({', '.join(marks)})" if marks else "")
    bits = [row.get("Type", ""), row.get("Amount", ""), row.get("Duration", "")]
    return f"- {row.get('Time','')} — " + ", ".join(b for b in bits if b and b != "—")


@mcp.tool()
def list_feeds(day: str = "") -> str:
    """Summarize the baby's feeds from the shared feed log
    (knowledge/baby_feed_log.md). Use this for ANY feed question — you CAN see
    the log through this tool, so never say you can't.

      - day="" (default) → per-day ounce totals for the last few days
      - day="today" / "yesterday" / "YYYY-MM-DD" / "M/D" → that day's feeds
        listed out, with the day's total.

    Ounce totals sum oz + mL and assume ~1 oz for a breastfeed with no stated
    volume; diaper-only rows are excluded from totals."""
    content = _read(_FEED_LOG)
    rows = [r for r in (feed_format.parse_row(l) for l in content.splitlines()
                        if feed_format.is_data_row(l)) if r]
    if not rows:
        return "No feeds logged yet."

    today = _local_now().date()
    target = _feed_day_target(day, today)

    if target is not None:
        day_rows = [r for r in rows if r.get("Date") == target.isoformat()]
        if not day_rows:
            return f"No feeds logged for {_pretty_date(target, today)}."
        feeds = [r for r in day_rows if r.get("Type") not in ("", "—")]
        total = sum(_amount_oz(r.get("Amount", ""), r.get("Type", "")) for r in feeds)
        head = (f"{_pretty_date(target, today)} — {len(feeds)} feeds, "
                f"~{_round_oz(total)} oz total:")
        return "\n".join([head] + [_feed_line(r) for r in day_rows])

    # No day → per-day totals for the most recent days (file is chronological).
    by_day: dict = {}
    for r in rows:
        by_day.setdefault(r.get("Date", ""), []).append(r)
    recent = list(by_day.keys())[-4:]
    out = ["Recent daily feed totals:"]
    for ds in reversed(recent):
        feeds = [r for r in by_day[ds] if r.get("Type") not in ("", "—")]
        total = sum(_amount_oz(r.get("Amount", ""), r.get("Type", "")) for r in feeds)
        try:
            label = _pretty_date(date.fromisoformat(ds), today)
        except ValueError:
            label = ds or "(undated)"
        out.append(f"- {label}: {len(feeds)} feeds, ~{_round_oz(total)} oz")
    return "\n".join(out)


if __name__ == "__main__":
    # Serves MCP over Streamable HTTP at /mcp on 0.0.0.0:MCP_PORT.
    mcp.run(transport="streamable-http")
