"""Agent tool registry.

Centralizes the list of tools the agent gets per turn so forks can
extend or replace it without editing `agent.handle_message` directly.

Two ways to extend:

  1. Edit `default_tools()` here.
  2. Pass a custom `tools` argument to `agent.handle_message(...)`
     (not yet wired — would require a small signature change in
     `agent.py`; planned for the next refactor pass).

The list is a mix of:
  - The local `memory` tool (a FileMemoryTool instance, rendered to dict).
  - Anthropic-hosted server-side tools, declared as plain dicts. The API
    handles their execution; we don't need to dispatch them locally.

For Anthropic's full tool catalog see:
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview
"""
from __future__ import annotations

from typing import List


def default_tools(memory) -> List[dict]:
    """Return the tools available on every agent turn.

    `memory` is a `FileMemoryTool` (or compatible) instance. Its `.to_dict()`
    is what the API expects.

    `max_uses` on the server-side tools caps how many times Claude can call
    each within a single API turn. Without these caps, "find a plumber" can
    snowball into 4–5 searches and several fetches, each adding 5–10s of
    latency and a few KB of result tokens to the conversation. Two of each
    is more than enough for the typical household question.
    """
    return [
        memory.to_dict(),
        _create_reminders_tool(),
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 2},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 2},
    ]


def _create_reminders_tool() -> dict:
    """Deterministic reminder creation. Keeps the LLM out of date-math and
    reminders.md formatting (which previously produced fragile files — stray
    headers, split slots, past timestamps). The model supplies WHAT and the
    time(s); reminder_builder writes the correct lines."""
    return {
        "name": "create_reminders",
        "description": (
            "Create one or more reminders. Use this for EVERY new reminder "
            "instead of writing reminders.md by hand — it does the date math and "
            "formatting correctly and tells you the exact times it scheduled. "
            "For 'N times a day', pass all N clock times in `times`; each fires at "
            "its next future occurrence (today if still ahead, otherwise tomorrow), "
            "so you can never create a reminder in the past. Confirm the returned "
            "times to the user verbatim — do not guess or restate them differently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "What to remind about, e.g. 'baby exercise' or 'give Siya vitamin D drops'.",
                },
                "times": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Clock time(s) of day, e.g. '9am', '3pm', '15:00', 'noon'. "
                        "For 'N times a day between X and Y', list all N times."
                    ),
                },
                "repeat": {
                    "type": "string",
                    "description": (
                        "Recurrence applied to every time: 'daily', 'weekly', "
                        "'hourly', or a span like '2h' / '30m' / '3d'. Omit for a "
                        "one-time reminder."
                    ),
                },
                "recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Household member name(s) responsible, e.g. ['Sunanda']. "
                        "They are tagged in the message. Omit to address the whole "
                        "household."
                    ),
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": "Escalation tier; default 'normal'. Use 'high' for time-critical things.",
                },
            },
            "required": ["text", "times"],
        },
    }
