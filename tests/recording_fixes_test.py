"""End-to-end tests for the failures found in the Oracle Fusion issue log of 30 Oct 2026.

Each page copies one situation from those runs:
- a list left open over a dialog's OK button (Account alias: the click picked 'India Transfer');
- a Business Unit that the application clears when the Customer changes (Create Order);
- a springboard tile clicked twice, recorded as two clicks (Create standard sales order);
- a sign-in whose password ended up in the Playwright trace.
Plus the recorder's handling of short test data values and of a double-click that
arrives as two clicks.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\recording_fixes_test.py
Uses port 8775, a temporary data folder, STUDIO_SLOW_FACTOR=0.5 and STUDIO_TRACE=always.
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
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-fixes-"))
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")
os.environ.setdefault("STUDIO_ACTION_TIMEOUT_MS", "12000")
os.environ["STUDIO_TRACE"] = "always"

import uvicorn  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from app import storage  # noqa: E402
from app.actions import parameterize  # noqa: E402
from app.main import app  # noqa: E402
from app.recorder import append_action  # noqa: E402

PORT = 8775
BASE = f"http://127.0.0.1:{PORT}"
PASSWORD = "S3cret-Pw!42"

# Oracle ADF's busy flag, which replay reads to know the application is working.
ADF = "let pending = 0; window.AdfPage = {PAGE: {isSynchronizedWithServer: () => pending === 0}};"

DIALOG = """<!doctype html><html><body style="font-family:sans-serif;margin:20px">
<div role="dialog" style="position:relative;width:320px;height:150px;border:1px solid #888">
  <label for="tt">Transaction Type</label> <input id="tt" readonly>
  <a href="#" id="open" title="Search: Transaction Type">&#9660;</a>
  <button id="ok" style="position:absolute;left:120px;top:100px">OK</button>
</div>
<div id="list" role="listbox" style="position:absolute;left:100px;top:80px;width:220px;background:#fff;
     border:1px solid #888;display:none;line-height:18px">
  <div role="option">Inventory Adjustment</div><div role="option">Cycle Count</div>
  <div role="option">India Transfer</div><div role="option">Scrap</div></div>
<div id="out"></div>
<script>
const $ = id => document.getElementById(id);
$('open').onclick = e => { e.preventDefault(); $('list').style.display = 'block'; };
// Picking a value fills the field but leaves the list open, as the second pick in the recording did.
$('list').onclick = e => { $('tt').value = e.target.textContent; };
if (!location.search.includes('stubborn'))
  document.addEventListener('keydown', e => { if (e.key === 'Escape') $('list').style.display = 'none'; });
$('ok').onclick = () => { $('out').textContent = 'Source: ' + $('tt').value; };
</script></body></html>"""

ORDER = """<!doctype html><html><body style="font-family:sans-serif;margin:20px">
<label for="bu">Business Unit</label>
<select id="bu"><option value="0"></option><option value="1">UK Business Unit</option>
  <option value="2">US Business Unit</option></select><br>
<label for="cust">Customer</label> <input id="cust"><br>
<a href="#" id="acct" title="Search: Bill-to Account">Bill-to Account &#9660;</a>
<table id="rows"></table><div id="out"></div>
<script>
""" + ADF + """
const $ = id => document.getElementById(id);
// Like Fusion's order header: a new customer re-defaults the header and clears the business unit.
$('cust').addEventListener('change', () => { pending++; setTimeout(() => { $('bu').value = '0'; pending--; }, 600); });
$('acct').onclick = e => {
  e.preventDefault();
  $('rows').innerHTML = $('bu').value === '2' ? '<tr><td>185 - Winfield United</td></tr>' : '<tr><td>Search...</td></tr>';
};
$('rows').onclick = e => { $('out').textContent = 'Account ' + e.target.textContent; };
</script></body></html>"""

TILE = """<!doctype html><html><body style="font-family:sans-serif;margin:20px">
<div role="presentation" title="Order Management" style="cursor:pointer;padding:20px;border:1px solid">Order Management</div>
<script>
document.querySelector('[role=presentation]').onclick = () =>
  setTimeout(() => { document.body.innerHTML = '<h1>Overview</h1>'; }, 300);
</script></body></html>"""

SIGNIN = """<!doctype html><html><body style="font-family:sans-serif;margin:20px">
<label for="u">Username</label> <input id="u"><br>
<label for="p">Password</label> <input id="p" type="password"><br>
<button id="next">Next</button>
<script>
document.getElementById('next').onclick = async () => {
  await fetch('/fixes/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({u: document.getElementById('u').value, p: document.getElementById('p').value})});
  location.href = '/fixes/home';
};
</script></body></html>"""

HOME = "<!doctype html><html><body><h1>Welcome back</h1></body></html>"
PAGES = {"dialog": DIALOG, "order": ORDER, "tile": TILE, "signin": SIGNIN, "home": HOME}


@app.get("/fixes/{name}", include_in_schema=False)
def fixes_page(name: str):
    return HTMLResponse(PAGES[name])


@app.post("/fixes/login", include_in_schema=False)
async def fixes_login(request: Request):
    await request.body()
    return JSONResponse({"ok": True})


def http(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def wait_idle(timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        if not http("GET", "/api/status")["active"]:
            return
        time.sleep(0.5)
    raise TimeoutError("session did not finish")


failures = []


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        failures.append(msg)


def run(suite_id, case_id, page, actions):
    storage.save_recording(suite_id, case_id, {
        "suite_id": suite_id, "case_id": case_id, "start_url": f"{BASE}/fixes/{page}", "status": "completed",
        "steps": [{"step_id": f"{case_id}.1", "name": "Step 1",
                   "actions": [{"type": "goto", "url": f"{BASE}/fixes/{page}"}] + copy.deepcopy(actions)}]})
    r = http("POST", f"/api/suites/{suite_id}/cases/{case_id}/run", {"headed": False})
    wait_idle()
    result = http("GET", f"/api/runs/{r['run_id']}")
    result["notes"] = [ev["message"] for ev in result.get("events", []) if ev["type"] in ("action", "info")]
    result["error"] = result["steps"][0].get("error") or ""
    return result


def show(result):
    for n in result["notes"]:
        print("        ", n[:200])


def css(v):
    return {"kind": "css", "value": v}


def unit_checks():
    # "2" chosen in Business Unit is not the "2" typed into Quantity; a "2" typed into Quantity is.
    rec = [{"step_id": "C.1", "actions": [
        {"type": "select", "target": 'combobox "Business Unit"', "value": "2",
         "locators": [{"kind": "label", "value": "Business Unit"}]},
        {"type": "fill", "target": 'field "Quantity"', "value": "2",
         "locators": [{"kind": "label", "value": "Quantity"}]},
        {"type": "fill", "target": 'field "Customer"', "value": "Winfield United",
         "locators": [{"kind": "label", "value": "Customer"}]}]}]
    parameterize(rec, [{"step_id": "C.1", "data": {"Quantity": "2", "Customer 2": "Winfield United"}}])
    bu, qty, cust = rec[0]["actions"]
    check(bu["value"] == "2" and "param" not in bu, f"a short value in another field stays as recorded ({bu['value']})")
    check(qty.get("param") == "Quantity", "a short value in the field its test data is named after is linked")
    check(cust.get("param") == "Customer 2", "a long value is linked whatever its key")

    # A double-click that reached the page as two clicks on an element the first click replaced.
    tile = {"type": "click", "target": 'presentation "Order Management"',
            "locators": [{"kind": "text", "value": "Order Management"}]}
    acts = []
    append_action(acts, {**tile, "el": "a:1", "recorded_at": 100.0})
    append_action(acts, {**tile, "el": "a:2", "recorded_at": 100.2})
    append_action(acts, {**tile, "el": "a:3", "recorded_at": 103.0})
    check(len(acts) == 2, f"two quick clicks on the same control are kept as one ({len(acts)} actions kept of 3)")
    # A real double-click still replaces its two clicks, and nothing before them.
    acts = []
    append_action(acts, {**tile, "el": "b:1", "recorded_at": 200.0})
    append_action(acts, {**tile, "el": "b:1", "recorded_at": 202.0})
    append_action(acts, {**tile, "el": "b:1", "recorded_at": 202.1})
    append_action(acts, {**tile, "type": "dblclick", "el": "b:1", "recorded_at": 202.15})
    check([a["type"] for a in acts] == ["click", "dblclick"],
          f"a double-click replaces only its own two clicks ({[a['type'] for a in acts]})")


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    unit_checks()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    created = http("POST", "/api/freeform", {"title": "Recording fixes", "url": f"{BASE}/fixes/dialog"})
    suite_id, case_id = created["suite_id"], created["case_id"]
    suite = storage.load_suite(suite_id)
    storage.find_case(suite, case_id)["steps"] = [{"step_id": f"{case_id}.1", "name": "Step 1", "data": {}}]
    storage.save_suite(suite)

    # 1. A list left open over OK: Escape closes it, and OK is clicked, not the list.
    pick = [{"type": "click", "target": 'a "Search: Transaction Type"', "locators": [css('a[title="Search: Transaction Type"]')]},
            {"type": "click", "target": 'option "Inventory Adjustment"',
             "locators": [{"kind": "role", "role": "option", "value": "Inventory Adjustment"}]},
            {"type": "click", "target": 'button "OK"', "locators": [{"kind": "role", "role": "button", "value": "OK"}]},
            {"type": "expect_text", "text": "Source: Inventory Adjustment"}]
    r = run(suite_id, case_id, "dialog", pick)
    show(r)
    check(r["status"] == "passed" and any("Escape" in n for n in r["notes"]),
          f"a list open over OK is closed with Escape before OK is clicked: {r['status']} {r['error'][:200]}")

    r = run(suite_id, case_id, "dialog?stubborn=1", pick)
    check(r["status"] == "failed" and "Escape did not close it" in r["error"],
          f"a list that stays open over OK fails the step instead of being clicked: {r['error'][:200]}")

    # 2. The customer clears the business unit: the run reports it where it happens and in the failure.
    order = [{"type": "select", "target": 'combobox "Business Unit"', "value": "US Business Unit",
              "locators": [{"kind": "label", "value": "Business Unit"}]},
             {"type": "fill", "target": 'field "Customer"', "value": "Winfield United",
              "locators": [{"kind": "label", "value": "Customer"}]},
             {"type": "click", "target": 'a "Search: Bill-to Account"', "locators": [css('a[title="Search: Bill-to Account"]')]},
             {"type": "click", "target": 'td "185 - Winfield United"', "locators": [{"kind": "text", "value": "185 - Winfield United"}]}]
    r = run(suite_id, case_id, "order", order)
    show(r)
    check(any('Business Unit" no longer shows "US Business Unit"' in n and "Customer" in n for n in r["notes"]),
          "a choice that a later action undid is reported right after that action")
    check(r["status"] == "failed" and "Likely cause" in r["error"],
          f"the failure names the undone choice as the likely cause: {r['error'][-260:]}")

    # Choosing it again after the customer (the advice given) passes; the option is found by its text.
    fixed = [order[1], order[0], order[2], order[3], {"type": "expect_text", "text": "Account 185 - Winfield United"}]
    r = run(suite_id, case_id, "order", fixed)
    check(r["status"] == "passed", f"business unit chosen after the customer, by its text: {r['status']} {r['error'][:200]}")

    # 3. A tile recorded as two clicks 0.2 s apart; the first opens the next page.
    tile = {"type": "click", "target": 'presentation "Order Management"',
            "locators": [{"kind": "role", "role": "presentation", "value": "Order Management"},
                         {"kind": "text", "value": "Order Management"}]}
    r = run(suite_id, case_id, "tile", [{**tile, "recorded_at": 50.0}, {**tile, "recorded_at": 50.2},
                                        {"type": "expect_text", "text": "Overview"}])
    show(r)
    check(r["status"] == "passed" and any("second half of a double-click" in n for n in r["notes"]),
          f"the second click of a double-click recorded as two clicks is skipped: {r['status']} {r['error'][:200]}")

    # 4. The password typed at sign-in is not in the trace; what follows the sign-in is.
    signin = [{"type": "fill", "target": 'field "Username"', "value": "bob", "locators": [{"kind": "label", "value": "Username"}]},
              {"type": "fill", "target": 'field "Password"', "value": PASSWORD, "masked": True,
               "locators": [{"kind": "label", "value": "Password"}]},
              {"type": "click", "target": 'button "Next"', "navigates": True,
               "locators": [{"kind": "role", "role": "button", "value": "Next"}]},
              {"type": "expect_text", "text": "Welcome back"}]
    r = run(suite_id, case_id, "signin", signin)
    trace = storage.run_dir(r["id"]) / "trace.zip"
    blob = b""
    if trace.exists():
        with zipfile.ZipFile(trace) as z:
            blob = b"".join(z.read(n) for n in z.namelist())
    check(r["status"] == "passed", f"sign-in run passes: {r['error'][:200]}")
    check(trace.exists() and PASSWORD.encode() not in blob, "the password is nowhere in the trace")
    check(b"Welcome back" in blob, "the trace still covers what happens after the sign-in")

    server.should_exit = True
    if failures:
        print(f"\n{len(failures)} check(s) FAILED")
        sys.exit(1)
    print("\nAll recording-fix checks passed.")


if __name__ == "__main__":
    main()
