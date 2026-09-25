"""Checks recording and replay against Outlook-style rich-text editors.

Outlook on the web uses contenteditable elements: the To box turns a typed
address into a recipient "chip" on Enter, and the body is a multi-line editor.
This test serves a page that behaves the same way and also checks {now}
tokens in test data.  Run:  .venv\\Scripts\\python tests\\rich_editor_test.py
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-rich-"))

import uvicorn  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from app import storage  # noqa: E402
from app.events import sessions  # noqa: E402
from app.main import app  # noqa: E402
from app.recorder import record_session  # noqa: E402
from app.replayer import run_session  # noqa: E402

PORT = 8767
BASE = f"http://127.0.0.1:{PORT}"

PAGE = """<!doctype html><html><body style="font-family:sans-serif;padding:20px">
<div id="app">
<h1>Compose</h1>
<div>To: <span id="chips"></span>
  <div id="to" contenteditable="true" role="textbox" aria-label="To" style="display:inline-block;min-width:300px;border:1px solid #999;padding:4px"></div></div>
<p><label>Add a subject <input id="subject" aria-label="Add a subject"></label></p>
<div id="body" contenteditable="true" role="textbox" aria-multiline="true" aria-label="Message body"
     style="border:1px solid #999;min-height:120px;padding:6px;white-space:pre-wrap"></div>
<p><button id="send" aria-label="Send" title="Send (Ctrl+Enter)"><span class="icon">&#xf699;</span>
<span>Send</span></button></p>
<div id="status" role="status"></div>
</div>
<script>
  // Like Outlook: the Send button's id is regenerated on every load.
  document.getElementById('send').id = 'splitButton-r' + Math.floor(Math.random() * 900 + 100) + '__primaryActionButton';
  // Like Outlook's editor: keystrokes are inserted by the editor itself, so the
  // browser never fires 'input' events for the message body.
  const bodyEl = document.getElementById('body');
  bodyEl.addEventListener('beforeinput', e => {
    const text = e.inputType === 'insertText' ? e.data
      : (e.inputType === 'insertParagraph' || e.inputType === 'insertLineBreak') ? '\\n' : null;
    if (text === null) return;
    e.preventDefault();
    const sel = getSelection(), r = sel.getRangeAt(0);
    r.deleteContents();
    const node = document.createTextNode(text);
    r.insertNode(node); r.setStartAfter(node); r.collapse(true);
    sel.removeAllRanges(); sel.addRange(r);
  });
  window.__bodyInputEvents = 0;
  bodyEl.addEventListener('input', () => window.__bodyInputEvents++);
  // Like Outlook's Send: clicking it keeps focus in the editor.
  document.querySelector('[aria-label=Send]').addEventListener('mousedown', e => e.preventDefault());
  const to = document.getElementById('to');
  to.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const addr = to.innerText.trim();
    if (!addr) return;
    const chip = document.createElement('span');
    chip.className = 'chip'; chip.textContent = addr + '; ';
    document.getElementById('chips').appendChild(chip);
    to.textContent = '';  // script change: no input event, like a real recipient picker
    // Like Outlook: a popup opens and the whole app is hidden from assistive tech.
    document.getElementById('app').setAttribute('aria-hidden', 'true');
    if (!document.querySelector('[role=dialog]')) document.body.appendChild(Object.assign(document.createElement('div'), {role: 'dialog'})).setAttribute('role', 'dialog');
  });
  document.querySelector('[aria-label=Send]').onclick = () => {
    const recips = [...document.querySelectorAll('.chip')].map(c => c.textContent.replace('; ', ''));
    const body = document.getElementById('body').innerText.trim();
    document.getElementById('status').textContent = recips.length && body
      ? 'Sent "' + document.getElementById('subject').value + '" to ' + recips.join(', ')
      : 'Nothing sent';
  };
</script></body></html>"""


@app.get("/rich-compose", include_in_schema=False)
def rich_compose():
    return HTMLResponse(PAGE)


def check(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        raise SystemExit(1)


def wait(session, timeout=90):
    end = time.time() + timeout
    while session.status == "running" and time.time() < end:
        time.sleep(0.3)
    return session.status


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    case = {"id": "TS_RICH", "sheet": "TS_RICH", "title": "Rich editor", "steps": [
        {"step_id": "TS_RICH.1", "name": "Write and send", "description": "Write the email and send it",
         "expected": "Sent", "data": {"URL": f"{BASE}/rich-compose", "To": "abhosale@acsesolutions.com",
                                       "Subject": "Automation Studio test {now}",
                                       "Body": "Line one\nLine two"}},
    ]}
    suite = {"id": storage.new_id("suite"), "cases": [case]}
    subject_seen = {}

    def user(page):
        page.get_by_label("To").click()
        page.keyboard.type("abhosale@acsesolutions.com")
        page.keyboard.press("Enter")
        subject = page.evaluate("() => document.querySelector('#__rec_toolbar_host').shadowRoot"
                                ".querySelector('.data').innerText")
        subject_seen["toolbar"] = subject
        value = [ln for ln in subject.splitlines() if ln.startswith("Automation Studio test")][0]
        subject_seen["value"] = value
        page.locator("#subject").click()
        page.keyboard.type(value)
        page.locator("#body").click()
        page.keyboard.type("Line one")
        page.keyboard.press("Enter")
        page.keyboard.type("Line two")
        subject_seen["input_events"] = page.evaluate("window.__bodyInputEvents")
        page.locator("button[aria-label=Send]").click()
        subject_seen["focus_after_send"] = page.evaluate("document.activeElement.id")
        page.locator("#check").click()
        page.locator("#status").click()
        page.locator("#next").click()

    s = sessions.start("record", record_session, suite_id=suite["id"], case_id=case["id"],
                       suite=suite, case=case, headless=True, on_ready=user)
    if wait(s) != "completed":
        for ev in s.log.all():
            print("   log:", ev["type"], ev["message"][:1500])
    check(s.status == "completed", "recording completed")
    rec = storage.load_recording(suite["id"], case["id"])
    acts = rec["steps"][0]["actions"]
    summary = [(a["type"], a.get("target", ""), a.get("value", a.get("text", ""))) for a in acts]
    for row in summary:
        print("      ", row)
    check(subject_seen["input_events"] == 0 and subject_seen["focus_after_send"] == "body",
          "test page reproduces Outlook: no input events from the body, Send keeps focus in it")
    check(rec["start_url"] == f"{BASE}/rich-compose", "start URL taken from the Excel URL data")
    check("{now}" not in subject_seen["toolbar"], "toolbar shows the resolved {now} subject")
    fills = [a for a in acts if a["type"] == "fill"]
    to_fills = [a for a in fills if a["target"] == 'field "To"']
    check(len(to_fills) == 1 and to_fills[0]["value"] == "${To}", "To editor recorded once and linked to ${To}")
    check(any(a["type"] == "press" and a["target"] == 'field "To"' for a in acts), "Enter in To recorded")
    check(any(a["value"] == "${Subject}" for a in fills), "timestamped subject linked to ${Subject}")
    check(any(a["value"] == "${Body}" for a in fills), "multi-line body recorded and linked to ${Body}")
    check(not any(a["type"] == "press" and "body" in a["target"].lower() for a in acts),
          "Enter inside the message body is not recorded as a key press")
    checks = [a["text"] for a in acts if a["type"] == "expect_text"]
    check(checks == ['Sent "${Subject}" to ${To}'], f"check text linked to the data inside it: {checks}")

    send = [a for a in acts if a["type"] == "click" and a.get("target") == 'button "Send"']
    check(len(send) == 1, "Send click recorded with an icon-free name")
    css = [l["value"] for l in send[0]["locators"] if l["kind"] == "css"]
    check('button[aria-label="Send"]' in css, f"aria-label locator saved: {css}")
    check(not any("splitButton" in c for c in css), "no locator built on the generated id")
    storage.save_suite(suite)  # results write-back needs a suite; the workbook is absent here
    run_id = storage.new_id("run")
    r = sessions.start("run", run_session, suite_id=suite["id"], case_id=case["id"], run_id=run_id,
                       suite=suite, case=case, recording=rec, headed=False)
    wait(r)
    run = storage.load_run(run_id)
    step = run["steps"][0]
    check(run["status"] == "passed", f"replay passed: {step.get('observed', step.get('error'))}")
    check('Sent "Automation Studio test 20' in step["observed"] and subject_seen["value"] not in step["observed"],
          "replay used a fresh timestamp in the subject")
    print("\nAll rich-editor checks passed.")
    server.should_exit = True


if __name__ == "__main__":
    main()
