"""End-to-end test: record and run a test without an Excel file.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\freeform_test.py
Uses port 8768 and a temporary data folder.
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-freeform-"))

import openpyxl  # noqa: E402
import uvicorn  # noqa: E402

from app import storage  # noqa: E402
from app.events import sessions  # noqa: E402
from app.excel_io import parse_workbook  # noqa: E402
from app.main import app  # noqa: E402
from app.recorder import record_session  # noqa: E402

PORT = 8768
BASE = f"http://127.0.0.1:{PORT}"
SUBJECT = "Q3 inventory adjustment summary"


def http(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def wait_idle(timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if not http("GET", "/api/status")["active"]:
            return
        time.sleep(0.5)
    raise TimeoutError("session did not finish")


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


seen = {}


def act_like_a_user(page):
    def type_into(label, text):
        page.get_by_label(label, exact=True).click()
        page.keyboard.type(text, delay=3)

    def add_check(locator):
        page.locator("#check").click()
        locator.click()

    # Step 1: named before a click that loads a new page; the name must survive the page load.
    page.locator("#stepname").fill("Log in")
    page.wait_for_timeout(800)
    type_into("User Name", "demo.user")
    type_into("Password", "demo123")
    page.get_by_role("button", name="Sign in").click()
    page.get_by_role("heading", name="Inbox").wait_for()
    page.wait_for_timeout(300)
    seen["name_after_navigation"] = page.locator("#stepname").input_value()
    add_check(page.get_by_role("heading", name="Inbox"))
    page.locator("#next").click()
    page.wait_for_timeout(300)
    # Step 2: named just before Next.
    page.get_by_role("link", name="Compose").click()
    type_into("To", "finance.team@acme.test")
    type_into("Subject", SUBJECT)
    type_into("Message", "Hi team, the Q3 inventory adjustments have been posted.")
    page.get_by_role("button", name="Send").click()
    add_check(page.get_by_text("Message sent", exact=True))
    page.locator("#stepname").fill("Write and send the email")
    page.locator("#next").click()
    page.wait_for_timeout(300)
    # An extra Next with no actions must not create an empty step.
    page.locator("#next").click()
    page.wait_for_timeout(300)
    # Step 3: left unnamed.
    page.get_by_role("link", name="Sent", exact=True).click()
    add_check(page.locator(".subject", has_text=SUBJECT))
    page.locator("#finish").click()


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    created = http("POST", "/api/freeform", {"title": "Send a status email", "url": f"{BASE}/mail/login"})
    suite_id, case_id = created["suite_id"], created["case_id"]
    detail = http("GET", f"/api/suites/{suite_id}/cases/{case_id}")
    check(case_id.startswith("REC-") and detail["case"]["freeform"] and detail["case"]["steps"] == [],
          f"test created without Excel: {case_id}, no steps yet")
    check(detail["target"]["url"] == f"{BASE}/mail/login", "start URL comes from the form")

    suite = storage.load_suite(suite_id)
    case = storage.find_case(suite, case_id)
    s = sessions.start("record", record_session, suite_id=suite_id, case_id=case_id, suite=suite, case=case,
                       headless=True, on_ready=act_like_a_user)
    wait_idle()
    if s.status != "completed":
        for ev in s.log.all():
            print("   log:", ev["type"], ev["message"][:300])
    check(s.status == "completed", "recording completed")
    check(seen["name_after_navigation"] == "Log in", "step name typed on the toolbar survives a page load")

    case = storage.find_case(storage.load_suite(suite_id), case_id)
    steps = case["steps"]
    check([st["name"] for st in steps] == ["Log in", "Write and send the email", "Step 3"],
          f"steps created from the recording: {[st['name'] for st in steps]}")
    check([st["step_id"] for st in steps] == [f"{case_id}.{n}" for n in (1, 2, 3)], "step IDs numbered 1-3")
    check(steps[0]["data"] == {"User Name": "demo.user"}, f"password not stored as test data: {steps[0]['data']}")
    check(list(steps[1]["data"]) == ["To", "Subject", "Message"], f"typed values became test data: {steps[1]['data']}")
    check("Message sent" in steps[1]["expected"], f"checks became expected results: {steps[1]['expected']}")

    rec = storage.load_recording(suite_id, case_id)
    fills = [a["value"] for st in rec["steps"] for a in st["actions"] if a["type"] == "fill" and not a.get("masked")]
    check(fills == ["${User Name}", "${To}", "${Subject}", "${Message}"], f"recording uses parameters: {fills}")
    check(not rec.get("warnings"), f"no review warnings: {rec.get('warnings')}")

    parsed, warnings = parse_workbook(storage.workbook_path(suite_id))
    check(len(parsed) == 1 and [(p["step_id"], p["name"], p["data"]) for p in parsed[0]["steps"]]
          == [(st["step_id"], st["name"], st["data"]) for st in steps] and not warnings,
          "generated Excel script reads back to the same steps and test data")

    run = http("POST", f"/api/suites/{suite_id}/cases/{case_id}/run", {"headed": False})
    wait_idle()
    run = http("GET", f"/api/runs/{run['run_id']}")
    check(run["status"] == "passed", f"automated run passed ({run['passed']} steps)")
    ws = openpyxl.load_workbook(storage.run_dir(run["id"]) / "results.xlsx").active
    check([ws.cell(r, 8).value for r in range(11, 14)] == ["Pass"] * 3, "results written into the generated script")

    run2 = http("POST", f"/api/suites/{suite_id}/cases/{case_id}/run",
                {"headed": False, "data": {"Subject": "Changed in the run form {now}"}})
    wait_idle()
    run2 = http("GET", f"/api/runs/{run2['run_id']}")
    check(run2["status"] == "passed" and "Changed in the run form 20" in run2["steps"][2]["observed"],
          f"run with changed test data passed and checked the new value: {run2['steps'][2]['observed']}")
    print("\nAll free-form checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
