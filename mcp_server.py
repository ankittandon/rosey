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
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from paths import memories_dir

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


@mcp.tool()
def list_reminders() -> str:
    """Return the household's reminders."""
    return _read("reminders.md") or "No reminders set."


@mcp.tool()
def add_reminder(text: str, when: str = "") -> str:
    """Add a reminder. `when` is a natural-language time if the user gave one."""
    suffix = f" (when: {when})" if when else ""
    _append("reminders.md", f"- [ ] {text}{suffix}")
    return f"Reminder added: {text}{suffix}"


@mcp.tool()
def remember(note: str) -> str:
    """Save a durable note to the household's shared memory."""
    _append("notes.md", f"- {datetime.now().date().isoformat()}: {note}")
    return "Saved to household memory."


if __name__ == "__main__":
    # Serves MCP over Streamable HTTP at /mcp on 0.0.0.0:MCP_PORT.
    mcp.run(transport="streamable-http")
