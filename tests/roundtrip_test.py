"""Round trip: what a user does on a page is recorded, and replaying the recording on a
fresh copy of the page ends in the same state.

Each case is one kind of interaction that real applications use and that a recorder can
lose or get wrong: a row that reacts to a click without being a button, a web component,
an iframe, a second tab, keys other than Enter, a double-click, a menu that opens on
hover, a field covered by its own label, two fields with the same name, a file upload.
For each case the test plays the user, checks what was recorded, replays it with the
studio's replayer in a clean browser and compares the page's state with the user's.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\roundtrip_test.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")
# Keep uploads out of the studio's real data folder, and failures quick.
os.environ.setdefault("STUDIO_DATA_DIR", tempfile.mkdtemp(prefix="studio-roundtrip-"))
os.environ.setdefault("STUDIO_ACTION_TIMEOUT_MS", "6000")

from playwright.sync_api import sync_playwright  # noqa: E402

from app import replayer  # noqa: E402
from app.recorder import RECORDER_JS, _where, append_action  # noqa: E402

URL = "http://roundtrip.test/"
PAGE = """<!doctype html><html><body style="font-family:sans-serif;margin:20px">
<label id="terms"><input type="checkbox" id="cb" style="position:absolute;opacity:0;pointer-events:none"> Accept terms</label>
<form id="f"><label>User <input id="u" name="u"></label><button type="submit">Sign in</button></form><span id="fo"></span>
<table id="t" border=1><tr><td>Row one</td><td>100</td></tr><tr><td>Row two</td><td>200</td></tr></table><span id="sel"></span>
<label>City <input id="city" role="combobox" aria-expanded="false"></label><div id="sugs"></div>
<my-btn></my-btn><span id="sh"></span>
<iframe id="fr" style="height:50px"></iframe><span id="ifo"></span>
<label>Attachment <input type="file" id="file"></label>
<table><tr><td><input aria-label="Quantity" id="q1"></td><td><button class="ed">Edit</button></td></tr>
       <tr><td><input aria-label="Quantity" id="q2"></td><td><button class="ed">Edit</button></td></tr></table><span id="edo"></span>
<label>Due date <input id="dd" readonly></label><div id="cal"></div>
<div id="menu" style="display:inline-block;padding:4px;border:1px solid">Actions<div id="sub" style="display:none"><a href="#" id="exp">Export</a></div></div><span id="mo"></span>
<div id="dbl">Invoice 1001</div><span id="dblo"></span>
<div style="position:relative;width:300px;height:40px">
  <input id="bu" role="combobox" aria-label="Business Unit" readonly style="width:100%;height:100%;box-sizing:border-box">
  <div id="inside" style="position:absolute;inset:0;padding:10px">Business Unit</div></div><div id="drop"></div>
<a href="/second" target="_blank" id="pop">Open report</a>
<div id="keys" tabindex="0">Shortcut area</div><span id="ko"></span>
<script>
const $ = id => document.getElementById(id);
let n = 0;
$('f').addEventListener('submit', e => { e.preventDefault(); $('fo').textContent = 'submitted ' + (++n) + ' time(s)'; });
document.querySelectorAll('#t tr').forEach(tr => tr.addEventListener('click', () => { $('sel').textContent = 'selected ' + tr.cells[0].textContent; }));
const city = $('city'), sugs = $('sugs'); let hi = -1;
city.addEventListener('input', () => { hi = -1; sugs.innerHTML = ['Pune', 'Paris', 'Prague'].map(c => '<div>' + c + '</div>').join(''); });
city.addEventListener('keydown', e => {
  const it = [...sugs.children];
  if (e.key === 'ArrowDown') hi = Math.min(hi + 1, it.length - 1);
  else if (e.key === 'Enter' && hi >= 0) { city.value = it[hi].textContent; sugs.innerHTML = ''; }
  else if (e.key === 'Escape') { city.value = ''; sugs.innerHTML = ''; }
});
customElements.define('my-btn', class extends HTMLElement {
  connectedCallback() {
    const r = this.attachShadow({ mode: 'open' });
    r.innerHTML = '<button>Delete all</button> <button>Save draft</button>';
    r.children[0].onclick = () => { $('sh').textContent = 'DELETED'; };
    r.children[1].onclick = () => { $('sh').textContent = 'saved'; };
  }
});
$('fr').srcdoc = '<button id="ib">Inside frame</button><scr' + 'ipt>document.getElementById("ib").onclick=()=>{parent.document.getElementById("ifo").textContent="frame clicked"}</scr' + 'ipt>';
document.querySelectorAll('.ed').forEach((b, i) => { b.onclick = () => { $('edo').textContent = 'editing row ' + (i + 1); }; });
const dd = $('dd'), cal = $('cal');
dd.addEventListener('click', () => {
  cal.innerHTML = [14, 15, 16].map(d => '<span role="gridcell" style="cursor:pointer;padding:4px">' + d + '</span>').join('');
  cal.onclick = e => { dd.value = '2026-10-' + e.target.textContent; cal.innerHTML = ''; };
});
const menu = $('menu'), sub = $('sub');
menu.onmouseenter = () => { sub.style.display = 'block'; };
menu.onmouseleave = () => { sub.style.display = 'none'; };
$('exp').onclick = () => { $('mo').textContent = 'exported'; return false; };
$('dbl').ondblclick = () => { $('dblo').textContent = 'opened'; };
const bu = $('bu'), drop = $('drop');
$('inside').addEventListener('click', () => {
  drop.innerHTML = '<div role="listbox"><div role="option">CACBC</div></div>';
  drop.firstChild.addEventListener('click', e => { bu.value = e.target.textContent; $('inside').textContent = ''; drop.innerHTML = ''; });
});
$('keys').addEventListener('keydown', e => { if (e.ctrlKey && e.key === 's') { e.preventDefault(); $('ko').textContent = 'saved by shortcut'; } });
$('file').addEventListener('change', e => { $('file').dataset.got = e.target.files[0].name; });
</script></body></html>"""
SECOND = """<!doctype html><html><body><button id="ack" onclick="this.textContent='Acknowledged'">Acknowledge</button></body></html>"""


def serve(route):
    route.fulfill(body=SECOND if route.request.url.endswith("/second") else PAGE, content_type="text/html")


def main() -> None:
    upload = replayer.UPLOADS_DIR / "invoice.txt"
    upload.parent.mkdir(parents=True, exist_ok=True)
    upload.write_text("invoice")
    failures = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        def case(name, user, expected, state):
            """Play the user, check the recording, replay it in a clean browser, compare the end state."""
            ctx = browser.new_context()
            tabs, actions = [], []
            ctx.on("page", lambda pg: tabs.append(pg) if pg not in tabs else None)
            ctx.expose_binding("__recAction", lambda s, a: append_action(actions, {**a, **_where(s, tabs)}))
            ctx.expose_binding("__recState", lambda _s: None)
            ctx.add_init_script(RECORDER_JS)
            ctx.route("**/*", serve)
            page = ctx.new_page()
            page.goto(URL)
            user(page)
            page.wait_for_timeout(700)  # typing and scrolling are saved once the user pauses
            wanted = state(ctx)
            got = [f"{a['type']} {a.get('key') or a.get('target')}" for a in actions]
            ctx.close()

            rctx = browser.new_context()
            rctx.route("**/*", serve)
            rpage = rctx.new_page()
            net = replayer._Network(rctx)
            rpage.goto(URL)
            error, prev, notes = None, None, []
            for i, a in enumerate(actions):
                try:
                    rpage = replayer._tab(net, a, rpage)
                    replayer._execute(rpage, a, {}, net, prev, notes.append, actions[i + 1] if i + 1 < len(actions) else None)
                    prev = a
                except Exception as exc:
                    error = f"{a['type']} {a.get('target')}: {replayer._short(exc)}"
                    break
            replayed = state(rctx)
            rctx.close()

            problems = []
            if expected is not None and got != expected:
                problems.append(f"recorded {got}, expected {expected}")
            if error:
                problems.append(f"replay failed at {error}")
            if replayed != wanted:
                problems.append(f"replay ended with {replayed!r}, the user with {wanted!r}")
            print(f"{'FAIL' if problems else 'ok  '}  {name}" + "".join(f"\n        {p}" for p in problems))
            if problems:
                failures.append(name)
            return actions, notes

        text = lambda sel: lambda ctx: ctx.pages[0].inner_text(sel)   # noqa: E731
        value = lambda sel: lambda ctx: ctx.pages[0].input_value(sel)  # noqa: E731

        case("checkbox ticked through its label (the box itself is hidden)",
             lambda p: p.click("#terms", position={"x": 60, "y": 8}),
             ['click label "Accept terms"'], lambda ctx: ctx.pages[0].is_checked("#cb"))

        def enter_in_form(p):
            p.click("#u")
            p.keyboard.type("bob")
            p.keyboard.press("Enter")
        case("Enter in a form submits once", enter_in_form,
             ['fill field "User"', 'press Enter'], text("#fo"))

        case("table row that reacts to a click", lambda p: p.click("text=Row two"),
             ['click td "Row two"'], text("#sel"))

        def pick_with_keys(p):
            p.click("#city")
            p.keyboard.type("P")
            p.keyboard.press("ArrowDown")
            p.keyboard.press("ArrowDown")
            p.keyboard.press("Enter")
        case("suggestion picked with the arrow keys", pick_with_keys, None, value("#city"))

        def escape_clears(p):
            p.click("#city")
            p.keyboard.type("Pu")
            p.keyboard.press("Escape")
            p.click("text=Row one")
        case("Escape in a field", escape_clears,
             ['fill combobox "City"', 'press Escape', 'click td "Row one"'], value("#city"))

        case("button inside a web component",
             lambda p: p.locator("my-btn button", has_text="Save draft").click(),
             ['click button "Save draft"'], text("#sh"))

        case("button inside an iframe",
             lambda p: p.frame_locator("#fr").get_by_text("Inside frame").click(),
             ['click button "Inside frame"'], text("#ifo"))

        acts, _ = case("file attached", lambda p: p.set_input_files("#file", str(upload)),
                       ['upload field "Attachment"'],
                       lambda ctx: ctx.pages[0].evaluate("document.getElementById('file').dataset.got"))

        def same_label(p):
            p.click("#q1")
            p.keyboard.type("5")
            p.click("#q2")
            p.keyboard.type("7")
            p.click("#u")
        case("two fields with the same label", same_label, None,
             lambda ctx: [ctx.pages[0].input_value("#q1"), ctx.pages[0].input_value("#q2")])

        case("the second of two buttons with the same name",
             lambda p: p.locator(".ed").nth(1).click(), ['click button "Edit"'], text("#edo"))

        def date(p):
            p.click("#dd")
            p.click("text=15")
        case("date picked from a calendar the field opens", date,
             ['click field "Due date"', 'click gridcell "15"'], value("#dd"))

        def hover_menu(p):
            p.hover("#menu")
            p.click("text=Export")
        case("item of a menu that opens on hover", hover_menu, ['click link "Export"'], text("#mo"))

        case("double-click", lambda p: p.dblclick("#dbl"), ['dblclick div "Invoice 1001"'], text("#dblo"))

        def covered(p):
            p.click("#inside")
            p.click("[role=option]")
        case("dropdown opened by the label drawn over its field", covered,
             ['click div "Business Unit"', 'click option "CACBC"'], value("#bu"))

        def shortcut(p):
            p.click("#keys")
            p.keyboard.press("Control+s")
        case("keyboard shortcut", shortcut, None, text("#ko"))

        def second_tab(p):
            with p.context.expect_page() as new:
                p.click("#pop")
            new.value.wait_for_load_state()
            new.value.click("#ack")
        acts, _ = case("button in a tab that a link opened", second_tab, None,
                       lambda ctx: ctx.pages[-1].inner_text("#ack") if len(ctx.pages) > 1 else "no second tab")
        if not any(a.get("tab") == 1 for a in acts):
            failures.append("the action in the second tab is not marked with its tab")

        # A field the automation's click cannot reach (covered for good) is still clicked, with a note.
        recorded = [{"type": "click", "target": 'combobox "Business Unit"',
                     "locators": [{"kind": "label", "value": "Business Unit"}]},
                    {"type": "click", "target": 'option "CACBC"',
                     "locators": [{"kind": "role", "role": "option", "value": "CACBC"}]}]
        ctx = browser.new_context()
        ctx.route("**/*", serve)
        page = ctx.new_page()
        page.goto(URL)
        notes = []
        for a in recorded:
            replayer._execute(page, a, {}, None, None, notes.append, None)
        ok = page.input_value("#bu") == "CACBC" and any("covered by" in n for n in notes)
        print(f"{'ok  ' if ok else 'FAIL'}  click on a covered field goes through at its position, with a note")
        if not ok:
            failures.append("covered click")
        ctx.close()
        browser.close()

    if failures:
        print(f"\n{len(failures)} round-trip check(s) FAILED: " + "; ".join(failures))
        sys.exit(1)
    print("\nAll round-trip checks passed.")


if __name__ == "__main__":
    main()
