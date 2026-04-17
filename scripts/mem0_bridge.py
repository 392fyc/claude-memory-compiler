"""Thin bridge between AgentKB hooks and the mem0 adapter.

Every entry point fails safe: if mem0 is not installed, or OPENAI_API_KEY is
missing, or the store errors mid-call, the bridge logs and returns a falsy
value. It NEVER raises — the daily-log write is the source of truth and must
not be blocked by memory-layer problems.

Use ``MERCURY_MEM0_DISABLED=1`` to force no-op mode (for CI / test isolation
/ rollback without uninstalling mem0ai).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


log = logging.getLogger("mem0_bridge")


def _disabled() -> bool:
    return os.environ.get("MERCURY_MEM0_DISABLED", "").strip() not in ("", "0", "false", "False")


def _load():
    try:
        import mem0_hooks  # type: ignore
    except Exception as exc:
        log.info("mem0_hooks unavailable (%s: %s); bridge no-op", type(exc).__name__, exc)
        return None
    return mem0_hooks


def ingest_session(
    summary: str,
    *,
    session_id: str,
    trigger: str,
    project_dir: str | None = None,
) -> bool:
    """Store a flush summary into mem0. Returns True on successful add."""
    if _disabled() or not summary or not summary.strip():
        return False
    if not os.environ.get("OPENAI_API_KEY"):
        log.info("OPENAI_API_KEY not set; skipping mem0 ingest")
        return False
    mem = _load()
    if mem is None:
        return False
    metadata = {"session_id": session_id, "trigger": trigger}
    if project_dir:
        metadata["project_dir"] = project_dir
    try:
        result = mem.add_safe(summary, user_id="mercury", metadata=metadata)
    except Exception as exc:
        log.warning("mem0 ingest failed: %s: %s", type(exc).__name__, exc)
        return False
    return result is not None


def recall(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Search mem0 for context relevant to ``query``. Returns [] on any failure."""
    if _disabled() or not query or not query.strip():
        return []
    if not os.environ.get("OPENAI_API_KEY"):
        return []
    mem = _load()
    if mem is None:
        return []
    try:
        return mem.search_safe(query, user_id="mercury", limit=limit)
    except Exception as exc:
        log.warning("mem0 recall failed: %s: %s", type(exc).__name__, exc)
        return []
