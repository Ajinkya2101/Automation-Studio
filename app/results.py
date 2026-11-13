"""How a test stands, as the dashboard shows it, and what the run form should start from.

A run fails for two different reasons: the test is broken, or its test data is used up
(the requisition was processed, the invoice number exists). The second is shown apart,
as "needs new test data", so a test that works is not counted as failing.
"""
from . import users
from .actions import MASK, is_secret

RECENT = 5  # results shown per test in the test list


def display_status(run: dict) -> str:
    """passed / failed / needs_data / stopped / running."""
    if run.get("status") == "failed" and run.get("needs_data"):
        return "needs_data"
    return run.get("status") or ""


def proof_gaps(recording: dict | None) -> list[str]:
    """Why a passing run of this recording would not prove the test worked: nothing is checked,
    or steps have no actions. Same rule as the dashboard's verdict (app.js proofGaps)."""
    if not recording:
        return []
    steps = recording.get("steps") or []
    gaps = []
    if not any(a.get("type") == "expect_text" for s in steps for a in s.get("actions") or []):
        gaps.append("no_checks")
    if any(not s.get("actions") for s in steps):
        gaps.append("empty_steps")
    return gaps


def unproven(run: dict | None, recording: dict | None) -> bool:
    """A passed run of a recording that checks nothing (or skips steps), recorded before the run."""
    return bool(run and run.get("status") == "passed" and proof_gaps(recording)
                and (recording.get("recorded_at") or "") <= (run.get("started_at") or ""))


def summary(run: dict) -> dict:
    """A run as the test list and history show it."""
    by = run.get("run_by")
    return {"id": run["id"], "status": run.get("status"), "display": display_status(run),
            "needs_data": run.get("needs_data") or "", "run_by": by, "by_admin": bool(by) and users.is_admin(by),
            "started_at": run.get("started_at"), "duration_ms": run.get("duration_ms")}


def recent(runs: list[dict]) -> list[dict]:
    """The last few results, oldest first (runs come newest first)."""
    return [summary(r) for r in runs[:RECENT]][::-1]


def _value(data: dict, key: str) -> str | None:
    v = (data or {}).get(key)
    return None if v is None or is_secret(key) or v == MASK else v


def run_form(case: dict, recording: dict | None, runs: list[dict]) -> dict:
    """What the run form starts from, and which values were already used.

    defaults: each value as the newest passing run had it, typed as the tester typed it
    ({stamp} kept), so a test that passed with new data is not run with the recording's.
    used: for values testers change from run to run (an invoice number, a requisition),
    the values the recording and the passing runs used. Those records are probably used
    up; the form says so when one of them is about to be used again.
    """
    saved = {k: v for s in case["steps"] for k, v in s["data"].items()}
    passed = [r for r in runs if r.get("status") == "passed"]  # newest first
    defaults = {}
    if passed:
        last = passed[0]
        typed = last.get("test_data_typed") or last.get("test_data") or {}
        for k in saved:
            v = _value(typed, k)
            if v is not None and v != saved[k]:
                defaults[k] = {"value": v, "run_id": last["id"], "at": last.get("started_at"), "by": last.get("run_by")}
    used = {}
    for k, v in saved.items():
        if is_secret(k):
            continue
        history = [{"value": v, "at": (recording or {}).get("recorded_at"), "by": (recording or {}).get("recorded_by"),
                    "what": "recording"}] if v else []
        for r in passed:
            rv = _value(r.get("test_data") or {}, k)
            if rv:
                history.append({"value": rv, "at": r.get("started_at"), "by": r.get("run_by"), "what": "run"})
        if len({h["value"] for h in history}) > 1:  # testers change it from run to run
            used[k] = history
    return {"defaults": defaults, "used": used}
