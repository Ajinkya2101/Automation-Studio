"""Recorder test for a dropdown whose options only exist while it is open.

The page copies an Oracle Redwood select: an input with role=combobox that
opens a list on click, and options that are added to the page only then. A
click on a field is normally not recorded (the typing that follows is), so the
recording used to start at the option and replay could never find it.

Checks that the click that opens the list is recorded when an option is picked
(also after scrolling the list), and left out only when the user just types into
the field. A list opened by a click on the field's label is recorded as exactly
that click, on the label: the recorder does not guess which field opened a list.
Also checks a pick from a suggestion list that acts on the mouse press and is gone
before the release (YouTube's search suggestions), for which no click event fires.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\dropdown_open_test.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright  # noqa: E402

from app.recorder import RECORDER_JS  # noqa: E402
from app.replayer import _candidates, _resolve  # noqa: E402

PAGE = """<!doctype html><html><body style="font-family:sans-serif;margin:20px">
<label for="bu">Business Unit</label> <input id="bu" role="combobox" readonly>
<label for="note">Note</label> <input id="note">
<span id="lab">Region</span> <input id="rg" role="combobox" aria-labelledby="lab" aria-controls="rgdrop" aria-expanded="false" readonly>
<div id="rgdrop"></div>
<div id="holder"></div><button id="go">Proceed</button>
<input id="q" role="combobox" placeholder="Find" aria-controls="sug" aria-expanded="false">
<div id="sug"></div><div id="res"></div>
<script>
  const q = document.getElementById('q'), sug = document.getElementById('sug');
  q.addEventListener('input', () => {
    q.setAttribute('aria-expanded', 'true');
    sug.innerHTML = '<div role="listbox"><div role="option">' + q.value + ' again</div></div>';
    sug.firstChild.addEventListener('mousedown', e => {
      e.preventDefault();
      document.getElementById('res').textContent = 'Results for ' + e.target.textContent;
      sug.innerHTML = ''; q.setAttribute('aria-expanded', 'false');
    });
  });
  const bu = document.getElementById('bu'), holder = document.getElementById('holder');
  bu.addEventListener('click', () => {
    holder.innerHTML = '<ul role="listbox" aria-label="Business Unit" style="height:60px;overflow:auto;width:200px">' +
      ['AAA', 'BBB', 'CCC', 'DDD', 'EEE', 'CACBC'].map(v => '<li role="option">' + v + '</li>').join('') + '</ul>';
    holder.firstChild.addEventListener('click', e => { bu.value = e.target.textContent; holder.innerHTML = ''; });
  });
  const rg = document.getElementById('rg'), rgdrop = document.getElementById('rgdrop');
  document.getElementById('lab').addEventListener('click', () => {
    rg.setAttribute('aria-expanded', 'true');
    rgdrop.innerHTML = '<div role="listbox"><div role="option">CACBC</div></div>';
    rgdrop.firstChild.addEventListener('click', e => {
      rg.value = e.target.textContent; rgdrop.innerHTML = ''; rg.setAttribute('aria-expanded', 'false');
    });
  });
</script></body></html>"""


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context()
        actions: list[dict] = []
        ctx.expose_binding("__recAction", lambda _s, a: actions.append(a))
        ctx.expose_binding("__recState", lambda _s: None)
        ctx.add_init_script(RECORDER_JS)
        page = ctx.new_page()
        page.route("http://dropdown.test/", lambda r: r.fulfill(body=PAGE, content_type="text/html"))

        def record(do) -> list[str]:
            actions.clear()
            page.goto("http://dropdown.test/")
            do()
            page.wait_for_timeout(700)  # a scroll is saved once it stops
            return [f"{a['type']} {a['target']}" for a in actions]

        def pick():
            page.click("#bu")
            page.click("text=AAA")
            page.click("#go")
        got = record(pick)
        assert got == ['click combobox "Business Unit"', 'click option "AAA"', 'click button "Proceed"'], got
        opener = actions[0]

        def scroll_and_pick():
            page.click("#bu")
            page.hover("[role=listbox]")
            page.mouse.wheel(0, 200)
            page.wait_for_timeout(600)
            page.click("text=CACBC")
        got = record(scroll_and_pick)
        assert got == ['click combobox "Business Unit"', 'scroll area "Business Unit"', 'click option "CACBC"'], got

        def type_only():
            page.click("#note")
            page.keyboard.type("hello")
            page.click("#go")
        got = record(type_only)
        assert got == ['fill field "Note"', 'click button "Proceed"'], got

        def open_then_leave():
            page.click("#bu")
            page.click("#go")
        got = record(open_then_leave)  # the field was clicked and not typed into: the click may matter
        assert got == ['click combobox "Business Unit"', 'click button "Proceed"'], got

        # Opened without a click on the field itself (Redwood: the label inside the field).
        def open_by_label():
            page.click("#lab")
            page.click("text=CACBC")
        got = record(open_by_label)
        assert got == ['click span "Region"', 'click option "CACBC"'], got
        by_label = list(actions)

        # The list acts on the press and removes the option, so no click event fires.
        def pick_on_press():
            page.click("#q")
            page.keyboard.type("abc")
            page.click("[role=option]")
            assert page.inner_text("#res") == "Results for abc again"
        got = record(pick_on_press)
        assert got == ['fill combobox "Find"', 'click option "abc again"'], got

        # Replay can find the recorded opener, and the option once it is open.
        page.goto("http://dropdown.test/")
        loc, _ = _resolve(page, _candidates(opener["locators"]), opener["target"], timeout_ms=3000)
        loc.click()
        page.get_by_role("option", name="CACBC", exact=True).click()
        assert page.input_value("#bu") == "CACBC"

        # The list opened through the label replays too: the label is what gets clicked.
        page.goto("http://dropdown.test/")
        for a in by_label:
            loc, _ = _resolve(page, _candidates(a["locators"]), a["target"], timeout_ms=3000)
            loc.click()
        assert page.input_value("#rg") == "CACBC"
        browser.close()
    print("PASS: the click that opens a dropdown is recorded when an option is picked")


if __name__ == "__main__":
    main()
