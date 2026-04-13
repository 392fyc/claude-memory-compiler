"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id>
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import asyncio
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Windows console-window fix: the Agent SDK spawns claude.exe via
# anyio.open_process() without CREATE_NO_WINDOW, causing a visible
# terminal to pop up on every flush. Monkey-patch anyio.open_process
# to inject the flag before the SDK imports anyio.
if sys.platform == "win32":
    import anyio as _anyio
    _original_open_process = _anyio.open_process

    async def _open_process_no_window(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return await _original_open_process(*args, **kwargs)

    _anyio.open_process = _open_process_no_window

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


def _log_cli_stderr(line: str) -> None:
    """Callback: forward every bundled CLI stderr line into flush.log.

    Without this, SDK ProcessError only surfaces the hardcoded string
    "Check stderr output for details" (subprocess_cli.py line 616),
    and the actual subprocess stderr is discarded because
    ClaudeAgentOptions.stderr defaults to None (line 378).
    """
    logging.error("[bundled-cli stderr] %s", line)


async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

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

    response = ""

    # Strip Mercury-specific env vars that confuse the nested bundled claude.exe.
    # CLAUDE_CODE_USE_POWERSHELL_TOOL: makes the CLI shell out to PowerShell;
    # nested invocations intermittently exit code 1 because the PowerShell context
    # of the parent session is not available to the child. Root-cause confirmed via
    # env-diagnostic in flush.log (2026-04-12 17:08:56 failure, Mercury #232).
    # CLAUDE_PROJECT_DIR: inheriting the parent's project dir conflicts with cwd=ROOT.
    # CLAUDE_CODE_ENTRYPOINT is already overridden to 'sdk-py' by the SDK itself.
    # flush.py is a fire-once background process; mutating os.environ is safe here.
    _STRIP_FOR_CHILD = ("CLAUDE_CODE_USE_POWERSHELL_TOOL", "CLAUDE_PROJECT_DIR")
    for _var in _STRIP_FOR_CHILD:
        os.environ.pop(_var, None)

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT),
                allowed_tools=[],
                max_turns=2,
                stderr=_log_cli_stderr,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, ResultMessage):
                pass
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        # Diagnostic: dump Claude-related env vars on failure so PreCompact vs
        # SessionEnd divergence can be isolated. Values truncated to 200 chars.
        claude_env = {
            k: v
            for k, v in os.environ.items()
            if k.startswith(("CLAUDE_", "MCP_", "ANTHROPIC_"))
        }
        logging.error(
            "[env-diagnostic] %d Claude-related env vars present: %s",
            len(claude_env),
            sorted(claude_env.keys()),
        )
        for k, v in sorted(claude_env.items()):
            val = v[:200] if isinstance(v, str) else v
            logging.error("[env-diagnostic] %s=%r", k, val)
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    return response


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
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
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

    # Save project dir BEFORE run_flush() pops it from environ
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
    response = asyncio.run(run_flush(context))

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
