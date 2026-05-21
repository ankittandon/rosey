"""Rosey MCP adapter.

Exposes the household's shared state (memory / grocery list / reminders) as MCP
tools, so the OpenAI Realtime voice agent — and any other MCP client — can read
and write the same data the WhatsApp/Telegram Rosey uses. This is what turns the
voice PWA from "a smart assistant" into "*Rosey*, who knows your household."

Run locally:
    pip install -r requirements.txt
    MEMORY_ROOT=./memories python server.py
    # MCP Streamable HTTP endpoint is served at  http://localhost:8089/mcp

Point the realtime session's mcp tool at  https://<this-app>/mcp  (see the PWA's
CONFIG.MCP_SERVER_URL).

------------------------------------------------------------------------------
TWO THINGS TO KNOW
------------------------------------------------------------------------------
1. SHARED MEMORY: this reads/writes plain files under MEMORY_ROOT using Rosey's
   existing layout (household.md, groceries/list.md, reminders.md). To actually
   *share* state with your live WhatsApp Rosey, MEMORY_ROOT must point at the
   SAME volume that household's engine uses (i.e. co-locate this with that VM's
   /data/memories). Running it standalone with its own volume gives you an
   isolated household — perfect for first tests, but not shared until co-located.

2. AUTH: this server is currently UNAUTHENTICATED — anyone with the URL can read
   and write. That's acceptable for a personal demo pointed at a scratch memory
   dir with an unguessable app name. It is NOT acceptable for real household
   data. Before pointing MEMORY_ROOT at a live household, add auth — a bearer
   check (e.g. a ROSEY_MCP_TOKEN gate via ASGI middleware) or a server-side
   proxy in front. Note that the realtime MCP tool config (server_url +
   authorization) is set in the browser by the PWA, so a client-passed token is
   visible to users and isn't strong auth on its own.

NOTE: the MCP Python SDK has moved quickly. If `mcp.run(transport=...)` or the
streamable-http endpoint path differ in your installed `mcp` version, adjust to
match — the tool definitions below are the part that matters.
"""
import os
from pathlib import Path
from datetime import datetime

from mcp.server.fastmcp import FastMCP

MEMORY_ROOT = Path(os.environ.get("MEMORY_ROOT", "./memories"))
MEMORY_ROOT.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", "8089"))
mcp = FastMCP("rosey", host="0.0.0.0", port=PORT)


# --- tiny file helpers (Rosey's memory layout) -------------------------------
def _read(rel: str) -> str:
    p = MEMORY_ROOT / rel
    return p.read_text(encoding="utf-8").strip() if p.exists() else ""


def _append(rel: str, line: str) -> None:
    p = MEMORY_ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


# --- tools -------------------------------------------------------------------
@mcp.tool()
def get_household() -> str:
    """Return the household roster and durable facts (who lives here, preferences,
    important context). Call this first when you need to know who someone is."""
    return _read("household.md") or "No household profile saved yet."


@mcp.tool()
def remember(note: str) -> str:
    """Save a durable fact or note to the household's shared memory so it can be
    recalled later (e.g. 'the pediatrician is Dr. Sharma', 'trash goes out Tuesday')."""
    _append("notes.md", f"- {datetime.now().date().isoformat()}: {note}")
    return "Saved to household memory."


@mcp.tool()
def list_grocery_items() -> str:
    """Return the current shared grocery list."""
    return _read("groceries/list.md") or "The grocery list is empty."


@mcp.tool()
def add_grocery_item(item: str) -> str:
    """Add an item to the shared grocery list."""
    _append("groceries/list.md", f"- {item}")
    return f"Added '{item}' to the grocery list."


@mcp.tool()
def list_reminders() -> str:
    """Return the household's upcoming reminders."""
    return _read("reminders.md") or "No reminders set."


@mcp.tool()
def add_reminder(text: str, when: str = "") -> str:
    """Add a reminder for the household. `when` is a natural-language time if the
    user gave one (e.g. 'tomorrow 9am', 'Friday')."""
    suffix = f" (when: {when})" if when else ""
    _append("reminders.md", f"- [ ] {text}{suffix}")
    return f"Reminder added: {text}{suffix}"


if __name__ == "__main__":
    # Serves MCP over Streamable HTTP at /mcp on 0.0.0.0:PORT.
    mcp.run(transport="streamable-http")
