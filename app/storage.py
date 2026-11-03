"""File-based storage: uploaded suites, recordings and runs live under data/."""
import json
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

SUITES = DATA_DIR / "suites"
RUNS = DATA_DIR / "runs"

# IDs end up in file names (suite and run folders, recordings/<case id>.json), so they must
# not be able to reach outside data/. Suite and run IDs are made by new_id(); case IDs come
# from the uploaded Excel, so only what could change the folder is refused there.
SAFE_ID = re.compile(r"^[a-z]+-\d{8}-\d{6}-[0-9a-f]{4}$")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def now_iso() -> str:
    # Includes the UTC offset so the dashboard shows the right local time.
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def safe_id(value: str) -> bool:
    return bool(SAFE_ID.match(value or ""))


def safe_name(value: str) -> bool:
    return (bool(value) and len(value) <= 120 and not value.startswith(".")
            and not any(c in value for c in '/\\:*?"<>|\0'))


# ---- suites (one uploaded workbook = one suite, each sheet = one test case)
def suite_dir(suite_id: str) -> Path:
    return SUITES / suite_id


def workbook_path(suite_id: str) -> Path:
    return suite_dir(suite_id) / "workbook.xlsx"


def save_suite(suite: dict) -> None:
    _write(suite_dir(suite["id"]) / "suite.json", suite)


def load_suite(suite_id: str) -> dict | None:
    return _read(suite_dir(suite_id) / "suite.json")


def list_suites() -> list[dict]:
    if not SUITES.exists():
        return []
    suites = [_read(p) for p in SUITES.glob("*/suite.json")]
    return sorted((s for s in suites if s), key=lambda s: s["uploaded_at"], reverse=True)


def find_case(suite: dict, case_id: str) -> dict | None:
    return next((c for c in suite["cases"] if c["id"] == case_id), None)


def delete_case(suite: dict, case_id: str) -> None:
    """Delete a test case with its recording and runs. A suite left without cases is removed whole."""
    for p in RUNS.glob("*/run.json") if RUNS.exists() else []:
        r = _read(p)
        if r and r["suite_id"] == suite["id"] and r["case_id"] == case_id:
            shutil.rmtree(p.parent, ignore_errors=True)
    suite["cases"] = [c for c in suite["cases"] if c["id"] != case_id]
    if not suite["cases"]:
        shutil.rmtree(suite_dir(suite["id"]), ignore_errors=True)
        return
    recording_path(suite["id"], case_id).unlink(missing_ok=True)
    save_suite(suite)


# ---- recordings
def recording_path(suite_id: str, case_id: str) -> Path:
    if not (safe_id(suite_id) and safe_name(case_id)):
        raise ValueError(f"Unsafe test case id: {case_id!r}")
    return suite_dir(suite_id) / "recordings" / f"{case_id}.json"


def save_recording(suite_id: str, case_id: str, recording: dict) -> None:
    _write(recording_path(suite_id, case_id), recording)


def load_recording(suite_id: str, case_id: str) -> dict | None:
    return _read(recording_path(suite_id, case_id))


# ---- runs
def run_dir(run_id: str) -> Path:
    return RUNS / run_id


def save_run(run: dict) -> None:
    _write(run_dir(run["id"]) / "run.json", run)


def load_run(run_id: str) -> dict | None:
    return _read(run_dir(run_id) / "run.json")


def list_runs(suite_id: str | None = None, case_id: str | None = None) -> list[dict]:
    if not RUNS.exists():
        return []
    out = []
    for p in RUNS.glob("*/run.json"):
        r = _read(p)
        if not r or (suite_id and r["suite_id"] != suite_id) or (case_id and r["case_id"] != case_id):
            continue
        r.pop("events", None)
        out.append(r)
    return sorted(out, key=lambda r: r["started_at"], reverse=True)
