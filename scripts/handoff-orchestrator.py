"""
Handoff orchestrator — starts a continuation Claude Code session via Agent SDK.

Reads a handoff document and launches a new session with the handoff content
injected as the opening prompt. Optionally updates the session_chain table
to link the previous session to the new one.

Usage:
    uv run python handoff-orchestrator.py --handoff-doc <path> [--prev-session <id>] [--cwd <dir>]

Part of Mercury Phase 4-1 Session Continuity.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "scripts" / "flush.log"
DB_PATH = ROOT / "stats" / "skill-usage.db"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [handoff] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Also log to stderr for interactive use
console = logging.StreamHandler(sys.stderr)
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
logging.getLogger().addHandler(console)


def update_session_chain(prev_session_id: str, next_session_id: str) -> None:
    """Link previous session to the new one in session_chain."""
    if not DB_PATH.exists():
        return
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute(
                "UPDATE session_chain SET next_session_id=? WHERE session_id=?",
                (next_session_id, prev_session_id),
            )
            conn.commit()
        logging.info(
            "session_chain linked: %s -> %s", prev_session_id, next_session_id
        )
    except Exception as e:
        logging.warning("Failed to update session_chain: %s", e)


async def start_continuation_session(
    handoff_doc: Path,
    cwd: str,
    prev_session_id: str | None = None,
) -> None:
    """Launch a new Claude Code session with the handoff content as prompt."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        SystemMessage,
        TextBlock,
        query,
    )

    handoff_content = handoff_doc.read_text(encoding="utf-8")

    prompt = (
        "Continue from previous session handoff:\n\n"
        f"{handoff_content}\n\n"
        "Acknowledge the handoff and begin with the first pending task."
    )

    logging.info("Starting continuation session (cwd=%s)", cwd)

    new_session_id: str | None = None

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=cwd,
                permission_mode="bypassPermissions",
                max_turns=None,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
            elif isinstance(message, ResultMessage):
                # ResultMessage signals session end
                logging.info(
                    "Continuation session ended: stop_reason=%s",
                    getattr(message, "stop_reason", "unknown"),
                )
    except Exception as e:
        logging.error("Continuation session error: %s", e)
        raise

    # Try to find the new session ID from recent sessions
    if prev_session_id:
        try:
            from claude_agent_sdk import list_sessions

            sessions = list_sessions(directory=cwd, limit=3)
            if sessions:
                # The most recent session that isn't the previous one
                for s in sessions:
                    sid = getattr(s, "session_id", None) or getattr(s, "tag", None)
                    if sid and sid != prev_session_id:
                        new_session_id = sid
                        break

            if new_session_id:
                update_session_chain(prev_session_id, new_session_id)
            else:
                logging.info(
                    "Could not determine new session_id; chain not linked"
                )
        except Exception as e:
            logging.warning("Failed to link session chain: %s", e)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start a continuation Claude Code session from a handoff document"
    )
    parser.add_argument(
        "--handoff-doc",
        required=True,
        help="Path to the handoff markdown document",
    )
    parser.add_argument(
        "--prev-session",
        default=None,
        help="Previous session ID (for chain linking)",
    )
    parser.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="Working directory for the new session",
    )
    args = parser.parse_args()

    handoff_doc = Path(args.handoff_doc)
    if not handoff_doc.exists():
        logging.error("Handoff document not found: %s", handoff_doc)
        sys.exit(1)

    logging.info(
        "Handoff orchestrator started: doc=%s prev=%s cwd=%s",
        handoff_doc,
        args.prev_session,
        args.cwd,
    )

    asyncio.run(
        start_continuation_session(
            handoff_doc=handoff_doc,
            cwd=args.cwd,
            prev_session_id=args.prev_session,
        )
    )


if __name__ == "__main__":
    main()
