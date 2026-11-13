"""Tests for how results are shown and what the run form starts from (13 Nov 2026).

From the admin rerun of 12 Nov: tests that worked were shown as failing because the
rerun used the recording's test data, which the recording itself used up.
- An Oracle Error with a code that the recording never met fails the step (a duplicate
  invoice number made Non-PO Invoice "pass" without creating anything); a Warning, or an
  Error the recording deals with, does not.
- A failure caused by used-up test data is "needs new test data", apart from failures.
- A list the recording itself chooses in several times is not reported as reset by the
  application (Shipping's "Scheduled Ship Date Operator").
- The run form starts from the last passing run's values, as typed ({stamp} kept), and
  marks values already used; the test list shows the last results, admin runs hollow.

All on local pages and temporary test copies: nothing runs against a real application.
Run from the Prototype folder:  .venv\\Scripts\\python tests\\replay_fixes_3_test.py
Uses port 8778, a temporary data folder and STUDIO_SLOW_FACTOR=0.5.
"""
import asyncio
import copy
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
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-replay-fixes-3-"))
os.environ.setdefault("STUDIO_LOGIN", "off")  # drives the API without signing in (only allowed on 127.0.0.1)
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")
os.environ.setdefault("STUDIO_ACTION_TIMEOUT_MS", "30000")  # 15 s with the slow factor

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import storage, users  # noqa: E402
from app.main import app  # noqa: E402

PORT = 8778
BASE = f"http://127.0.0.1:{PORT}"
STYLE = "<style>body{font-family:sans-serif;margin:20px}</style>"

# Save is refused: the invoice number exists (AP-810245). Validate and Done still respond, as on the
# Oracle invoice page, so a recording without checks went on clicking and "passed".
INVOICE = """<!doctype html><html><head>""" + STYLE + """</head><body>
<label for="num">Number</label> <input id="num"> <button id="save">Save</button> <button id="val">Validate</button>
<div id="msg"></div>
<div id="dlg" data-afr-popupid="dlg" style="display:none;border:1px solid #c00;padding:8px"></div>
<script>
  const $ = id => document.getElementById(id);
  const kind = new URLSearchParams(location.search).get('kind') || 'error';
  $('save').onclick = () => {
    $('dlg').innerHTML = kind === 'error'
      ? 'Error You must enter a different number. There\\'s already an invoice with that number. (AP-810245)'
      : 'Warning Verify the payment terms. Credit and debit memos are usually set for immediate payment. (AP-810038)';
    $('dlg').style.display = 'block';
  };
  $('val').onclick = () => { $('dlg').style.display = 'none'; $('msg').textContent = 'Validated'; };
</script></body></html>"""

# The first Submit is refused (EGO-2745049); closing the message and submitting again works. The
# recording met this error and dealt with it, so a run that meets it again is fine.
RETRY = """<!doctype html><html><head>""" + STYLE + """</head><body>
<button id="submit">Submit</button><div id="msg"></div>
<div id="dlg" data-afr-popupid="dlg" style="display:none;border:1px solid #c00;padding:8px">
  <div>Error</div><div id="text">You must provide a valid value for the Status attribute. (EGO-2745049)</div>
  <button id="close">Close</button></div>
<script>
  const $ = id => document.getElementById(id);
  let tries = 0;
  $('submit').onclick = () => { if (++tries === 1) $('dlg').style.display = 'block'; else $('msg').textContent = 'Submitted'; };
  $('close').onclick = () => { $('dlg').style.display = 'none'; };
</script></body></html>"""

# A list chosen in four times by the recording, then a search that finds nothing.
OPERATOR = """<!doctype html><html><head>""" + STYLE + """</head><body>
<label for="op">Scheduled Ship Date Operator</label>
<select id="op"><option>Before</option><option>Today and the next 7 days</option><option>15 to 30 days from today</option></select>
<button id="search">Search</button><table><tbody id="rows"></tbody></table>
<script>
  document.getElementById('search').onclick = () => {
    document.getElementById('rows').innerHTML = '<tr><td>No results found.</td></tr>'; };
</script></body></html>"""

PAGES = {"invoice": INVOICE, "retry": RETRY, "operator": OPERATOR}


@app.get("/replay-fixes-3/{name}", include_in_schema=False)
def fixes_page(name: str):
    return HTMLResponse(PAGES[name])


def goto(name, query=""):
    return {"type": "goto", "url": f"{BASE}/replay-fixes-3/{name}{query}"}


def button(name):
    return {"type": "click", "target": f'button "{name}"', "locators": [{"kind": "role", "role": "button", "value": name}]}


NUMBER = {"type": "fill", "value": "${Number}", "param": "Number", "recorded_value": "test_inv_11",
          "target": 'field "Number"', "locators": [{"kind": "label", "value": "Number"}]}
DUPLICATE = [goto("invoice"), NUMBER, button("Save"), button("Validate"), {"type": "expect_text", "text": "Validated"}]
RETRIED = [goto("retry"), button("Submit"),
           {"type": "click", "target": 'div "You must provide a valid value for the Status attribute. (EG"',
            "locators": [{"kind": "text", "value": "You must provide a valid value for the Status attribute. (EGO-2745049)"}]},
           button("Close"), button("Submit"), {"type": "expect_text", "text": "Submitted"}]
OP = {"target": 'combobox "Scheduled Ship Date Operator"', "locators": [{"kind": "label", "value": "Scheduled Ship Date Operator"}]}
OPERATOR_CHOICES = [goto("operator")] + [{"type": "select", "value": v, **copy.deepcopy(OP)} for v in
                                          ("Today and the next 7 days", "15 to 30 days from today", "Before")] + [
    button("Search"), {"type": "click", "target": 'link "1155596"', "locators": [{"kind": "role", "role": "link", "value": "1155596"}]}]
MISSING = [goto("operator"), button("Approve")]


def http(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def wait_idle(timeout=240):
    end = time.time() + timeout
    while time.time() < end:
        st = http("GET", "/api/status")
        if not st["active"] and not st["others"]:
            return
        time.sleep(0.5)
    raise TimeoutError("session did not finish")


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


def new_case(title, data, actions):
    created = http("POST", "/api/freeform", {"title": title, "url": actions[0]["url"]})
    suite_id, case_id = created["suite_id"], created["case_id"]
    suite = storage.load_suite(suite_id)
    storage.find_case(suite, case_id)["steps"] = [{"step_id": f"{case_id}.1", "name": title, "data": data}]
    storage.save_suite(suite)
    storage.save_recording(suite_id, case_id, {
        "suite_id": suite_id, "case_id": case_id, "start_url": actions[0]["url"], "status": "completed",
        "recorded_at": "2026-11-03T20:21:57+05:30", "recorded_by": "user07",
        "steps": [{"step_id": f"{case_id}.1", "name": title, "actions": copy.deepcopy(actions)}]})
    return suite_id, case_id


def start(suite_id, case_id, data=None):
    r = http("POST", f"/api/suites/{suite_id}/cases/{case_id}/run", {"headed": False, "data": data or {}})
    wait_idle()
    out = http("GET", f"/api/runs/{r['run_id']}")
    for ev in out.get("events", []):
        if ev["type"] in ("action", "info") and not ev["message"].startswith(("Device", "Test data")):
            print("      ", ev.get("ms", ""), ev["message"][:190])
    return out


def run(title, actions, saved=None, data=None):
    return start(*new_case(title, saved or {}, actions), data)


def error_of(r):
    return r["steps"][0].get("error") or ""


def listed(suite_id, case_id):
    return next(c for c in http("GET", "/api/cases") if c["suite_id"] == suite_id and c["case_id"] == case_id)


def fake_run(suite_id, case_id, status, run_by, started, data, typed=None, needs_data=""):
    """A past run as the server keeps it (to test what the dashboard makes of the history)."""
    rid = f"run-{started.replace('-', '').replace(':', '').replace('T', '-')[:15]}-{run_by[-2:]}"
    run = {"id": rid, "suite_id": suite_id, "case_id": case_id, "title": "copy", "status": status,
           "started_at": started, "run_by": run_by, "test_data": data, "steps": [], "environment": {}}
    if typed is not None:
        run["test_data_typed"] = typed
    if needs_data:
        run["needs_data"] = needs_data
    storage.save_run(run)


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    users.set_password("admin", "admin-pass", create=True, role="admin")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    # --- Oracle errors
    suite_id, case_id = new_case("Duplicate invoice number", {"Number": "test_inv_11"}, DUPLICATE)
    r = start(suite_id, case_id)
    err = error_of(r)
    check(r["status"] == "failed" and "AP-810245" in err and "reported an error" in err,
          f"an Oracle Error the recording never met fails the step: {err[:200]}")
    check(r.get("needs_data") and r["steps"][0].get("needs_data"), f"... as used-up test data: {r.get('needs_data')}")
    check(listed(suite_id, case_id)["last_run"]["display"] == "needs_data", "the test list shows 'needs new test data'")
    check(r.get("test_data_typed", {}).get("Number") == "test_inv_11", "the run keeps the values as typed")

    r = run("Payment terms warning", [goto("invoice", "?kind=warning")] + DUPLICATE[1:], saved={"Number": "dm_1"})
    check(r["status"] == "passed", f"an Oracle Warning does not fail the run: {error_of(r)}")

    r = run("Error the recording deals with", RETRIED)
    check(r["status"] == "passed", f"an Error the recording met and dealt with does not fail the run: {error_of(r)}")

    # --- real failures stay failures; a list the recording itself changes is not 'reset'
    s2, c2 = new_case("Control missing", {}, MISSING)
    r = start(s2, c2)
    check(r["status"] == "failed" and not r.get("needs_data") and listed(s2, c2)["last_run"]["display"] == "failed",
          "a missing control is a failure, not used-up data")

    r = run("Operator chosen four times", OPERATOR_CHOICES)
    err = error_of(r)
    check(r["status"] == "failed" and "Likely cause" not in err and "No results found" in err,
          f"choosing again in the same list is not reported as the application resetting it: {err[-160:]}")
    check(not any("no longer shows" in e["message"] for e in r["events"]), "no 'no longer shows' warning")
    check(bool(r.get("needs_data")), "an empty search is used-up test data")

    # --- run form and history (a copy of Non-PO Prepayment's history)
    s3, c3 = new_case("Prepayment copy", {"Supplier": "b", "Number": "test_inv_11", "Amount": "60"}, DUPLICATE)
    fake_run(s3, c3, "passed", "user07", "2026-11-03T20:24:54+05:30", {"Supplier": "b", "Number": "test_inv_12", "Amount": "60"})
    fake_run(s3, c3, "passed", "admin", "2026-11-12T16:17:18+05:30",
             {"Supplier": "b", "Number": "regr_pp_20261112-161718", "Amount": "60"},
             typed={"Supplier": "b", "Number": "regr_pp_{stamp}", "Amount": "60"})
    fake_run(s3, c3, "failed", "admin", "2026-11-12T20:34:30+05:30", {"Supplier": "b", "Number": "test_inv_11", "Amount": "60"},
             needs_data="The application says a value in the test data is already used.")
    detail = http("GET", f"/api/suites/{s3}/cases/{c3}")
    form = detail["run_form"]
    check(form["defaults"].get("Number", {}).get("value") == "regr_pp_{stamp}",
          f"the run form starts from the last passing run, as typed: {form['defaults']}")
    check("Supplier" not in form["used"] and "Amount" not in form["used"],
          f"values nobody changes are not marked: {list(form['used'])}")
    check([h["value"] for h in form["used"]["Number"]][:2] == ["test_inv_11", "regr_pp_20261112-161718"],
          f"used numbers are known: {form['used'].get('Number')}")
    item = listed(s3, c3)
    check([d["display"] for d in item["recent"]] == ["passed", "passed", "needs_data"]
          and [d["by_admin"] for d in item["recent"]] == [False, True, True],
          f"the test list has the last results, admin runs marked: {item['recent']}")
    check(all("by_admin" in r for r in detail["runs"]), "the run history marks admin runs")
    overview = http("GET", "/api/admin/overview")
    t = next(x for x in overview["tests"] if x["case_id"] == c3)
    check(t["status"] == "needs_data" and t["failed"] == 0 and t["needs_data"] == 1,
          f"the admin overview counts used-up data apart from failures: {t['status']} {t['failed']} {t['needs_data']}")

    # --- the dashboard itself
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1400, "height": 900})
        shots = os.environ.get("STUDIO_TEST_SHOTS")  # a folder: save what the checks look at, to look at it
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/")
        # Polled from here: the dashboard's Content-Security-Policy forbids the eval that wait_for_function uses.
        for _ in range(100):
            if page.evaluate("() => typeof selectCase === 'function' && document.querySelectorAll('#case-list li').length > 1"):
                break
            page.wait_for_timeout(100)
        dots = page.locator("#case-list .dot").count()
        badge = page.locator("#case-list .badge.needs_data").count()
        page.evaluate("([s, c]) => selectCase(s, c)", [s3, c3])
        page.evaluate("() => { S.runForm = true; renderRunForm(); }")
        number = page.locator('[data-key="Number"]')
        first = number.input_value()
        number.fill("test_inv_12")
        hint = page.locator("#u-Number")
        shown = hint.is_visible() and "Already used by the passing run" in hint.inner_text()
        if shots:
            page.screenshot(path=f"{shots}/run-form.png")
        hint.locator("button").click()
        unique = number.input_value()
        hidden_after = not hint.is_visible()
        page.evaluate("() => { S.tab = 'runs'; renderMain(); }")
        history = page.inner_text("#tab-body")
        if shots:
            page.screenshot(path=f"{shots}/history.png")
        page.evaluate("() => showAdmin()")
        page.wait_for_selector(".stat.needs_data")
        if shots:
            page.screenshot(path=f"{shots}/admin.png", full_page=True)
        b.close()
    check(dots == 0 and badge >= 1, f"the test list shows 'needs new test data' ({badge}) and no result markers ({dots})")
    check(first == "regr_pp_{stamp}", f"the form shows the last passing run's value: {first}")
    check(shown, "a value used before is marked under the field")
    check(unique == "test_inv_{stamp}" and hidden_after, f"... with a value that is new every run: {unique}")
    check("admin run" in history and "needs new test data" in history, "the history marks admin runs and used-up data")
    check(not errors, f"no script errors in the dashboard: {errors}")

    print("\nAll result and run-form checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
