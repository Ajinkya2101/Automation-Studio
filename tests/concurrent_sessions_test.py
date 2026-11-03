"""Checks that several users record and run at the same time without colliding.

1. The session rules, without a browser: one session per user, a test case being
   recorded is left alone, each shown browser gets a screen nobody else has, and
   the limits refuse a session instead of sharing.
2. Two users' runs of a slow page at the same time, each with its own data: both
   pass, they overlap, each sees only its own values, and each uses its own
   browser profile.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\concurrent_sessions_test.py
Uses port 8774 and a temporary data folder.
"""
import asyncio
import copy
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-concurrent-"))
# These tests drive the API without signing in (allowed only on 127.0.0.1).
os.environ.setdefault("STUDIO_LOGIN", "off")
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from app import storage  # noqa: E402
from app.browser import profile_dir  # noqa: E402
from app.events import BusyError, SessionManager, sessions  # noqa: E402
from app.main import app  # noqa: E402
from app.replayer import run_session  # noqa: E402

PORT = 8774
BASE = f"http://127.0.0.1:{PORT}"

PAGE = """<!doctype html><html><body style="font-family:sans-serif;padding:20px">
<label for="who">Customer</label> <input id="who"> <button id="go">Save</button>
<div id="msg"></div>
<script>
  document.getElementById('go').onclick = () =>
    fetch('/concurrent/save?who=' + encodeURIComponent(document.getElementById('who').value))
      .then(r => r.json()).then(who => { document.getElementById('msg').textContent = 'Saved for ' + who; });
</script></body></html>"""


@app.get("/concurrent", include_in_schema=False)
def concurrent_page():
    return HTMLResponse(PAGE)


@app.get("/concurrent/save", include_in_schema=False)
def concurrent_save(who: str = ""):
    time.sleep(4)  # long enough for the two runs to overlap
    return JSONResponse(who)


ACTIONS = [
    {"type": "goto", "url": f"{BASE}/concurrent"},
    {"type": "fill", "value": "${Customer}", "param": "Customer", "target": 'field "Customer"',
     "locators": [{"kind": "label", "value": "Customer"}]},
    {"type": "click", "target": 'button "Save"', "locators": [{"kind": "role", "role": "button", "value": "Save"}]},
    {"type": "expect_text", "text": "Saved for ${Customer}"},
]


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


def refused(fn) -> str:
    try:
        fn()
    except BusyError as exc:
        return str(exc)
    return ""


def rules():
    print("session rules")
    mgr = SessionManager(screens=2, limit=3)
    release = threading.Event()

    def hold(_session, **_kw):
        release.wait(30)
        return "completed"

    def start(kind, user, case="C1", **kw):
        return mgr.start(kind, hold, suite_id="suite-1", case_id=case, user=user, **kw)

    a = start("run", "user01", headed=True)
    b = start("run", "user02", case="C2", headed=True)
    check(a.screen is not None and b.screen is not None and a.screen != b.screen
          and a.display != b.display and a.vnc_port != b.vnc_port,
          f"two shown runs at once, each on its own screen: {a.display}, {b.display}")
    check("already have a run" in refused(lambda: start("run", "user01", case="C3", headed=False)),
          "a user's second session at once is refused")
    check("live screens are in use" in refused(lambda: start("run", "user03", case="C3", headed=True)),
          "a third shown browser is refused when both screens are in use")
    c = start("run", "user03", case="C3", headed=False)
    check(c.screen is None and c.display is None, "a hidden run needs no screen")
    check("limit" in refused(lambda: start("run", "user04", case="C4", headed=False)),
          "the session limit is kept")
    check("in progress, started by user01" in refused(lambda: start("record", "admin", case="C1")),
          "a test case that is running is not recorded meanwhile")
    release.set()
    for s in (a, b, c):
        while s.status == "running":
            time.sleep(0.05)
    release.clear()
    d = start("record", "user04", case="C4")
    check(d.screen == 0, "a finished session's screen is used again")
    check("recording in progress, started by user04" in refused(lambda: start("run", "user05", case="C4", headed=False)),
          "a test case being recorded is not run by someone else meanwhile")
    release.set()

    check(mgr.active("user04") is d and mgr.active("user01") is None, "active() is per user")
    check(SessionManager(screens=0, limit=3).start("record", lambda s: "completed", suite_id="s", case_id="c",
                                                    user="u").screen is None,
          "outside the container no screens are handed out")


def side_by_side():
    print("two users' runs at the same time")
    created = []
    for user, customer in (("user01", "Winfield United"), ("user02", "Acme Farms")):
        suite_id = storage.new_id("suite")
        case = {"id": "TS_CONC", "sheet": "TS_CONC", "title": f"Save for {customer}", "start_url": f"{BASE}/concurrent",
                "steps": [{"step_id": "TS_CONC.1", "name": "Save", "data": {"Customer": customer}}]}
        suite = {"id": suite_id, "filename": "x.xlsx", "uploaded_at": storage.now_iso(), "created_by": user,
                 "warnings": [], "cases": [case]}
        storage.save_suite(suite)
        rec = {"suite_id": suite_id, "case_id": "TS_CONC", "start_url": f"{BASE}/concurrent", "status": "completed",
               "steps": [{"step_id": "TS_CONC.1", "name": "Save", "actions": copy.deepcopy(ACTIONS)}]}
        storage.save_recording(suite_id, "TS_CONC", rec)
        created.append((user, customer, suite, case, rec))

    runs = []
    for user, customer, suite, case, rec in created:
        run_id = storage.new_id("run")
        s = sessions.start("run", run_session, suite_id=suite["id"], case_id=case["id"], run_id=run_id, user=user,
                           suite=suite, case=case, recording=rec, headed=False)
        runs.append((user, customer, run_id, s))
    check(len(sessions.running()) == 2, "the second user's run started while the first one was running")
    for *_, s in runs:
        while s.status == "running":
            time.sleep(0.2)

    loaded = [(user, customer, storage.load_run(run_id)) for user, customer, run_id, _ in runs]
    for user, customer, run in loaded:
        step = run["steps"][0]
        check(run["status"] == "passed" and f"Saved for {customer}" in (step.get("observed") or ""),
              f"{user}'s run passed with their own data: {step.get('observed') or step.get('error')}")
    (_, _, first), (_, _, second) = loaded
    check(second["started_at"] < first["finished_at"], "the two runs overlapped")
    dirs = {profile_dir(BASE, user) for user, *_ in created}
    check(len(dirs) == 2 and all(d.exists() for d in dirs), f"each user ran in their own browser profile: "
          f"{sorted(str(d.relative_to(d.parents[2])) for d in dirs)}")
    check(profile_dir(BASE, "local") == profile_dir(BASE), "without the studio sign-in the profile stays where it was")


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    rules()
    side_by_side()
    print("\nAll concurrent-session checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
