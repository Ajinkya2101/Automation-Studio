"""End-to-end test for menus like the Oracle Fusion Navigator.

The page copies what broke a Fusion recording: menu groups are plain elements
with script click handlers (not buttons or links), their items are hidden until
the group is expanded, and more groups load only after the panel is scrolled.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\menu_scroll_test.py
Uses port 8770 and a temporary data folder.
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
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-menu-"))

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from app import storage  # noqa: E402
from app.events import sessions  # noqa: E402
from app.main import app  # noqa: E402
from app.recorder import record_session  # noqa: E402

PORT = 8770
BASE = f"http://127.0.0.1:{PORT}"

PAGE = """<!doctype html><html><head><style>
  body{font-family:sans-serif;margin:0}
  header{background:#333;color:#fff;padding:10px}
  #nav{color:#fff;cursor:pointer;text-decoration:underline}
  #panel{display:none;position:fixed;top:40px;left:0;width:320px;height:300px;overflow:auto;background:#fff;border:1px solid #999}
  .grp{padding:12px;border-bottom:1px solid #ddd;cursor:pointer}
  .items{display:none;padding-left:24px}
  .items a{display:block;padding:6px;cursor:pointer;color:#036}
  #content{margin:60px 0 0 360px;font-size:20px}
</style></head><body>
<header><a id="nav" role="link" tabindex="0">Navigator</a></header>
<div id="panel"></div>
<div id="content">Welcome</div>
<script>
  const panel = document.getElementById('panel');
  function addGroup(name, items) {
    const g = document.createElement('div');
    g.className = 'grp';
    g.innerHTML = '<span>' + name + '</span> <span>&#9662;</span>';   // plain element, script handler
    const list = document.createElement('div');
    list.className = 'items';
    items.forEach(t => {
      const a = document.createElement('a');
      a.setAttribute('role', 'link'); a.tabIndex = 0; a.textContent = t;
      a.addEventListener('click', () => { document.getElementById('content').textContent = t + ' work area'; panel.style.display = 'none'; });
      list.appendChild(a);
    });
    g.addEventListener('click', () => { list.style.display = list.style.display === 'block' ? 'none' : 'block'; });
    panel.appendChild(g); panel.appendChild(list);
  }
  ['Me', 'My Team', 'My Client Groups', 'Benefits', 'Workspace', 'Sales', 'Service', 'Help Desk', 'Order Management',
   'Contract Management'].forEach(n => addGroup(n, [n + ' overview']));
  let loaded = false;
  panel.addEventListener('scroll', () => {   // more groups load only near the bottom
    if (!loaded && panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 5) {
      loaded = true;
      addGroup('Payables', ['Invoices', 'Payments']);
      addGroup('Receivables', ['Billing', 'Cash App Remittance', 'Receipts']);
    }
  });
  document.getElementById('nav').addEventListener('click', () => { panel.style.display = 'block'; });
</script></body></html>"""


@app.get("/fusion-like", include_in_schema=False)
def fusion_like():
    return HTMLResponse(PAGE)


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


def act_like_a_user(page):
    page.get_by_role("link", name="Navigator").click()
    page.locator("#panel").hover()
    page.mouse.wheel(0, 3000)                       # a real wheel scroll, like the user
    page.get_by_text("Receivables", exact=True).wait_for()
    page.wait_for_timeout(600)                      # the recorder saves a scroll once it stops
    page.get_by_text("Receivables", exact=True).click()
    page.get_by_role("link", name="Cash App Remittance").click()
    page.locator("#check").click()
    page.locator("#content").click()
    page.locator("#finish").click()


def run(suite_id, case_id):
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

    created = http("POST", "/api/freeform", {"title": "Open Cash App Remittance", "url": f"{BASE}/fusion-like"})
    suite_id, case_id = created["suite_id"], created["case_id"]
    suite = storage.load_suite(suite_id)
    case = storage.find_case(suite, case_id)
    s = sessions.start("record", record_session, suite_id=suite_id, case_id=case_id, suite=suite, case=case,
                       headless=True, on_ready=act_like_a_user)
    wait_idle()
    if s.status != "completed":
        for ev in s.log.all():
            print("   log:", ev["type"], ev["message"][:300])
    check(s.status == "completed", "recording completed")

    rec = storage.load_recording(suite_id, case_id)
    acts = rec["steps"][0]["actions"]
    for a in acts:
        print("      ", a["type"], a.get("target", a.get("url", "")), a.get("y", ""))
    scrolls = [a for a in acts if a["type"] == "scroll"]
    check(len(scrolls) == 1 and not scrolls[0]["page"] and scrolls[0]["y"] > 0,
          f"panel scroll recorded once, at its final position ({scrolls[0]['y'] if scrolls else '-'} px)")
    groups = [a for a in acts if a["type"] == "click" and "Receivables" in a["target"]]
    check(len(groups) == 1, f"click on the plain menu group recorded: {groups[0]['target'] if groups else 'missing'}")
    order = [a["type"] for a in acts]
    check(order.index("scroll") < order.index("click", order.index("scroll")),
          "scroll recorded before the click that needed it")

    ok = run(suite_id, case_id)
    check(ok["status"] == "passed", f"replay passed: {ok['steps'][0].get('observed') or ok['steps'][0].get('error')}")

    # Without the group click, the item is in the page but hidden: the run must say so clearly.
    broken = copy.deepcopy(rec)
    broken["steps"][0]["actions"] = [a for a in acts if a not in groups]
    storage.save_recording(suite_id, case_id, broken)
    bad = run(suite_id, case_id)
    err = bad["steps"][0].get("error") or ""
    check(bad["status"] == "failed" and "stayed hidden" in err and "Cash App Remittance" in err,
          f"missing group click gives a clear error: {err[:150]}")
    print("\nAll menu and scroll checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
