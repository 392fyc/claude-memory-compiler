"""
Extract Skill tool invocations from a Claude Code transcript JSONL and record to SQLite.

Usage:
    python skill_stats.py <transcript_path> <session_id> [project_dir]

The transcript is a JSONL file where each line is a JSON object. Tool invocations
appear as content blocks with type "tool_use" and name "Skill". The skill name is
in input.skill, optional args in input.args.

Database: $AGENTKB_DIR/stats/skill-usage.db
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATS_DIR = ROOT / "stats"
DB_PATH = STATS_DIR / "skill-usage.db"


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create the database and table if they don't exist."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS skill_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill TEXT NOT NULL,
            args TEXT,
            session_id TEXT,
            timestamp TEXT NOT NULL,
            project TEXT,
            UNIQUE (session_id, skill, timestamp)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_skill_usage_skill ON skill_usage(skill)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_skill_usage_timestamp ON skill_usage(timestamp)
    """)
    conn.commit()
    return conn


def extract_skill_invocations(transcript_path: Path) -> list[dict]:
    """Parse transcript JSONL and extract all Skill tool_use blocks."""
    invocations = []

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
            if not isinstance(msg, dict):
                continue

            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            timestamp = entry.get("timestamp", "")

            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") != "Skill":
                    continue

                tool_input = block.get("input", {})
                if not isinstance(tool_input, dict):
                    continue

                skill_name = tool_input.get("skill", "")
                skill_args = tool_input.get("args", "")

                if skill_name:
                    invocations.append({
                        "skill": skill_name,
                        "args": skill_args or None,
                        "timestamp": timestamp,
                    })

    return invocations


def record_invocations(
    conn: sqlite3.Connection,
    invocations: list[dict],
    session_id: str,
    project: str | None = None,
) -> int:
    """Write skill invocations to the database. Returns count of records inserted."""
    if not invocations:
        return 0

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    rows = []
    for inv in invocations:
        rows.append((
            inv["skill"],
            inv.get("args"),
            session_id,
            inv.get("timestamp") or now,
            project,
        ))

    conn.executemany(
        "INSERT OR IGNORE INTO skill_usage (skill, args, session_id, timestamp, project) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def process_transcript(transcript_path: Path, session_id: str, project: str | None = None) -> int:
    """Main entry point: extract skills from transcript and record to DB."""
    invocations = extract_skill_invocations(transcript_path)
    if not invocations:
        return 0

    conn = init_db(DB_PATH)
    try:
        return record_invocations(conn, invocations, session_id, project)
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <transcript_path> <session_id> [project_dir]")
        sys.exit(1)

    transcript_path = Path(sys.argv[1])
    session_id = sys.argv[2]
    project = sys.argv[3] if len(sys.argv) > 3 else None

    if not transcript_path.exists():
        print(f"Error: transcript not found: {transcript_path}")
        sys.exit(1)

    count = process_transcript(transcript_path, session_id, project)
    print(f"Recorded {count} skill invocation(s) to {DB_PATH}")


if __name__ == "__main__":
    main()
