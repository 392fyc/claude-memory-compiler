"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude CLI in
print mode to decide what's worth saving, and appends the result to today's
daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id>
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "daily"
SCRIPTS_DIR = ROOT / "scripts"
STATE_FILE = SCRIPTS_DIR / "last-flush.json"
LOG_FILE = SCRIPTS_DIR / "flush.log"

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our only observability channel.
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def load_flush_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def append_to_daily_log(content: str, section: str = "Session") -> None:
    """Append content to today's daily log."""
    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")
    entry = f"### {section} ({time_str})\n\n{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)


def _find_claude_exe() -> Path | None:
    """Locate the claude CLI executable."""
    # 1. CLAUDE_CODE_EXECPATH (set by parent Claude Code session)
    execpath = os.environ.get("CLAUDE_CODE_EXECPATH")
    if execpath:
        p = Path(execpath)
        if p.is_file():
            return p

    # 2. Well-known install locations
    home = Path.home()
    for name in ("claude.exe", "claude"):
        candidate = home / ".local" / "bin" / name
        if candidate.is_file():
            return candidate

    # 3. PATH lookup
    found = shutil.which("claude")
    if found:
        return Path(found)

    return None


def run_flush(context: str) -> str:
    """Call claude CLI in print mode to extract knowledge from conversation context.

    Bypasses the Agent SDK entirely — the SDK's bundled CLI intermittently
    exits with code 1 during PreCompact hooks, and the SDK's error handling
    swallows the real stderr (hardcoded "Check stderr output for details").
    Direct subprocess call gives us real stderr capture and eliminates the
    SDK as a failure point.  See Mercury #232.
    """

    prompt = f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""

    claude_exe = _find_claude_exe()
    if not claude_exe:
        logging.error("claude executable not found")
        return "FLUSH_ERROR: claude executable not found"

    logging.info("Using claude CLI: %s", claude_exe)

    # Build clean environment: strip CLAUDE_*/MCP_* vars that cause the
    # nested CLI to misbehave inside a live Claude Code session.
    # Keep CLAUDE_INVOKED_BY so the child's hooks respect the recursion guard.
    env = {}
    for k, v in os.environ.items():
        if k == "CLAUDE_INVOKED_BY":
            env[k] = v
        elif k.startswith(("CLAUDE_", "MCP_")):
            continue
        else:
            env[k] = v

    cmd = [str(claude_exe), "-p"]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
            cwd=str(ROOT),
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        logging.error("claude -p timed out after 120s")
        return "FLUSH_ERROR: claude -p timed out after 120s"
    except Exception as e:
        logging.error("Failed to run claude -p: %s", e)
        return f"FLUSH_ERROR: {type(e).__name__}: {e}"

    if result.returncode != 0:
        stderr_text = (result.stderr or "").strip()
        logging.error("claude -p failed (rc=%d)", result.returncode)
        if stderr_text:
            for line in stderr_text.splitlines()[:20]:
                logging.error("[claude stderr] %s", line)
        return f"FLUSH_ERROR: claude -p exit code {result.returncode}: {stderr_text[:500]}"

    output = result.stdout.strip()
    if not output:
        stderr_text = (result.stderr or "").strip()
        logging.error("claude -p returned empty stdout (rc=0)")
        if stderr_text:
            logging.error("[claude stderr] %s", stderr_text[:500])
        return "FLUSH_ERROR: claude -p returned empty output"

    return output


COMPILE_AFTER_HOUR = 18  # 6 PM local time


def maybe_trigger_compilation() -> None:
    """If it's past the compile hour and today's log hasn't been compiled, run compile.py."""
    import subprocess as _sp

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return

    # Check if today's log has already been compiled
    today_log = f"{now.strftime('%Y-%m-%d')}.md"
    compile_state_file = SCRIPTS_DIR / "state.json"
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
            if today_log in ingested:
                # Already compiled today - check if the log has changed since
                from hashlib import sha256
                log_path = DAILY_DIR / today_log
                if log_path.exists():
                    current_hash = sha256(log_path.read_bytes()).hexdigest()[:16]
                    if ingested[today_log].get("hash") == current_hash:
                        return  # log unchanged since last compile
        except (json.JSONDecodeError, OSError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        # CREATE_NO_WINDOW suppresses the console window without breaking
        # subprocess I/O. DETACHED_PROCESS caused intermittent terminal flashes
        # on Windows 11 — root cause of the ~2h observation window flashes
        # reported by Mercury S54.
        kwargs["creationflags"] = _sp.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


def encode_project_path(path: str) -> str:
    """Encode a project directory path for use in Claude auto-memory paths.

    Mirrors Claude Code's internal encoding:
      D:\\Mercury\\Mercury → D--Mercury-Mercury
    """
    return path.replace(":", "-").replace("\\", "-").replace("/", "-").lstrip("-")


def write_auto_memory_checkpoint(
    project_dir: str, session_id: str, summary: str
) -> None:
    """Write a session checkpoint to the project's auto-memory directory.

    Called after PreCompact flush so the next session (or post-compaction context)
    has a structured snapshot of in-progress work.
    """
    encoded = encode_project_path(project_dir)
    memory_dir = Path.home() / ".claude" / "projects" / encoded / "memory"
    if not memory_dir.exists():
        return  # auto-memory dir doesn't exist for this project; skip silently
    checkpoint = memory_dir / "session-checkpoint.md"
    now = datetime.now(timezone.utc).astimezone()
    content = (
        f"# Session Checkpoint — {now.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"**Session**: {session_id}\n"
        f"**Project**: {project_dir}\n"
        f"**Trigger**: PreCompact (auto-saved before context compaction)\n\n"
        f"{summary}\n"
    )
    checkpoint.write_text(content, encoding="utf-8")
    logging.info("Wrote session-checkpoint.md to auto-memory: %s", checkpoint)


def main():
    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id>", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]

    # Save project dir before we enter run_flush() which strips CLAUDE_* env vars
    saved_project_dir = os.environ.get("CLAUDE_PROJECT_DIR")

    # Detect trigger source from context file naming convention:
    #   flush-context-*  → PreCompact hook
    #   session-flush-*  → SessionEnd hook
    is_precompact = context_file.name.startswith("flush-context-")

    logging.info(
        "flush.py started for session %s, context: %s (trigger: %s)",
        session_id,
        context_file,
        "PreCompact" if is_precompact else "SessionEnd",
    )

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    # Deduplication: skip if same session was flushed within 60 seconds
    state = load_flush_state()
    if (
        state.get("session_id") == session_id
        and time.time() - state.get("timestamp", 0) < 60
    ):
        logging.info("Skipping duplicate flush for session %s", session_id)
        context_file.unlink(missing_ok=True)
        return

    # Read pre-extracted context
    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    logging.info("Flushing session %s: %d chars", session_id, len(context))

    # Run the LLM extraction
    response = run_flush(context)

    # Append to daily log
    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush"
        )
    elif "FLUSH_ERROR" in response:
        logging.error("Result: %s", response)
        append_to_daily_log(response, "Memory Flush")
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, "Session")

    # Update dedup state
    save_flush_state({"session_id": session_id, "timestamp": time.time()})

    # Clean up context file
    context_file.unlink(missing_ok=True)

    # Phase 4-1: PreCompact checkpoint — write summary to auto-memory so
    # the next session (or post-compaction context) has a structured snapshot.
    if is_precompact and saved_project_dir and "FLUSH_ERROR" not in response and response.strip() not in ("", "FLUSH_OK"):
        try:
            write_auto_memory_checkpoint(saved_project_dir, session_id, response)
        except Exception as e:
            logging.warning("Failed to write auto-memory checkpoint: %s", e)

    # End-of-day auto-compilation: if it's past the compile hour and today's
    # log hasn't been compiled yet, trigger compile.py in the background.
    maybe_trigger_compilation()

    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
