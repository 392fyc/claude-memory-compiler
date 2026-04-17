"""Offline test for mem0_bridge no-raise contract.

Runs without mem0ai installed (forces disabled mode) and without
OPENAI_API_KEY set, to confirm every bridge path short-circuits safely.
Used as a pre-flight check before wiring into live hooks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mem0_bridge  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    mark = "OK  " if cond else "FAIL"
    print(f"{mark} {label}")
    if not cond:
        FAILURES.append(label)


def main() -> int:
    # disabled-mode short-circuit (no-op even if mem0 is installed)
    os.environ["MERCURY_MEM0_DISABLED"] = "1"
    check(
        mem0_bridge.ingest_session("x", session_id="s", trigger="PreCompact") is False,
        "disabled: ingest_session returns False",
    )
    check(
        mem0_bridge.recall("x") == [],
        "disabled: recall returns []",
    )
    del os.environ["MERCURY_MEM0_DISABLED"]

    # no API key → silent skip regardless of install state
    saved_key = os.environ.pop("OPENAI_API_KEY", None)
    check(
        mem0_bridge.ingest_session("x", session_id="s", trigger="PreCompact") is False,
        "no-key: ingest_session returns False",
    )
    check(mem0_bridge.recall("x") == [], "no-key: recall returns []")
    if saved_key is not None:
        os.environ["OPENAI_API_KEY"] = saved_key

    # empty / whitespace content always rejected
    os.environ["OPENAI_API_KEY"] = "sk-dummy"
    check(
        mem0_bridge.ingest_session("", session_id="s", trigger="x") is False,
        "empty: ingest_session returns False",
    )
    check(
        mem0_bridge.ingest_session("   \n", session_id="s", trigger="x") is False,
        "whitespace: ingest_session returns False",
    )
    check(mem0_bridge.recall("") == [], "empty-query: recall returns []")
    if saved_key is not None:
        os.environ["OPENAI_API_KEY"] = saved_key
    else:
        del os.environ["OPENAI_API_KEY"]

    print(f"\nresult: {len(FAILURES)} failure(s)")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
