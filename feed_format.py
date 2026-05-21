"""Single source of truth for the baby feed log (knowledge/baby_feed_log.md).

The log is a GitHub-flavored Markdown table under a "## Individual Feeds"
heading:

    | Date | Time | Type | Amount | Duration | Pee | Poop | Notes |

The chat agent writes rows via its memory tool (guided by FORMAT_DOC, which is
inlined into agent.py's system prompt) and the voice MCP server writes rows via
render_row + append_feed/amend_last_feed here — BOTH pull from this module so
the two channels emit byte-compatible rows and neither sees the other's entries
as "malformed lines at the bottom".
"""
from __future__ import annotations

import re

HEADING = "## Individual Feeds"
COLUMNS = ["Date", "Time", "Type", "Amount", "Duration", "Pee", "Poop", "Notes"]
# Content widths matching the live file, used when creating a table from scratch.
DEFAULT_WIDTHS = [10, 8, 9, 8, 9, 12, 4, 5]

# Inlined verbatim into agent.py's system prompt so the chat agent writes the
# exact same row shape the voice tool does.
FORMAT_DOC = (
    "Feeds live in knowledge/baby_feed_log.md as a Markdown table under "
    "'## Individual Feeds' with columns "
    "| Date | Time | Type | Amount | Duration | Pee | Poop | Notes |. "
    "Date is YYYY-MM-DD; Time is 'h:mm am/pm' lowercase; Type is one of "
    "BF, BF-L, BF-R, BF-L+R, Bottle (or '—' for a diaper-only entry); Amount is "
    "like '~1 oz' / '1.5 oz' / '15 mL'; Duration is like '20 mins' (blank for "
    "bottles); Pee/Poop are '✓' (optionally with a volume) or blank. Add new "
    "feeds as new rows in chronological order — never write feed entries outside "
    "this table."
)

_AFFIRMATIVE = {"yes", "y", "yep", "yeah", "true", "t", "1", "✓", "✔",
                "pee", "poop", "both", "did"}


def fmt_time(dt) -> str:
    """'5:20 pm' — 12-hour, lowercase meridiem, matching the log."""
    hour = dt.hour % 12 or 12
    return f"{hour}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def diaper_cell(value: str) -> str:
    """Normalize a pee/poop value to the log's convention: '✓', a passed-through
    detail string (e.g. '✓ ~2 oz'), or '' when absent/negative."""
    v = (value or "").strip()
    if not v:
        return ""
    low = v.lower()
    if low in {"no", "n", "none", "false", "f", "0", "✗", "x"}:
        return ""
    if low in _AFFIRMATIVE:
        return "✓"
    return v  # already detailed, e.g. "✓ ~2 oz" — pass through


# ---------------------------------------------------------------------------
# Row parsing / rendering
# ---------------------------------------------------------------------------

def _split_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_separator(line: str) -> bool:
    s = line.strip()
    return bool(s) and bool(re.fullmatch(r"\|[\s:|-]+\|", s)) and "-" in s


def is_data_row(line: str) -> bool:
    """True for a table data row — starts with '|', not the header or separator."""
    s = line.strip()
    if not s.startswith("|") or _is_separator(s):
        return False
    return _split_cells(s)[:1] != ["Date"]  # exclude the header row


def parse_row(line: str) -> dict | None:
    """Parse a data row into {column: value}, or None if it isn't one."""
    if not is_data_row(line):
        return None
    cells = _split_cells(line)
    cells += [""] * (len(COLUMNS) - len(cells))
    return {col: cells[i] for i, col in enumerate(COLUMNS)}


def _widths_from_separator(separator_line: str) -> list[int]:
    return [max(len(seg), 1) for seg in _split_cells(separator_line)]


def render_row(fields: dict, widths: list[int] | None = None) -> str:
    """Render {column: value} into a '| ... |' line. Pads to `widths` when given
    (cosmetic — Markdown ignores padding, but it keeps the file tidy)."""
    vals = [str(fields.get(col, "") or "") for col in COLUMNS]
    if widths and len(widths) >= len(vals):
        cells = [f" {v.ljust(widths[i])} " for i, v in enumerate(vals)]
    else:
        cells = [f" {v} " for v in vals]
    return "|" + "|".join(cells) + "|"


# ---------------------------------------------------------------------------
# Whole-file helpers (pure: take + return the file content string)
# ---------------------------------------------------------------------------

def _table_span(lines: list[str]):
    """Locate the Individual Feeds table. Returns (header_idx, sep_idx,
    last_data_idx), where last_data_idx == sep_idx when the table has no rows
    yet, or None if there's no table."""
    start = 0
    for i, l in enumerate(lines):
        if l.strip() == HEADING:
            start = i + 1
            break
    header_idx = None
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("| Date"):
            header_idx = i
            break
    if header_idx is None or header_idx + 1 >= len(lines):
        return None
    if not _is_separator(lines[header_idx + 1]):
        return None
    sep_idx = header_idx + 1
    last_data = sep_idx
    for i in range(sep_idx + 1, len(lines)):
        if is_data_row(lines[i]):
            last_data = i
        else:
            break
    return header_idx, sep_idx, last_data


def _fresh_table(row_line: str) -> list[str]:
    header = render_row({c: c for c in COLUMNS}, DEFAULT_WIDTHS)
    sep = "|" + "|".join("-" * (w + 2) for w in DEFAULT_WIDTHS) + "|"
    return [HEADING, header, sep, row_line]


def append_feed(content: str, fields: dict) -> str:
    """Insert a feed row in chronological position (after the last existing row)
    of the Individual Feeds table. Creates the table if none exists."""
    trailing_nl = content.endswith("\n") or not content
    lines = content.splitlines()
    span = _table_span(lines)
    if span is None:
        block = _fresh_table(render_row(fields, DEFAULT_WIDTHS))
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    else:
        _, sep_idx, last_data = span
        widths = _widths_from_separator(lines[sep_idx])
        lines.insert(last_data + 1, render_row(fields, widths))
    out = "\n".join(lines)
    return out + "\n" if trailing_nl else out


def last_feed(content: str):
    """Return (line_index, parsed_row_dict) for the most recent feed, or None."""
    lines = content.splitlines()
    span = _table_span(lines)
    if span is None:
        return None
    _, sep_idx, last_data = span
    if last_data == sep_idx:
        return None
    return last_data, parse_row(lines[last_data])


def amend_last_feed(content: str, updates: dict):
    """Merge non-empty `updates` into the most recent feed row, rewriting it in
    place. Returns (new_content, merged_row) or None if there's no row."""
    trailing_nl = content.endswith("\n") or not content
    lines = content.splitlines()
    span = _table_span(lines)
    if span is None:
        return None
    _, sep_idx, last_data = span
    if last_data == sep_idx:
        return None
    row = parse_row(lines[last_data]) or {c: "" for c in COLUMNS}
    for k, v in updates.items():
        if v:
            row[k] = v
    lines[last_data] = render_row(row, _widths_from_separator(lines[sep_idx]))
    out = "\n".join(lines)
    return (out + "\n" if trailing_nl else out), row
