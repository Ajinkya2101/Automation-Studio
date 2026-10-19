"""Automation Studio (prototype): dashboard API and live event stream, served
by one FastAPI process."""
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import diagnostics, storage
from .actions import NO_START_URL, case_target
from .browser import signed_in_at
from .config import DATA_DIR, IN_CONTAINER, LIVE_VIEW_URL, SAMPLE_WORKBOOK, STATIC_DIR
from .events import BusyError, sessions
from .excel_io import WorkbookError, build_workbook, parse_workbook
from .recorder import record_session, signin_session
from .replayer import run_session

DATA_DIR.mkdir(parents=True, exist_ok=True)
diagnostics.setup_logging()
log = diagnostics.log

app = FastAPI(title="Automation Studio (Prototype)")


@app.middleware("http")
async def always_current_dashboard(request: Request, call_next):
    """Make browsers re-check the dashboard files on every load.

    Without this, a browser can keep an old app.js after an update (the new page
    then shows buttons the old script does not handle). With no-cache the check
    is cheap: an unchanged file gets a 304 reply via its ETag.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def log_server_errors(request: Request, call_next):
    """Unexpected server errors go to data/logs/app.log, so they are in the issue log."""
    try:
        return await call_next(request)
    except Exception:
        log.exception("Server error on %s %s", request.method, request.url.path)
        raise


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
# Only run output (screenshots) is served. The rest of data/ holds the browser profiles
# with their sign-in cookies and the recordings with any password typed while recording.
storage.RUNS.mkdir(parents=True, exist_ok=True)
app.mount("/artifacts/runs", StaticFiles(directory=storage.RUNS), name="artifacts")


class RunRequest(BaseModel):
    headed: bool = True
    slow_mo: int = 250
    data: dict[str, str] = {}


class FreeformRequest(BaseModel):
    title: str = ""
    url: str = ""


def _suite_case(suite_id: str, case_id: str) -> tuple[dict, dict]:
    suite = storage.load_suite(suite_id)
    case = storage.find_case(suite, case_id) if suite else None
    if not case:
        raise HTTPException(404, "Test case not found")
    return suite, case


def _need_target(case: dict) -> None:
    if not case_target(case):
        raise HTTPException(400, NO_START_URL)


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
def sample():
    return FileResponse(SAMPLE_WORKBOOK, filename=SAMPLE_WORKBOOK.name)


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
        log.warning("Upload of %s refused: %s", file.filename, exc)
        raise HTTPException(400, str(exc))
    suite = {"id": storage.new_id("suite"), "filename": file.filename,
             "uploaded_at": storage.now_iso(), "warnings": warnings, "cases": cases}
    storage.suite_dir(suite["id"]).mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_path), storage.workbook_path(suite["id"]))
    storage.save_suite(suite)
    log.info("Uploaded %s as %s: %d case(s)%s", file.filename, suite["id"], len(cases),
             "".join(f"\n  note: {w}" for w in warnings))
    return {"suite_id": suite["id"], "cases": [c["id"] for c in cases], "warnings": warnings}


@app.post("/api/freeform")
def create_freeform(req: FreeformRequest):
    """A test without an Excel file: the steps are created while the user records."""
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter the start URL of the application to test, beginning with http:// or https://")
    case_id = f"REC-{datetime.now():%Y%m%d-%H%M%S}"
    title = req.title.strip() or f"Recorded test {datetime.now():%d %b %H:%M}"
    case = {"id": case_id, "sheet": case_id, "title": title, "description": f"Recorded without Excel on {url}",
            "steps": [], "freeform": True, "start_url": url}
    suite = {"id": storage.new_id("suite"), "filename": "Recorded without Excel", "source": "recording",
             "uploaded_at": storage.now_iso(), "warnings": [], "cases": [case]}
    storage.save_suite(suite)
    build_workbook(storage.workbook_path(suite["id"]), case)
    return {"suite_id": suite["id"], "case_id": case_id}


@app.get("/api/suites/{suite_id}/workbook")
def suite_workbook(suite_id: str):
    """The test script as Excel: the uploaded file, or the one written for a recorded test."""
    suite = storage.load_suite(suite_id)
    path = storage.workbook_path(suite_id)
    if not suite or not path.exists():
        raise HTTPException(404, "Workbook not found")
    name = suite["filename"] if suite.get("source") != "recording" else f"{suite['cases'][0]['id']}.xlsx"
    return FileResponse(path, filename=name)


@app.get("/api/suites/{suite_id}/cases/{case_id}")
def case_detail(suite_id: str, case_id: str):
    suite, case = _suite_case(suite_id, case_id)
    target = case_target(case)
    return {
        "suite": {k: suite.get(k) for k in ("id", "filename", "uploaded_at", "warnings", "source")},
        "case": case,
        "target": {"url": target, "has_profile": bool(target and signed_in_at(target)),
                   "signed_in_at": signed_in_at(target) if target else None},
        "recording": diagnostics.mask_recording(storage.load_recording(suite_id, case_id)),
        "runs": storage.list_runs(suite_id, case_id),
    }


@app.delete("/api/suites/{suite_id}/cases/{case_id}")
def delete_case(suite_id: str, case_id: str):
    suite, case = _suite_case(suite_id, case_id)
    s = sessions.active()
    if s and s.suite_id == suite["id"] and s.case_id == case["id"]:
        raise HTTPException(409, "This test case is being recorded or run. Stop it before deleting.")
    storage.delete_case(suite, case["id"])
    log.info("Deleted test case %s from %s (its session logs are kept)", case["id"], suite["id"])
    return {"ok": True}


@app.post("/api/suites/{suite_id}/cases/{case_id}/signin")
def start_signin(suite_id: str, case_id: str):
    suite, case = _suite_case(suite_id, case_id)
    _need_target(case)
    s = _start("signin", signin_session, suite_id=suite_id, case_id=case_id, suite=suite, case=case)
    return s.info()


@app.post("/api/suites/{suite_id}/cases/{case_id}/record")
def start_recording(suite_id: str, case_id: str):
    suite, case = _suite_case(suite_id, case_id)
    _need_target(case)
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
               slow_mo=max(0, min(req.slow_mo, 2000)))
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


def _download(path: Path, name: str) -> FileResponse:
    return FileResponse(path, filename=name, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True))


@app.get("/api/runs/{run_id}/issue-log")
def run_issue_log(run_id: str):
    """One run's issue log as a ZIP: report, screenshots, page snapshots, trace, test case and recording."""
    if not storage.load_run(run_id):
        raise HTTPException(404, "Run not found")
    return _download(*diagnostics.export_issue_log([run_id]))


@app.get("/api/issue-log")
def issue_log(include_passed: bool = False):
    """This device's issue log as a ZIP: every run that did not pass (or every run), all session logs
    and the studio's own log, with an Excel summary."""
    return _download(*diagnostics.export_issue_log(None, include_passed))


class ClientError(BaseModel):
    message: str = ""
    stack: str = ""
    where: str = ""


@app.post("/api/client-errors")
def client_error(err: ClientError, request: Request):
    """Errors in the dashboard itself, so they are in app.log too."""
    log.error("Dashboard error (%s): %s\n%s\n  browser: %s", err.where[:200], err.message[:2000], err.stack[:4000],
              request.headers.get("user-agent", ""))
    return {"ok": True}
