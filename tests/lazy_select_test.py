"""End-to-end test for a dropdown that only reports a choice once it has been pointed at.

The page copies the Business Unit list of an Oracle Fusion (ADF) sales order:
- ADF wires a dropdown's change handler when the control is first pointed at
  or focused, not when the page loads;
- the chosen value is sent to the server by that handler;
- the Bill-to Account list is filled by the server from the Business Unit it
  was sent, so it lists nothing when the choice never arrived.

A person's click on the dropdown points at it first, so the choice always
arrives. Replay must do the same before it chooses the option.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\lazy_select_test.py
Uses port 8773, a temporary data folder and STUDIO_SLOW_FACTOR=0.5 to keep it short.
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
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-select-"))
# These tests drive the API without signing in (allowed only on 127.0.0.1).
os.environ.setdefault("STUDIO_LOGIN", "off")
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from app import storage  # noqa: E402
from app.main import app  # noqa: E402

PORT = 8773
BASE = f"http://127.0.0.1:{PORT}"
server_bu = {"value": ""}  # the Business Unit the server was told about

PAGE = """<!doctype html><html><head><style>
  body{font-family:sans-serif;margin:20px} td{padding:4px}
</style></head><body>
<label for="bu">Business Unit</label>
<select id="bu"><option value="0"></option><option value="1">Verdesian UK Business Unit</option>
  <option value="2">Verdesian US Business Unit</option></select>
<p><a href="#" id="open" title="Search: Bill-to Account">&#9660;</a></p>
<div id="holder"></div>
<div id="msg"></div>
<script>
  const $ = id => document.getElementById(id);
  let pending = 0, wired = false;
  window.AdfPage = {PAGE: {isSynchronizedWithServer: () => pending === 0}};
  function wire() {   // ADF's lazy initialisation: the change handler only exists from here on
    if (wired) return;
    wired = true;
    $('bu').addEventListener('change', () => {
      const opt = $('bu').selectedOptions[0];
      pending++;
      fetch('/lazy-select/bu?value=' + encodeURIComponent(opt.textContent)).then(() => { pending--; });
    });
  }
  $('bu').addEventListener('mouseover', wire);
  $('bu').addEventListener('focus', wire);
  $('open').addEventListener('click', e => {
    e.preventDefault();
    pending++;
    fetch('/lazy-select/accounts').then(r => r.json()).then(rows => {
      pending--;
      $('holder').innerHTML = '<table id="drop">' +
        (rows.map(r => '<tr><td>' + r + '</td></tr>').join('') || '<tr><td>No results found.</td></tr>') + '</table>';
      $('drop').addEventListener('click', ev => {
        if (rows.includes(ev.target.textContent)) $('msg').textContent = 'Bill-to Account: ' + ev.target.textContent;
      });
    });
  });
</script></body></html>"""


@app.get("/lazy-select", include_in_schema=False)
def select_page():
    server_bu["value"] = ""
    return HTMLResponse(PAGE)


@app.get("/lazy-select/bu", include_in_schema=False)
def set_bu(value: str = ""):
    server_bu["value"] = value
    return JSONResponse(True)


@app.get("/lazy-select/accounts", include_in_schema=False)
def accounts():
    return JSONResponse(["185 - Winfield United - Winfield United"] if server_bu["value"] else [])


ACTIONS = [
    {"type": "goto", "url": f"{BASE}/lazy-select"},
    {"type": "select", "value": "Verdesian US Business Unit", "target": 'combobox "Business Unit"',
     "locators": [{"kind": "label", "value": "Business Unit"}]},
    {"type": "click", "target": 'a "Search: Bill-to Account"',
     "locators": [{"kind": "css", "value": 'a[title="Search: Bill-to Account"]'}]},
    {"type": "click", "target": 'td "185 - Winfield United - Winfield United"',
     "locators": [{"kind": "text", "value": "185 - Winfield United - Winfield United"}]},
    {"type": "expect_text", "text": "Bill-to Account: 185 - Winfield United - Winfield United"},
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
        st = http("GET", "/api/status")
        if not st["active"] and not st["others"]:  # nobody's session, including ones started without a user
            return
        time.sleep(0.5)
    raise TimeoutError("session did not finish")


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


def run(suite_id, case_id, actions):
    storage.save_recording(suite_id, case_id, {
        "suite_id": suite_id, "case_id": case_id, "start_url": f"{BASE}/lazy-select", "status": "completed",
        "steps": [{"step_id": f"{case_id}.1", "name": "Order header", "actions": copy.deepcopy(actions)}]})
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

    created = http("POST", "/api/freeform", {"title": "Lazily wired dropdown", "url": f"{BASE}/lazy-select"})
    suite_id, case_id = created["suite_id"], created["case_id"]
    suite = storage.load_suite(suite_id)
    storage.find_case(suite, case_id)["steps"] = [{"step_id": f"{case_id}.1", "name": "Order header", "data": {}}]
    storage.save_suite(suite)

    ok = run(suite_id, case_id, ACTIONS)
    for ev in ok.get("events", []):
        if ev["type"] in ("action", "info"):
            print("      ", ev.get("ms", ""), ev["message"][:160])
    check(ok["status"] == "passed",
          f"a dropdown choice reaches the server before the list that depends on it: "
          f"{ok['steps'][0].get('observed') or ok['steps'][0].get('error')}")
    print("\nAll lazily wired dropdown checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
