"""End-to-end tests for the failed runs on the deployed studio, 3-4 Nov 2026.

Each page copies one situation from those runs:
- the task list switches to shipment tasks late, and the 2nd link by position is another
  task (Shipping a standard sales order: 'Create Miscellaneous Transaction' was opened);
- a run with another item number than the one recorded, where the clicked suggestion and
  link show that number (Change order: 1008017 instead of 1008018);
- a payment process request that only shows up after a few Refresh clicks, run with
  another name (Invoice Payment: test_ppr_02 instead of test_ppr_01);
- the springboard's right arrow, clicked once more than the strip needs (Process Non
  Catalogue Requisition);
- a search that finds nothing because the recording used the record up (02 Process
  Non CTG Requisition);
plus the recorder keeping a recording when a new session records nothing.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\replay_fixes_test.py
Uses port 8776, a temporary data folder and STUDIO_SLOW_FACTOR=0.5.
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
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-replay-fixes-"))
# These tests drive the API without signing in (allowed only on 127.0.0.1).
os.environ.setdefault("STUDIO_LOGIN", "off")
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")
os.environ.setdefault("STUDIO_ACTION_TIMEOUT_MS", "30000")  # 15 s with the slow factor

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from app import storage  # noqa: E402
from app.actions import data_changes, follow_data, parameterize  # noqa: E402
from app.main import app  # noqa: E402
from app.recorder import _keep_previous  # noqa: E402

PORT = 8776
BASE = f"http://127.0.0.1:{PORT}"

STYLE = "<style>body{font-family:sans-serif;margin:20px} td{padding:4px;border:1px solid #ccc}</style>"

# 'Show Tasks' switches the task list 7 s after the choice, without telling anyone it is busy:
# replay has already waited out the 5 s grace for CSS paths by then.
TASKS = """<!doctype html><html><head>""" + STYLE + """</head><body>
<label for="show">Show Tasks</label>
<select id="show"><option>Inventory</option><option>Shipments</option></select>
<div><div><ul id="tasks"></ul></div></div><div id="msg"></div>
<script>
  const lists = {Inventory: ['Manage Item Quantities', 'Create Miscellaneous Transaction', 'Manage Lots'],
                 Shipments: ['Manage Shipments', 'Manage Shipment Lines', 'Confirm Pick Slips']};
  const never = location.search.includes('never');
  function draw(name) {
    document.getElementById('tasks').innerHTML =
      lists[name].map(t => '<li><a href="#">' + t + '</a></li>').join('');
    document.querySelectorAll('#tasks a').forEach(a => a.addEventListener('click', e => {
      e.preventDefault(); document.getElementById('msg').textContent = 'Opened ' + a.textContent; }));
  }
  draw('Inventory');
  document.getElementById('show').addEventListener('change', e => {
    if (!never) setTimeout(() => draw(e.target.value), 7000);
  });
</script></body></html>"""

# Typing an item lists the recent items as suggestions (always all three, in this order);
# picking one adds a row whose number is a link.
ITEMS = """<!doctype html><html><head>""" + STYLE + """</head><body>
<label for="items">Items</label> <input id="items" role="combobox">
<div><div><ul id="sugg"></ul></div></div>
<table><tbody id="rows"></tbody></table><div id="msg"></div>
<script>
  const items = {'1008016': 'Test automation test 4th', '1008017': 'Test 4th nir RPA', '1008018': 'Test item NIR RPA'};
  const $ = id => document.getElementById(id);
  $('items').addEventListener('input', () => setTimeout(() => {
    $('sugg').innerHTML = Object.entries(items).map(([n, d]) => '<li><div>' + n + '(VLS): ' + d + '</div></li>').join('');
    document.querySelectorAll('#sugg div').forEach(div => div.addEventListener('click', () => {
      const n = div.textContent.slice(0, 7);
      $('sugg').innerHTML = '';
      $('rows').innerHTML += '<tr><td>' + items[n] + '</td><td><a href="#" data-n="' + n + '">' + n + '</a></td></tr>';
      document.querySelectorAll('#rows a').forEach(a => a.onclick = e => {
        e.preventDefault(); $('msg').textContent = 'Opened item ' + a.dataset.n; });
    }));
  }, 300));
</script></body></html>"""

# A submitted request shows up in the table 4.5 s later, and only when Refresh is clicked.
PPR = """<!doctype html><html><head>""" + STYLE + """</head><body>
<label for="name">Name</label> <input id="name"> <button id="submit">Submit</button>
<a href="#" id="refresh" title="Refresh">Refresh</a>
<table><tbody id="rows"><tr><td>No data to display.</td></tr></tbody></table><div id="msg"></div>
<script>
  const $ = id => document.getElementById(id);
  const sent = [];
  $('submit').addEventListener('click', () => sent.push({name: $('name').value, at: Date.now()}));
  $('refresh').addEventListener('click', e => {
    e.preventDefault();
    const done = sent.filter(s => Date.now() - s.at > 4500);
    $('rows').innerHTML = done.map(s => '<tr><td>' + s.name + '</td></tr>').join('')
      || '<tr><td>No data to display.</td></tr>';
    document.querySelectorAll('#rows td').forEach(td => td.addEventListener('click', () => {
      $('msg').textContent = 'Opened ' + td.textContent; }));
  });
</script></body></html>"""

# The right arrow moves the strip one group along and hides at the end (after 2 clicks here).
SPRINGBOARD = """<!doctype html><html><head>""" + STYLE + """</head><body>
<div id="strip"><a href="#">Order Management</a> <a href="#">Supply Chain Execution</a>
  <a href="#">Procurement</a></div>
<div id="clusters-right-nav"><svg width="20" height="20"><rect width="20" height="20"></rect></svg></div>
<div id="msg"></div>
<script>
  let at = 0;
  document.querySelector('#clusters-right-nav > svg').addEventListener('click', () => {
    at++;
    if (at >= 2) document.getElementById('clusters-right-nav').style.visibility = 'hidden';
  });
  document.querySelectorAll('#strip a').forEach(a => a.addEventListener('click', e => {
    e.preventDefault(); document.getElementById('msg').textContent = 'Opened ' + a.textContent; }));
</script></body></html>"""

# The requisition was processed by the recording itself: searching for it finds nothing.
SEARCH = """<!doctype html><html><head>""" + STYLE + """</head><body>
<label for="req">Requisition</label> <input id="req"> <button id="search">Search</button>
<table><tbody id="rows"></tbody></table>
<script>
  document.getElementById('search').addEventListener('click', () => {
    document.getElementById('rows').innerHTML = '<tr><td>No results found.</td></tr>'; });
</script></body></html>"""

# A control named with a count: recorded as "(0 unread)", now "(2 unread)".
NOTIFY = """<!doctype html><html><head>""" + STYLE + """</head><body>
<div><ul><li><a href="#" id="bell">Notifications (2 unread)</a></li></ul></div><div id="msg"></div>
<script>
  document.getElementById('bell').addEventListener('click', e => {
    e.preventDefault(); document.getElementById('msg').textContent = 'Opened notifications'; });
</script></body></html>"""

PAGES = {"tasks": TASKS, "items": ITEMS, "ppr": PPR, "springboard": SPRINGBOARD, "search": SEARCH, "notify": NOTIFY}


@app.get("/replay-fixes/{name}", include_in_schema=False)
def fixes_page(name: str):
    return HTMLResponse(PAGES[name])


def goto(name, query=""):
    return {"type": "goto", "url": f"{BASE}/replay-fixes/{name}{query}"}


def link(name, css=None):
    locs = [{"kind": "role", "role": "link", "value": name}, {"kind": "text", "value": name}]
    return {"type": "click", "target": f'link "{name}"', "locators": locs + ([{"kind": "css", "value": css}] if css else [])}


SHIPMENT_TASK = [
    goto("tasks"),
    {"type": "select", "value": "Shipments", "target": 'combobox "Show Tasks"',
     "locators": [{"kind": "label", "value": "Show Tasks"}]},
    link("Manage Shipment Lines", "div > div > ul > li:nth-of-type(2) > a"),
    {"type": "expect_text", "text": "Opened Manage Shipment Lines"},
]

COUNT_CHANGED = [goto("notify"), link("Notifications (0 unread)", "div > ul > li:nth-of-type(1) > a"),
                 {"type": "expect_text", "text": "Opened notifications"}]

CHANGE_ORDER = [
    goto("items"),
    # Linked to ${Items} when recorded with 1008018. No recorded_value: recorded before it was kept,
    # so the run falls back on the test case's saved data.
    {"type": "fill", "value": "${Items}", "param": "Items", "target": 'combobox "Items"',
     "locators": [{"kind": "label", "value": "Items"}]},
    {"type": "click", "target": 'div "1008018(VLS): Test item NIR RPA"',
     "locators": [{"kind": "text", "value": "1008018(VLS): Test item NIR RPA"},
                  {"kind": "css", "value": "div > div > ul > li:nth-of-type(3) > div"}]},
    link("1008018"),
    {"type": "expect_text", "text": "Opened item ${Items}", "param": "Items"},
]

PAYMENT_REQUEST = [
    goto("ppr"),
    {"type": "fill", "value": "${Name}", "param": "Name", "recorded_value": "test_ppr_01", "target": 'field "Name"',
     "locators": [{"kind": "label", "value": "Name"}]},
    {"type": "click", "target": 'button "Submit"', "locators": [{"kind": "role", "role": "button", "value": "Submit"}]},
    link("Refresh"),
    {"type": "click", "target": 'td "test_ppr_01"', "locators": [{"kind": "text", "value": "test_ppr_01"}]},
    {"type": "expect_text", "text": "Opened ${Name}", "param": "Name"},
]

ARROW = {"type": "click", "target": 'presentation ""', "locators": [{"kind": "css", "value": "#clusters-right-nav > svg"}],
         "pos": [10, 10]}
SPRINGBOARD_CLICKS = [goto("springboard"), ARROW, copy.deepcopy(ARROW), copy.deepcopy(ARROW), link("Procurement"),
                      {"type": "expect_text", "text": "Opened Procurement"}]

USED_UP = [
    goto("search"),
    {"type": "fill", "value": "${Requisition}", "param": "Requisition", "target": 'field "Requisition"',
     "locators": [{"kind": "label", "value": "Requisition"}]},
    {"type": "click", "target": 'button "Search"', "locators": [{"kind": "role", "role": "button", "value": "Search"}]},
    {"type": "click", "target": 'td "US1015808"', "locators": [{"kind": "text", "value": "US1015808"}]},
]


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


def new_case(title, data):
    created = http("POST", "/api/freeform", {"title": title, "url": f"{BASE}/replay-fixes/tasks"})
    suite_id, case_id = created["suite_id"], created["case_id"]
    suite = storage.load_suite(suite_id)
    storage.find_case(suite, case_id)["steps"] = [{"step_id": f"{case_id}.1", "name": title, "data": data}]
    storage.save_suite(suite)
    return suite_id, case_id


def run(title, actions, saved=None, data=None):
    suite_id, case_id = new_case(title, saved or {})
    storage.save_recording(suite_id, case_id, {
        "suite_id": suite_id, "case_id": case_id, "start_url": actions[0]["url"], "status": "completed",
        "steps": [{"step_id": f"{case_id}.1", "name": title, "actions": copy.deepcopy(actions)}]})
    r = http("POST", f"/api/suites/{suite_id}/cases/{case_id}/run", {"headed": False, "data": data or {}})
    wait_idle()
    out = http("GET", f"/api/runs/{r['run_id']}")
    for ev in out.get("events", []):
        if ev["type"] in ("action", "info") and not ev["message"].startswith(("Device", "Test data")):
            print("      ", ev.get("ms", ""), ev["message"][:170])
    return out


def outcome(r):
    return r["steps"][0].get("observed") or r["steps"][0].get("error") or ""


def unit_checks():
    rec = [{"step_id": "C.1", "actions": [
        {"type": "fill", "value": "1008018", "target": 'combobox "Items"', "locators": [{"kind": "label", "value": "Items"}]}]}]
    parameterize(rec, [{"step_id": "C.1", "data": {"Items": "1008018"}}])
    fill = rec[0]["actions"][0]
    check(fill["value"] == "${Items}" and fill.get("recorded_value") == "1008018",
          f"a linked value keeps what was typed when recording: {fill}")

    pw = [{"step_id": "C.1", "actions": [
        {"type": "fill", "value": "demo123", "masked": True, "target": 'field "Password"',
         "locators": [{"kind": "label", "value": "Password"}]}]}]
    parameterize(pw, [{"step_id": "C.1", "data": {"Password": "demo123"}}])
    check("demo123" not in json.dumps(pw), "a linked password is not kept as typed")
    check(data_changes(pw, {"Password": "demo123"}, {"Password": "other456"}) == [],
          "a changed password is never looked for on screen")

    check(data_changes(rec, {"Items": "1008018"}, {"Items": "1008017"}) == [("Items", "1008018", "1008017")],
          "a changed value is found")
    check(data_changes(rec, {"Items": "1008018"}, {"Items": "1008018"}) == [], "an unchanged value is not")
    short = [{"step_id": "C.1", "actions": [{"type": "fill", "value": "${Qty}", "param": "Qty"}]}]
    check(data_changes(short, {"Qty": "2"}, {"Qty": "3"}) == [], "a short value (2 -> 3) is never followed")
    stamped = [{"step_id": "C.1", "actions": [{"type": "fill", "value": "${Subject}", "param": "Subject"}]}]
    check(data_changes(stamped, {"Subject": "Test {now}"}, {"Subject": "Test 2026-11-04 10:00:00"}) == [],
          "a value made from {now} is not followed (the recorded text is unknown)")

    click = {"type": "click", "target": 'td "10080181"', "locators": [{"kind": "text", "value": "10080181"}]}
    check(follow_data(click, [("Items", "1008018", "1008017")])[1] == [],
          "a number that only contains the recorded one is left alone")

    old = {"steps": [{"actions": [{"type": "goto"}, {"type": "click"}]}], "recorded_by": "user02",
           "recorded_at": "2026-11-04T22:18:53+05:30"}
    empty = [{"actions": [{"type": "goto"}]}]
    check("existing recording was kept" in _keep_previous(old, empty),
          "a session that recorded nothing keeps the existing recording")
    check(_keep_previous(old, [{"actions": [{"type": "goto"}, {"type": "click"}]}]) == "",
          "a session that recorded something replaces it")
    check(_keep_previous(None, empty) == "", "a first recording is saved even when empty")


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    unit_checks()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    r = run("Shipment task", SHIPMENT_TASK)
    check(r["status"] == "passed", f"waits for the task list instead of clicking another task by position: {outcome(r)}")

    never = copy.deepcopy(SHIPMENT_TASK)
    never[0]["url"] += "?never"
    r = run("Task list never switches", never)
    err = r["steps"][0].get("error") or ""
    check(r["status"] == "failed" and "Create Miscellaneous Transaction" in err and "not clicked" in err,
          f"the other task at that position is named and not clicked: {err[:220]}")
    check(not any(e["type"] == "action" and e.get("status") == "pass" and "Manage Shipment Lines" in e["message"]
                  for e in r["events"]), "no other link was clicked in its place")

    r = run("Count in a name", COUNT_CHANGED)
    check(r["status"] == "passed", f"a control whose count changed is still clicked by its position: {outcome(r)}")

    r = run("Change order", CHANGE_ORDER, saved={"Items": "1008018"}, data={"Items": "1008017"})
    check(r["status"] == "passed", f"suggestion and link follow the changed item number: {outcome(r)}")
    check(any("now Items = \"1008017\"" in e["message"] for e in r["events"]), "the log says which data it followed")

    r = run("Payment process request", PAYMENT_REQUEST, saved={"Name": "test_ppr_01"}, data={"Name": "test_ppr_02"})
    check(r["status"] == "passed", f"Refresh is clicked until the renamed request shows up: {outcome(r)}")

    r = run("Springboard", SPRINGBOARD_CLICKS)
    check(r["status"] == "passed", f"a 3rd click on an arrow hidden at the end of the strip is skipped: {outcome(r)}")
    check(any("not needed" in e["message"] for e in r["events"]), "the skip is logged")

    r = run("Used-up requisition", USED_UP, saved={"Requisition": "US1015808"})
    err = r["steps"][0].get("error") or ""
    check(r["status"] == "failed" and "No results found" in err and "test data" in err,
          f"an empty search result is named as the likely cause: {err[-230:]}")

    print("\nAll replay-fix checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
