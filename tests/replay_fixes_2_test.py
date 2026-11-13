"""End-to-end tests for the fixes from the deployed studio's runs of 9 and 12 Nov 2026.

Each page copies one situation from those runs:
- a pick slip that Oracle numbers anew each run: the recording clicked link "1156596", the
  run's search lists "1157596" (Shipping a standard sales order). One record in the list is
  this run's; with several, none may be clicked;
- a message with this run's shipment number, "The shipment 1218329 was created.", whose
  number later actions use;
- a list of values with nothing to offer ("Search..." only) over a table that has the
  value the recording picked (Item Structure Creation from Copy);
- an Oracle error with its code, EGO-2745049 (Change order);
- a worklist whose invoice link names another invoice each run, made test data from the
  clicked text (Invoice Approvals - 2);
plus the linking of typed values (two GL codes of 4111, a user name typed over), the repair
command for existing tests, and the dashboard script.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\replay_fixes_2_test.py
Uses port 8777, a temporary data folder and STUDIO_SLOW_FACTOR=0.5.
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-replay-fixes-2-"))
os.environ.setdefault("STUDIO_LOGIN", "off")  # drives the API without signing in (only allowed on 127.0.0.1)
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")
os.environ.setdefault("STUDIO_ACTION_TIMEOUT_MS", "30000")  # 15 s with the slow factor

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from app import relink, storage  # noqa: E402
from app.actions import data_changes, parameterize  # noqa: E402
from app.config import DATA_DIR  # noqa: E402
from app.main import app  # noqa: E402
from app.recorder import freeform_steps  # noqa: E402
from app.replayer import _numbers_only_differ  # noqa: E402

PORT = 8777
BASE = f"http://127.0.0.1:{PORT}"
STYLE = "<style>body{font-family:sans-serif;margin:20px} td{padding:4px;border:1px solid #ccc}</style>"

# Searching by order lists its pick slips; Oracle numbered them anew (1157596, not 1156596 as recorded).
# ?rows=2 lists two. A pick slip page offers 'Confirm <number>'.
PICKSLIPS = """<!doctype html><html><head>""" + STYLE + """</head><body>
<label for="order">Order</label> <input id="order"> <button id="search">Search</button>
<table><tbody id="rows"></tbody></table><div id="slip"></div><div id="msg"></div>
<script>
  const $ = id => document.getElementById(id);
  const n = new URLSearchParams(location.search).get('rows') || '1';
  $('search').addEventListener('click', () => setTimeout(() => {
    const nums = ['1157596', '1157597'].slice(0, Number(n));
    $('rows').innerHTML = nums.map(x => '<tr><td>' + $('order').value + '</td><td><span><a href="#">' + x
      + '</a></span></td></tr>').join('');
    document.querySelectorAll('#rows a').forEach(a => a.onclick = e => {
      e.preventDefault();
      $('slip').innerHTML = '<a href="#" id="confirm">Confirm ' + a.textContent + '</a>';
      $('confirm').onclick = ev => { ev.preventDefault(); $('msg').textContent = 'Confirmed ' + a.textContent; };
    });
  }, 300));
</script></body></html>"""

# Autocreate Shipment reports this run's shipment number, then links to it.
SHIPMENT = """<!doctype html><html><head>""" + STYLE + """</head><body>
<button id="create">Autocreate Shipment</button>
<table><tbody id="msgs"></tbody></table><div id="links"></div><div id="msg"></div>
<script>
  const $ = id => document.getElementById(id);
  $('create').addEventListener('click', () => {
    $('msgs').innerHTML = '<tr><td>The shipment 1218329 was created.</td></tr>';
    $('msgs').querySelector('td').onclick = () => {
      $('links').innerHTML = '<a href="#" id="s">1218329</a>';
      $('s').onclick = e => { e.preventDefault(); $('msg').textContent = 'Opened shipment 1218329'; };
    };
  });
</script></body></html>"""

# 'Search: Structure Name' opens a dialog (glass over the page) whose list has nothing to offer;
# the table behind it already shows 'Primary'.
EMPTY_LOV = """<!doctype html><html><head>""" + STYLE + """</head><body>
<table><tbody><tr><td>Primary</td><td>Allowed</td></tr></tbody></table>
<a href="#" id="lov" title="Search: Structure Name">Search: Structure Name</a>
<div id="glass" class="AFModalGlassPane" style="display:none;position:fixed;inset:0;z-index:30"></div>
<div id="pop" data-afr-popupid="pop" style="display:none;position:fixed;left:200px;top:150px;z-index:40;
     background:#fff;border:1px solid #888;padding:10px"><a href="#">Search...</a></div>
<script>
  document.getElementById('lov').addEventListener('click', e => {
    e.preventDefault();
    document.getElementById('glass').style.display = 'block';
    document.getElementById('pop').style.display = 'block';
  });
</script></body></html>"""

# Submit shows an Oracle error with its code; the recording then expects a Refresh that never comes.
ORACLE_ERROR = """<!doctype html><html><head>""" + STYLE + """</head><body>
<button id="submit">Submit</button>
<div id="err" data-afr-popupid="err" style="display:none;border:1px solid #c00;padding:8px">
  <b>Error</b> You must provide a valid value for the Status attribute. (EGO-2745049) <button id="ok">OK</button></div>
<script>
  document.getElementById('submit').addEventListener('click', () => {
    document.getElementById('err').style.display = 'block'; });
  document.getElementById('ok').addEventListener('click', () => {
    document.getElementById('err').style.display = 'none'; });
</script></body></html>"""

# The approver's worklist: another invoice is first, this run's further down.
WORKLIST = """<!doctype html><html><head>""" + STYLE + """</head><body>
<table><tbody>
  <tr><td>1</td><td>x</td><td><span><table><tbody><tr><td><a href="#">Approval of Invoice test_inv_10 from Prince Agri Products, Inc. (-90.00 USD)</a></td></tr></tbody></table></span></td></tr>
  <tr><td>2</td><td>x</td><td><span><table><tbody><tr><td><a href="#">Approval of Invoice TEST_INV_16 from Wheatland Tube LLC (125.00 USD)</a></td></tr></tbody></table></span></td></tr>
</tbody></table><div id="msg"></div>
<script>
  document.querySelectorAll('a').forEach(a => a.onclick = e => {
    e.preventDefault(); document.getElementById('msg').textContent = 'Opened ' + a.textContent.split(' ')[3]; });
</script></body></html>"""

PAGES = {"pickslips": PICKSLIPS, "shipment": SHIPMENT, "emptylov": EMPTY_LOV, "oracle": ORACLE_ERROR,
         "worklist": WORKLIST}


@app.get("/replay-fixes-2/{name}", include_in_schema=False)
def fixes_page(name: str):
    return HTMLResponse(PAGES[name])


def goto(name, query=""):
    return {"type": "goto", "url": f"{BASE}/replay-fixes-2/{name}{query}"}


def button(name):
    return {"type": "click", "target": f'button "{name}"', "locators": [{"kind": "role", "role": "button", "value": name}]}


def link(name, css=None):
    locs = [{"kind": "role", "role": "link", "value": name}, {"kind": "text", "value": name}]
    return {"type": "click", "target": f'link "{name}"', "locators": locs + ([{"kind": "css", "value": css}] if css else [])}


PICK_SLIP = [
    goto("pickslips"),
    {"type": "fill", "value": "${Order}", "param": "Order", "recorded_value": "25721", "target": 'field "Order"',
     "locators": [{"kind": "label", "value": "Order"}]},
    button("Search"),
    link("1156596", "tbody > tr > td:nth-of-type(2) > span > a"),
    link("Confirm 1156596"),
    {"type": "expect_text", "text": "Confirmed 1157596"},
]

SHIPMENT_MESSAGE = [
    goto("shipment"),
    button("Autocreate Shipment"),
    {"type": "click", "target": 'td "The shipment 1218328 was created."',
     "locators": [{"kind": "text", "value": "The shipment 1218328 was created."},
                  {"kind": "css", "value": "table > tbody > tr:nth-of-type(1) > td:nth-of-type(1)"}]},
    link("1218328"),
    {"type": "expect_text", "text": "Opened shipment 1218329"},
]

EMPTY_LIST_PICK = [
    goto("emptylov"),
    link("Search: Structure Name"),
    {"type": "click", "target": 'td "Primary"', "locators": [{"kind": "text", "value": "Primary"}]},
]

ORACLE_CODE = [goto("oracle"), button("Submit"), button("Refresh")]

INVOICE = "Approval of Invoice TEST_INV_15 from Wheatland Tube LLC (100.00 USD)"
APPROVAL = [
    goto("worklist"),
    {"type": "click", "target": f'link "{INVOICE[:60]}"', "navigates": True,
     "locators": [{"kind": "role", "role": "link", "value": INVOICE}, {"kind": "text", "value": INVOICE},
                  {"kind": "css", "value": "tr:nth-of-type(1) > td:nth-of-type(3) > span > table > tbody > tr > td > a"}]},
    {"type": "expect_text", "text": "Opened ${Invoice}", "param": "Invoice"},
]


def http(method, path, payload=None, expect_error=False):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method,
                                 headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        if not expect_error:
            raise
        return {"status": exc.code, **json.loads(exc.read() or b"{}")}


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


def fill(label, value, **extra):
    return {"type": "fill", "value": value, "target": f'field "{label}"',
            "locators": [{"kind": "label", "value": label}], **extra}


def click(target, **extra):
    return {"type": "click", "target": target, "locators": [{"kind": "text", "value": target}], **extra}


def unit_checks():
    # Two GL codes typed as 4111: each links to its own data (both were ${GL_Code_Location}).
    rec = [{"step_id": "C.1", "actions": [fill("GL_Code_Location", "4111"), fill("GL_Code_Product_Cat", "4111")]}]
    parameterize(rec, [{"step_id": "C.1", "data": {"GL_Code_Location": "4111", "GL_Code_Product_Cat": "4111"}}])
    check([a["param"] for a in rec[0]["actions"]] == ["GL_Code_Location", "GL_Code_Product_Cat"],
          f"the same value in two fields links each to its own data: {[a['param'] for a in rec[0]['actions']]}")

    case = {"id": "REC-1"}
    def built(actions):
        steps, recs = freeform_steps(case, [{"step_id": "REC-1.1", "name": "Step 1", "data": {}}],
                                     [{"step_id": "REC-1.1", "name": "Step 1", "actions": copy.deepcopy(actions)}])
        return steps[0]["data"], recs[0]["actions"]

    # The sign-in page came up with the last user; the tester typed over it.
    data, acts = built([fill("Username", "VLS.AMS.User"), fill("Password", "x", masked=True),
                        click('field "Username"'), click('field "Username"'), fill("Username", "jennifer.watkins"),
                        click('field "Password"'), fill("Password", "y", masked=True), click('button "Next"')])
    check(data == {"Username": "jennifer.watkins"}, f"a user name typed over is one value, the last: {data}")
    check([a.get("value") for a in acts if a["type"] == "fill" and not a.get("masked")] == ["${Username}"] * 2
          and acts[4].get("recorded_value") == "jennifer.watkins", "both fills type ${Username}")

    data, _ = built([fill("Search", "A100"), click('button "Search"'), fill("Search", "B200")])
    check(data == {"Search": "A100", "Search 2": "B200"}, f"a field searched twice keeps two values: {data}")
    data, acts = built([fill("Amount", "125"), click('button "Next"'), fill("Amount", "125")])
    check(data == {"Amount": "125"} and acts[2]["value"] == "${Amount}",
          f"the same amount on header and line is one value: {data}")
    data, _ = built([fill("GL_Code_Location", "4111"), fill("GL_Code_Product_Cat", "4111")])
    check(data == {"GL_Code_Location": "4111", "GL_Code_Product_Cat": "4111"}, f"two GL codes stay two: {data}")

    click_tp = [{"step_id": "C.1", "actions": [{"type": "click", "target": "link", "locators": [],
                                                "text_params": [{"key": "Invoice", "value": "TEST_INV_15"}]}]}]
    check(data_changes(click_tp, {"Invoice": "TEST_INV_15"}, {"Invoice": "TEST_INV_16"})
          == [("Invoice", "TEST_INV_15", "TEST_INV_16")], "clicked text made test data is followed")
    check(_numbers_only_differ("The shipment 1218328 was created.", "The shipment 1218329 was created.")
          == [("1218328", "1218329")], "a message that differs only in its record number is recognised")
    check(_numbers_only_differ("Order 25721 shipped", "Order 25721 cancelled") == [],
          "a message with other words is not")


def relink_check():
    """An Invoice Approvals - 2 like test as saved on 9 Nov: the repair command fixes it, with a backup."""
    actions = [goto("worklist"),
               fill("Username", "${Username}", param="Username"), fill("Password", "a", masked=True),
               click('field "Username"'), fill("Username", "${Username 2}", param="Username 2"),
               click('button "Next"'),
               {"type": "fill", "value": "${GL_Code_Location}", "param": "GL_Code_Location",
                "target": 'combobox "GL_Code_Location"', "locators": [{"kind": "label", "value": "GL_Code_Location"}]},
               {"type": "fill", "value": "${GL_Code_Location}", "param": "GL_Code_Location",
                "target": 'combobox "GL_Code_Product_Cat"', "locators": [{"kind": "label", "value": "GL_Code_Product_Cat"}]}]
    suite_id, case_id = new_case("Old linking", {"Username": "VLS.AMS.User", "Username 2": "jennifer.watkins",
                                                 "GL_Code_Location": "4111", "GL_Code_Product_Cat": "4111"}, actions)
    suite = storage.load_suite(suite_id)
    storage.find_case(suite, case_id)["freeform"] = True
    storage.save_suite(suite)
    relink.main(["--apply", "--user", "tester"])
    rec = storage.load_recording(suite_id, case_id)
    params = [a.get("param") for a in rec["steps"][0]["actions"] if a["type"] == "fill" and not a.get("masked")]
    data = storage.find_case(storage.load_suite(suite_id), case_id)["steps"][0]["data"]
    check(params == ["Username", "Username", "GL_Code_Location", "GL_Code_Product_Cat"],
          f"the repair links each field to its own data: {params}")
    check(data == {"Username": "jennifer.watkins", "GL_Code_Location": "4111", "GL_Code_Product_Cat": "4111"},
          f"and the test data has no 'Username 2' any more: {data}")
    check(any((DATA_DIR / "backups").glob(f"relink-*/{suite_id}/suite.json")), "the test was backed up first")
    again = relink.relink(storage.load_suite(suite_id), storage.find_case(storage.load_suite(suite_id), case_id), rec)
    check(again is None, "running the repair again changes nothing")


def dashboard_check(suite_id, case_id):
    """The dashboard script loads, shows clicked text made test data, and survives events with no live panel."""
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(BASE + "/")
        # Polled from here: the dashboard's Content-Security-Policy forbids the eval that wait_for_function uses.
        for _ in range(100):
            if page.evaluate("() => typeof selectCase === 'function'"):
                break
            page.wait_for_timeout(100)
        page.evaluate("([s, c]) => selectCase(s, c)", [suite_id, case_id])
        page.evaluate("() => { S.tab = 'recording'; renderMain(); }")
        page.wait_for_selector("[data-tp-del]")
        shown = page.inner_text("#tab-body")
        # Events arriving while the user looks at another test (S.live null) must not break the page.
        page.evaluate("""() => { const live = newLive('run'); S.sessionLive = live; S.live = null;
                                 applyEvent(live, {type: 'run_start', steps: [], message: 'x', ts: 1});
                                 if (S.live === live) renderLive(); }""")
        b.close()
    check("${Invoice}" in shown, "the Recording tab shows the clicked text as ${Invoice}")
    check(not errors, f"no script errors in the dashboard: {errors}")


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    unit_checks()

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    r = run("Pick slip, one in the list", PICK_SLIP, saved={"Order": "25721"})
    check(r["status"] == "passed", f"the one pick slip in the list is clicked under its new number: {error_of(r)}")
    check(any("new number" in e["message"] and "1157596" in e["message"] for e in r["events"]),
          "the log says which number it clicked instead")

    two = copy.deepcopy(PICK_SLIP)
    two[0]["url"] += "?rows=2"
    r = run("Pick slip, two in the list", two, saved={"Order": "25721"})
    err = error_of(r)
    check(r["status"] == "failed" and "2 other numbers" in err and "none was clicked" in err,
          f"with two pick slips listed, none is clicked: {err[:260]}")
    check(not any(e["type"] == "action" and e.get("status") == "pass" and "1156596" in e["message"]
                  for e in r["events"]), "no pick slip was clicked")

    r = run("Shipment message", SHIPMENT_MESSAGE)
    check(r["status"] == "passed", f"the shipment number from the message is used by the later link: {error_of(r)}")

    r = run("Empty list of values", EMPTY_LIST_PICK)
    err = error_of(r)
    check(r["status"] == "failed" and "offers nothing to choose" in err and "dialog that the recording" not in err,
          f"an empty list is named as such, not as a dialog: {err[:240]}")

    r = run("Oracle error", ORACLE_CODE)
    err = error_of(r)
    check(r["status"] == "failed" and "EGO-2745049" in err, f"the Oracle error is part of the failure: {err[-200:]}")
    check(any(e.get("level") == "warn" and "EGO-2745049" in e["message"] for e in r["events"]),
          "the message is logged when it appears")
    check(any(e["type"] == "action" and e.get("status") == "fail" and "EGO-2745049" in e["message"]
              for e in r["events"]), "the live log's failure line has the whole explanation")

    suite_id, case_id = new_case("Invoice approval", {}, APPROVAL)
    path = f"/api/suites/{suite_id}/cases/{case_id}/text-params"
    bad = http("POST", path, {"step": 0, "action": 1, "text": "INV_99", "key": "Invoice"}, expect_error=True)
    check(bad.get("status") == 400, f"text that is not in the click is refused: {bad}")
    bad = http("POST", path, {"step": 0, "action": 0, "text": "TEST_INV_15", "key": "Invoice"}, expect_error=True)
    check(bad.get("status") == 400, "only a click's text can be made test data")
    http("POST", path, {"step": 0, "action": 1, "text": "TEST_INV_15", "key": "Invoice"})
    case = storage.find_case(storage.load_suite(suite_id), case_id)
    check(case["steps"][0]["data"].get("Invoice") == "TEST_INV_15", "the clicked text is now test data 'Invoice'")
    r = start(suite_id, case_id, {"Invoice": "TEST_INV_16"})
    check(r["status"] == "passed", f"a run with Invoice = TEST_INV_16 approves that invoice: {error_of(r)}")
    check(not any("test_inv_10" in e["message"] and e["type"] == "action" for e in r["events"]),
          "the other invoice at the recorded position was not clicked")
    audit = (DATA_DIR / "logs" / "audit.jsonl").read_text(encoding="utf-8")
    check('"add_test_data"' in audit, "adding test data is in the audit log")

    dashboard_check(suite_id, case_id)

    http("DELETE", path, {"step": 0, "action": 1, "key": "Invoice"})
    rec = storage.load_recording(suite_id, case_id)
    case = storage.find_case(storage.load_suite(suite_id), case_id)
    check("text_params" not in rec["steps"][0]["actions"][1] and "Invoice" in case["steps"][0]["data"],
          "removing it from the link keeps the test data, which the check still uses")

    relink_check()

    print("\nAll replay-fix (12 Nov) checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
