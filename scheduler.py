"""Persistent reminder scheduler with per-addressee escalation ladders.

Each pending reminder line in /memories/reminders.md fans out into a
ladder of one-shot DateTrigger jobs in the SQLAlchemy-backed jobstore:

    fire:<task_id>:<addressee_slug>      one per @-mentioned addressee
                                         (fires to that person's channels)
    escalate:<task_id>:<addressee_slug>  one per addressee, at T+escalate
                                         (louder re-ping to same channels)
    fallback:<task_id>                   one per reminder, at T+fallback
                                         (pages the dynamically-resolved
                                          fallback person)
    miss:<task_id>                       one per reminder, at T+miss
                                         (terminal log — drop the chase)

The whole lifecycle is encoded as annotations the line accumulates over
time (`(fired at …)`, `(escalated to X …)`, `(fallback to Y …)`, `(acked
by Z …)`, `(missed at …)`). Each downstream job re-reads the line at
fire time and self-skips if `(acked` is present, so a single ack on the
line cancels every pending job across every addressee's ladder. Robust
to crashes, file edits, and out-of-order delivery — no explicit job
cancellation needed.

Urgency tier (`urg:low|normal|high` on the line) picks the interval
preset; see URGENCY_INTERVALS below for the defaults. Without an explicit
tier the line is treated as "normal" — escalate at +15m, fallback at
+45m, miss at +2h. Per-line `esc:Nm` / `miss:Nh` tags override the
preset's escalate / miss horizon.

Public API:
    start()                   start the singleton scheduler (idempotent)
    shutdown()                stop it (idempotent)
    reconcile()               sync reminders.md → scheduler jobs; call on
                              startup and after each agent turn that may
                              have written reminders.md
    mark_acked(task_id, by)   record an ack annotation on the matching
                              line. Every downstream job self-skips on it.
    find_task_by_chat_msg(c,m) look up which task_id (if any) sent the
                              Telegram message at (chat_id, msg_id), used
                              by reply-to-bot ack detection
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.date import DateTrigger

import channels
import roster
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

log = logging.getLogger("rosey.scheduler")

# Section headers in reminders.md. Order in the file: head → Fired →
# Missed → Malformed → Failed_Delivery.
_FIRED_HEADER = "## Fired"
_MISSED_HEADER = "## Missed"
_MALFORMED_HEADER = "## Malformed"
_FAILED_HEADER = "## Failed_Delivery"

# Urgency tier presets — the agent picks one at schedule time and writes
# `urg:low|normal|high` on the line. Each preset maps to a (escalate,
# fallback, miss) timedelta triple. A None entry means "skip this tier" —
# low has no escalate/fallback (it's fire-and-forget; the miss horizon
# exists only so the line gets logged as missed if untouched).
#
# Explicit `esc:Nm` / `miss:Nh` tags on the same line override the
# corresponding preset entry. Fallback is preset-only (no per-line tag) —
# if you want fine-grained control, set the urgency.
URGENCY_INTERVALS: dict[str, dict[str, Optional[timedelta]]] = {
    "low":    {"escalate": None,                  "fallback": None,                 "miss": timedelta(hours=1)},
    "normal": {"escalate": timedelta(minutes=15), "fallback": timedelta(minutes=45), "miss": timedelta(hours=2)},
    "high":   {"escalate": timedelta(minutes=3),  "fallback": timedelta(minutes=10), "miss": timedelta(minutes=30)},
}
DEFAULT_URGENCY = "normal"

# Backward-compat: when reconcile sees a line with neither `urg:` nor
# `esc:`/`miss:`, treat as DEFAULT_URGENCY. These two consts kept so
# external callers / migrations referencing them don't break.
DEFAULT_ESCALATE_AFTER = URGENCY_INTERVALS[DEFAULT_URGENCY]["escalate"]
DEFAULT_MISS_AFTER = URGENCY_INTERVALS[DEFAULT_URGENCY]["miss"]

# Module-level singleton scheduler.
_scheduler: BackgroundScheduler | None = None
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    """Where the SQLite jobstore lives. Defaults to a sibling of memories/
    so it backs up alongside everything else.
    """
    override = os.environ.get("SCHEDULER_DB_PATH")
    if override:
        return Path(override)
    return memories_dir().parent / "scheduler.db"


def _local_tz():
    tz_name = os.environ.get("SCHEDULER_TZ", "UTC")
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def _now_str() -> str:
    tz = _local_tz()
    return datetime.now(tz=tz).strftime("%Y-%m-%d %H:%M")


def _generate_id(ts_str: str, message: str) -> str:
    """12-char content-derived id, stable across restarts. Same line →
    same id → reconcile is idempotent. Edited line → new id, old jobs
    age out as orphans on next reconcile.
    """
    return hashlib.sha1(f"{ts_str}|{message}".encode("utf-8")).hexdigest()[:12]


def _parse_duration(value: str, unit: str) -> timedelta:
    n = int(value)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    raise ValueError(f"unknown duration unit: {unit!r}")


# `_strip_to_user_message` now lives in reminder_format (alongside the tag
# regexes it depends on) so every reader of reminders.md — this scheduler and
# the co-located MCP server — strips the identical tag set. It's imported above
# and remains accessible as `scheduler._strip_to_user_message` for callers and
# tests that reference it by that path.


def _format_assignee_html(assignees) -> str:
    """Render `assignees` as space-joined Telegram HTML mentions.

    Accepts either:
      - list of (name, tg_chat_id_str | None) tuples (new format)
      - list of plain name strings (legacy format from older job entries)

    For known chat_ids, emits `<a href="tg://user?id=NNN">Name</a>`,
    which renders as a clickable mention AND fires a notification ping
    for that user (works even without a public @username, as long as
    the user has shared a chat with the bot).

    For unknown chat_ids (None or absent), falls back to plain `@Name`
    text — visible attribution but no notification ping.

    Names and IDs are HTML-escaped defensively to keep odd characters
    from breaking Telegram's HTML parser.
    """
    parts: list[str] = []
    for entry in assignees or []:
        if isinstance(entry, str):
            name, chat_id = entry, None
        else:
            name = entry[0] if len(entry) > 0 else ""
            chat_id = entry[1] if len(entry) > 1 else None
        if not name:
            continue
        safe_name = html.escape(name)
        if chat_id and str(chat_id).lstrip("-").isdigit():
            parts.append(f'<a href="tg://user?id={int(chat_id)}">{safe_name}</a>')
        else:
            parts.append(f"@{safe_name}")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Fallback resolution — runs at fallback-fire time, not at schedule time,
# so roster edits between scheduling and firing take effect.
# ---------------------------------------------------------------------------

def _slug_for_addressee(name: str) -> str:
    """Stable lowercase slug for use in APScheduler job IDs. Strips
    everything that isn't alphanumeric so `fire:<task_id>:<slug>` stays
    a clean opaque key.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower()) or "anon"


def _resolve_fallback_recipient(
    raw_message: str, origin_chat: str,
) -> tuple[str, list[str]]:
    """Return (fallback_name, list_of_identifiers) for the fallback tier.

    Resolution order, most specific first:
      1. explicit `fb:Name` tag on the line
      2. another @-mentioned person on the line who isn't an addressee
         (i.e. someone the agent surfaced as a co-witness)
      3. owner of the `from:` chat if a different person from any addressee
      4. next member of household.md, in roster order, excluding addressees
      5. ("", []) — nothing, fallback tier silently skips

    The "addressees" set is the @-mentioned people on the line; the
    fallback by definition is not one of them.
    """
    addressee_names = [n.lower() for n in MENTION_RE.findall(raw_message)]

    # Group roster by name → all idents.
    grouped: dict[str, list[str]] = {}
    canonical: dict[str, str] = {}
    for m in roster.members():
        grouped.setdefault(m.name.lower(), []).append(m.identifier)
        canonical.setdefault(m.name.lower(), m.name)

    def _idents_for(name_lower: str) -> tuple[str, list[str]]:
        idents = grouped.get(name_lower, [])
        return canonical.get(name_lower, name_lower), idents

    # 1. explicit fb: tag
    fb_match = FB_RE.search(raw_message)
    if fb_match:
        name, idents = _idents_for(fb_match.group(1).lower())
        if idents:
            return name, idents
        # explicit fb: that doesn't resolve in roster — log and fall through
        log.info(
            "fallback resolve: explicit fb:%s didn't resolve in household.md, "
            "falling through to dynamic resolution",
            fb_match.group(1),
        )

    # 2. another @-mentioned non-addressee
    # (Reminders rarely have non-addressee @-mentions, but if the agent
    # writes "@Ankit (and @Sunanda will know if anything)", Sunanda
    # qualifies.)
    # Skipping for now since MENTION_RE doesn't distinguish addressees
    # from co-witnesses — the convention is all @-mentions are addressees.
    # If/when the agent starts marking co-witnesses differently, slot in here.

    # 3. owner of from: chat if different from addressees
    if origin_chat:
        for m in roster.members():
            if m.identifier == origin_chat and m.name.lower() not in addressee_names:
                # If this person has multiple identifiers, fan to all.
                name, idents = _idents_for(m.name.lower())
                if idents:
                    return name, idents

    # 4. next household member by roster order, excluding addressees
    for m in roster.members():
        lower = m.name.lower()
        if lower in addressee_names:
            continue
        name, idents = _idents_for(lower)
        if idents:
            return name, idents

    # 5. nothing
    return "", []


# ---------------------------------------------------------------------------
# Lifecycle: start / shutdown
# ---------------------------------------------------------------------------

def start() -> BackgroundScheduler:
    """Start the singleton scheduler. Idempotent."""
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            return _scheduler

        db_path = _db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        jobstore = SQLAlchemyJobStore(url=f"sqlite:///{db_path}")

        sched = BackgroundScheduler(
            jobstores={"default": jobstore},
            timezone=_local_tz() or "UTC",
            # If the bot was offline when a job was due, fire it within
            # this window on resume. coalesce → only once even if many
            # ticks were missed.
            job_defaults={"coalesce": True, "misfire_grace_time": 24 * 60 * 60},
        )
        sched.start()
        _scheduler = sched
        log.info("scheduler started — jobstore=%s", db_path)
        return sched


def shutdown(wait: bool = False) -> None:
    global _scheduler
    with _lock:
        if _scheduler is None:
            return
        try:
            _scheduler.shutdown(wait=wait)
        except Exception:
            log.exception("scheduler shutdown failed")
        _scheduler = None


def schedule_periodic_reconcile(interval_seconds: int | None = None) -> None:
    """Register a recurring reconcile so reminders written OUT-OF-BAND become
    live scheduler jobs without waiting for an agent turn or a process restart.

    The co-located MCP/voice server (mcp_server.py) writes reminders.md from a
    SEPARATE process, so it can't trigger the per-turn reconcile in agent.py
    (which only fires when the agent itself touched the file during a turn).
    Running reconcile on a timer here — in the engine process that owns the
    scheduler — closes that gap so a reminder added by voice fires on schedule.
    reconcile() is idempotent (content-derived ids + replace_existing), so a
    periodic pass only adds genuinely-new lines and prunes orphans.

    Engine-only: call this from server.py startup, NOT from start(), so unit
    tests that drive start()/reconcile() directly keep a deterministic job set.
    Set RECONCILE_INTERVAL_SECONDS=0 to disable.
    """
    if interval_seconds is None:
        interval_seconds = int(os.environ.get("RECONCILE_INTERVAL_SECONDS", "60"))
    if interval_seconds <= 0:
        log.info("periodic reconcile disabled (interval=%s)", interval_seconds)
        return
    sched = start()
    sched.add_job(
        reconcile,
        trigger="interval",
        seconds=interval_seconds,
        id="reconcile:periodic",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    log.info("periodic reconcile registered — every %ds", interval_seconds)


# ---------------------------------------------------------------------------
# File parsing / rewriting
# ---------------------------------------------------------------------------

def _split_sections(content: str) -> dict:
    """Parse reminders.md into its named sections.

    Returns a dict with keys: head, fired, missed, malformed, failed.
    Order in the file is parsed in reverse so relative ordering doesn't
    matter — but on rewrite we always emit in the canonical order.
    """
    fired = ""
    missed = ""
    malformed = ""
    failed = ""
    rest = content
    if _FAILED_HEADER in rest:
        rest, failed = rest.split(_FAILED_HEADER, 1)
    if _MALFORMED_HEADER in rest:
        rest, malformed = rest.split(_MALFORMED_HEADER, 1)
    if _MISSED_HEADER in rest:
        rest, missed = rest.split(_MISSED_HEADER, 1)
    if _FIRED_HEADER in rest:
        rest, fired = rest.split(_FIRED_HEADER, 1)
    return {
        "head": rest,
        "fired": fired,
        "missed": missed,
        "malformed": malformed,
        "failed": failed,
    }


def _rewrite_file(
    path: Path,
    head_lines: list[str],
    fired_lines: list[str],
    missed_lines: list[str],
    malformed_block: str,
    failed_block: str,
    new_malformed: list[str] | None = None,
    new_failed: list[tuple[str, str]] | None = None,
) -> None:
    """Atomically rewrite reminders.md with the canonical section order.

    `head_lines`, `fired_lines`, `missed_lines` are the desired post-rewrite
    contents of each section (callers manage the membership). `malformed_block`
    and `failed_block` are passed through verbatim, optionally with new
    entries appended via `new_malformed` / `new_failed`.
    """
    parts: list[str] = []

    head_text = "\n".join(head_lines).rstrip()
    if head_text:
        parts.append(head_text + "\n")

    if fired_lines:
        parts.append(f"\n{_FIRED_HEADER}\n" + "\n".join(fired_lines) + "\n")

    if missed_lines:
        parts.append(f"\n{_MISSED_HEADER}\n" + "\n".join(missed_lines) + "\n")

    combined_malformed: list[str] = []
    if malformed_block.strip():
        combined_malformed.extend(malformed_block.strip().splitlines())
    if new_malformed:
        combined_malformed.extend(new_malformed)
    if combined_malformed:
        parts.append(
            f"\n{_MALFORMED_HEADER}\n"
            "(these lines didn't match `[YYYY-MM-DD HH:MM] message ...` "
            "and were not scheduled — fix the format and they'll be picked up)\n"
            + "\n".join(l.rstrip() for l in combined_malformed)
            + "\n"
        )

    combined_failed: list[str] = []
    if failed_block.strip():
        combined_failed.extend(failed_block.strip().splitlines())
    if new_failed:
        for raw_line, reason in new_failed:
            combined_failed.append(f"{raw_line.rstrip()}  ⚠️ {reason}")
    if combined_failed:
        parts.append(
            f"\n{_FAILED_HEADER}\n"
            "(these reminders parsed correctly but had no resolvable "
            "recipients — list members in /memories/household.md or include "
            "a `from:tg:<chat_id>` tag, then move the line back above)\n"
            + "\n".join(combined_failed)
            + "\n"
        )

    path.write_text("".join(parts), encoding="utf-8")


def _ensure_id(line: str, ts_str: str, raw_message: str) -> tuple[str, str, bool]:
    """Return (line_with_id, task_id, mutated). If the line lacked an
    `id:<hex>` tag, append one and report mutated=True so the caller
    rewrites the file.
    """
    found = ID_RE.findall(raw_message)
    if found:
        return line, found[0], False
    task_id = _generate_id(ts_str, raw_message)
    new_line = f"{line.rstrip()} id:{task_id}"
    return new_line, task_id, True


def _line_for_task(task_id: str, sections: dict) -> Optional[tuple[str, str]]:
    """Find the line with `id:<task_id>` in any section. Returns
    (section_name, line) or None.
    """
    for section in ("head", "fired", "missed"):
        for line in sections[section].splitlines():
            if not line.lstrip().startswith("- "):
                continue
            ids = ID_RE.findall(line)
            if task_id in ids:
                return section, line
    return None


def _replace_line_in_section(
    section_text: str, old_line: str, new_line: str | None
) -> str:
    """Replace `old_line` in a section. If `new_line` is None, drop it."""
    out = []
    replaced = False
    for line in section_text.splitlines():
        if not replaced and line == old_line:
            replaced = True
            if new_line is not None:
                out.append(new_line)
            continue
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Job targets — fire / escalate / miss
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Recurring reminders — write the next occurrence after a successful fire.
# ---------------------------------------------------------------------------

def _parse_repeat_interval(spec: str) -> timedelta | None:
    """Parse a `repeat:` value into a timedelta. Accepts named intervals
    ("daily", "weekly", "hourly") and numeric forms ("5m", "3h", "2d").
    Returns None for unknown forms.
    """
    if not spec:
        return None
    spec = spec.strip().lower()
    if spec == "daily":
        return timedelta(days=1)
    if spec == "weekly":
        return timedelta(days=7)
    if spec == "hourly":
        return timedelta(hours=1)
    if len(spec) >= 2 and spec[:-1].isdigit() and spec[-1] in "mhd":
        n = int(spec[:-1])
        unit = spec[-1]
        if unit == "m":
            return timedelta(minutes=n)
        if unit == "h":
            return timedelta(hours=n)
        if unit == "d":
            return timedelta(days=n)
    return None


def _maybe_schedule_next_occurrence(task_id: str, ts_str: str, raw_message: str) -> bool:
    """If `raw_message` carries a `repeat:` tag, write a fresh reminder
    line for the next occurrence into the head section of reminders.md.

    Idempotent: the new line's id is deterministic from (next_ts, body),
    so calling this twice writes the same line; the second write is a
    no-op via the same-id check in reconcile. Safe to invoke from
    fire_one for every addressee — only the first call materially
    changes the file.

    Returns True if a new line was added (or already existed), False if
    no recurrence is configured or parsing failed.
    """
    repeat_match = REPEAT_RE.search(raw_message)
    if not repeat_match:
        return False
    interval = _parse_repeat_interval(repeat_match.group(1))
    if interval is None:
        log.warning(
            "recur task=%s: unparseable repeat spec %r — chain stops",
            task_id, repeat_match.group(1),
        )
        return False

    # Compute the next timestamp. Use the ORIGINAL fire timestamp as the
    # anchor so the cadence stays aligned with the user's intent (daily
    # at 9am stays at 9am, not "1 day after whenever I happened to fire").
    try:
        # The line stores "YYYY-MM-DD HH:MM" (space) or "YYYY-MM-DDTHH:MM" (T).
        ts_clean = ts_str.replace("T", " ")
        current = datetime.strptime(ts_clean, "%Y-%m-%d %H:%M")
    except ValueError:
        log.warning("recur task=%s: unparseable ts %r — chain stops", task_id, ts_str)
        return False
    next_dt = current + interval
    next_ts = next_dt.strftime("%Y-%m-%d %H:%M")

    # Strip any annotations or existing id from the raw_message so the
    # new line starts clean. The repeat: tag itself stays — that's what
    # keeps the chain going.
    body = raw_message
    body = re.sub(r"\([^)]*\)", "", body)         # drop lifecycle annotations
    body = ID_RE.sub("", body)                     # drop the previous id; reconcile assigns new
    body = " ".join(body.split())                  # collapse whitespace

    new_id = _generate_id(next_ts, body)

    path = memories_dir() / "reminders.md"
    if not path.exists():
        log.warning("recur task=%s: reminders.md missing — chain stops", task_id)
        return False
    content = path.read_text(encoding="utf-8")

    # Idempotency: if a line with this exact id already exists anywhere
    # in the file, don't add another.
    if f"id:{new_id}" in content:
        return True

    new_line = f"- [{next_ts}] {body} id:{new_id}".rstrip()

    # Insert the new line into the head section (before any ## headers).
    # Crude but matches the existing _split_sections convention.
    sections = _split_sections(content)
    head = sections["head"].rstrip()
    head = head + "\n" + new_line if head else new_line
    sections["head"] = head + "\n"

    head_lines = sections["head"].splitlines()
    fired_lines = [l for l in sections["fired"].splitlines() if l.strip()]
    missed_lines = [l for l in sections["missed"].splitlines() if l.strip()]
    _rewrite_file(
        path, head_lines, fired_lines, missed_lines,
        sections["malformed"], sections["failed"],
    )
    log.info(
        "recur task=%s — wrote next occurrence at %s (new id=%s)",
        task_id, next_ts, new_id,
    )

    # The new line needs a ladder of scheduler jobs (fire/escalate/etc).
    # Trigger reconcile inline so the chain is live without waiting for
    # the next agent turn.
    try:
        reconcile()
    except Exception:
        log.exception("recur task=%s: reconcile-after-write failed", task_id)
    return True


def fire_one(
    task_id: str,
    ts_str: str,
    raw_message: str,
    recipients: list[str],
    origin_chat: str,
    assignee_names: list[str] | None = None,
) -> None:
    """Primary fire: send to recipients, capture msg_ids, move line from
    head to ## Fired with annotation.

    `assignee_names` is the list of @-named people on the line (e.g.
    ["Ankit", "Sunanda"]), included in the message body for attribution.
    When the reminder fans out to a group chat (via the from: fallback),
    this is what tells everyone who's responsible. Backwards-compatible
    default of None keeps older jobstore entries firing without crashing
    on the missing kwarg.
    """
    if not recipients:
        log.warning("fire_one task=%s: no recipients, noop", task_id)
        return

    body_text = html.escape(_strip_to_user_message(raw_message))
    mention_html = _format_assignee_html(assignee_names)
    if mention_html:
        body = f"⏰ Reminder for {mention_html}: {body_text}"
    else:
        body = f"⏰ Reminder: {body_text}"

    sent_pairs: list[tuple[str, int]] = []
    for ident in recipients:
        msg_id = channels.send_returning_msg_id(ident, body, parse_mode="HTML")
        if msg_id is not None:
            sent_pairs.append((ident, msg_id))

    if not sent_pairs:
        log.warning("fire_one task=%s: no successful sends, leaving pending", task_id)
        return

    log.info("fire_one task=%s sent_to=%s", task_id, [p[0] for p in sent_pairs])

    msg_pairs_str = " ".join(f"chat:{ident} msg:{mid}" for ident, mid in sent_pairs)
    annotation = f"(fired at {_now_str()} {msg_pairs_str})"
    _annotate_and_move(task_id, annotation, target_section="fired")

    # If this line has a `repeat:` tag, write the next occurrence into
    # head so the chain continues. Idempotent — safe to call once per
    # addressee for multi-addressee reminders; only the first call
    # materially changes the file.
    try:
        _maybe_schedule_next_occurrence(task_id, ts_str, raw_message)
    except Exception:
        log.exception("recur scheduling failed task=%s", task_id)


def escalate_one(
    task_id: str,
    ts_str: str,
    raw_message: str,
    origin_chat: str,
    assignee_names: list[str] | None = None,
    *,
    addressee_name: str | None = None,
    addressee_idents: list[str] | None = None,
) -> None:
    """If the task hasn't been acked yet, re-ping a specific addressee on
    every channel they're on (louder phrasing this time). Per-addressee:
    one escalate job per @-named person, each fanning to that person's
    own identifiers.

    Self-skips if the line is already acked OR if this addressee has
    already been escalated (multiple addressees each have their own
    escalation; the line-level "(acked …)" annotation kills them all
    when any addressee acks).

    Backwards-compat: if `addressee_idents` is omitted (legacy job entries
    persisted before this refactor), falls back to sending to `origin_chat`
    so old jobs in the SQLite jobstore still deliver something on first
    fire after deploy. Reconcile orphans them on the next pass.
    """
    state = _read_task_state(task_id)
    if state is None:
        log.info("escalate_one task=%s: line not found, noop", task_id)
        return
    if state["acked"] or state["missed"]:
        log.info("escalate_one task=%s: already %s, noop",
                 task_id, "acked" if state["acked"] else "missed")
        return

    # Per-addressee escalation marker — distinct from the legacy global
    # "(escalated …)" check so multi-addressee reminders don't have one
    # addressee's escalation suppress the other's.
    if addressee_name and f"escalated to {addressee_name}" in state["line"]:
        log.info(
            "escalate_one task=%s addressee=%s: already escalated, noop",
            task_id, addressee_name,
        )
        return

    targets = list(addressee_idents) if addressee_idents else [origin_chat]
    body_text = html.escape(_strip_to_user_message(raw_message))
    mention_html = _format_assignee_html(assignee_names)
    name_phrase = f" for {mention_html}" if mention_html else ""
    body = (
        f"⏰ Still pending{name_phrase} — please ack: {body_text}"
    )

    sent_pairs: list[tuple[str, int]] = []
    for ident in targets:
        msg_id = channels.send_returning_msg_id(ident, body, parse_mode="HTML")
        if msg_id is not None:
            sent_pairs.append((ident, msg_id))

    if not sent_pairs:
        log.warning("escalate_one task=%s addressee=%s: no successful sends",
                    task_id, addressee_name)
        return

    log.info(
        "escalate_one task=%s addressee=%s sent_to=%s",
        task_id, addressee_name, [p[0] for p in sent_pairs],
    )

    msg_pairs_str = " ".join(f"chat:{ident} msg:{mid}" for ident, mid in sent_pairs)
    name_tag = f"to {addressee_name} " if addressee_name else ""
    annotation = f"(escalated {name_tag}{msg_pairs_str} at {_now_str()})"
    _append_annotation(task_id, annotation)


def fallback_one(
    task_id: str,
    ts_str: str,
    raw_message: str,
    origin_chat: str,
    assignee_names: list[str] | None = None,
) -> None:
    """Page the fallback person if no addressee has acked by the fallback
    horizon. Resolution happens at fire time (not at schedule time) so the
    fallback adapts to roster edits between scheduling and firing.

    Resolution order (most specific → least):
      1. explicit `fb:Name` tag on the line
      2. another @-mentioned person on the line who isn't already an addressee
      3. the human owner of the `from:` chat if that person isn't an addressee
      4. next household member by roster order, excluding addressees
      5. nothing — silently skip the fallback tier (logged for debugging)

    The actual page goes to ALL of that person's channels, with phrasing
    that says "X hasn't acked Y — can you take it?".
    """
    state = _read_task_state(task_id)
    if state is None:
        return
    if state["acked"] or state["missed"]:
        log.info("fallback_one task=%s: already %s, noop",
                 task_id, "acked" if state["acked"] else "missed")
        return

    fallback_name, fallback_idents = _resolve_fallback_recipient(raw_message, origin_chat)
    if not fallback_idents:
        log.info(
            "fallback_one task=%s: no resolvable fallback person, skipping tier",
            task_id,
        )
        return

    body_text = html.escape(_strip_to_user_message(raw_message))
    addressee_phrase = (
        " ".join(f"@{n}" for n in (assignee_names or []) if isinstance(n, str))
        or "the person assigned"
    )
    # `assignee_names` may also be a list of (name, chat_id) tuples per the
    # display contract — pull the names back out for the plain-text phrase.
    if assignee_names and not all(isinstance(n, str) for n in assignee_names):
        addressee_phrase = " ".join(
            f"@{e[0]}" for e in assignee_names
            if isinstance(e, (list, tuple)) and e and e[0]
        ) or addressee_phrase
    body = (
        f"⏰ Heads up {fallback_name} — {addressee_phrase} hasn't acked: "
        f"{body_text}. Can you take it or nudge them?"
    )

    sent_pairs: list[tuple[str, int]] = []
    for ident in fallback_idents:
        msg_id = channels.send_returning_msg_id(ident, body, parse_mode="HTML")
        if msg_id is not None:
            sent_pairs.append((ident, msg_id))

    if not sent_pairs:
        log.warning("fallback_one task=%s: send to %s failed",
                    task_id, fallback_idents)
        return

    log.info(
        "fallback_one task=%s fallback_to=%s sent_to=%s",
        task_id, fallback_name, [p[0] for p in sent_pairs],
    )

    msg_pairs_str = " ".join(f"chat:{ident} msg:{mid}" for ident, mid in sent_pairs)
    annotation = f"(fallback to {fallback_name} {msg_pairs_str} at {_now_str()})"
    _append_annotation(task_id, annotation)


def miss_one(
    task_id: str,
    ts_str: str,
    raw_message: str,
    origin_chat: str,
    assignee_names: list[str] | None = None,
) -> None:
    """If still un-acked at the miss horizon, move to ## Missed and notify."""
    state = _read_task_state(task_id)
    if state is None:
        return
    if state["acked"] or state["missed"]:
        return

    body_text = html.escape(_strip_to_user_message(raw_message))
    mention_html = _format_assignee_html(assignee_names)
    if mention_html:
        body = f"⚠️ Missed reminder for {mention_html} (no acknowledgement): {body_text}"
    else:
        body = f"⚠️ Missed reminder (no acknowledgement): {body_text}"
    channels.send_returning_msg_id(origin_chat, body, parse_mode="HTML")

    log.info("miss_one task=%s — moved to ## Missed", task_id)
    annotation = f"(missed at {_now_str()} — no ack)"
    _annotate_and_move(task_id, annotation, target_section="missed")


# ---------------------------------------------------------------------------
# Ack helpers — called from telegram_bot for reply-to-bot acks, and indirectly
# (via agent annotation) for natural-language acks.
# ---------------------------------------------------------------------------

def mark_acked(task_id: str, by_name: str) -> bool:
    """Append `(acked by <name> at <now>)` to the task's line. Returns True
    if the line was found and annotated, False otherwise. Future escalate/
    miss runs will self-skip on the annotation.

    Also fires a cross-channel ack broadcast: if the reminder's `from:`
    tag points to a group chat, the originating group is notified that
    the addressee completed the task. Idempotent — won't double-broadcast.
    """
    annotation = f"(acked by {by_name} at {_now_str()})"
    ok = _append_annotation(task_id, annotation)
    if ok:
        # Best-effort: don't fail the ack if the broadcast can't go out.
        try:
            _broadcast_ack_to_origin(task_id, by_name)
        except Exception:
            log.exception("ack broadcast failed for task=%s", task_id)
    return ok


# Annotation appended by `_broadcast_ack_to_origin` to prevent re-sending
# the same completion notice when the post-turn scanner runs.
_BROADCASTED_RE = re.compile(r"\(broadcasted at ")


def _broadcast_ack_to_origin(task_id: str, by_name: str) -> bool:
    """Send a brief completion notice to the reminder's origin chat if it
    is a group. Idempotent: scans the line for a `(broadcasted at …)`
    annotation and noops if one is already present, then appends one
    after a successful send.

    Returns True if a notice was sent (or already had been), False if
    the line couldn't be found, the origin isn't a group, or send failed.
    """
    state = _read_task_state(task_id)
    if not state:
        return False
    line = state.get("line") or ""
    if _BROADCASTED_RE.search(line):
        return True  # already broadcasted on a prior pass
    from_match = FROM_RE.search(line)
    if not from_match:
        return False
    origin = from_match.group(1)
    # Only broadcast for group origins. 1:1 origins (the originator and
    # acker are typically the same person, or the originator can read
    # the ack from reminders.md when next asked) don't benefit from a
    # follow-up notification, and broadcasting back to the same person
    # in their own DM is noise.
    if "group:" not in origin:
        return False
    # Body of the reminder — strip metadata to a plain message for display.
    m = LINE_RE.match(line.lstrip())
    if not m:
        return False
    display_msg = _strip_to_user_message(m.group(2))
    body = f"✓ {by_name} completed: {display_msg}"
    sent = channels.send(origin, body)
    if not sent:
        log.warning("ack broadcast send failed: task=%s origin=%s", task_id, origin)
        return False
    log.info("ack broadcast task=%s to=%s by=%s", task_id, origin, by_name)
    # Mark the line so a later scanner pass (or a re-call) won't re-send.
    _append_annotation(task_id, f"(broadcasted at {_now_str()})")
    return True


def scan_pending_ack_broadcasts() -> int:
    """Scan reminders.md for acked lines whose ack hasn't yet been
    broadcast to the origin group, and broadcast each. Called after every
    agent turn to catch agent-driven natural-language acks (where the
    agent appends `(acked by …)` via str_replace, bypassing mark_acked).

    Returns the count of broadcasts dispatched on this scan.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return 0
    sent = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.lstrip().startswith("- "):
            continue
        if not ACKED_RE.search(raw_line):
            continue
        if _BROADCASTED_RE.search(raw_line):
            continue
        ids = ID_RE.findall(raw_line)
        if not ids:
            continue
        # Extract the most-recent acker name from the (acked by NAME …)
        # annotation so the broadcast attributes correctly.
        ack_m = re.search(r"\(acked by ([^\s)]+)", raw_line)
        by_name = ack_m.group(1) if ack_m else "someone"
        if _broadcast_ack_to_origin(ids[0], by_name):
            sent += 1
    return sent


def find_task_by_chat_msg(chat_id: str, msg_id: int) -> Optional[str]:
    """For reply-to-bot ack lookup. Scan reminders.md's annotations for a
    matching `chat:<chat_id> msg:<msg_id>` token. Returns the task_id of
    the line containing it, or None.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return None
    needle = f"chat:{chat_id} msg:{msg_id}"
    for line in path.read_text(encoding="utf-8").splitlines():
        if needle in line:
            ids = ID_RE.findall(line)
            if ids:
                return ids[0]
    return None


def recent_fires_for(identifier: str, within_minutes: int = 10) -> list[dict]:
    """Reminders that fired to `identifier` (e.g. "tg:8600355980", or a
    "wa:+155…" ident) within the last `within_minutes`, that are still
    un-acked. Returned as a list of dicts ordered most-recent first:

        [
          { "task_id": "abc123", "fired_at": "2026-05-10 14:32",
            "msg_id": "1234", "summary": "pick up baby from daycare" },
          ...
        ]

    Used by the agent to disambiguate casual "ok" / "yep" / "got it"
    replies — when the user sends one of those within minutes of
    receiving a reminder, this gives the agent the exact line to ack
    instead of guessing.

    Reads reminders.md only (no scheduler state). Lines that have an
    `(acked …)` annotation OR that are in the ## Missed section are
    excluded — there's nothing left to ack on those.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return []

    tz = _local_tz()
    now = datetime.now(tz=tz)
    horizon = now - timedelta(minutes=within_minutes)

    out: list[dict] = []
    sections = _split_sections(path.read_text(encoding="utf-8"))
    # Look in head + fired sections — both can have un-acked reminders
    # whose fire annotation matches. Skip ## Missed entirely.
    for section_name in ("head", "fired"):
        for line in sections[section_name].splitlines():
            if not line.lstrip().startswith("- "):
                continue
            if ACKED_RE.search(line):
                continue
            ids = ID_RE.findall(line)
            if not ids:
                continue
            task_id = ids[0]

            # Each fire/escalate annotation may contain multiple
            # `chat:<ident> msg:<n>` pairs. We want any pair where
            # the chat matches `identifier`.
            #   "(fired at 2026-05-10 14:32 chat:tg:8600 msg:1234 chat:wa:+1… msg:5678)"
            # Simple regex extraction over each parenthetical:
            for paren in re.finditer(r"\(([^)]+)\)", line):
                blob = paren.group(1)
                if not (blob.startswith("fired at ") or blob.startswith("escalated ")):
                    continue
                # Pull out the timestamp (first "YYYY-MM-DD HH:MM" in blob).
                ts_m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", blob)
                if not ts_m:
                    continue
                try:
                    fired_at = datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M")
                    if tz is not None:
                        fired_at = fired_at.replace(tzinfo=tz)
                except ValueError:
                    continue
                if fired_at < horizon:
                    continue
                # Find a chat:<identifier> msg:<n> pair that matches.
                # Identifiers may be `tg:NNN`, `wa:+NNN`, `wa:group:JID`, etc.
                for chat_m in re.finditer(
                    r"chat:(\S+)\s+msg:(\d+)", blob,
                ):
                    chat_ident = chat_m.group(1)
                    if chat_ident != identifier:
                        continue
                    summary = _strip_to_user_message(line[2:])  # drop leading "- "
                    out.append({
                        "task_id": task_id,
                        "fired_at": ts_m.group(1),
                        "msg_id": chat_m.group(2),
                        "summary": summary[:120],
                    })
                    break  # one match per parenthetical is enough
    # Most recent first.
    out.sort(key=lambda r: r["fired_at"], reverse=True)
    return out


def _read_task_state(task_id: str) -> Optional[dict]:
    """Return state dict for the task or None if no line found. Searches
    head, ## Fired, and ## Missed sections.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return None
    sections = _split_sections(path.read_text(encoding="utf-8"))
    found = _line_for_task(task_id, sections)
    if found is None:
        return None
    section_name, line = found
    return {
        "section": section_name,
        "line": line,
        "acked": bool(ACKED_RE.search(line)),
        "escalated": "(escalated" in line,
        "missed": section_name == "missed" or "(missed" in line,
    }


def _append_annotation(task_id: str, annotation: str) -> bool:
    """In-place: append `annotation` to the task's line, regardless of
    which section it's in. Used for ack and escalation markers.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    sections = _split_sections(content)
    found = _line_for_task(task_id, sections)
    if found is None:
        return False
    section_name, line = found
    new_line = f"{line.rstrip()} {annotation}"
    sections[section_name] = _replace_line_in_section(sections[section_name], line, new_line)

    head_lines = sections["head"].splitlines()
    fired_lines = [l for l in sections["fired"].splitlines() if l.strip()]
    missed_lines = [l for l in sections["missed"].splitlines() if l.strip()]
    _rewrite_file(
        path, head_lines, fired_lines, missed_lines,
        sections["malformed"], sections["failed"],
    )
    return True


def _annotate_and_move(task_id: str, annotation: str, target_section: str) -> bool:
    """Append `annotation` to the line and move it to `target_section`
    (one of "fired" or "missed"). The line might currently be in head
    (initial fire) or fired (miss after escalate). Idempotent: if the line
    is already in target_section we just append the annotation in place.
    """
    if target_section not in ("fired", "missed"):
        raise ValueError(f"invalid target_section: {target_section}")

    path = memories_dir() / "reminders.md"
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    sections = _split_sections(content)
    found = _line_for_task(task_id, sections)
    if found is None:
        return False

    current_section, line = found
    new_line = f"{line.rstrip()} {annotation}"

    if current_section == target_section:
        # Just annotate in place.
        sections[current_section] = _replace_line_in_section(
            sections[current_section], line, new_line,
        )
    else:
        # Remove from current section, append to target section.
        sections[current_section] = _replace_line_in_section(
            sections[current_section], line, None,
        )
        if sections[target_section].strip():
            sections[target_section] = sections[target_section].rstrip() + "\n" + new_line
        else:
            sections[target_section] = new_line

    head_lines = sections["head"].splitlines()
    fired_lines = [l for l in sections["fired"].splitlines() if l.strip()]
    missed_lines = [l for l in sections["missed"].splitlines() if l.strip()]
    _rewrite_file(
        path, head_lines, fired_lines, missed_lines,
        sections["malformed"], sections["failed"],
    )
    return True


# ---------------------------------------------------------------------------
# Reconcile: the entry point called on startup and after each agent turn.
# ---------------------------------------------------------------------------

def reconcile() -> None:
    """Sync /memories/reminders.md → scheduler jobs.

    For each pending head line:
      - assign an `id:` if missing (rewriting the file)
      - resolve recipients via @-mentions → from: tag → all members cascade
      - register fire/escalate/miss jobs (idempotent, replace_existing)
      - if no recipients, move to ## Failed_Delivery

    Malformed lines (don't match LINE_RE) → ## Malformed.
    Stale jobs whose lines are gone or already acked → removed as orphans.
    """
    sched = start()
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return

    content = path.read_text(encoding="utf-8")
    sections = _split_sections(content)
    head = sections["head"]
    fired_block = sections["fired"]
    missed_block = sections["missed"]
    malformed_block = sections["malformed"]
    failed_block = sections["failed"]

    pending: list[dict] = []     # parsed valid pending lines
    new_malformed: list[str] = []
    kept_head_lines: list[str] = []
    file_mutated = False

    for raw_line in head.splitlines():
        stripped = raw_line.strip()
        if not stripped or not stripped.startswith("- "):
            kept_head_lines.append(raw_line)
            continue
        m = LINE_RE.match(raw_line)
        if not m:
            new_malformed.append(raw_line)
            continue
        ts_str = m.group(1).replace("T", " ")
        message = m.group(2)
        # Skip lines that are already acked (a natural-language ack
        # annotation may have been added by the agent).
        if ACKED_RE.search(raw_line):
            kept_head_lines.append(raw_line)
            continue
        # Ensure id present, mutating the line if needed.
        canonical_line, task_id, mutated = _ensure_id(raw_line, ts_str, message)
        if mutated:
            file_mutated = True
            # Re-extract message from the mutated line so it includes id:
            mm = LINE_RE.match(canonical_line)
            message = mm.group(2) if mm else message
        kept_head_lines.append(canonical_line)
        pending.append({
            "task_id": task_id,
            "ts_str": ts_str,
            "raw_message": message,
            "raw_line": canonical_line,
        })

    # Recipient cascade + per-line escalation cadences.
    # A single person can have multiple identifiers (e.g. tg + wa) — we
    # fan out @Name mentions to ALL of their identifiers so reminders
    # arrive on whichever channel the person is currently on.
    # canonical_name preserves the original casing for display in the
    # reminder body; lookup is lowercased.
    member_objs = roster.members()
    members_by_name_all: dict[str, list[str]] = {}
    canonical_name: dict[str, str] = {}
    for m in member_objs:
        members_by_name_all.setdefault(m.name.lower(), []).append(m.identifier)
        canonical_name.setdefault(m.name.lower(), m.name)
    # Deduped list of all identifiers across all members (for the
    # "no @-mention given → fan to whole household" fallback).
    all_idents: list[str] = []
    seen = set()
    for idents in members_by_name_all.values():
        for ident in idents:
            if ident not in seen:
                seen.add(ident)
                all_idents.append(ident)
    failed_to_deliver: list[tuple[str, str, str]] = []

    desired_jobs: dict[str, dict] = {}
    for p in pending:
        msg = p["raw_message"]
        # Build the assignee list as (canonical_name, tg_chat_id) tuples.
        # canonical_name comes from household.md when the @-mention resolves
        # (so casing is consistent), or the raw user-typed name otherwise.
        # tg_chat_id is the numeric Telegram id (extracted from "tg:NNN")
        # for known members, or None for un-rostered names. The chat_id is
        # what lets us emit `<a href="tg://user?id=NNN">Name</a>` mention
        # links so Telegram pings the user with a notification.
        raw_mentions = MENTION_RE.findall(msg)
        mentions = [n.lower() for n in raw_mentions]
        assignees: list[tuple[str, str | None]] = []
        for raw_n in raw_mentions:
            lower = raw_n.lower()
            # Skip unresolved mentions entirely. The agent occasionally
            # invents pseudo-addressees like `@g` (meaning "the group")
            # — including those in `assignees` leaks the literal "g" into
            # user-facing reminder text via `_format_assignee_html`. The
            # addressee-routing logic further down still handles the line
            # correctly (via from: fallback or all_idents), so dropping
            # unresolved names here only affects the display attribution.
            if lower not in canonical_name:
                continue
            display_name = canonical_name[lower]
            # Use the FIRST telegram identifier for the mention link, if
            # any. Multi-channel members (tg + wa) get linked via their
            # tg id — WhatsApp doesn't support inline mentions the same way.
            chat_id_str: str | None = None
            for ident in members_by_name_all.get(lower, []):
                if ident.startswith("tg:"):
                    chat_id_str = ident[len("tg:"):]
                    break
            assignees.append((display_name, chat_id_str))
        from_chats = FROM_RE.findall(msg)

        # Build per-addressee chains. Each entry is (display_name,
        # [their_idents]) — the unit that gets its own fire+escalate
        # ladder. Ack on the line cancels every chain (line-level
        # annotation; self-skip pattern in fire/escalate/fallback/miss).
        #
        #   @-mentions present  → one chain per resolved person
        #   no @-mentions, roster has members → one chain per member
        #   no @-mentions, no roster → one anonymous chain to from: chat
        addressee_chains: list[tuple[str, list[str]]] = []
        if mentions:
            unresolved: list[str] = []
            for raw_n in raw_mentions:
                lower = raw_n.lower()
                ids_for_name = members_by_name_all.get(lower, [])
                if ids_for_name:
                    addressee_chains.append(
                        (canonical_name.get(lower, raw_n), list(ids_for_name)),
                    )
                else:
                    unresolved.append(lower)
            if not addressee_chains:
                # All @-mentions are unknown — fall back to from: chat.
                if from_chats:
                    addressee_chains = [(from_chats[0], [from_chats[0]])]
                elif all_idents:
                    # Fan to whole household, one chain per member.
                    for name_lower, idents in members_by_name_all.items():
                        addressee_chains.append(
                            (canonical_name.get(name_lower, name_lower), list(idents)),
                        )
                log.warning(
                    "reconcile: unknown mentions %s, falling back to %s chains",
                    mentions, len(addressee_chains),
                )
            elif unresolved:
                log.info(
                    "reconcile: partial mention resolution — known=%s unknown=%s",
                    [n for n in mentions if n not in unresolved], unresolved,
                )
        else:
            # No @-mentions: fan to whole household. Each member gets
            # their own escalation chain. If household.md is empty, fall
            # back to a single anonymous chain on the from: chat.
            if all_idents:
                for name_lower, idents in members_by_name_all.items():
                    addressee_chains.append(
                        (canonical_name.get(name_lower, name_lower), list(idents)),
                    )
            elif from_chats:
                addressee_chains = [(from_chats[0], [from_chats[0]])]

        if not addressee_chains:
            reason = (
                "no @-mentions resolved and no recipients available "
                "(household.md is empty and no from: tag)"
            )
            log.warning(
                "reconcile: failed delivery ts=%s msg=%r — %s",
                p["ts_str"], _strip_to_user_message(msg), reason,
            )
            failed_to_deliver.append((p["task_id"], p["raw_line"], reason))
            continue

        # Pick origin chat: prefer from: tag, else first chain's first ident.
        origin_chat = from_chats[0] if from_chats else addressee_chains[0][1][0]

        # Urgency tier → (escalate, fallback, miss) preset. `urg:` on the
        # line picks; default is "normal". Explicit `esc:` / `miss:`
        # overrides the corresponding preset entry.
        urg_match = URG_RE.search(msg)
        urg_tier = urg_match.group(1) if urg_match else DEFAULT_URGENCY
        intervals = dict(URGENCY_INTERVALS[urg_tier])

        esc_match = ESC_RE.search(msg)
        miss_match = MISS_RE.search(msg)
        if esc_match:
            try:
                intervals["escalate"] = _parse_duration(*esc_match.groups())
            except ValueError:
                pass
        if miss_match:
            try:
                intervals["miss"] = _parse_duration(*miss_match.groups())
            except ValueError:
                pass

        desired_jobs[p["task_id"]] = {
            "ts_str": p["ts_str"],
            "raw_message": msg,
            "addressee_chains": addressee_chains,
            "origin_chat": origin_chat,
            "intervals": intervals,
            "urg_tier": urg_tier,
            "assignees": assignees,
        }

    # Remove failed-to-deliver lines from head so the rewrite drops them.
    truly_new_failed: list[tuple[str, str]] = []
    if failed_to_deliver:
        existing_failed = failed_block.strip().splitlines() if failed_block.strip() else []
        existing_failed_set = {
            l.strip() for l in existing_failed if l.strip().startswith("- ")
        }
        for tid, raw_line, reason in failed_to_deliver:
            if not raw_line or raw_line.strip() in existing_failed_set:
                continue
            try:
                kept_head_lines.remove(raw_line)
            except ValueError:
                pass
            truly_new_failed.append((raw_line, reason))

    # Sync APScheduler jobstore.
    #
    # Job-id shape (post-refactor):
    #   fire:<task_id>:<addressee_slug>      one per addressee
    #   escalate:<task_id>:<addressee_slug>  one per addressee (if tier has it)
    #   fallback:<task_id>                   one per reminder (if tier has it)
    #   miss:<task_id>                       one per reminder
    #
    # Old shape (`fire:<tid>`, `escalate:<tid>`, `miss:<tid>` without slug)
    # gets cleaned up by the orphan pass below — anything not in
    # desired_job_ids on the next reconcile pass is removed.
    tz = _local_tz()
    desired_job_ids: set[str] = set()
    added_reminders = 0
    added_jobs = 0
    for tid, info in desired_jobs.items():
        try:
            due = datetime.strptime(info["ts_str"], "%Y-%m-%d %H:%M")
            if tz is not None:
                due = due.replace(tzinfo=tz)
        except ValueError:
            log.warning("reconcile: bad timestamp %r — skipping", info["ts_str"])
            continue

        assignees = info.get("assignees") or []
        chains = info["addressee_chains"]
        intervals = info["intervals"]

        # Per-addressee chains: fire (always) + escalate (if tier provides one).
        for addr_name, addr_idents in chains:
            slug = _slug_for_addressee(addr_name)
            fire_id = f"fire:{tid}:{slug}"
            desired_job_ids.add(fire_id)
            sched.add_job(
                fire_one,
                trigger=DateTrigger(run_date=due),
                args=[tid, info["ts_str"], info["raw_message"],
                      addr_idents, info["origin_chat"], assignees],
                id=fire_id,
                replace_existing=True,
            )
            added_jobs += 1
            if intervals.get("escalate") is not None:
                esc_id = f"escalate:{tid}:{slug}"
                desired_job_ids.add(esc_id)
                sched.add_job(
                    escalate_one,
                    trigger=DateTrigger(run_date=due + intervals["escalate"]),
                    args=[tid, info["ts_str"], info["raw_message"],
                          info["origin_chat"], assignees],
                    kwargs={
                        "addressee_name": addr_name,
                        "addressee_idents": addr_idents,
                    },
                    id=esc_id,
                    replace_existing=True,
                )
                added_jobs += 1

        # Per-reminder fallback: pages a different person if no addressee
        # has acked. Resolution happens at fire time (in fallback_one) so
        # roster edits between scheduling and firing take effect.
        if intervals.get("fallback") is not None:
            fb_id = f"fallback:{tid}"
            desired_job_ids.add(fb_id)
            sched.add_job(
                fallback_one,
                trigger=DateTrigger(run_date=due + intervals["fallback"]),
                args=[tid, info["ts_str"], info["raw_message"],
                      info["origin_chat"], assignees],
                id=fb_id,
                replace_existing=True,
            )
            added_jobs += 1

        # Per-reminder miss — terminal log + drop the chase.
        miss_id = f"miss:{tid}"
        desired_job_ids.add(miss_id)
        sched.add_job(
            miss_one,
            trigger=DateTrigger(run_date=due + intervals["miss"]),
            args=[tid, info["ts_str"], info["raw_message"],
                  info["origin_chat"], assignees],
            id=miss_id,
            replace_existing=True,
        )
        added_jobs += 1
        added_reminders += 1

    # Orphans: jobs in store that aren't desired (line edited, removed,
    # or carries a stale job-id shape from before the per-addressee
    # refactor) AND legacy `reminder:` prefix jobs from the original
    # scheduler version.
    removed = 0
    for j in sched.get_jobs():
        is_legacy_prefix = j.id.startswith("reminder:")
        is_lifecycle_prefix = j.id.startswith(
            ("fire:", "escalate:", "fallback:", "miss:")
        )
        if is_legacy_prefix or (is_lifecycle_prefix and j.id not in desired_job_ids):
            try:
                sched.remove_job(j.id)
                removed += 1
            except Exception:
                log.exception("reconcile: failed to remove orphan %s", j.id)

    # Rewrite reminders.md if anything changed (id: insertion, quarantines).
    if file_mutated or new_malformed or truly_new_failed:
        # Filter new_malformed against existing malformed to stay idempotent.
        existing_malformed = malformed_block.strip().splitlines() if malformed_block.strip() else []
        existing_malformed_set = {
            l.strip() for l in existing_malformed if l.strip().startswith("- ")
        }
        truly_new_malformed = [
            l for l in new_malformed if l.strip() not in existing_malformed_set
        ]

        fired_lines = [l for l in fired_block.splitlines() if l.strip()]
        missed_lines = [l for l in missed_block.splitlines() if l.strip()]
        _rewrite_file(
            path,
            kept_head_lines,
            fired_lines,
            missed_lines,
            malformed_block,
            failed_block,
            new_malformed=truly_new_malformed,
            new_failed=truly_new_failed,
        )

    if added_reminders or removed:
        log.info(
            "reconcile: +%d reminders (=%d total jobs across per-addressee chains), -%d orphans",
            added_reminders, added_jobs, removed,
        )


# ---------------------------------------------------------------------------
# Status — read-only summary of what's pending, fired, missed. Used by the
# `/status` and "rosey status" admin command for at-a-glance verification.
# ---------------------------------------------------------------------------

def compute_status() -> str:
    """Build a short, plain-text summary suitable for a Telegram reply.
    Reads reminders.md only — no scheduler state, no API calls.
    """
    path = memories_dir() / "reminders.md"
    if not path.exists():
        return "No reminders file yet — nothing scheduled."

    sections = _split_sections(path.read_text(encoding="utf-8"))
    pending = [l for l in sections["head"].splitlines() if l.strip().startswith("- ")]
    fired = [l for l in sections["fired"].splitlines() if l.strip().startswith("- ")]
    missed = [l for l in sections["missed"].splitlines() if l.strip().startswith("- ")]
    failed = [l for l in sections["failed"].splitlines() if l.strip().startswith("- ")]
    malformed = [l for l in sections["malformed"].splitlines() if l.strip().startswith("- ")]

    # Find the next-due pending reminder (smallest timestamp).
    next_due_ts: str | None = None
    next_due_msg: str | None = None
    for line in pending:
        m = LINE_RE.match(line)
        if not m:
            continue
        ts = m.group(1).replace("T", " ")
        if next_due_ts is None or ts < next_due_ts:
            next_due_ts = ts
            next_due_msg = _strip_to_user_message(m.group(2))

    # Categorize the ## Fired section's lines by current state. Lines may
    # have multiple annotations; we look at terminal state.
    acked_count = 0
    pending_ack_count = 0  # fired but not yet acked, escalation may be in flight
    for line in fired:
        if ACKED_RE.search(line):
            acked_count += 1
        else:
            pending_ack_count += 1

    parts: list[str] = []
    if pending:
        if next_due_ts and next_due_msg:
            parts.append(
                f"📅 {len(pending)} pending. Next: \"{next_due_msg}\" at {next_due_ts}."
            )
        else:
            parts.append(f"📅 {len(pending)} pending.")
    else:
        parts.append("📅 0 pending.")

    if fired:
        chunks = [f"{len(fired)} fired"]
        if acked_count:
            chunks.append(f"{acked_count} acked")
        if pending_ack_count:
            chunks.append(f"{pending_ack_count} awaiting ack")
        parts.append("✅ " + ", ".join(chunks) + ".")

    if missed:
        parts.append(f"⚠️ {len(missed)} missed.")
    if failed:
        parts.append(f"❌ {len(failed)} undeliverable (in `## Failed_Delivery`).")
    if malformed:
        parts.append(f"❓ {len(malformed)} malformed (in `## Malformed`).")

    # Mention scheduler health in one line.
    try:
        n_jobs = len(start().get_jobs())
        parts.append(f"🕒 Scheduler: {n_jobs} jobs registered.")
    except Exception:
        parts.append("🕒 Scheduler: unavailable.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Compatibility shim — old jobstore entries from before the lifecycle rewrite
# referenced this function. They get cleaned up on first reconcile (see
# orphan removal above), but keep the symbol importable so APScheduler can
# load and immediately delete them without ImportError.
# ---------------------------------------------------------------------------

def fire_reminder(*args, **kwargs) -> None:  # pragma: no cover
    log.info("legacy fire_reminder shim invoked — should be cleaned up on next reconcile")
