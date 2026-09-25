"""Automation Studio (prototype): dashboard API, live event stream and the
Acme Mail target app, all served by one FastAPI process."""
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import mockmail, storage
from .actions import start_url
from .browser import signed_in_at
from .config import (DATA_DIR, IN_CONTAINER, LIVE_VIEW_URL, OUTLOOK_WORKBOOK, SAMPLE_WORKBOOK,
                     STATIC_DIR, TARGET_START_URL)
from .events import BusyError, sessions
from .excel_io import WorkbookError, parse_workbook
from .recorder import record_session, signin_session
from .replayer import run_session

DATA_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Automation Studio (Prototype)")
app.include_router(mockmail.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/artifacts", StaticFiles(directory=DATA_DIR), name="artifacts")


class RunRequest(BaseModel):
    headed: bool = True
    slow_mo: int = 250
    reset_mailbox: bool = True
    data: dict[str, str] = {}


def _suite_case(suite_id: str, case_id: str) -> tuple[dict, dict]:
    suite = storage.load_suite(suite_id)
    case = storage.find_case(suite, case_id) if suite else None
    if not case:
        raise HTTPException(404, "Test case not found")
    return suite, case


def _start(kind, target, **kw):
    try:
        return sessions.start(kind, target, **kw)
    except BusyError as exc:
        raise HTTPException(409, str(exc))


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status():
    s = sessions.active()
    return {"active": s.info() if s else None}


@app.get("/api/config")
def config():
    return {"live_view_url": LIVE_VIEW_URL, "in_container": IN_CONTAINER}


@app.get("/api/sample")
def sample(name: str = "acme"):
    path = OUTLOOK_WORKBOOK if name == "outlook" else SAMPLE_WORKBOOK
    return FileResponse(path, filename=path.name)


@app.get("/api/cases")
def cases():
    runs = storage.list_runs()
    out = []
    for suite in storage.list_suites():
        for c in suite["cases"]:
            last = next((r for r in runs if r["suite_id"] == suite["id"] and r["case_id"] == c["id"]), None)
            out.append({
                "suite_id": suite["id"], "filename": suite["filename"], "uploaded_at": suite["uploaded_at"],
                "case_id": c["id"], "title": c["title"], "steps": len(c["steps"]),
                "recorded": storage.recording_path(suite["id"], c["id"]).exists(),
                "last_run": {"status": last["status"], "started_at": last["started_at"]} if last else None,
            })
    return out


@app.post("/api/suites")
async def upload(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "Please upload an .xlsx workbook.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = Path(tmp.name)
    try:
        cases, warnings = parse_workbook(tmp_path)
    except WorkbookError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(400, str(exc))
    suite = {"id": storage.new_id("suite"), "filename": file.filename,
             "uploaded_at": storage.now_iso(), "warnings": warnings, "cases": cases}
    storage.suite_dir(suite["id"]).mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_path), storage.workbook_path(suite["id"]))
    storage.save_suite(suite)
    return {"suite_id": suite["id"], "cases": [c["id"] for c in cases], "warnings": warnings}


@app.get("/api/suites/{suite_id}/cases/{case_id}")
def case_detail(suite_id: str, case_id: str):
    suite, case = _suite_case(suite_id, case_id)
    target = start_url(case["steps"], TARGET_START_URL)
    return {
        "suite": {k: suite[k] for k in ("id", "filename", "uploaded_at", "warnings")},
        "case": case,
        "target": {"url": target, "is_demo": target == TARGET_START_URL,
                   "has_profile": bool(signed_in_at(target)), "signed_in_at": signed_in_at(target)},
        "recording": storage.load_recording(suite_id, case_id),
        "runs": storage.list_runs(suite_id, case_id),
    }


@app.post("/api/suites/{suite_id}/cases/{case_id}/signin")
def start_signin(suite_id: str, case_id: str):
    suite, case = _suite_case(suite_id, case_id)
    s = _start("signin", signin_session, suite_id=suite_id, case_id=case_id, suite=suite, case=case)
    return s.info()


@app.post("/api/suites/{suite_id}/cases/{case_id}/record")
def start_recording(suite_id: str, case_id: str):
    suite, case = _suite_case(suite_id, case_id)
    s = _start("record", record_session, suite_id=suite_id, case_id=case_id, suite=suite, case=case)
    return s.info()


@app.post("/api/suites/{suite_id}/cases/{case_id}/run")
def start_run(suite_id: str, case_id: str, req: RunRequest):
    suite, case = _suite_case(suite_id, case_id)
    recording = storage.load_recording(suite_id, case_id)
    if not recording:
        raise HTTPException(400, "Record this test case before running it.")
    run_id = storage.new_id("run")
    s = _start("run", run_session, suite_id=suite_id, case_id=case_id, run_id=run_id,
               suite=suite, case=case, recording=recording, data=req.data, headed=req.headed,
               slow_mo=max(0, min(req.slow_mo, 2000)), reset_mailbox=req.reset_mailbox)
    return s.info()


@app.post("/api/sessions/{session_id}/stop")
def stop(session_id: str):
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    s.stop_event.set()
    return s.info()


@app.get("/api/sessions/{session_id}/events")
def events(session_id: str, request: Request, after: int = 0):
    s = sessions.get(session_id)
    if not s:
        raise HTTPException(404, "Session not found")
    last_id = request.headers.get("last-event-id")  # EventSource reconnect: resume, don't repeat
    if last_id and last_id.isdigit():
        after = int(last_id) + 1
    return StreamingResponse(s.log.stream(after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str):
    run = storage.load_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/runs/{run_id}/results")
def run_results(run_id: str):
    run = storage.load_run(run_id)
    path = storage.run_dir(run_id) / "results.xlsx"
    if not run or not path.exists():
        raise HTTPException(404, "Results workbook not found")
    return FileResponse(path, filename=f"{run['case_id']}_{run_id}_results.xlsx")


@app.post("/api/mailbox/reset")
def reset_mailbox():
    mockmail.reset_mailbox()
    return {"ok": True}
