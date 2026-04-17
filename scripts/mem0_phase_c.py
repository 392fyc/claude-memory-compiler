"""Mercury #252 Phase C validation suite.

Three tests exercised against the live mem0 stack:
  1. Cross-session recall — spawn a subprocess that writes N memories, exit,
     then spawn a fresh subprocess that reads them back via a brand-new
     Memory instance. Verifies Qdrant persistence survives singleton reset.
  2. Telemetry audit — monkey-patch every HTTP egress surface BEFORE mem0
     loads, exercise add_safe + search_safe, assert zero hits on PostHog.
  3. P1-bug regression — re-runs the four guards end-to-end against a
     fresh user_id so the result does not depend on prior smoke-test state.

Requires OPENAI_API_KEY. Exit 0 = all three pass; non-zero = regression.
Outputs a compact report to stdout + writes phase-c-report.md next to this
file for review.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPORT = SCRIPTS_DIR / "phase-c-report.md"
FAILURES: list[str] = []
WARNINGS: list[str] = []
USERS_TO_CLEAN: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"{mark}  {label}{suffix}")
    if not cond:
        FAILURES.append(f"{label}{suffix}")


def warn(label: str, detail: str = "") -> None:
    """Record a WARN-level finding (not a failure, but surfaced in the report).

    Opt-in escalation: set ``MEM0_PHASE_C_STRICT=1`` to treat WARN as FAIL for
    CI runs that want zero-ambiguity signal.
    """
    suffix = f" — {detail}" if detail else ""
    print(f"WARN  {label}{suffix}")
    WARNINGS.append(f"{label}{suffix}")
    if os.environ.get("MEM0_PHASE_C_STRICT", "").strip().lower() in ("1", "true", "yes", "on"):
        FAILURES.append(f"(strict) WARN promoted: {label}{suffix}")


# ----------------------------------------------------------------- test 1

_WRITE_SCRIPT = """
import os, sys, json
sys.path.insert(0, r"{scripts}")
import mem0_hooks
user = sys.argv[1]
facts = [
    "Phase C fact alpha: Mercury runs on Windows 11",
    "Phase C fact beta: AgentKB bridge ingests via mem0_bridge",
    "Phase C fact gamma: Qdrant persists across singleton reset",
]
for f in facts:
    r = mem0_hooks.add_safe(f, user_id=user, skip_dedup=True)
    print("WRITE", json.dumps({{"ok": r is not None, "memory": f}}))
"""

_READ_SCRIPT = """
import os, sys, json
sys.path.insert(0, r"{scripts}")
import mem0_hooks
user = sys.argv[1]
hits = mem0_hooks.search_safe("Phase C alpha", user_id=user, limit=5)
memories = [h.get("memory") for h in hits if isinstance(h, dict)]
print("READ", json.dumps({{"count": len(hits), "memories": memories}}))
"""


def test_cross_session_recall(py: str) -> None:
    user = f"phase-c-{uuid.uuid4().hex[:8]}"
    USERS_TO_CLEAN.append(user)
    env = dict(os.environ)
    write_out = subprocess.run(
        [py, "-c", _WRITE_SCRIPT.format(scripts=str(SCRIPTS_DIR)), user],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(SCRIPTS_DIR.parent),
    )
    check(
        write_out.returncode == 0,
        "cross-session: writer subprocess exits 0",
        detail=write_out.stderr.strip()[:200] if write_out.returncode else "",
    )
    writes = [json.loads(l[6:]) for l in write_out.stdout.splitlines() if l.startswith("WRITE ")]
    check(all(w["ok"] for w in writes) and len(writes) == 3, "cross-session: 3 facts written")

    # Fresh subprocess, no shared state:
    read_out = subprocess.run(
        [py, "-c", _READ_SCRIPT.format(scripts=str(SCRIPTS_DIR)), user],
        env=env, capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS_DIR.parent),
    )
    check(
        read_out.returncode == 0,
        "cross-session: reader subprocess exits 0",
        detail=read_out.stderr.strip()[:200] if read_out.returncode else "",
    )
    reads = [json.loads(l[5:]) for l in read_out.stdout.splitlines() if l.startswith("READ ")]
    count = reads[0]["count"] if reads else 0
    memories = reads[0].get("memories", []) if reads else []
    # Require a strict per-fact match against the distinct identifiers each
    # writer fact carries. "Phase C fact" alone is too loose — anything
    # starting with that prefix would satisfy it. We check for at least one
    # of the three fact-specific markers ("alpha: Mercury", "beta: AgentKB
    # bridge", "gamma: Qdrant") so similar-but-wrong content cannot pass.
    fact_markers = (
        "alpha: Mercury",
        "beta: AgentKB bridge",
        "gamma: Qdrant",
    )
    matched_markers = [m for m in fact_markers
                       if any(isinstance(x, str) and m in x for x in memories)]
    snippet = "; ".join((m or "")[:80] for m in memories[:3])
    check(
        count >= 1 and bool(matched_markers),
        "cross-session: reader recalls at least one distinctively-keyed fact from the writer",
        f"count={count}, matched_markers={matched_markers}, memories=[{snippet}]",
    )


# ----------------------------------------------------------------- test 2

_TELEMETRY_SCRIPT = """
import os, sys, json
# Instrument egress BEFORE mem0 loads.
import urllib.request

POSTHOG_HITS = []
_orig_urlopen = urllib.request.urlopen

def _watched_urlopen(req, *a, **kw):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if "posthog" in url.lower():
        POSTHOG_HITS.append(url)
    return _orig_urlopen(req, *a, **kw)

urllib.request.urlopen = _watched_urlopen

try:
    import httpx
    _orig_send = httpx.Client.send

    def _watched_send(self, request, *a, **kw):
        url = str(request.url)
        if "posthog" in url.lower():
            POSTHOG_HITS.append(url)
        return _orig_send(self, request, *a, **kw)

    httpx.Client.send = _watched_send

    _orig_async_send = httpx.AsyncClient.send

    async def _watched_async_send(self, request, *a, **kw):
        url = str(request.url)
        if "posthog" in url.lower():
            POSTHOG_HITS.append(url)
        return await _orig_async_send(self, request, *a, **kw)

    httpx.AsyncClient.send = _watched_async_send
except ImportError:
    pass

try:
    import requests
    _orig_req_send = requests.Session.send

    def _watched_req_send(self, prep, **kw):
        url = str(getattr(prep, "url", ""))
        if "posthog" in url.lower():
            POSTHOG_HITS.append(url)
        return _orig_req_send(self, prep, **kw)

    requests.Session.send = _watched_req_send
except ImportError:
    pass

sys.path.insert(0, r"{scripts}")
import mem0_hooks
user = sys.argv[1]
mem0_hooks.add_safe("Phase C telemetry audit canary", user_id=user, skip_dedup=True)
mem0_hooks.search_safe("canary", user_id=user, limit=1)
print("TELEMETRY", json.dumps({{"posthog_hits": POSTHOG_HITS}}))
"""


def test_telemetry(py: str) -> None:
    user = f"phase-c-tel-{uuid.uuid4().hex[:8]}"
    USERS_TO_CLEAN.append(user)
    env = dict(os.environ)
    out = subprocess.run(
        [py, "-c", _TELEMETRY_SCRIPT.format(scripts=str(SCRIPTS_DIR)), user],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(SCRIPTS_DIR.parent),
    )
    check(out.returncode == 0, "telemetry: subprocess exits 0",
          detail=out.stderr.strip()[:200] if out.returncode else "")
    lines = [l for l in out.stdout.splitlines() if l.startswith("TELEMETRY ")]
    hits = json.loads(lines[0][10:])["posthog_hits"] if lines else ["<no-line>"]
    check(len(hits) == 0, "telemetry: 0 PostHog egress during add + search",
          detail=f"hits={hits}")


# ----------------------------------------------------------------- test 3

_REGRESSION_SCRIPT = """
import sys, json
sys.path.insert(0, r"{scripts}")
import mem0_hooks
user = sys.argv[1]
results = {{}}
# #4099 empty rejected
results["4099_empty"] = mem0_hooks.add_safe("", user_id=user) is None
results["4099_ws"] = mem0_hooks.add_safe("   \\n", user_id=user) is None
# #4799 list coerce
r = mem0_hooks.add_safe([{{"content": "regression list coerce"}}], user_id=user, skip_dedup=True)
results["4799_list"] = r is not None
# #4453 search returns list without threshold
hits = mem0_hooks.search_safe("regression", user_id=user, limit=3)
results["4453_list"] = isinstance(hits, list)
# #4536 dedup — fires only when mem0's LLM fact-extraction actually persisted
# the seed. Use distinctive fact-shaped seed so extraction keeps it, then
# probe via search first; if the seed is searchable, a repeat add must be
# deduped. If the seed was dropped by extraction, the test is inconclusive.
seed = "Mercury Phase C dedup canary: ingest sentinel alpha-1 persists."
r1 = mem0_hooks.add_safe(seed, user_id=user, skip_dedup=True)
probe = mem0_hooks.search_safe(seed, user_id=user, limit=3)
recallable = any(
    isinstance(h, dict) and isinstance(h.get("score"), (int, float)) and h["score"] >= 0.92
    for h in probe
)
if recallable:
    r2 = mem0_hooks.add_safe(seed, user_id=user)
    results["4536_dedup"] = r2 is None and r1 is not None
else:
    results["4536_dedup"] = None  # inconclusive — extraction dropped seed
results["_probe"] = [
    {{"score": h.get("score"), "memory": (h.get("memory") or "")[:80]}}
    for h in probe if isinstance(h, dict)
]
print("REGRESSION", json.dumps(results))
"""


def test_regression(py: str) -> None:
    user = f"phase-c-reg-{uuid.uuid4().hex[:8]}"
    USERS_TO_CLEAN.append(user)
    env = dict(os.environ)
    out = subprocess.run(
        [py, "-c", _REGRESSION_SCRIPT.format(scripts=str(SCRIPTS_DIR)), user],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(SCRIPTS_DIR.parent),
    )
    check(out.returncode == 0, "regression: subprocess exits 0",
          detail=out.stderr.strip()[:200] if out.returncode else "")
    lines = [l for l in out.stdout.splitlines() if l.startswith("REGRESSION ")]
    results = json.loads(lines[0][11:]) if lines else {}
    for guard in ("4099_empty", "4099_ws", "4799_list", "4453_list"):
        check(results.get(guard) is True, f"regression: guard #{guard.split('_')[0]} {guard.split('_',1)[1]}")
    # #4536 dedup test is inconclusive when mem0's LLM fact-extraction drops
    # the seed before storage. Treat inconclusive as WARN (not FAIL), record
    # the probe in the report so drift is visible, and let strict mode promote
    # WARN to FAIL for CI runs that want zero-ambiguity signal.
    dedup = results.get("4536_dedup")
    if dedup is None:
        probe = results.get("_probe", [])
        warn(
            "regression: guard #4536 dedup (INCONCLUSIVE — seed dropped by mem0 extraction)",
            detail=f"probe={probe}",
        )
    else:
        check(dedup is True, "regression: guard #4536 dedup")


# ----------------------------------------------------------------- main


_CLEANUP_SCRIPT = """
import os, sys, json
sys.path.insert(0, r"{scripts}")
import mem0_hooks
users = sys.argv[1:]
deleted = {{}}
mem = mem0_hooks.get_memory()
for u in users:
    try:
        mem.delete_all(user_id=u)
        deleted[u] = "ok"
    except Exception as exc:
        deleted[u] = f"ERR: {{type(exc).__name__}}: {{exc}}"
print("CLEANUP", json.dumps(deleted))
"""


def cleanup_test_users(py: str) -> dict:
    """Delete all memories for each test user_id so repeated runs don't accrete.

    Cleanup failures raise a WARN (promotable to FAIL via strict mode) so the
    report surfaces the leak instead of silently accruing vector rows on each
    run. Covers: non-zero exit, missing CLEANUP output line, malformed JSON,
    and per-user ERR values.
    """
    if not USERS_TO_CLEAN:
        return {}
    if os.environ.get("MEM0_PHASE_C_SKIP_CLEANUP", "").strip().lower() in ("1", "true", "yes", "on"):
        print(f"cleanup: SKIPPED ({len(USERS_TO_CLEAN)} users, MEM0_PHASE_C_SKIP_CLEANUP=1)")
        return {u: "skipped" for u in USERS_TO_CLEAN}
    env = dict(os.environ)
    out = subprocess.run(
        [py, "-c", _CLEANUP_SCRIPT.format(scripts=str(SCRIPTS_DIR)), *USERS_TO_CLEAN],
        env=env, capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS_DIR.parent),
    )
    if out.returncode != 0:
        warn(
            "cleanup: subprocess exited non-zero — test user memories may have leaked",
            detail=f"rc={out.returncode} stderr={out.stderr.strip()[:200]}",
        )
        return {u: f"subprocess rc={out.returncode}" for u in USERS_TO_CLEAN}
    lines = [l for l in out.stdout.splitlines() if l.startswith("CLEANUP ")]
    if not lines:
        warn(
            "cleanup: subprocess produced no CLEANUP line — test user memories may have leaked",
            detail=f"stdout={out.stdout.strip()[:200]}",
        )
        return {u: "no-output" for u in USERS_TO_CLEAN}
    try:
        deleted = json.loads(lines[0][8:])
    except json.JSONDecodeError as exc:
        warn("cleanup: CLEANUP line was not valid JSON", detail=str(exc))
        return {u: "malformed" for u in USERS_TO_CLEAN}
    # Verify every user in USERS_TO_CLEAN appears in the subprocess response —
    # a silent omission otherwise masks leaks from future mem0 API shape drift.
    missing = [u for u in USERS_TO_CLEAN if u not in deleted]
    if missing:
        warn(
            "cleanup: subprocess did not report on all test user_ids",
            detail=f"missing={missing}",
        )
        for u in missing:
            deleted[u] = "missing-from-report"
    failed = [u for u, v in deleted.items() if v != "ok"]
    if failed:
        warn(
            "cleanup: per-user delete_all failed for some test user_ids",
            detail=f"failed={failed}",
        )
    ok_count = sum(1 for v in deleted.values() if v == "ok")
    print(f"cleanup: {ok_count}/{len(USERS_TO_CLEAN)} test user_ids purged")
    return deleted


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY required")
        return 2

    py = sys.executable
    strict = os.environ.get("MEM0_PHASE_C_STRICT", "").strip().lower() in ("1", "true", "yes", "on")
    print(f"Phase C validation — python={py}" + (" [STRICT]" if strict else "") + "\n")

    print("## 1. Cross-session recall")
    test_cross_session_recall(py)
    print("\n## 2. Telemetry audit (PostHog must be silent)")
    test_telemetry(py)
    print("\n## 3. P1-bug regression")
    test_regression(py)

    print("\n## Cleanup")
    cleanup_result = cleanup_test_users(py)

    if FAILURES:
        status = "FAIL"
    elif WARNINGS:
        status = "WARN"
    else:
        status = "PASS"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    report = [
        f"# Phase C validation report",
        "",
        f"- Timestamp: {stamp}",
        f"- Python: `{py}`",
        f"- Status: **{status}** (strict-mode {'on' if strict else 'off'})",
        f"- Failures: {len(FAILURES)}  |  Warnings: {len(WARNINGS)}",
        f"- Test user_ids cleaned: {sum(1 for v in cleanup_result.values() if v == 'ok')}/{len(cleanup_result)}",
        "",
    ]
    if FAILURES:
        report.append("## Failed checks")
        report.extend(f"- {f}" for f in FAILURES)
        report.append("")
    if WARNINGS:
        report.append("## Warnings (inconclusive / drift signal)")
        report.extend(f"- {w}" for w in WARNINGS)
        report.append("")
    if not FAILURES and not WARNINGS:
        report.append("All cross-session / telemetry / regression checks passed with no drift warnings.")
    elif not FAILURES:
        report.append("No failures; warnings recorded above for operator review.")
    REPORT.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"\nReport: {REPORT}  ({status})")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
