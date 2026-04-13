"""
SessionEnd hook - captures conversation transcript for memory extraction.

When a Claude Code session ends, this hook reads the transcript path from
stdin, extracts conversation context, and spawns flush.py as a background
process to extract knowledge into the daily log.

The hook itself does NO API calls - only local file I/O for speed (<10s).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow importing from scripts/ directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# Recursion guard: if we were spawned by flush.py (which calls Agent SDK,
# which runs Claude Code, which would fire this hook again), exit immediately.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_DIR = SCRIPTS_DIR

logging.basicConfig(
    filename=str(SCRIPTS_DIR / "flush.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [hook] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000
MIN_TURNS_TO_FLUSH = 1


def extract_conversation_context(transcript_path: Path) -> tuple[str, int]:
    """Read JSONL transcript and extract last ~N conversation turns as markdown."""
    turns: list[str] = []

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                role = entry.get("role", "")
                content = entry.get("content", "")

            if role not in ("user", "assistant"):
                continue

            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)

            if isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Assistant"
                turns.append(f"**{label}:** {content.strip()}\n")

    recent = turns[-MAX_TURNS:]
    context = "\n".join(recent)

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1 :]

    return context, len(recent)


def record_session_chain(session_id: str, cwd: str) -> None:
    """Record a session_chain entry in the skill-usage DB (Phase 4-1).

    Captures branch name and recently-referenced issue IDs from git log.
    Non-blocking: all failures are logged but do not propagate.
    """
    branch = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=cwd or None, timeout=5,
        )
        branch = result.stdout.strip()
    except Exception as e:
        logging.warning("Failed to get git branch: %s", e)

    issue_ids: list[str] = []
    try:
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-20"],
            capture_output=True, text=True, cwd=cwd or None, timeout=5,
        )
        issue_ids = list(dict.fromkeys(re.findall(r"#(\d+)", log_result.stdout)))[:5]
    except Exception as e:
        logging.warning("Failed to get git log issue IDs: %s", e)

    import json as _json
    import sqlite3

    db_path = ROOT / "stats" / "skill-usage.db"
    if not db_path.parent.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)

    # Reuse init_db from skill_stats to avoid schema drift (single source of truth).
    from skill_stats import init_db as _init_db
    conn = _init_db(db_path)
    try:
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO session_chain
               (session_id, issue_ids, branch, start_time, end_time, status)
               VALUES (?, ?, ?, ?, ?, 'complete')
               ON CONFLICT(session_id) DO UPDATE SET
                   issue_ids=excluded.issue_ids,
                   branch=excluded.branch,
                   end_time=excluded.end_time,
                   status=excluded.status""",
            (session_id, _json.dumps(issue_ids), branch, now_iso, now_iso),
        )
        conn.commit()
    finally:
        conn.close()

    logging.info(
        "session_chain recorded: session=%s branch=%s issues=%s",
        session_id, branch, issue_ids,
    )


def main() -> None:
    # Read hook input from stdin
    # Claude Code on Windows may pass paths with unescaped backslashes
    try:
        raw_input = sys.stdin.read()
        try:
            hook_input: dict = json.loads(raw_input)
        except json.JSONDecodeError:
            fixed_input = re.sub(r'(?<!\\)\\(?!["\\])', r'\\\\', raw_input)
            hook_input = json.loads(fixed_input)
    except (json.JSONDecodeError, ValueError, EOFError) as e:
        logging.error("Failed to parse stdin: %s", e)
        return

    session_id = hook_input.get("session_id", "unknown")
    source = hook_input.get("source", "unknown")
    transcript_path_str = hook_input.get("transcript_path", "")

    logging.info("SessionEnd fired: session=%s source=%s", session_id, source)

    if not transcript_path_str or not isinstance(transcript_path_str, str):
        logging.info("SKIP: no transcript path")
        return

    transcript_path = Path(transcript_path_str)
    if not transcript_path.exists():
        logging.info("SKIP: transcript missing: %s", transcript_path_str)
        return

    # Extract conversation context in the hook (fast, no API calls)
    try:
        context, turn_count = extract_conversation_context(transcript_path)
    except Exception as e:
        logging.error("Context extraction failed: %s", e)
        return

    if not context.strip():
        logging.info("SKIP: empty context")
        return

    if turn_count < MIN_TURNS_TO_FLUSH:
        logging.info("SKIP: only %d turns (min %d)", turn_count, MIN_TURNS_TO_FLUSH)
        return

    # Extract skill usage stats from transcript (fast, no API calls)
    try:
        from skill_stats import process_transcript
        skill_count = process_transcript(transcript_path, session_id, hook_input.get("cwd"))
        if skill_count > 0:
            logging.info("Recorded %d skill invocation(s) for session %s", skill_count, session_id)
    except Exception as e:
        logging.warning("Skill stats extraction failed (non-fatal): %s", e)

    # Phase 4-1: record session chain entry (non-blocking)
    try:
        record_session_chain(session_id, hook_input.get("cwd", ""))
    except Exception as e:
        logging.warning("session_chain recording failed (non-fatal): %s", e)

    # Write context to a temp file for the background process
    timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    context_file = STATE_DIR / f"session-flush-{session_id}-{timestamp}.md"
    context_file.write_text(context, encoding="utf-8")

    # Spawn flush.py as a background process
    flush_script = SCRIPTS_DIR / "flush.py"

    cmd = [
        "uv",
        "run",
        "--directory",
        str(ROOT),
        "python",
        str(flush_script),
        str(context_file),
        session_id,
    ]

    # On Windows, use CREATE_NO_WINDOW to avoid flash console window.
    # Do NOT use DETACHED_PROCESS — it breaks the Agent SDK's subprocess I/O.
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        logging.info("Spawned flush.py for session %s (%d turns, %d chars)", session_id, turn_count, len(context))
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)


if __name__ == "__main__":
    main()
