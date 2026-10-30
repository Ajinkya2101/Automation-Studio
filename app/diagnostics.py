"""Issue log: what is kept about each run and session, and the export of it.

The studio runs on several testers' devices. Everything needed to investigate a
problem away from the device that had it is kept here: which device and versions,
the full error with Playwright's own log, the page at the moment of failure, the
browser's console and network errors, and a Playwright trace. The export packs
it into one ZIP with an Excel summary (issues.xlsx) on top.

Passwords never leave the device: test data whose key mentions "pass" and values
masked while recording are replaced with dots in everything exported. Screenshots,
page snapshots, traces and results.xlsx show the application as it was, so they
carry whatever the test typed or the page displayed.
"""
import getpass
import html
import json
import logging
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import zipfile
from collections import deque
from datetime import datetime
from importlib import metadata
from logging.handlers import RotatingFileHandler
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

from . import storage
from .actions import MASK
from .config import (ACTION_TIMEOUT_MS, BASE_DIR, DATA_DIR, DEVICE_NAME, IN_CONTAINER, LOGS_DIR, OS_USER,
                     SCREEN, SLOW_FACTOR, TRACE)

SESSIONS_DIR = LOGS_DIR / "sessions"
APP_LOG = LOGS_DIR / "app.log"
log = logging.getLogger("studio")


# ---------------------------------------------------------------- masking
def is_secret(key: str) -> bool:
    return "pass" in (key or "").lower()


def mask_data(data: dict) -> dict:
    return {k: (MASK if is_secret(k) and v else v) for k, v in (data or {}).items()}


def mask_recording(recording: dict | None) -> dict | None:
    """Values typed into password fields while recording are stored as typed; show them as dots."""
    if recording:
        for step in recording["steps"]:
            step["actions"] = [{**a, "value": MASK} if a.get("masked") and not a.get("param") else a
                               for a in step["actions"]]
    return recording


SECRET_LINE = re.compile(r"^(\s*[^:\n]*pass[^:\n]*:\s*)\S.*$", re.I | re.M)


def _masked_case(case: dict) -> dict:
    """The case with secret test data masked, both parsed and as written in the Excel cell."""
    steps = []
    for s in case.get("steps", []):
        s = {**s, "data": mask_data(s.get("data"))}
        if s.get("test_data_raw"):
            s["test_data_raw"] = SECRET_LINE.sub(lambda m: m.group(1) + MASK, s["test_data_raw"])
        steps.append(s)
    return {**case, "steps": steps}


# ---------------------------------------------------------------- device and environment
def device() -> dict:
    try:
        user = OS_USER or getpass.getuser()
    except Exception:
        user = ""
    return {"device": DEVICE_NAME or socket.gethostname(), "os_user": user}


_version: str | None = None


def app_version() -> str:
    """The studio's git commit, read from .git directly so it also works in the container (no git there)."""
    global _version
    if _version is None:
        _version = "unknown"
        try:
            git = BASE_DIR / ".git"
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
            sha = head
            if head.startswith("ref: "):
                ref = head[5:]
                path = git / ref
                if path.exists():
                    sha = path.read_text(encoding="utf-8").strip()
                else:  # refs packed by git gc
                    packed = (git / "packed-refs").read_text(encoding="utf-8")
                    sha = next(line.split()[0] for line in packed.splitlines() if line.endswith(" " + ref))
                sha = f"{ref.rsplit('/', 1)[-1]}@{sha[:10]}"
            else:
                sha = sha[:10]
            _version = sha
            if shutil.which("git"):
                dirty = subprocess.run(["git", "-C", str(BASE_DIR), "status", "--porcelain", "--untracked-files=no"],
                                       capture_output=True, text=True, timeout=5).stdout.strip()
                if dirty:
                    _version += " (with local changes)"
        except Exception:
            pass
    return _version


def _package(name: str) -> str:
    try:
        return metadata.version(name)
    except Exception:
        return ""


def environment() -> dict:
    """The device and software a run or session happened on."""
    return {
        **device(),
        "app_version": app_version(),
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "packages": {n: _package(n) for n in ("playwright", "fastapi", "uvicorn", "openpyxl")},
        "in_container": IN_CONTAINER,
        "screen": SCREEN if IN_CONTAINER else "",
        "timezone": time.strftime("%Z (UTC%z)"),
        "slow_factor": SLOW_FACTOR,
        "action_timeout_ms": ACTION_TIMEOUT_MS,
        "trace": TRACE,
        "data_dir": str(DATA_DIR),
    }


BROWSER_JS = """() => ({user_agent: navigator.userAgent, language: navigator.language,
  window: innerWidth + 'x' + innerHeight, screen: screen.width + 'x' + screen.height,
  pixel_ratio: devicePixelRatio})"""


def browser_info(page) -> dict:
    try:
        return page.evaluate(BROWSER_JS)
    except Exception:
        return {}


# ---------------------------------------------------------------- application log
def setup_logging() -> None:
    """data/logs/app.log: sessions, crashes and server errors, kept across restarts (rotates at 2 MB)."""
    if log.handlers:
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(APP_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    env = environment()
    log.info("Studio started on %s (user %s), version %s, %s, Python %s, Playwright %s",
             env["device"], env["os_user"], env["app_version"], env["os"], env["python"],
             env["packages"]["playwright"])


# ---------------------------------------------------------------- what happened in the browser
class BrowserLog:
    """Console errors, script errors and failed requests from every tab of a browser context."""
    KEEP = 2000

    def __init__(self, ctx):
        self.entries: deque = deque(maxlen=self.KEEP)
        ctx.on("page", self._page)
        ctx.on("requestfailed", self._failed)
        ctx.on("response", self._response)
        for pg in ctx.pages:
            self._page(pg)

    def _add(self, kind: str, text: str, **extra) -> None:
        self.entries.append({"ts": time.time(), "kind": kind, "text": (text or "")[:2000], **extra})

    def _page(self, pg) -> None:
        pg.on("console", self._console)
        pg.on("pageerror", lambda err: self._add("script error", getattr(err, "message", "") or str(err),
                                                 stack=(getattr(err, "stack", "") or "")[:4000], url=pg.url[:500]))
        pg.on("crash", lambda _pg: self._add("tab crashed", "The browser tab crashed", url=pg.url[:500]))

    def _console(self, msg) -> None:
        if msg.type in ("error", "warning"):
            try:
                loc = msg.location or {}
            except Exception:
                loc = {}
            self._add(f"console {msg.type}", msg.text, url=(loc.get("url") or "")[:500], line=loc.get("lineNumber"))

    def _failed(self, req) -> None:
        self._add("request failed", f"{req.method} {req.url[:500]}: {req.failure or 'failed'}",
                  resource=req.resource_type)

    def _response(self, resp) -> None:
        if resp.status >= 400:
            self._add("http error", f"{resp.status} {resp.request.method} {resp.url[:500]}",
                      status=resp.status, resource=resp.request.resource_type)

    def since(self, ts: float) -> list[dict]:
        return [e for e in list(self.entries) if e["ts"] >= ts]

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(list(self.entries), indent=1, ensure_ascii=False), encoding="utf-8")


def capture_page(page, folder: Path, stem: str) -> dict:
    """The page as it was when a step ended badly: address, title, tabs, size and its HTML."""
    out = {}
    try:
        out["url"] = page.url
        out["title"] = page.title()
        out["tabs"] = [pg.url for pg in page.context.pages if not pg.is_closed()]
        out.update(browser_info(page))
    except Exception as exc:
        out["capture_error"] = str(exc)[:500]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        out["html"] = f"{folder.name}/{stem}.html"
    except Exception as exc:
        out["html_error"] = str(exc)[:500]
    return out


def error_details(exc: BaseException) -> dict:
    """The full error: Playwright's call log (what it waited for and why it gave up) and the traceback."""
    root = exc.__cause__ or exc
    return {"type": type(root).__name__, "raw": str(root)[:20000],
            "traceback": "".join(traceback.format_exception(exc))[-20000:]}


def start_trace(ctx, title: str) -> bool:
    if TRACE == "off":
        return False
    try:
        ctx.tracing.start(title=title, screenshots=True, snapshots=True, sources=False)
        return True
    except Exception as exc:
        log.warning("Could not start the Playwright trace: %s", exc)
        return False


# A password field with something typed in it, in this frame.
SECRET_JS = "() => Array.from(document.querySelectorAll('input[type=password]')).some(i => i.value)"


def secret_on_screen(ctx) -> bool:
    """A password is typed into a field in any tab or frame."""
    for pg in ctx.pages:
        for frame in pg.frames:
            try:
                if frame.evaluate(SECRET_JS):
                    return True
            except Exception:  # navigating or detached: no field to read
                continue
    return False


def pause_trace(ctx) -> bool:
    """Drop what the trace holds so far and stop adding to it.

    A trace keeps whatever the run typed, in several places: the typing itself,
    copies of the page with the field's value, the sign-in request. So before a
    password is typed, the trace so far is dropped and recording stops until no
    password is on screen (resume_trace). The trace then starts after the sign-in.
    """
    try:
        ctx.tracing.stop_chunk()
        return True
    except Exception as exc:
        log.warning("Could not pause the Playwright trace: %s", exc)
        return False


def resume_trace(ctx) -> bool:
    try:
        ctx.tracing.start_chunk()
        return True
    except Exception as exc:
        log.warning("Could not resume the Playwright trace: %s", exc)
        return False


def scrub_trace(path: Path, secrets: list[str]) -> None:
    """Replace every copy of the secrets in a saved trace, as typed, JSON-escaped or URL-encoded.

    The last line of defence after pause_trace: a request the page sent while the
    trace was paused is still written to it once its answer arrives, which can be
    after the trace resumed (the sign-in request, with the password in its body).
    """
    forms = set()
    for s in secrets:
        if len(s) >= 4:  # shorter strings occur everywhere by chance
            forms.update({s, json.dumps(s)[1:-1], urllib.parse.quote(s, safe=""), urllib.parse.quote_plus(s),
                          html.escape(s)})
    forms = sorted((f.encode("utf-8") for f in forms), key=len, reverse=True)
    if not forms:
        return
    with zipfile.ZipFile(path) as z:
        entries = [(info, z.read(info)) for info in z.infolist()]
    if not any(f in data for _, data in entries for f in forms):
        return
    tmp = path.with_suffix(".scrub")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in entries:
            for f in forms:
                data = data.replace(f, b"******")
            out.writestr(info, data)
    tmp.replace(path)


def stop_trace(ctx, path: Path | None, paused: bool = False, secrets: list[str] | None = None) -> bool:
    """Save the trace to path, or drop it when path is None. True when a trace file was written."""
    if paused:
        resume_trace(ctx)  # tracing only stops from a running chunk; this one stays empty
    try:
        if path and TRACE in ("failures", "always"):
            ctx.tracing.stop(path=str(path))
            scrub_trace(path, secrets or [])
            return True
        ctx.tracing.stop()
    except Exception as exc:
        log.warning("Could not save the Playwright trace: %s", exc)
    return False


# ---------------------------------------------------------------- session logs
def save_session_log(session) -> None:
    """Every sign-in, recording and run leaves a log, including how it crashed if it did."""
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        info = session.info()
        doc = {**info, "started_at": datetime.fromtimestamp(session.started_at).astimezone().isoformat(timespec="seconds"),
               "finished_at": storage.now_iso(), "error": session.error, "environment": environment(),
               "events": session.log.all()}
        name = f"{datetime.fromtimestamp(session.started_at):%Y%m%d-%H%M%S}_{session.kind}_{session.id}.json"
        (SESSIONS_DIR / name).write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        if session.run_id:
            _close_crashed_run(session)
    except Exception:
        log.exception("Could not save the log of session %s", session.id)


def _close_crashed_run(session) -> None:
    """A run that crashed (e.g. the browser would not start) still gets a result and its log."""
    run = storage.load_run(session.run_id)
    if not run or run.get("status") != "running":
        return
    run.update(status="failed", finished_at=storage.now_iso(), error=session.error or "The run stopped unexpectedly.",
               events=session.log.all())
    for s in run["steps"]:
        if s["status"] in ("pending", "running"):
            s.update(status="skipped", observed="Not run (the run stopped unexpectedly).")
    storage.save_run(run)


def list_session_logs(case_ids: set[tuple[str, str]] | None = None) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(SESSIONS_DIR.glob("*.json")) if SESSIONS_DIR.exists() else []:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if case_ids is None or (doc.get("suite_id"), doc.get("case_id")) in case_ids:
            out.append((p, doc))
    return out


# ---------------------------------------------------------------- export
def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s or "").strip("-")[:60] or "device"


def _ts(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.") + f"{int((t % 1) * 1000):03d}" if t else ""


def _indent(text: str, n: int = 6) -> str:
    return "\n".join(" " * n + line for line in (text or "").splitlines())


def _event_line(ev: dict) -> str:
    extra = [x for x in (f"{ev['ms']}ms" if ev.get("ms") is not None and ev["type"] == "action" else "",
                         ev.get("locator") or "", ev.get("note") or "") if x]
    return (f"{_ts(ev.get('ts'))}  {(ev.get('level') or 'info').upper():7} {ev.get('message', '')}"
            + (f"  ({' · '.join(extra)})" if extra else ""))


def run_log_text(run: dict) -> str:
    """A run as a readable report: where it ran, each step, and every failure in full."""
    env = run.get("environment") or {}
    b = env.get("browser") or {}
    lines = [f"Run {run['id']} · {run['case_id']} · {run.get('title', '')}",
             f"Status: {run['status'].upper()} ({run.get('passed', 0)} passed, {run.get('failed', 0)} failed, "
             f"{run.get('skipped', 0)} skipped)",
             f"Started {run.get('started_at')} · finished {run.get('finished_at')} · "
             f"{(run.get('duration_ms') or 0) / 1000:.1f}s",
             f"Device: {env.get('device', '?')} (user {env.get('os_user', '?')}) · studio {env.get('app_version', '?')}",
             f"System: {env.get('os', '?')} · Python {env.get('python', '?')} · "
             f"Playwright {(env.get('packages') or {}).get('playwright', '?')}"
             + (" · in container" if env.get("in_container") else ""),
             f"Browser: {b.get('user_agent', '?')} · window {b.get('window', '?')} · screen {b.get('screen', '?')}",
             f"Settings: {'shown' if run.get('headed') else 'hidden'} browser, slow factor {env.get('slow_factor', '?')}, "
             f"wait limit {env.get('action_timeout_ms', '?')} ms"]
    if run.get("error"):
        lines += ["", "THE RUN STOPPED UNEXPECTEDLY:", _indent(run["error"], 2)]
    if run.get("test_data"):
        lines += ["", "Test data used" + (f" (changed in the run form: {', '.join(run['data_overrides'])})"
                                         if run.get("data_overrides") else "") + ":"]
        lines += [f"  {k} = {v}" for k, v in run["test_data"].items()]
    lines += ["", "Steps:"]
    for i, s in enumerate(run["steps"], start=1):
        lines.append(f"  {s['step_id']:<16} {s['status'].upper():<8} {(s.get('duration_ms') or 0) / 1000:6.1f}s  {s['name']}")
        d = s.get("diagnostics")
        if s.get("error"):
            lines.append(f"      Error: {s['error']}")
        if d:
            pg = d.get("page") or {}
            lines.append(f"      Page: {pg.get('url', '?')}  \"{pg.get('title', '')}\"")
            if len(pg.get("tabs") or []) > 1:
                lines.append(f"      Open tabs: {' | '.join(pg['tabs'])}")
            if s.get("screenshot"):
                lines.append(f"      Screenshot: screenshots/{s['screenshot'].rsplit('/', 1)[-1]}")
            if pg.get("html"):
                lines.append(f"      Page HTML: {pg['html']}")
            for e in d.get("browser_log") or []:
                lines.append(f"      Browser {e['kind']}: {e['text'][:400]}")
            err = d.get("error") or {}
            if err.get("raw"):
                lines += ["      Full error from Playwright:", _indent(err["raw"], 8)]
            if err.get("traceback"):
                lines += ["      Traceback:", _indent(err["traceback"], 8)]
    lines += ["", "Event log:"] + ["  " + _event_line(ev) for ev in run.get("events") or []]
    return "\n".join(lines) + "\n"


def _session_log_text(doc: dict) -> str:
    env = doc.get("environment") or {}
    lines = [f"{doc['kind'].capitalize()} session {doc['id']} · {doc.get('case_id')} · {doc.get('status', '').upper()}",
             f"Started {doc.get('started_at')} · finished {doc.get('finished_at')}",
             f"Device: {env.get('device', '?')} (user {env.get('os_user', '?')}) · studio {env.get('app_version', '?')}"]
    if doc.get("error"):
        lines += ["", "CRASHED:", _indent(doc["error"], 2)]
    lines += ["", "Event log:"] + ["  " + _event_line(ev) for ev in doc.get("events") or []]
    return "\n".join(lines) + "\n"


HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
SEVERITY_FILL = {"High": PatternFill("solid", fgColor="FFC7CE"), "Medium": PatternFill("solid", fgColor="FFEB9C"),
                 "Low": PatternFill("solid", fgColor="EDEDED")}


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list], widths: dict[int, int] | None = None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD_FILL
        c.alignment = Alignment(vertical="center")
    for row in rows:
        ws.append([v if not isinstance(v, tuple) else v[0] for v in row])
        r = ws.max_row
        for i, v in enumerate(row, start=1):
            cell = ws.cell(r, i)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if isinstance(v, tuple) and v[1]:  # (text, relative link inside the export)
                cell.hyperlink = v[1]
                cell.font = Font(color="0563C1", underline="single")
    for i, h in enumerate(headers, start=1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = (widths or {}).get(i, max(12, min(len(h) + 4, 30)))
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    return ws


def _issue_rows(runs: list[dict], sessions: list[dict], cases: dict) -> list[list]:
    """One row per problem: failed or stopped steps, crashes, fallback locators, recording warnings."""
    rows = []

    def row(when, env, kind, severity, case_id, run_id="", step_id="", step_name="", message="", app_msg="",
            page=None, browser="", shot="", html="", trace="", folder=""):
        page = page or {}
        rows.append([when, env.get("device", ""), env.get("os_user", ""), kind, severity, case_id,
                     cases.get(case_id, {}).get("title", ""), run_id, step_id, step_name, message, app_msg,
                     page.get("url", ""), page.get("title", ""), browser,
                     (shot and "Screenshot", shot), (html and "Page HTML", html), (trace and "Trace", trace),
                     (folder and "Open", folder)])

    for r in runs:
        env, folder = r.get("environment") or {}, f"runs/{r['id']}/"
        trace = f"{folder}trace.zip" if r.get("trace") else ""
        if r.get("error"):
            row(r.get("finished_at") or r["started_at"], env, "Run crashed", "High", r["case_id"], r["id"],
                message=r["error"].strip().splitlines()[-1][:1000], trace=trace, folder=f"{folder}log.txt")
        for s in r["steps"]:
            if s["status"] not in ("failed", "stopped"):
                continue
            d = s.get("diagnostics") or {}
            pg = d.get("page") or {}
            blog = d.get("browser_log") or []
            browser = "\n".join(f"{e['kind']}: {e['text'][:300]}" for e in blog[:8]) + (
                f"\n… {len(blog) - 8} more in log.txt" if len(blog) > 8 else "")
            shot = f"{folder}screenshots/{s['screenshot'].rsplit('/', 1)[-1]}" if s.get("screenshot") else ""
            row(r.get("finished_at") or r["started_at"], env,
                "Step failed" if s["status"] == "failed" else "Run stopped by tester",
                "High" if s["status"] == "failed" else "Medium", r["case_id"], r["id"], s["step_id"], s["name"],
                s.get("error") or "", s.get("app_message") or "", pg, browser, shot,
                f"{folder}{pg['html']}" if pg.get("html") else "", trace, f"{folder}log.txt")
        for ev in r.get("events") or []:
            if ev.get("type") == "action" and ev.get("note"):
                row(_ts(ev.get("ts")), env, "Fallback locator used", "Low", r["case_id"], r["id"], ev.get("step_id", ""),
                    message=f"{ev['message']}: {ev['note']} ({ev.get('locator', '')}). The recorded element was not "
                    "found the first way; the page may differ on this device or have changed since recording.",
                    folder=f"{folder}log.txt")
            elif ev.get("type") == "warning":
                row(_ts(ev.get("ts")), env, "Run warning", "Low", r["case_id"], r["id"], message=ev["message"],
                    folder=f"{folder}log.txt")
    for doc in sessions:
        if doc.get("error") and doc["kind"] != "run":
            row(doc.get("finished_at", ""), doc.get("environment") or {}, f"{doc['kind'].capitalize()} crashed", "High",
                doc.get("case_id", ""), message=doc["error"].strip().splitlines()[-1][:1000],
                folder=f"sessions/{doc['_file']}.txt")
    for case_id, c in cases.items():
        for w in (c.get("recording") or {}).get("warnings") or []:
            row(c["recording"].get("recorded_at", ""), {}, "Recording warning", "Medium", case_id, message=w,
                folder=f"cases/{c['folder']}/recording.json")
    order = {"High": 0, "Medium": 1, "Low": 2}
    rows.sort(key=lambda x: (order[x[4]], str(x[0])), reverse=False)
    return rows


ISSUE_HEADERS = ["When", "Device", "OS user", "Kind", "Severity", "Test case", "Title", "Run", "Step", "Step name",
                 "Error / message", "Application message", "Page URL", "Page title", "Browser errors during step",
                 "Screenshot", "Page HTML", "Trace", "Full log"]


def _issues_workbook(path: Path, runs: list[dict], sessions: list[dict], cases: dict, scope: str) -> int:
    wb = Workbook()
    wb.remove(wb.active)
    issues = _issue_rows(runs, sessions, cases)
    ws = _sheet(wb, "Issues", ISSUE_HEADERS, issues,
                {1: 20, 4: 20, 6: 14, 7: 28, 8: 26, 9: 16, 10: 24, 11: 70, 12: 40, 13: 45, 14: 28, 15: 60})
    for r in range(2, ws.max_row + 1):
        sev = ws.cell(r, 5)
        if sev.value in SEVERITY_FILL:
            sev.fill = SEVERITY_FILL[sev.value]
    _sheet(wb, "Runs", ["Run", "Device", "OS user", "Test case", "Title", "Started", "Finished", "Status", "Passed",
                        "Failed", "Skipped", "Duration (s)", "Browser shown", "Data changed in run form",
                        "Studio version", "Browser", "Log"],
           [[r["id"], (r.get("environment") or {}).get("device", ""), (r.get("environment") or {}).get("os_user", ""),
             r["case_id"], r.get("title", ""), r["started_at"], r.get("finished_at") or "", r["status"],
             r.get("passed", 0), r.get("failed", 0), r.get("skipped", 0), round((r.get("duration_ms") or 0) / 1000, 1),
             "yes" if r.get("headed") else "no", ", ".join(r.get("data_overrides") or []),
             (r.get("environment") or {}).get("app_version", ""),
             ((r.get("environment") or {}).get("browser") or {}).get("user_agent", ""),
             ("log.txt", f"runs/{r['id']}/log.txt")] for r in runs],
           {1: 26, 5: 28, 6: 22, 7: 22, 16: 50})
    _sheet(wb, "Sessions", ["Started", "Kind", "Device", "OS user", "Test case", "Status", "Error", "Log"],
           [[d.get("started_at", ""), d["kind"], (d.get("environment") or {}).get("device", ""),
             (d.get("environment") or {}).get("os_user", ""), d.get("case_id", ""), d.get("status", ""),
             (d.get("error") or "").strip().splitlines()[-1:][0] if d.get("error") else "",
             ("log", f"sessions/{d['_file']}.txt")] for d in sessions],
           {1: 22, 7: 70})
    env = environment()
    env_rows = [[k, json.dumps(v) if isinstance(v, (dict, list)) else str(v)] for k, v in env.items()]
    _sheet(wb, "This export", ["Item", "Value"],
           [["Exported at", storage.now_iso()], ["Scope", scope]] + env_rows, {1: 22, 2: 80})
    wb.save(path)
    return len(issues)


README = """Automation Studio issue log
===========================

Exported {exported} from device {device} (user {user}), studio version {version}.
Scope: {scope}.

Start with issues.xlsx: one row per problem (failed or stopped steps, crashes,
fallback locators, recording warnings), with links to the screenshot, the page
HTML, the Playwright trace and the full log of each. Keep the folder unzipped
so the links work.

  issues.xlsx                 summary: Issues, Runs, Sessions and this device's setup
  environment.json            this device: OS, Python, package versions, settings
  app.log                     the studio's own log on this device (starts, crashes, server errors)
  runs/<run>/log.txt          the run as a readable report, each failure in full
  runs/<run>/run.json         the same as data, with every event
  runs/<run>/screenshots/     the screen after each step
  runs/<run>/failure/         the page's HTML when a step failed or was stopped
  runs/<run>/browser_log.json console errors, script errors and failed requests during the run
  runs/<run>/trace.zip        Playwright trace: every action with the page before and after,
                              network and console. Open it with
                                  playwright show-trace trace.zip
                              or drag it onto https://trace.playwright.dev (it stays in your browser)
  runs/<run>/results.xlsx     the test script with the results written in
  cases/<case>/               the test case and its recording, as they were on this device
  sessions/                   each sign-in, recording and run session, including crashes

Passwords are masked in the JSON and text files. Screenshots, page HTML, traces and
results.xlsx show the application as it was, including anything the test typed.
"""


def export_issue_log(run_ids: list[str] | None = None, include_passed: bool = False) -> tuple[Path, str]:
    """Pack the issue log into a ZIP. run_ids None means every run on this device.

    Returns the temporary ZIP and the file name to download it as.
    """
    all_runs = storage.list_runs()
    if run_ids is not None:
        chosen = [r for r in all_runs if r["id"] in run_ids]
        scope = "run " + ", ".join(run_ids)
    else:
        chosen = [r for r in all_runs if include_passed or r["status"] != "passed"]
        scope = "all runs on this device" if include_passed else "every run on this device that did not pass"
    runs = [storage.load_run(r["id"]) for r in chosen]
    runs = [r for r in runs if r]
    if run_ids is not None:
        keys = {(r["suite_id"], r["case_id"]) for r in runs}
        session_docs = list_session_logs(keys)
    else:
        session_docs = list_session_logs()
        keys = {(r["suite_id"], r["case_id"]) for r in runs} | {
            (d.get("suite_id"), d.get("case_id")) for _, d in session_docs}

    dev = device()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"issue-log_{_slug(dev['device'])}_{runs[0]['id'] if run_ids and len(runs) == 1 else stamp}"
    tmp = Path(tempfile.mkdtemp(prefix="studio-export-"))
    out = tmp / f"{name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def put(arc: str, path: Path) -> None:
            stored = path.suffix.lower() in (".png", ".jpg", ".zip", ".xlsx")
            z.write(path, f"{name}/{arc}", compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)

        def text(arc: str, content: str) -> None:
            z.writestr(f"{name}/{arc}", content)

        for r in runs:
            rdir = storage.run_dir(r["id"])
            r["test_data"] = mask_data(r.get("test_data"))
            r["trace"] = (rdir / "trace.zip").exists()
            for f in sorted(rdir.rglob("*")):
                if f.is_file() and f.name != "run.json" and f.suffix != ".tmp":
                    put(f"runs/{r['id']}/{f.relative_to(rdir).as_posix()}", f)
            text(f"runs/{r['id']}/run.json", json.dumps(r, indent=1, ensure_ascii=False, default=str))
            text(f"runs/{r['id']}/log.txt", run_log_text(r))

        cases = {}
        for suite_id, case_id in sorted(k for k in keys if all(k)):
            suite = storage.load_suite(suite_id)
            case = storage.find_case(suite, case_id) if suite else None
            if not case:
                continue  # deleted since; its runs and session logs are still exported
            folder = f"{_slug(case_id)}_{suite_id}"
            recording = mask_recording(storage.load_recording(suite_id, case_id))
            cases[case_id] = {"title": case["title"], "recording": recording, "folder": folder}
            text(f"cases/{folder}/case.json", json.dumps(
                {"suite": {k: suite.get(k) for k in ("id", "filename", "uploaded_at", "warnings", "source")},
                 "case": _masked_case(case)}, indent=1, ensure_ascii=False))
            if recording:
                text(f"cases/{folder}/recording.json", json.dumps(recording, indent=1, ensure_ascii=False))

        sessions = []
        for path, doc in session_docs:
            doc["_file"] = path.stem
            sessions.append(doc)
            text(f"sessions/{path.name}", json.dumps({k: v for k, v in doc.items() if k != "_file"},
                                                    indent=1, ensure_ascii=False, default=str))
            text(f"sessions/{path.stem}.txt", _session_log_text(doc))

        for f in sorted(LOGS_DIR.glob("app.log*")) if LOGS_DIR.exists() else []:
            put(f.name, f)
        text("environment.json", json.dumps(environment(), indent=1))
        xlsx = tmp / "issues.xlsx"
        count = _issues_workbook(xlsx, runs, sessions, cases, scope)
        put("issues.xlsx", xlsx)
        text("README.txt", README.format(exported=storage.now_iso(), device=dev["device"], user=dev["os_user"],
                                         version=app_version(), scope=scope))
    log.info("Issue log exported (%s): %d run(s), %d session(s), %d issue(s)", scope, len(runs), len(sessions), count)
    return out, f"{name}.zip"
