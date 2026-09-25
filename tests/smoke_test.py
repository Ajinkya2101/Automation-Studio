"""End-to-end smoke test: upload -> record (simulated user) -> automated runs.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\smoke_test.py
Uses port 8765 so it does not clash with a running studio.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Keep test suites and runs out of the studio's real data folder.
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-smoke-"))

import openpyxl  # noqa: E402
import uvicorn  # noqa: E402

from app import storage  # noqa: E402
from app.config import SAMPLE_WORKBOOK  # noqa: E402
from app.events import sessions  # noqa: E402
from app.main import app  # noqa: E402
from app.recorder import record_session  # noqa: E402

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def http(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def upload(path: Path):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return http("POST", "/api/suites", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def wait_idle(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if not http("GET", "/api/status")["active"]:
            return
        time.sleep(0.5)
    raise TimeoutError("session did not finish")


def act_like_a_user(page):
    """What a tester would do in the recording window, including toolbar clicks."""
    def type_into(label, text):
        page.get_by_label(label, exact=True).click()
        page.keyboard.type(text, delay=5)

    def add_check(locator):
        page.locator("#check").click()
        locator.click()

    def next_step():
        page.locator("#next").click()
        page.wait_for_timeout(300)

    # Step 1: login
    type_into("User Name", "demo.user")
    type_into("Password", "demo123")
    page.get_by_role("button", name="Sign in").click()
    add_check(page.get_by_role("heading", name="Inbox"))
    next_step()
    # Step 2: compose
    page.get_by_role("link", name="Compose").click()
    add_check(page.get_by_role("heading", name="New message"))
    next_step()
    # Step 3: write
    type_into("To", "finance.team@acme.test")
    type_into("Subject", "Q3 inventory adjustment summary")
    type_into("Message", "Hi team, the Q3 inventory adjustments have been posted. Regards, Demo User")
    next_step()
    # Step 4: send
    page.get_by_role("button", name="Send").click()
    add_check(page.get_by_text("Message sent", exact=True))
    next_step()
    # Step 5: verify in Sent
    page.get_by_role("link", name="Sent", exact=True).click()
    add_check(page.locator(".subject", has_text="Q3 inventory adjustment summary"))
    page.locator("#next").click()  # "Finish recording"


def run_case(suite_id, case_id, data=None):
    info = http("POST", f"/api/suites/{suite_id}/cases/{case_id}/run",
                json.dumps({"headed": False, "reset_mailbox": True, "data": data or {}}).encode(),
                {"Content-Type": "application/json"})
    try:
        wait_idle(60)
    except TimeoutError:
        for ev in sessions.get(info["id"]).log.all():
            print("   log:", ev["type"], ev["message"][:400])
        raise
    if not storage.load_run(info["run_id"]):
        for ev in sessions.get(info["id"]).log.all():
            print("   log:", ev["type"], ev["message"][:400])
    return http("GET", f"/api/runs/{info['run_id']}")


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    up = upload(SAMPLE_WORKBOOK)
    suite_id, case_id = up["suite_id"], up["cases"][0]
    check(case_id == "TS_EML001" and not up["warnings"], f"upload parsed {case_id} with no warnings")

    suite = storage.load_suite(suite_id)
    case = storage.find_case(suite, case_id)
    rec_session = sessions.start("record", record_session, suite_id=suite_id, case_id=case_id, suite=suite, case=case,
                   start_url=f"{BASE}/mail/login", headless=True, on_ready=act_like_a_user)
    wait_idle()
    rec = storage.load_recording(suite_id, case_id)
    if not rec or rec["status"] != "completed":
        for ev in rec_session.log.all():
            print("   log:", ev["type"], ev["message"][:300])
    check(rec and rec["status"] == "completed", "recording completed")
    counts = [len(s["actions"]) for s in rec["steps"]]
    check(all(counts), f"every step has actions {counts}")
    params = sorted({a["param"] for s in rec["steps"] for a in s["actions"] if a.get("param")})
    check(params == ["Body", "Password", "Subject", "To", "User Name"], f"values linked to Excel data {params}")
    nav = [a["target"] for s in rec["steps"] for a in s["actions"] if a.get("navigates")]
    check(len(nav) >= 3, f"page-loading clicks detected: {nav}")

    run = run_case(suite_id, case_id)
    check(run["status"] == "passed", f"automated run passed ({run['passed']} steps, {run['duration_ms']}ms)")
    shots = list((storage.run_dir(run["id"]) / "screenshots").glob("*.png"))
    check(len(shots) == 5, f"{len(shots)} step screenshots saved")
    ws = openpyxl.load_workbook(storage.run_dir(run["id"]) / "results.xlsx")["TS_EML001"]
    statuses = [ws.cell(r, 8).value for r in range(11, 16)]
    check(statuses == ["Pass"] * 5, f"results workbook statuses {statuses}")
    check(ws.cell(5, 2).value == "Automation (prototype)", "tester name written back")

    run2 = run_case(suite_id, case_id, {"Subject": "Changed subject from the dashboard"})
    check(run2["status"] == "passed", "same automation passes with a different Subject")
    check("Changed subject from the dashboard" in run2["steps"][4]["observed"],
          f"check followed the new data: {run2['steps'][4]['observed']}")

    run3 = run_case(suite_id, case_id, {"To": "not-an-email"})
    by = {s["step_id"]: s["status"] for s in run3["steps"]}
    check(run3["status"] == "failed" and by["TS_EML001.4"] == "failed" and by["TS_EML001.5"] == "skipped",
          f"bad data fails at the Send check and skips the rest: {by}")
    print("   failure logged as:", run3["steps"][3]["error"][:140])
    print("\nAll smoke checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
