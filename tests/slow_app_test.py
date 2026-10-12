"""End-to-end test for a slow application, like an overloaded Oracle Fusion pod.

The page copies what broke Fusion runs on a slow instance:
- the search box is on screen at once but only reacts to Enter seconds later,
  so the first Enter is lost and nothing is found;
- search results come from a slow server request;
- choosing a field from a dropdown adds its row only after a delay, and the new
  field stays read-only for a while longer;
- Save is disabled until the field has a value.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\slow_app_test.py
Uses port 8771, a temporary data folder and STUDIO_SLOW_FACTOR=0.5 to keep it short.
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
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-slow-"))
os.environ.setdefault("STUDIO_SLOW_FACTOR", "0.5")

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse  # noqa: E402

from app import storage  # noqa: E402
from app.main import app  # noqa: E402

PORT = 8771
BASE = f"http://127.0.0.1:{PORT}"

PAGE = """<!doctype html><html><head><style>
  body{font-family:sans-serif;margin:20px} #results a{display:block;margin:6px 0}
  #form{display:none;margin-top:20px} td{padding:4px}
</style></head><body>
<label for="q">Search</label> <input id="q">
<div id="results"></div>
<div id="form">
  <label for="add">Add field</label>
  <select id="add"><option value="">Choose</option><option>Amount</option></select>
  <table id="rows"></table>
  <button id="save" disabled>Save</button>
</div>
<div id="msg"></div>
<script>
  const $ = id => document.getElementById(id);
  setTimeout(() => {   // on screen at once, wired up only later: an early Enter does nothing
    $('q').addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      fetch('/slow-fusion/search?q=' + encodeURIComponent($('q').value)).then(r => r.json()).then(names => {
        $('results').innerHTML = '';
        names.forEach(n => {
          const a = document.createElement('a');
          a.href = '#'; a.textContent = n;
          a.onclick = e => { e.preventDefault(); $('form').style.display = 'block'; };
          $('results').appendChild(a);
        });
      });
    });
  }, 8000);
  $('add').addEventListener('change', () => {
    setTimeout(() => {   // the chosen field's row takes a while to appear...
      $('rows').insertAdjacentHTML('beforeend',
        '<tr><td><label for="amt">Amount</label></td><td><input id="amt" disabled></td></tr>');
      setTimeout(() => {   // ...and stays read-only a bit longer
        $('amt').disabled = false;
        $('amt').addEventListener('input', () => { $('save').disabled = !$('amt').value; });
      }, 3000);
    }, 5000);
  });
  $('save').addEventListener('click', () =>
    fetch('/slow-fusion/save?v=' + encodeURIComponent($('amt').value)).then(r => r.text()).then(t => { $('msg').textContent = t; }));
</script></body></html>"""


@app.get("/slow-fusion", include_in_schema=False)
def slow_page():
    return HTMLResponse(PAGE)


@app.get("/slow-fusion/search", include_in_schema=False)
def slow_search(q: str = ""):
    time.sleep(3)
    return JSONResponse(["Abhishek Beck"] if q.strip() else [])


@app.get("/slow-fusion/save", include_in_schema=False)
def slow_save(v: str = ""):
    time.sleep(2)
    return PlainTextResponse(f"Saved {v}")


def by_label(text):
    return [{"kind": "label", "value": text}]


ACTIONS = [
    {"type": "goto", "url": f"{BASE}/slow-fusion"},
    {"type": "fill", "value": "abhishek", "target": 'field "Search"', "locators": by_label("Search")},
    {"type": "press", "key": "Enter", "target": 'field "Search"', "locators": by_label("Search")},
    {"type": "click", "target": 'link "Abhishek Beck"',
     "locators": [{"kind": "role", "role": "link", "value": "Abhishek Beck"}]},
    {"type": "select", "value": "Amount", "target": 'combobox "Add field"', "locators": by_label("Add field")},
    {"type": "fill", "value": "42", "target": 'field "Amount"', "locators": by_label("Amount")},
    {"type": "click", "target": 'button "Save"', "locators": [{"kind": "role", "role": "button", "value": "Save"}]},
    {"type": "expect_text", "text": "Saved 42"},
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
        "suite_id": suite_id, "case_id": case_id, "start_url": f"{BASE}/slow-fusion", "status": "completed",
        "steps": [{"step_id": f"{case_id}.1", "name": "Slow screen", "actions": copy.deepcopy(actions)}]})
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

    created = http("POST", "/api/freeform", {"title": "Slow Fusion screen", "url": f"{BASE}/slow-fusion"})
    suite_id, case_id = created["suite_id"], created["case_id"]
    suite = storage.load_suite(suite_id)
    storage.find_case(suite, case_id)["steps"] = [{"step_id": f"{case_id}.1", "name": "Slow screen", "data": {}}]
    storage.save_suite(suite)

    ok = run(suite_id, case_id, ACTIONS)
    for ev in ok.get("events", []):
        if ev["type"] in ("action", "info"):
            print("      ", ev.get("ms", ""), ev["message"][:160])
    check(ok["status"] == "passed", f"slow run passed: {ok['steps'][0].get('observed') or ok['steps'][0].get('error')}")
    repeats = [ev["message"] for ev in ok["events"] if "Repeating it" in ev["message"]]
    check(len(repeats) == 1 and "Press Enter" in repeats[0], "the lost Enter was noticed and repeated once")

    # Without typing the amount, Save never enables: the run must say so instead of a bare timeout.
    bad = run(suite_id, case_id, [a for a in ACTIONS if a.get("value") != "42"])
    err = bad["steps"][0].get("error") or ""
    check(bad["status"] == "failed" and "stayed disabled" in err and "Save" in err,
          f"a control that never enables gives a clear error: {err[:150]}")
    print("\nAll slow-application checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
