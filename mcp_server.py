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

from paths import memories_dir
from reminder_format import (
    ACKED_RE,
    FB_RE,
    FROM_RE,
    ID_RE,
    LINE_RE,
    MENTION_RE,
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


def _pending_head(content: str) -> str:
    """The 'head' of reminders.md — everything before the first '## ' section
    header. Fired/Missed/Malformed/Failed_Delivery all live below a header, so
    the head is exactly the set of not-yet-fired (pending) reminders."""
    out: list[str] = []
    for raw in content.splitlines():
        if raw.lstrip().startswith("## "):
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


def _parse_when(when: str, now: datetime) -> datetime | None:
    """Best-effort parse of a `when` expression into a concrete datetime.
    Returns None when no time can be determined (caller should ask the user)."""
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
        # Bare time → today if still ahead, otherwise tomorrow.
        cand = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
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


@mcp.tool()
def remember(note: str) -> str:
    """Save a durable note to the household's shared memory."""
    _append("notes.md", f"- {datetime.now().date().isoformat()}: {note}")
    return "Saved to household memory."


if __name__ == "__main__":
    # Serves MCP over Streamable HTTP at /mcp on 0.0.0.0:MCP_PORT.
    mcp.run(transport="streamable-http")
