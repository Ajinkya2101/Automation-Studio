"""Automation Studio (prototype): dashboard API and live event stream, served
by one FastAPI process."""
import asyncio
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit, auth, diagnostics, storage, users
from .actions import NO_START_URL, case_target
from .browser import signed_in_at
from .config import (BIND_HOST, DATA_DIR, IN_CONTAINER, LIVE_VIEW_URL, LOGIN, MAX_UPLOAD_MB, NOVNC_DIR,
                     SAMPLE_WORKBOOK, SESSION_HOURS, STATIC_DIR, VNC_PORT)
from .events import BusyError, sessions
from .excel_io import WorkbookError, build_workbook, parse_workbook
from .recorder import record_session, signin_session
from .replayer import run_session

DATA_DIR.mkdir(parents=True, exist_ok=True)
diagnostics.setup_logging()
log = diagnostics.log

if not LOGIN and BIND_HOST not in ("127.0.0.1", "localhost", "::1"):
    raise RuntimeError("STUDIO_LOGIN=off is only allowed when the studio listens on 127.0.0.1.")
if LOGIN and (_initial := users.ensure_users()):
    log.warning("Created 10 dashboard users; their passwords are in %s", _initial)
    print(f"  Created 10 dashboard users. Their passwords are in {_initial}", flush=True)

# No /docs or /openapi.json: the API is for the dashboard only.
app = FastAPI(title="Automation Studio (Prototype)", docs_url=None, redoc_url=None, openapi_url=None)

# Reachable without signing in. The dashboard's script and styles hold nothing secret.
PUBLIC_PATHS = {"/login", "/api/login", "/api/logout", "/api/health"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "connect-src 'self'; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'; "
       "form-action 'self'")


def _same_origin(headers) -> bool:
    """A browser names the page a request comes from; refuse changes sent from another site."""
    origin = headers.get("origin")
    if not origin:
        return True
    host = headers.get("x-forwarded-host") or headers.get("host") or ""
    return urlparse(origin).netloc.lower() == host.split(",")[0].strip().lower()


def _user(request: Request) -> str:
    return request.state.user


def _ip(conn) -> str:
    return conn.client.host if conn.client else ""


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Everything except the sign-in page and the health check needs a signed-in user."""
    request.state.user = auth.user_from_token(request.cookies.get(auth.COOKIE)) if LOGIN else "local"
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request.headers):
        return JSONResponse({"detail": "Request from another site refused."}, status_code=403)
    if request.state.user or path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)
    if path == "/":
        return RedirectResponse("/login", status_code=303)
    return JSONResponse({"detail": "Sign in to the studio first."}, status_code=401)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if not request.url.path.startswith("/vnc/"):  # noVNC's own page is left as it ships
        response.headers.setdefault("Content-Security-Policy", CSP)
    return response


@app.middleware("http")
async def always_current_dashboard(request: Request, call_next):
    """Make browsers re-check the dashboard files on every load.

    Without this, a browser can keep an old app.js after an update (the new page
    then shows buttons the old script does not handle). With no-cache the check
    is cheap: an unchanged file gets a 304 reply via its ETag.
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    elif path == "/" or path.startswith(("/api/", "/artifacts/")):
        # Signed-in content: never stored, so Back after signing out cannot show it again.
        response.headers["Cache-Control"] = "no-store"
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
# Only run output (screenshots) is served, by artifact() below. The rest of data/ holds the browser
# profiles with their sign-in cookies and the recordings with any password typed while recording.
storage.RUNS.mkdir(parents=True, exist_ok=True)


class RunRequest(BaseModel):
    headed: bool = True
    slow_mo: int = 250
    data: dict[str, str] = {}


class FreeformRequest(BaseModel):
    title: str = ""
    url: str = ""


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


def _can_see(request: Request, suite: dict | None) -> bool:
    """A test case belongs to whoever created it; an admin sees everyone's."""
    return _is_admin(request) or bool(suite and suite.get("created_by") == _user(request))


def _suite_case(request: Request, suite_id: str, case_id: str) -> tuple[dict, dict]:
    suite = storage.load_suite(suite_id) if storage.safe_id(suite_id) else None
    if not _can_see(request, suite):
        suite = None  # someone else's: answered as not found, so its existence is not revealed
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


def _load_run(request: Request, run_id: str) -> dict:
    run = storage.load_run(run_id) if storage.safe_id(run_id) else None
    if not run or not _can_see(request, storage.load_suite(run["suite_id"])):
        raise HTTPException(404, "Run not found")
    return run


def _session(request: Request, session_id: str):
    s = sessions.get(session_id)
    if not s or not (_is_admin(request) or s.user == _user(request)):
        raise HTTPException(404, "Session not found")
    return s


def _session_info(request: Request, s) -> dict:
    """Someone else's session shows only that the browser is busy and who is using it."""
    info = s.info()
    if _is_admin(request) or s.user == _user(request):
        return info
    return {k: info[k] for k in ("id", "kind", "status", "user", "started_at")}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if request.state.user:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    name = req.username.strip().lower()[:64]
    wait = auth.lockout.wait_seconds(name)
    if wait:
        audit.record(name, "login_locked", _ip(request))
        raise HTTPException(429, f"Too many wrong passwords for {name}. Try again in {wait // 60 + 1} minutes.")
    user = users.verify(name, req.password)
    if not user:
        if users.get(name):  # only real usernames are counted, so made-up ones cannot fill memory
            auth.lockout.failed(name)
        audit.record(name, "login_failed", _ip(request))
        raise HTTPException(401, "Wrong username or password.")
    auth.lockout.clear(name)
    audit.record(user, "login", _ip(request))
    response = JSONResponse({"user": user})
    response.set_cookie(auth.COOKIE, auth.make_token(user), max_age=int(SESSION_HOURS * 3600), httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")
    return response


@app.post("/api/logout")
def logout(request: Request):
    if request.state.user:
        audit.record(_user(request), "logout", _ip(request))
    auth.revoke(request.cookies.get(auth.COOKIE))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    return response


@app.get("/api/me")
def me(request: Request):
    return {"user": _user(request), "login": LOGIN, "admin": _is_admin(request)}


def _is_admin(request: Request) -> bool:
    return not LOGIN or users.is_admin(_user(request))


@app.get("/api/admin/overview")
def admin_overview(request: Request):
    """Admin screen: every test case with who made it and how it stands, and what each user has done."""
    if not _is_admin(request):
        raise HTTPException(403, "Only an admin can open this.")
    runs_by_case: dict[tuple, list] = {}
    all_runs = storage.list_runs()  # newest first
    for r in all_runs:
        runs_by_case.setdefault((r["suite_id"], r["case_id"]), []).append(r)
    tests = []
    for suite in storage.list_suites():
        for c in suite["cases"]:
            rec = storage.load_recording(suite["id"], c["id"])
            runs = runs_by_case.get((suite["id"], c["id"]), [])
            last = runs[0] if runs else None
            tests.append({
                "suite_id": suite["id"], "case_id": c["id"], "title": c["title"],
                "source": "recording" if suite.get("source") == "recording" else "excel",
                "filename": suite["filename"], "created_by": suite.get("created_by"),
                "created_at": suite["uploaded_at"], "steps": len(c["steps"]),
                "recorded_by": rec.get("recorded_by") if rec else None,
                "recorded_at": rec.get("recorded_at") if rec else None,
                "recording_warnings": len(rec.get("warnings") or []) if rec else 0,
                "runs": len(runs), "passed": sum(r["status"] == "passed" for r in runs),
                "failed": sum(r["status"] == "failed" for r in runs),
                "last_run": {k: last.get(k) for k in ("id", "status", "started_at", "run_by", "duration_ms")}
                if last else None,
                "status": last["status"] if last else ("recorded" if rec else "new"),
            })
    last_seen: dict[str, str] = {}
    for e in audit.recent(20000):  # newest first
        if e.get("action") not in ("login_failed", "login_locked"):
            last_seen.setdefault(e.get("user"), e.get("ts"))
    people = [{
        "user": name, "role": u.get("role", "user"),
        "tests": sum(t["created_by"] == name for t in tests),
        "recordings": sum(t["recorded_by"] == name for t in tests),
        "runs": sum(r.get("run_by") == name for r in all_runs),
        "last_seen": last_seen.get(name),
    } for name, u in sorted(users.load().items())]
    active = sessions.active()
    return {"tests": tests, "users": people, "active": active.info() if active else None}


@app.get("/api/audit")
def audit_log(request: Request, limit: int = 500):
    """Who did what, newest first. A user sees their own entries, an admin everyone's."""
    entries = audit.recent(max(1, min(limit, 5000)) if _is_admin(request) else 20000)
    if not _is_admin(request):
        entries = [e for e in entries if e.get("user") == _user(request)][:max(1, min(limit, 5000))]
    return entries


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/status")
def status(request: Request):
    s = sessions.active()
    return {"active": _session_info(request, s) if s else None}


@app.get("/api/config")
def config():
    return {"live_view_url": LIVE_VIEW_URL, "in_container": IN_CONTAINER}


@app.get("/api/sample")
def sample():
    return FileResponse(SAMPLE_WORKBOOK, filename=SAMPLE_WORKBOOK.name)


@app.get("/api/cases")
def cases(request: Request):
    runs = storage.list_runs()
    out = []
    for suite in storage.list_suites():
        if not _can_see(request, suite):
            continue
        for c in suite["cases"]:
            last = next((r for r in runs if r["suite_id"] == suite["id"] and r["case_id"] == c["id"]), None)
            out.append({
                "suite_id": suite["id"], "filename": suite["filename"], "uploaded_at": suite["uploaded_at"],
                "created_by": suite.get("created_by"),
                "case_id": c["id"], "title": c["title"], "steps": len(c["steps"]),
                "recorded": storage.recording_path(suite["id"], c["id"]).exists(),
                "last_run": {"status": last["status"], "started_at": last["started_at"]} if last else None,
            })
    return out


@app.post("/api/suites")
async def upload(request: Request, file: UploadFile = File(...)):
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Please upload an .xlsx workbook.")
    limit = MAX_UPLOAD_MB * 1024 * 1024
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        tmp_path = Path(tmp.name)
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                break
            tmp.write(chunk)
    if size > limit:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(413, f"The workbook is larger than {MAX_UPLOAD_MB} MB.")
    try:
        cases, warnings = parse_workbook(tmp_path)
        unsafe = [c["id"] for c in cases if not storage.safe_name(c["id"])]
        if unsafe:
            raise WorkbookError(f"Test Case ID {unsafe[0]!r} cannot be used: it must not start with '.' "
                                "or contain any of / \\ : * ? \" < > |")
    except WorkbookError as exc:
        tmp_path.unlink(missing_ok=True)
        log.warning("Upload of %s refused: %s", filename, exc)
        raise HTTPException(400, str(exc))
    suite = {"id": storage.new_id("suite"), "filename": filename, "uploaded_at": storage.now_iso(),
             "created_by": _user(request), "warnings": warnings, "cases": cases}
    storage.suite_dir(suite["id"]).mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp_path), storage.workbook_path(suite["id"]))
    storage.save_suite(suite)
    log.info("Uploaded %s as %s: %d case(s)%s", filename, suite["id"], len(cases),
             "".join(f"\n  note: {w}" for w in warnings))
    audit.record(_user(request), "upload", _ip(request), suite_id=suite["id"], file=filename,
                 cases=[c["id"] for c in cases])
    return {"suite_id": suite["id"], "cases": [c["id"] for c in cases], "warnings": warnings}


@app.post("/api/freeform")
def create_freeform(req: FreeformRequest, request: Request):
    """A test without an Excel file: the steps are created while the user records."""
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter the start URL of the application to test, beginning with http:// or https://")
    case_id = f"REC-{datetime.now():%Y%m%d-%H%M%S}"
    title = req.title.strip() or f"Recorded test {datetime.now():%d %b %H:%M}"
    case = {"id": case_id, "sheet": case_id, "title": title, "description": f"Recorded without Excel on {url}",
            "steps": [], "freeform": True, "start_url": url}
    suite = {"id": storage.new_id("suite"), "filename": "Recorded without Excel", "source": "recording",
             "uploaded_at": storage.now_iso(), "created_by": _user(request), "warnings": [], "cases": [case]}
    storage.save_suite(suite)
    build_workbook(storage.workbook_path(suite["id"]), case)
    audit.record(_user(request), "create_test", _ip(request), suite_id=suite["id"], case_id=case_id,
                 title=title, url=url)
    return {"suite_id": suite["id"], "case_id": case_id}


@app.get("/api/suites/{suite_id}/workbook")
def suite_workbook(suite_id: str, request: Request):
    """The test script as Excel: the uploaded file, or the one written for a recorded test."""
    suite = storage.load_suite(suite_id) if storage.safe_id(suite_id) else None
    path = storage.workbook_path(suite_id)
    if not suite or not _can_see(request, suite) or not path.exists():
        raise HTTPException(404, "Workbook not found")
    name = suite["filename"] if suite.get("source") != "recording" else f"{suite['cases'][0]['id']}.xlsx"
    return FileResponse(path, filename=name)


@app.get("/api/suites/{suite_id}/cases/{case_id}")
def case_detail(suite_id: str, case_id: str, request: Request):
    suite, case = _suite_case(request, suite_id, case_id)
    target = case_target(case)
    return {
        "suite": {k: suite.get(k) for k in ("id", "filename", "uploaded_at", "created_by", "warnings", "source")},
        "case": case,
        "target": {"url": target, "has_profile": bool(target and signed_in_at(target)),
                   "signed_in_at": signed_in_at(target) if target else None},
        "recording": diagnostics.mask_recording(storage.load_recording(suite_id, case_id)),
        "runs": storage.list_runs(suite_id, case_id),
    }


@app.delete("/api/suites/{suite_id}/cases/{case_id}")
def delete_case(suite_id: str, case_id: str, request: Request):
    suite, case = _suite_case(request, suite_id, case_id)
    s = sessions.active()
    if s and s.suite_id == suite["id"] and s.case_id == case["id"]:
        raise HTTPException(409, "This test case is being recorded or run. Stop it before deleting.")
    storage.delete_case(suite, case["id"])
    log.info("Deleted test case %s from %s (its session logs are kept)", case["id"], suite["id"])
    audit.record(_user(request), "delete_case", _ip(request), suite_id=suite["id"], case_id=case["id"],
                 title=case["title"], created_by=suite.get("created_by"))
    return {"ok": True}


@app.post("/api/suites/{suite_id}/cases/{case_id}/signin")
def start_signin(suite_id: str, case_id: str, request: Request):
    suite, case = _suite_case(request, suite_id, case_id)
    _need_target(case)
    s = _start("signin", signin_session, suite_id=suite_id, case_id=case_id, user=_user(request),
               suite=suite, case=case)
    audit.record(_user(request), "signin_start", _ip(request), suite_id=suite_id, case_id=case_id,
                 url=case_target(case), session=s.id)
    return s.info()


@app.post("/api/suites/{suite_id}/cases/{case_id}/record")
def start_recording(suite_id: str, case_id: str, request: Request):
    suite, case = _suite_case(request, suite_id, case_id)
    _need_target(case)
    replaces = storage.load_recording(suite_id, case_id)
    s = _start("record", record_session, suite_id=suite_id, case_id=case_id, user=_user(request),
               suite=suite, case=case)
    audit.record(_user(request), "record_start", _ip(request), suite_id=suite_id, case_id=case_id, session=s.id,
                 replaces_recording_by=replaces.get("recorded_by") if replaces else None)
    return s.info()


@app.post("/api/suites/{suite_id}/cases/{case_id}/run")
def start_run(suite_id: str, case_id: str, req: RunRequest, request: Request):
    suite, case = _suite_case(request, suite_id, case_id)
    recording = storage.load_recording(suite_id, case_id)
    if not recording:
        raise HTTPException(400, "Record this test case before running it.")
    run_id = storage.new_id("run")
    s = _start("run", run_session, suite_id=suite_id, case_id=case_id, run_id=run_id, user=_user(request),
               suite=suite, case=case, recording=recording, data=req.data, headed=req.headed,
               slow_mo=max(0, min(req.slow_mo, 2000)))
    excel = {}
    for step in case["steps"]:
        for k, v in step["data"].items():
            excel.setdefault(k, v)
    # Names only: values can be passwords.
    audit.record(_user(request), "run_start", _ip(request), suite_id=suite_id, case_id=case_id, run_id=run_id,
                 changed_data=sorted(k for k, v in req.data.items() if excel.get(k) != v))
    return s.info()


@app.post("/api/sessions/{session_id}/stop")
def stop(session_id: str, request: Request):
    s = _session(request, session_id)
    if s.status == "running" and not s.stop_event.is_set():
        s.stopped_by = _user(request)
        s.log.emit("info", f"Stop requested by {s.stopped_by}.")
        audit.record(s.stopped_by, "stop", _ip(request), kind=s.kind, suite_id=s.suite_id, case_id=s.case_id,
                     run_id=s.run_id, started_by=s.user)
    s.stop_event.set()
    return s.info()


@app.get("/api/sessions/{session_id}/events")
def events(session_id: str, request: Request, after: int = 0):
    s = _session(request, session_id)
    last_id = request.headers.get("last-event-id")  # EventSource reconnect: resume, don't repeat
    if last_id and last_id.isdigit():
        after = int(last_id) + 1
    return StreamingResponse(s.log.stream(after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, request: Request):
    return _load_run(request, run_id)


@app.get("/api/runs/{run_id}/results")
def run_results(run_id: str, request: Request):
    run = _load_run(request, run_id)
    path = storage.run_dir(run_id) / "results.xlsx"
    if not path.exists():
        raise HTTPException(404, "Results workbook not found")
    return FileResponse(path, filename=f"{run['case_id']}_{run_id}_results.xlsx")


def _download(path: Path, name: str) -> FileResponse:
    return FileResponse(path, filename=name, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, path.parent, ignore_errors=True))


@app.get("/api/runs/{run_id}/issue-log")
def run_issue_log(run_id: str, request: Request):
    """One run's issue log as a ZIP: report, screenshots, page snapshots, trace, test case and recording."""
    _load_run(request, run_id)
    audit.record(_user(request), "export_issue_log", _ip(request), run_id=run_id)
    # The studio's own log covers everyone's activity: admins only.
    return _download(*diagnostics.export_issue_log([run_id], app_log=_is_admin(request)))


@app.get("/api/issue-log")
def issue_log(request: Request, include_passed: bool = False):
    """This device's issue log as a ZIP: every run that did not pass (or every run), all session logs
    and the studio's own log, with an Excel summary."""
    audit.record(_user(request), "export_issue_log", _ip(request), scope="all", include_passed=include_passed)
    if _is_admin(request):
        return _download(*diagnostics.export_issue_log(None, include_passed))
    own = {(suite["id"], c["id"]) for suite in storage.list_suites() if _can_see(request, suite) for c in suite["cases"]}
    return _download(*diagnostics.export_issue_log(None, include_passed, only_cases=own, app_log=False))


@app.get("/artifacts/runs/{run_id}/{path:path}", include_in_schema=False)
def artifact(run_id: str, path: str, request: Request):
    """A run's screenshots, for whoever may see the run."""
    _load_run(request, run_id)
    root = storage.run_dir(run_id).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.name == "run.json":
        raise HTTPException(404, "Not found")
    return FileResponse(target)


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


# ---- live view: noVNC's page and its WebSocket, behind the same sign-in as the dashboard.
# The VNC server listens inside the container only; this relays the browser's WebSocket to it.
@app.websocket("/vnc/websockify")
async def live_view_socket(ws: WebSocket):
    user = auth.user_from_token(ws.cookies.get(auth.COOKIE)) if LOGIN else "local"
    if not user or not _same_origin(ws.headers):
        await ws.close(code=1008)
        return
    offered = ws.scope.get("subprotocols") or []
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
    except OSError:
        await ws.close(code=1011)
        return
    await ws.accept(subprotocol="binary" if "binary" in offered else None)
    log.info("Live view opened by %s from %s", user, _ip(ws))

    async def to_vnc():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            writer.write(msg.get("bytes") or (msg.get("text") or "").encode())
            await writer.drain()

    async def to_browser():
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(to_vnc()), asyncio.create_task(to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass  # already closed by the browser


# Mounted after the WebSocket route so /vnc/websockify reaches it.
if NOVNC_DIR.is_dir():
    app.mount("/vnc", StaticFiles(directory=NOVNC_DIR), name="vnc")
