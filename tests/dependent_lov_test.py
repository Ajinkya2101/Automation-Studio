"""End-to-end test for a list of values that depends on a field typed just before it.

The page copies an Oracle Fusion (ADF) table row such as a miscellaneous
transaction line:
- Item is only checked by the server when focus leaves the field;
- that check redraws the row, which closes any dropdown that is open;
- the Subinventory dropdown lists nothing until the item has been checked.

A person never notices, because they pause between typing and clicking. Replay
must leave the typed field and wait for the check before it opens the dropdown.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\dependent_lov_test.py
Uses port 8772, a temporary data folder and STUDIO_SLOW_FACTOR=0.5 to keep it short.
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
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-lov-"))
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from app import storage  # noqa: E402
from app.main import app  # noqa: E402

PORT = 8772
BASE = f"http://127.0.0.1:{PORT}"

PAGE = """<!doctype html><html><head><style>
  body{font-family:sans-serif;margin:20px} td{padding:4px}
  #drop{border:1px solid #999;display:inline-block;margin-top:6px}
</style></head><body>
<div id="row"></div>
<div id="msg"></div>
<script>
  const $ = id => document.getElementById(id);
  let pending = 0, item = '', checked = false, sub = '';
  // The busy flag classic Fusion pages expose; replay reads it to know the app is working.
  window.AdfPage = {PAGE: {isSynchronizedWithServer: () => pending === 0}};
  function draw() {   // redrawing the row drops any open dropdown, as a partial page refresh does
    $('row').innerHTML = '<label for="item">Item</label> <input id="item"> ' +
      '<label for="sub">Subinventory</label> <input id="sub" readonly> ' +
      '<a href="#" id="open" title="Search: Subinventory">&#9660;</a><div id="holder"></div>';
    $('item').value = item;
    $('sub').value = sub;
    $('item').addEventListener('change', () => {
      item = $('item').value;
      pending++;
      fetch('/dependent-lov/check?item=' + encodeURIComponent(item)).then(r => r.json()).then(ok => {
        checked = ok; pending--; draw();
      });
    });
    $('open').addEventListener('click', e => {
      e.preventDefault();
      const rows = checked ? ['FINGOODS', 'STAGING'] : [];
      $('holder').innerHTML = '<table id="drop">' +
        (rows.map(r => '<tr><td>' + r + '</td></tr>').join('') || '<tr><td>No results found.</td></tr>') + '</table>';
      $('drop').addEventListener('click', ev => {
        if (!rows.includes(ev.target.textContent)) return;
        sub = ev.target.textContent;
        draw();
        $('msg').textContent = 'Line ready: ' + item + ' in ' + sub;
      });
    });
  }
  draw();
</script></body></html>"""


@app.get("/dependent-lov", include_in_schema=False)
def lov_page():
    return HTMLResponse(PAGE)


@app.get("/dependent-lov/check", include_in_schema=False)
def lov_check(item: str = ""):
    time.sleep(2)
    return JSONResponse(bool(item.strip()))


ACTIONS = [
    {"type": "goto", "url": f"{BASE}/dependent-lov"},
    {"type": "fill", "value": "1001342", "target": 'combobox "Item"', "locators": [{"kind": "label", "value": "Item"}]},
    {"type": "click", "target": 'a "Search: Subinventory"',
     "locators": [{"kind": "css", "value": 'a[title="Search: Subinventory"]'}]},
    {"type": "click", "target": 'td "FINGOODS"', "locators": [{"kind": "text", "value": "FINGOODS"}]},
    {"type": "expect_text", "text": "Line ready: 1001342 in FINGOODS"},
]


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


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


def run(suite_id, case_id, actions):
    storage.save_recording(suite_id, case_id, {
        "suite_id": suite_id, "case_id": case_id, "start_url": f"{BASE}/dependent-lov", "status": "completed",
        "steps": [{"step_id": f"{case_id}.1", "name": "Transaction line", "actions": copy.deepcopy(actions)}]})
    r = http("POST", f"/api/suites/{suite_id}/cases/{case_id}/run", {"headed": False})
    wait_idle()
    return http("GET", f"/api/runs/{r['run_id']}")


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    created = http("POST", "/api/freeform", {"title": "Dependent list of values", "url": f"{BASE}/dependent-lov"})
    suite_id, case_id = created["suite_id"], created["case_id"]
    suite = storage.load_suite(suite_id)
    storage.find_case(suite, case_id)["steps"] = [{"step_id": f"{case_id}.1", "name": "Transaction line", "data": {}}]
    storage.save_suite(suite)

    ok = run(suite_id, case_id, ACTIONS)
    for ev in ok.get("events", []):
        if ev["type"] in ("action", "info"):
            print("      ", ev.get("ms", ""), ev["message"][:160])
    check(ok["status"] == "passed",
          f"a value picked from a list that depends on the typed item: {ok['steps'][0].get('observed') or ok['steps'][0].get('error')}")
    print("\nAll dependent list-of-values checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
