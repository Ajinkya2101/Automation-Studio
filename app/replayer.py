"""Replay a recording as an automated run, step by step, with logs.

No recording is needed at run time beyond the saved actions: each action's
locator candidates are tried in order (most stable first), values come from
the Excel test data, and every step ends with a screenshot.
"""
import re
import time
from datetime import datetime

from playwright.sync_api import Page, expect, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import storage
from .actions import (MissingDataError, describe, locator_label, repeated_click, resolve_tokens,
                      resolved_steps, substitute)
from .browser import first_page, open_context
from .config import ACTION_TIMEOUT_MS, DATA_DIR, SLOW_FACTOR, TRACE
from .diagnostics import (BrowserLog, browser_info, capture_page, environment, error_details, mask_data,
                          pause_trace, resume_trace, secret_on_screen, start_trace, stop_trace)
from .events import Session
from .excel_io import write_results


class StepFailed(Exception):
    pass


class Stopped(Exception):
    pass


ICON_GLYPHS = re.compile("[-]")  # icon-font characters (private use area)
GENERATED_ID = re.compile(r"#[^\s>]*\d")        # ids like #splitButton-r44 or #\35 88 change every load
STRUCTURAL_GRACE_S = 5  # how long semantic locators get before CSS fallbacks are allowed
SETTLE_MAX_S = 30 * SLOW_FACTOR      # longest wait for the app to finish reacting to an action
RECOVER_AFTER_S = 12 * SLOW_FACTOR   # target still missing this long with the app idle: the last action was lost
NET_QUIET_S = 0.5        # no app requests in flight for this long = network settled
NAVIGATION_WAIT_S = 5 * SLOW_FACTOR  # how long a click that loaded a page when recorded gets to start loading one
REVEAL_AFTER_S = 1.5     # target in the page but hidden this long: try hovering what it hangs under
COVERED_CLICK_MS = max(ACTION_TIMEOUT_MS // 3, 2000)  # how long a covered element gets to be uncovered
TAB_WAIT_MS = int(10000 * SLOW_FACTOR)  # how long a recorded tab gets to open before the newest tab stands in
UPLOADS_DIR = DATA_DIR / "uploads"  # files that recorded uploads attach
DOM_QUIET_MS = 400       # no DOM changes for this long = screen settled

# True while the app's own busy flags say it is still working: Oracle ADF (classic
# Fusion pages) and Oracle JET / Visual Builder (Redwood pages, which load JET
# through RequireJS instead of a global `oj`).
BUSY_JS = """() => {
  try { if (window.AdfPage && AdfPage.PAGE && !AdfPage.PAGE.isSynchronizedWithServer()) return true; } catch (e) {}
  try { if (window.oj && oj.Context && !oj.Context.getPageContext().getBusyContext().isReady()) return true; } catch (e) {}
  try {
    if (window.require && require.defined && require.defined('ojs/ojcontext')
        && !require('ojs/ojcontext').getPageContext().getBusyContext().isReady()) return true;
  } catch (e) {}
  return document.readyState !== 'complete';
}"""

# Resolves once the page looks idle: no DOM changes for `quiet` ms and no busy flags.
SETTLE_JS = """([quiet, max]) => new Promise(resolve => {
  const start = Date.now();
  let last = Date.now();
  const obs = new MutationObserver(() => { last = Date.now(); });
  obs.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
  const busy = """ + BUSY_JS + """;
  (function tick() {
    const now = Date.now();
    if ((now - last >= quiet && !busy()) || now - start >= max) { obs.disconnect(); resolve(); return; }
    setTimeout(tick, 100);
  })();
})"""


class _Network:
    """Counts the app's in-flight requests, so replay can wait for AJAX updates to finish.

    A click that swaps the screen in place (no navigation) is only done once the
    requests it started are answered, just as a person waits for the new screen
    before acting on it.
    """
    TRACKED = {"document", "xhr", "fetch", "script"}

    def __init__(self, ctx):
        self.inflight = {}  # request -> when it started
        self.last = time.time()
        self.navigations = 0  # main-frame page loads so far, in any tab
        self.tabs = []        # tabs in the order they opened, as when recording
        self.stand_ins = {}   # recorded tab number -> tab used for it when the run never opened it
        ctx.on("request", self._start)
        ctx.on("requestfinished", self._end)
        ctx.on("requestfailed", self._end)
        ctx.on("page", self._tab)
        for pg in ctx.pages:
            self._tab(pg)

    def _tab(self, pg):
        if pg in self.tabs:
            return
        self.tabs.append(pg)
        pg.set_default_timeout(ACTION_TIMEOUT_MS)
        pg.on("framenavigated", self._navigated)
        # Without a listener a recorded click on 'Browse' opens the system file window, which
        # nothing closes. With one, the browser waits for the upload action to set the file.
        pg.on("filechooser", lambda _chooser: None)

    def _navigated(self, frame):
        if frame.parent_frame is None:
            self.navigations += 1

    def _start(self, req):
        if req.resource_type in self.TRACKED:
            self.inflight[req] = time.time()
            self.last = time.time()

    def _end(self, req):
        if self.inflight.pop(req, None) is not None:
            self.last = time.time()

    def busy(self) -> bool:
        """A request is still being answered (long-polling ones older than SETTLE_MAX_S don't count)."""
        now = time.time()
        return any(now - t < SETTLE_MAX_S for t in list(self.inflight.values())) or now - self.last < NET_QUIET_S


def _idle(page: Page, net: "_Network | None") -> bool:
    """The app is not working on anything: no requests in flight and no busy flags."""
    if net and net.busy():
        return False
    try:
        return not page.evaluate(BUSY_JS)
    except Exception:  # mid-navigation
        return False


def _settle(page: Page, net: "_Network | None") -> None:
    """Wait until the app has finished reacting to the last action (best effort, never fails)."""
    deadline = time.time() + SETTLE_MAX_S
    page.wait_for_timeout(150)  # let the action's own requests and DOM updates start
    if net:
        # Long-polling requests never finish, so give up on the network after the deadline.
        while time.time() < deadline and (net.inflight or time.time() - net.last < NET_QUIET_S):
            page.wait_for_timeout(100)
    remaining = int((deadline - time.time()) * 1000)
    if remaining > 0:
        try:
            page.evaluate(SETTLE_JS, [DOM_QUIET_MS, remaining])
        except Exception:  # page navigated mid-check: wait for the new one to load instead
            try:
                page.wait_for_load_state("load", timeout=max(remaining, 1000))
            except Exception:
                pass


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", ICON_GLYPHS.sub("", s or "")).strip()


def _css_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _candidates(recorded: list[dict], visible: bool = True) -> list[dict]:
    """Recorded locators, cleaned up, plus fallbacks that survive app quirks.

    - Icon glyphs are stripped from names and text (they are not part of the
      accessible name or reliably part of the text).
    - CSS paths through generated ids are dropped: they point at a different
      element, or nothing, on the next page load.
    - Role locators ignore anything under aria-hidden. Apps such as Outlook set
      aria-hidden on the whole page while a popup is open, so each role locator
      also gets an include-hidden twin and an [aria-label] CSS twin.
    - Only visible elements count. A text or CSS match can be an element that is
      in the page but hidden, e.g. a link inside a collapsed menu group.
    """
    out = []
    for c in recorded:
        c = {**c, "must_be_visible": visible}
        if c["kind"] == "css":
            if GENERATED_ID.search(c["value"]):
                continue
        elif c["kind"] != "testid":
            c["value"] = _clean(c["value"])
        if c["value"]:
            out.append(c)
    for c in [c for c in out if c["kind"] == "role"]:
        twin = {k: v for k, v in c.items() if k not in ("count", "index")}  # the position was counted without hidden ones
        out.append({**twin, "include_hidden": True})
        out.append({"kind": "css", "value": f'[aria-label="{_css_str(c["value"])}"]', "must_be_visible": visible})
    seen, unique = set(), []
    for c in out:
        key = (c["kind"], c.get("role"), c["value"], c.get("include_hidden"))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _build(page: Page, c: dict):
    k, v = c["kind"], c["value"]
    if k == "role":
        return page.get_by_role(c["role"], name=v, exact=True, include_hidden=c.get("include_hidden", False))
    if k == "label":
        return page.get_by_label(v, exact=True)
    if k == "placeholder":
        return page.get_by_placeholder(v, exact=True)
    if k == "testid":
        return page.get_by_test_id(v)
    if k == "text":
        return page.get_by_text(v, exact=True)
    return page.locator(v)


# The nearest ancestor of a hidden element that is itself on screen: what a person
# would point at to make the element appear (a menu that opens on hover).
VISIBLE_ANCESTOR_JS = """el => {
  const up = n => n.parentElement || (n.getRootNode().host || null);
  for (let a = up(el); a; a = up(a)) {
    if (a.getClientRects().length && getComputedStyle(a).visibility !== 'hidden') return a;
  }
  return null;
}"""


def _reveal(hidden) -> None:
    """Best effort: point at what a hidden target hangs under, as the user's mouse did on the way to it."""
    try:
        anchor = hidden.evaluate_handle(VISIBLE_ANCESTOR_JS, timeout=2000).as_element()
        if anchor:
            anchor.hover(timeout=2000)
    except Exception:
        pass


def _resolve(page, candidates: list[dict], target: str, timeout_ms: int = ACTION_TIMEOUT_MS,
             css_grace_s: float = STRUCTURAL_GRACE_S):
    """First candidate matching exactly one visible element wins; waits while the page loads.

    `page` is the page or the iframe the action was recorded in.

    Semantic locators (role, label, text) are accepted at once. Structural CSS
    fallbacks only after a grace period, so a CSS path that happens to match
    something while the page is still loading cannot win the race.

    A name shared by several elements ("Search" on the page and in a dialog,
    "Edit" in every row) was recorded with its position among them. When the
    same number of matches is there again, that position is used. Otherwise
    the other candidates get their chance first, and only when none of them
    identifies one element is the first match of an ambiguous one taken.
    """
    start = time.time()
    deadline = start + timeout_ms / 1000
    hidden_only, hidden, revealed = False, None, False
    while True:
        allow_css = time.time() - start >= css_grace_s
        positional = fallback = None
        for i, c in enumerate(candidates):
            if c["kind"] == "css" and not allow_css:
                continue
            try:
                loc = _build(page, c)
                n = loc.count()
                if n and c.get("must_be_visible"):
                    every = loc
                    loc = loc.filter(visible=True)
                    visible = loc.count()
                    if visible == 0:
                        hidden_only, hidden = True, hidden or every.first
                    n = visible
            except Exception:  # page mid-navigation
                continue
            if n == 1:
                return loc, i
            if n > 1:
                if c.get("count") == n and isinstance(c.get("index"), int) and positional is None:
                    positional = (loc.nth(c["index"]), i)
                elif fallback is None:
                    fallback = (loc.first, i, n)
        if positional:
            return positional
        if fallback and allow_css:
            candidates[fallback[1]]["matches"] = fallback[2]
            return fallback[:2]
        if hidden is not None and not revealed and time.time() - start >= REVEAL_AFTER_S:
            revealed = True
            _reveal(hidden)
            continue
        if time.time() > deadline:
            if hidden_only:
                raise StepFailed(
                    f"Found {target}, but it stayed hidden (for example inside a collapsed menu group or a closed "
                    "panel). The recording is probably missing the click that opens it: re-record this step "
                    "and click the menu group or panel header before the item.")
            raise StepFailed(f"Could not find {target} on the page (tried {len(candidates)} locator(s)).")
        page.wait_for_timeout(250)


def _scope(page: Page, a: dict, until: float | None = None):
    """The page, or the iframe the action was recorded in (waits for the frame to load until `until`)."""
    ref = a.get("frame")
    if not ref:
        return page
    url = ref.get("url") or ""
    while True:
        inner = [f for f in page.frames if f.parent_frame is not None and not f.is_detached()]
        found = (next((f for f in inner if ref.get("name") and f.name == ref["name"]), None)
                 or next((f for f in inner if url and not url.startswith("about:")
                          and f.url.split("?")[0].split("#")[0] == url), None)
                 or (inner[ref["index"]] if isinstance(ref.get("index"), int) and ref["index"] < len(inner) else None))
        if found:
            return found
        if until is None:
            return page
        if time.time() >= until:
            raise StepFailed(f"The embedded frame that {a.get('target', 'this element')} was recorded in "
                             f"({ref.get('name') or url or 'frame ' + str(ref.get('index'))}) did not appear.")
        page.wait_for_timeout(250)


def _scroll(page: Page, a: dict) -> str:
    """Best effort: repeat a recorded scroll so lists that load more items on scroll do so.

    A scroll never fails a step: if the scrolled area cannot be found, the next
    action still gets its chance (Playwright scrolls to elements before clicking).
    """
    x, y = int(a.get("x", 0)), int(a.get("y", 0))
    scope = _scope(page, a)
    if a.get("page"):
        scope.evaluate("([x, y]) => window.scrollTo(x, y)", [x, y])
        page.wait_for_timeout(300)
        return "page"
    try:
        # Scroll areas rarely have a label or short text, so their CSS path is allowed at once.
        loc, i = _resolve(scope, _candidates(a.get("locators", [])), a.get("target", "area"),
                          timeout_ms=int(6000 * SLOW_FACTOR), css_grace_s=0)
    except StepFailed:
        return "skipped: scrolled area not found"
    loc.evaluate("(el, [x, y]) => { el.scrollLeft = x; el.scrollTop = y; }", [x, y])
    page.wait_for_timeout(300)  # let lazily loaded items appear
    return locator_label(_candidates(a.get("locators", []))[i])


def _repeatable(a: dict | None) -> bool:
    """Actions that are safe to do a second time: typing (it replaces the text), Enter in a
    field that stays on the page (runs a search or picks a list value), and scrolling."""
    if not a:
        return False
    return a["type"] in ("fill", "scroll") or (a["type"] == "press" and a.get("key") == "Enter"
                                               and not a.get("navigates") and not a.get("global"))


def _redo(page: Page, a: dict, data: dict) -> None:
    """Repeat an action that had no effect. Typing is repeated key by key, because
    search-as-you-type fields react to keystrokes, not to a pasted value."""
    if a["type"] == "scroll":
        _scroll(page, a)
        return
    loc, _ = _resolve(_scope(page, a), _candidates(a["locators"]), a.get("target", "field"),
                      timeout_ms=5000, css_grace_s=0)
    if a["type"] == "fill":
        loc.fill("")
        loc.press_sequentially(substitute(a.get("value", ""), data), delay=40)
    else:
        loc.press(a["key"])


def _ready(page: Page, candidates: list[dict], target: str, until: float):
    """Find the target, then wait until it is enabled: on a slow instance a button often
    appears before the data it depends on has loaded."""
    loc, idx = _resolve(page, candidates, target, timeout_ms=max(int((until - time.time()) * 1000), 0))
    while True:
        try:
            if loc.is_enabled(timeout=1000):
                return loc, idx
        except Exception:  # re-rendered while checking: find it again
            loc, idx = _resolve(page, candidates, target, timeout_ms=max(int((until - time.time()) * 1000), 0))
            continue
        if time.time() >= until:
            raise StepFailed(
                f"Found {target}, but it stayed disabled. Usually an earlier action did not take effect (see the "
                "screenshot), or the recording is missing the step that enables it, such as selecting a row.")
        page.wait_for_timeout(250)


def _find(page: Page, a: dict, data: dict, net: "_Network | None", prev: dict | None, note) -> tuple:
    """Wait for the action's target, recovering once from a lost previous action.

    On a slow instance an action can do nothing: Enter pressed before the search
    box was wired up, or text typed while the screen was still being built. When
    the target is still missing (or disabled) after RECOVER_AFTER_S and the app
    is idle, so it is not still working on the previous action, that action is
    repeated once if it is safe to repeat. Clicks are never repeated.
    """
    candidates = _candidates(a["locators"], visible=a["type"] != "upload")  # a file input is usually hidden
    target = a.get("target", "element")
    start = time.time()
    deadline = start + ACTION_TIMEOUT_MS / 1000
    scope = _scope(page, a, until=deadline)
    recover_at = start + RECOVER_AFTER_S if _repeatable(prev) else None
    while True:
        try:
            loc, idx = _ready(scope, candidates, target, min(deadline, recover_at) if recover_at else deadline)
            return candidates, loc, idx
        except StepFailed:
            if not recover_at or time.time() >= deadline:
                raise
        if not _idle(page, net):
            recover_at = time.time() + 2  # still working on it: keep waiting
            continue
        note(f"{target} has not appeared after {time.time() - start:.0f}s and the application is idle, so the "
             f"previous action probably had no effect. Repeating it: {describe(prev, data)}")
        try:
            _redo(page, prev, data)
            _settle(page, net)
        except Exception:
            pass  # its field is gone too; keep waiting for the target anyway
        recover_at = None


def _execute(page: Page, a: dict, data: dict, net: "_Network | None" = None, prev: dict | None = None,
             note=lambda msg: None, nxt: dict | None = None) -> tuple[str, int]:
    """Run one action and wait for the app to settle. Returns (locator used, candidate index)."""
    loads = net.navigations if net else 0
    result = _perform(page, a, data, net, prev, note)
    if a.get("navigates") and net:
        # This action loaded a page when it was recorded. Give that a moment to start, so the
        # next action is not tried on the page that is about to go away. It is not required:
        # the same click can open a panel instead of a page on another day.
        until = time.time() + NAVIGATION_WAIT_S
        while net.navigations == loads and time.time() < until:
            page.wait_for_timeout(100)
    if a["type"] not in ("expect_text", "scroll"):
        _settle(page, net)
    if a["type"] == "fill":
        _check_fill(page, a, data, net)
        _commit(page, a, net, nxt)
    elif a["type"] == "select":
        _check_select(page, a, data, net)
    return result


# The chosen option of a dropdown: its value and its text.
SELECTED_JS = "el => el.selectedOptions && el.selectedOptions.length ? [el.value, el.selectedOptions[0].label.trim()] : ['', '']"


def _selected(page: Page, a: dict) -> list[str] | None:
    """What the dropdown of a select action shows now; None when it is no longer on the page."""
    try:
        loc, _ = _resolve(_scope(page, a), _candidates(a["locators"]), a.get("target", "list"),
                          timeout_ms=0, css_grace_s=0)
        return loc.evaluate(SELECTED_JS, timeout=2000)
    except Exception:
        return None


def _check_select(page: Page, a: dict, data: dict, net: "_Network | None") -> None:
    """Make sure the chosen option is still chosen once the app settles; choose it again once if not."""
    value = substitute(a.get("value", ""), data)
    got = _selected(page, a)
    if got is None or value in got:
        return
    loc, _ = _resolve(_scope(page, a), _candidates(a["locators"]), a.get("target", "list"))
    loc.select_option(value)
    _settle(page, net)


def _undone_picks(page: Page, picked: list[dict], a: dict, data: dict) -> list[str]:
    """Dropdown choices made earlier in the run that action `a` undid.

    Classic Fusion pages reset some lists when a field they depend on changes:
    choosing the Customer of a sales order clears its Business Unit. The run then
    fails much later, at whatever needed that choice, with an error that does not
    point at the cause. Each earlier choice is read again after every action, and
    one that changed is reported right there. A list that is no longer on the
    page is not watched any more.
    """
    out = []
    for p in list(picked):
        if p["action"] is a:
            continue
        got = _selected(page, p["action"])
        if got is None:
            picked.remove(p)
        elif p["shown"] not in got:
            picked.remove(p)
            out.append(f"{p['action'].get('target', 'A list')} no longer shows \"{p['shown']}\" "
                       f"(now \"{got[1] or 'blank'}\") after: {describe(a, data)}. The application resets that "
                       "list when this changes, so choose it again after this action (re-record the choice "
                       "after it).")
    return out


# Leaves the field with Tab's effect on the value (blur + change) on classic Fusion pages only.
COMMIT_JS = """el => {
  if (!window.AdfPage || document.activeElement !== el || !['INPUT', 'TEXTAREA'].includes(el.tagName)) return false;
  el.blur();
  return true;
}"""


def _commit(page: Page, a: dict, net: "_Network | None", nxt: dict | None) -> None:
    """Hand a typed value to the application before the next action (best effort, never fails).

    Classic Fusion (ADF) pages check a field with the server only when focus
    leaves it, then redraw what depends on it: the rest of the table row, the
    values other lists offer. If the next click is what takes focus away, that
    check runs while the click is being handled, so a dropdown opened by the
    click is empty or closed again by the redraw. Leaving the field first and
    waiting keeps the two apart. Not done when the next action continues in
    the same field (Enter to search, more typing).
    """
    if not nxt or (nxt["type"] in ("press", "fill") and nxt.get("target") == a.get("target")):
        return
    try:
        loc, _ = _resolve(_scope(page, a), _candidates(a["locators"]), a.get("target", "field"),
                          timeout_ms=5000, css_grace_s=0)
        if loc.evaluate(COMMIT_JS, timeout=2000):
            _settle(page, net)
    except Exception:
        pass


def _check_fill(page: Page, a: dict, data: dict, net: "_Network | None") -> None:
    """Make sure typed text is still in the field once the app settles.

    If the screen was replaced while typing (the field came from the previous
    screen), type it again into the field that is there now.
    """
    value = substitute(a.get("value", ""), data)
    candidates = _candidates(a["locators"])
    scope = _scope(page, a)
    try:
        loc, _ = _resolve(scope, candidates, a.get("target", "field"), timeout_ms=5000, css_grace_s=0)
        if loc.input_value(timeout=2000) == value:
            return
    except Exception:
        return  # not a plain input (e.g. a rich editor); nothing reliable to compare
    loc, _ = _resolve(scope, candidates, a.get("target", "field"))
    loc.fill(value)
    _settle(page, net)


def _perform(page: Page, a: dict, data: dict, net: "_Network | None" = None, prev: dict | None = None,
             note=lambda msg: None) -> tuple[str, int]:
    t = a["type"]
    if t == "goto":
        page.goto(substitute(a["url"], data))
        return "", 0
    if t == "expect_text":
        text = substitute(a["text"], data)
        try:
            expect(_scope(page, a).get_by_text(text).first).to_be_visible(timeout=ACTION_TIMEOUT_MS)
        except AssertionError:
            raise StepFailed(f"\"{text}\" did not appear within {ACTION_TIMEOUT_MS // 1000}s") from None
        return f'text contains "{text}"', 0
    if t == "scroll":
        return _scroll(page, a), 0
    if t == "press" and (a.get("global") or not a.get("locators")):
        page.wait_for_timeout(600)  # as for a key in a field: let the page react to what came before
        page.keyboard.press(a["key"])  # pressed with nothing in particular focused
        return "", 0

    candidates, loc, idx = _find(page, a, data, net, prev, note)
    used =locator_label(candidates[idx]) + (" (under aria-hidden)" if candidates[idx].get("include_hidden") else "")
    _check_position(a, candidates[idx], loc, note)
    try:
        _act(page, a, data, loc, note)
    except StepFailed:
        raise
    except Exception as exc:
        # A CSS path that fits several elements is a guess: the first one may be the wrong control.
        n = candidates[idx].get("matches")
        if n and candidates[idx]["kind"] == "css":
            raise StepFailed(
                f"{_short(exc)} The recording identifies {a.get('target', 'this element')} only by its position "
                f"in the page ({used}), which fits {n} elements here, so the wrong one was probably tried. "
                "Re-record this step so the control is saved by its name.") from exc
        raise
    return used, idx


def _check_position(a: dict, cand: dict, loc, note) -> None:
    """Warn when a click found its element only by its position in the page, and that element
    shows other text than was clicked when recording: a row of a list picked by position can
    be another value than the one recorded. Only a warning, as the text can legitimately change
    (a message with this run's order number)."""
    if a["type"] not in ("click", "dblclick") or cand["kind"] != "css" or "nth-of-type" not in cand["value"]:
        return
    text = next((_clean(c["value"]) for c in a.get("locators", []) if c["kind"] == "text"), "")
    if not text:
        return
    try:
        shown = _clean(loc.inner_text(timeout=2000))
    except Exception:
        return
    if text not in shown:
        note(f"{a.get('target', 'The element')} was not found by its text, and the element at its recorded "
             f"position shows \"{shown[:60]}\" instead. If a later action fails, this click is the likely cause.")


COVERED = re.compile(r"(<[^\n]*?) intercepts pointer events")

# Puts focus in the field an Oracle ADF list icon belongs to. The icon cannot take focus
# itself, and in a popup (the 'Source' dialog of a miscellaneous transaction) the list it
# opens then belongs to no focused field: a click on a row only highlights the row, and the
# field stays empty. A person's click on the icon happens with the field in focus.
LOV_FIELD_JS = """el => {
  if (!window.AdfPage || !el.id || !el.id.endsWith('::lovIconId')) return false;
  const field = document.getElementById(el.id.slice(0, -'::lovIconId'.length) + '::content');
  if (!field || field === document.activeElement || field.disabled || field.readOnly) return false;
  field.focus();
  return true;
}"""


# The popup (a list, menu or dialog) drawn over the spot a click aims at, when the element
# is not part of it: e.g. a list of values left open over a dialog's OK button. Null otherwise.
COVERING_POPUP_JS = """(el, pos) => {
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + (pos ? pos[0] : r.width / 2), r.top + (pos ? pos[1] : r.height / 2));
  if (!hit || el.contains(hit) || hit.contains(el)) return null;
  const popup = hit.closest('[data-afr-popupid], [role=listbox], [role=menu], [role=dialog], [role=alertdialog], ' +
                            '.oj-popup, .oj-listbox-drop');
  return popup && !popup.contains(el) ? popup.outerHTML.slice(0, 120) : null;
}"""


def _covering_popup(loc, a: dict) -> str | None:
    try:
        return loc.evaluate(COVERING_POPUP_JS, a.get("pos"), timeout=2000)
    except Exception:
        return None


def _click(loc, a: dict, note, double: bool = False) -> None:
    """Click like a person does: at the element, whatever part of it is drawn over it.

    The automation normally refuses to click an element that something else covers.
    That is right while a loading mask is up, so the element first gets time to be
    uncovered. But some controls are always covered by a part of themselves (the
    label drawn inside an Oracle Redwood dropdown); a person's click lands on that
    part and the control reacts. So after the wait, the click is made at the same
    spot regardless.

    Not when what covers it is another popup, such as a list an earlier pick left
    open over a dialog's OK button: a click there picks from the list instead. The
    popup gets Escape to close it; if it stays, the step fails.
    """
    do = loc.dblclick if double else loc.click
    target = a.get("target", "The element")
    try:
        if loc.evaluate(LOV_FIELD_JS, timeout=2000):
            loc.page.wait_for_timeout(300)  # let the field's focus handlers run
    except Exception:
        pass
    kw = {}
    if a.get("pos"):  # recorded on a plain element: click the same spot in it
        kw["position"] = {"x": a["pos"][0], "y": a["pos"][1]}
    try:
        do(timeout=COVERED_CLICK_MS, **kw)
        return
    except PlaywrightTimeout as exc:
        covered = COVERED.search(str(exc))
        if not covered:
            raise
    popup = _covering_popup(loc, a)
    if popup:
        note(f"{target} is covered by a list or popup that is still open ({popup}), so Escape was pressed "
             "to close it.")
        loc.page.keyboard.press("Escape")
        loc.page.wait_for_timeout(500)
        try:
            do(timeout=5000, **kw)
            return
        except PlaywrightTimeout as exc:
            covered = COVERED.search(str(exc))
            if not covered:
                raise
        if _covering_popup(loc, a):
            raise StepFailed(
                f"{target} is covered by a list or popup that is not part of it ({popup}), and Escape did not "
                "close it. A click at its position would land in that popup instead, so the step stops here. "
                "Usually an earlier pick left a list open: re-record so the list is closed before this click.")
    note(f"{target} is covered by {covered.group(1)[:120]}, so the click was made "
         "at its position, as a person's click would be.")
    do(force=True, timeout=5000, **kw)


def _act(page: Page, a: dict, data: dict, loc, note=lambda msg: None) -> None:
    t = a["type"]
    if t == "click":
        _click(loc, a, note)
    elif t == "dblclick":
        _click(loc, a, note, double=True)
    elif t == "press":
        page.wait_for_timeout(600)  # let pickers/suggestions react to the typed text first
        loc.press(a["key"])
    elif t == "fill":
        loc.fill(substitute(a.get("value", ""), data))
    elif t == "select":
        loc.select_option(substitute(a.get("value", ""), data))
    elif t == "upload":
        files = [UPLOADS_DIR / name for name in a.get("files", [])]
        missing = [f.name for f in files if not f.exists()]
        if missing:
            raise StepFailed(f"The file to attach is not available to the automation: {', '.join(missing)}. "
                             f"Copy it into the folder {UPLOADS_DIR} and run again.")
        loc.set_input_files([str(f) for f in files])


def _tab(net: "_Network", a: dict, current: Page, note=lambda msg: None) -> Page:
    """The tab the action was recorded in; waits for a tab that the previous action is still opening.

    A tab can appear while recording without any recorded action opening it (the sign-in
    page opened the app in a new tab, or the user opened one). If it does not appear in
    the run, the action is done in the newest open tab instead, where the app usually is.
    """
    n = a.get("tab", 0)
    if n in net.stand_ins and not net.stand_ins[n].is_closed():
        page = net.stand_ins[n]
    else:
        until = time.time() + TAB_WAIT_MS / 1000
        while len(net.tabs) <= n and time.time() < until:
            current.wait_for_timeout(250)
        if len(net.tabs) > n:
            page = net.tabs[n]
        else:
            open_tabs = [t for t in net.tabs if not t.is_closed()]
            if not open_tabs:
                raise StepFailed(f"This action was recorded in browser tab {n + 1}, but the run has no open tab.")
            page = net.stand_ins[n] = open_tabs[-1]
            note(f"This action was recorded in browser tab {n + 1}, but no earlier action opens that tab, so it "
                 f"is done in tab {net.tabs.index(page) + 1} instead.")
    if page.is_closed():
        raise StepFailed(f"Browser tab {n + 1}, where this action was recorded, is already closed.")
    if page is not current:
        page.bring_to_front()
    return page


def _page_errors(page: Page) -> str:
    """Error messages the application itself is showing, to explain a failure."""
    try:
        texts = page.locator("[role=alert], .error, .err").filter(visible=True).all_inner_texts()
    except Exception:
        return ""
    texts = [t.strip() for t in texts if t.strip()]
    return " | ".join(texts)[:300]


# Lines of the automation's own log that say why an action could not be done.
REASONS = ("intercepts pointer events", "not visible", "not enabled", "not stable", "not editable",
           "outside of the viewport", "not attached", "detached")


def _short(exc: Exception) -> str:
    """The error with its reason from the automation's log, e.g. '<div> intercepts pointer events'."""
    msg = str(exc).strip().splitlines()
    if not msg:
        return exc.__class__.__name__
    why = []
    for line in msg[1:]:
        line = line.strip(" -")
        if any(r in line for r in REASONS) and line not in why:
            why.append(line)
    return msg[0][:300] + (" Reason: " + "; ".join(w[:160] for w in why[-3:]) + "." if why else "")


def run_session(session: Session, suite: dict, case: dict, recording: dict,
                data: dict | None = None, headed: bool = True, slow_mo: int = 250) -> str:
    log = session.log
    run_id = session.run_id
    rdir = storage.run_dir(run_id)
    shots = rdir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    test_data = {k: v for s in resolved_steps(case["steps"], now) for k, v in s["data"].items()}
    test_data.update({k: resolve_tokens(v, now) for k, v in (data or {}).items() if v is not None})
    target_url = recording.get("start_url", "")
    names = {s["step_id"]: s["name"] for s in case["steps"]}
    shown_data = mask_data(test_data)

    # environment and test_data (passwords masked) are for the issue log: which device, versions and values.
    run = {"id": run_id, "suite_id": suite["id"], "case_id": case["id"], "title": case["title"],
           "started_at": storage.now_iso(), "finished_at": None, "status": "running",
           "headed": headed, "slow_mo": slow_mo, "data_overrides": sorted((data or {}).keys()),
           "recorded_at": recording.get("recorded_at"), "start_url": target_url,
           "environment": environment(), "test_data": shown_data,
           "steps": [{"step_id": s["step_id"], "name": names.get(s["step_id"], s["name"]),
                      "status": "pending", "actions": len(s["actions"])} for s in recording["steps"]]}
    storage.save_run(run)
    t_run = time.time()
    log.emit("run_start", f"Run {run_id} started for {case['id']}: {len(run['steps'])} steps.",
             run_id=run_id, steps=[{"step_id": s["step_id"], "name": s["name"]} for s in run["steps"]])
    env = run["environment"]
    log.emit("info", f"Device {env['device']} (user {env['os_user']}), studio {env['app_version']}, "
             f"Playwright {env['packages']['playwright']}.")
    log.emit("info", "Test data: " + "; ".join(f"{k} = {v}" for k, v in shown_data.items()))

    following = iter([a for s in recording["steps"] for a in s["actions"]][1:])  # the action after the current one
    with sync_playwright() as p:
        browser = open_context(p, target_url, headless=not headed,
                               slow_mo=slow_mo if headed else 0)
        trace = {"on": start_trace(browser, f"{case['id']} {run_id}"), "paused": False}
        blog = BrowserLog(browser)
        page = first_page(browser)
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        net = _Network(browser)
        env["browser"] = browser_info(page)
        try:
            _run_steps(session, run, recording, test_data, page, net, blog, shots, following, trace)
        except Exception:
            run["crashed"] = True  # the session saves the traceback; keep the trace for it
            raise
        finally:
            failed = any(s["status"] == "failed" for s in run["steps"]) or run.get("crashed", False)
            stopped = any(s["status"] == "stopped" for s in run["steps"])
            if trace["on"]:
                keep = failed or stopped or TRACE == "always"
                run["trace"] = stop_trace(browser, rdir / "trace.zip" if keep else None, trace["paused"],
                                          _secrets(recording, test_data))
            try:
                blog.save(rdir / "browser_log.json")
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    counts = {k: sum(1 for s in run["steps"] if s["status"] == k) for k in ("passed", "failed", "skipped")}
    run["status"] = "stopped" if stopped else ("failed" if failed else "passed")
    run["finished_at"] = storage.now_iso()
    run["duration_ms"] = int((time.time() - t_run) * 1000)
    run.update(counts)

    try:
        write_results(storage.workbook_path(suite["id"]), rdir / "results.xlsx", case["sheet"], run)
        run["results_xlsx"] = f"/api/runs/{run_id}/results"
    except Exception as exc:
        log.emit("warning", f"Could not write the results workbook: {exc}", level="warn")

    log.emit("run_end", f"Run {run['status'].upper()}: {counts['passed']} passed, {counts['failed']} failed, "
             f"{counts['skipped']} skipped in {run['duration_ms'] / 1000:.1f}s.",
             level="success" if run["status"] == "passed" else "error",
             status=run["status"], results_url=run.get("results_xlsx"), **counts)
    run["events"] = log.all()
    storage.save_run(run)
    return "completed" if run["status"] == "passed" else run["status"]


def _secrets(recording: dict, data: dict) -> list[str]:
    """The passwords a run types: values of the masked fields."""
    out = []
    for s in recording["steps"]:
        for a in s["actions"]:
            if a.get("masked"):
                try:
                    out.append(substitute(a.get("value", ""), data))
                except MissingDataError:
                    pass
    return out


def _run_steps(session: Session, run: dict, recording: dict, test_data: dict, page: Page,
               net: "_Network", blog: BrowserLog, shots, following, trace: dict | None = None) -> None:
    """Play each step in turn; after a failure or a stop the remaining steps are skipped."""
    log = session.log
    run_id = run["id"]
    rdir = storage.run_dir(run_id)
    trace = trace or {"on": False, "paused": False}
    failed = stopped = False
    prev = None  # the action before the current one, which may need repeating if it had no effect
    picked = []  # dropdown choices made so far, read again after each action (see _undone_picks)
    undone = []  # choices that a later action undid: the likely cause if the run fails later
    for idx, (rstep, res) in enumerate(zip(recording["steps"], run["steps"])):
        sid = rstep["step_id"]
        if failed or stopped:
            res.update(status="skipped", observed="Not run (an earlier step did not pass).")
            log.emit("step_skipped", f"{sid} skipped", step_id=sid, idx=idx, level="warn")
            continue

        log.emit("step_start", f"{sid} · {res['name']}", step_id=sid, idx=idx)
        t_step = time.time()
        done, checks, error, diag, a = 0, [], None, None, None
        try:
            for a in rstep["actions"]:
                if session.stop_event.is_set():
                    raise Stopped()
                text = describe(a, test_data)
                t_act = time.time()
                nxt = next(following, None)
                warn = lambda msg: log.emit("info", msg, step_id=sid, idx=idx, level="warn")
                if repeated_click(prev, a):
                    # Recorded before the recorder merged these: replayed, the click finds nothing left.
                    warn(f"{text} skipped: it is the second half of a double-click on the same element, "
                         "recorded as two clicks.")
                    prev = a
                    done += 1
                    continue
                if a.get("masked") and trace["on"] and not trace["paused"]:
                    trace["paused"] = pause_trace(page.context)
                    if trace["paused"]:
                        log.emit("info", "The trace leaves out the sign-in: it starts once no password is on "
                                 "screen, so the password is not saved in it.", step_id=sid, idx=idx)
                try:
                    page = _tab(net, a, page, warn)
                    used, cand = _execute(page, a, test_data, net, prev, warn, nxt)
                    prev = a
                    if a["type"] == "select":
                        shown = _selected(page, a)
                        if shown and shown[1]:
                            picked.append({"action": a, "shown": shown[1]})
                    if picked and a["type"] not in ("expect_text", "scroll"):
                        for msg in _undone_picks(page, picked, a, test_data):
                            undone.append(msg)
                            warn(msg)
                    if trace["paused"] and not secret_on_screen(page.context):
                        trace["paused"] = not resume_trace(page.context)
                except (StepFailed, MissingDataError) as exc:
                    raise StepFailed(f"{text}: {exc}") from exc
                except Stopped:
                    raise
                except Exception as exc:
                    raise StepFailed(f"{text}: {_short(exc)}") from exc
                done += 1
                if a["type"] == "expect_text":
                    checks.append(substitute(a["text"], test_data))
                log.emit("action", text, step_id=sid, idx=idx, status="pass",
                         ms=int((time.time() - t_act) * 1000), locator=used,
                         level="warn" if cand else "info",
                         note="primary locator not found; used a fallback" if cand else "")
            status = "passed"
        except Stopped:
            status, stopped, error = "stopped", True, "Stopped by user"
            diag = {"stopped_during": describe(a, test_data) if a else ""}
        except StepFailed as exc:
            status, failed, error = "failed", True, str(exc)
            # The recorded action as replay saw it: which element, found which ways, in which tab or frame.
            diag = {"error": error_details(exc),
                    "action": {k: v for k, v in a.items() if k in ("type", "target", "locators", "tab", "frame",
                                                                    "param", "navigates")}}
            log.emit("action", str(exc), step_id=sid, idx=idx, status="fail", level="error")
            shown = _page_errors(page)
            if shown:
                res["app_message"] = shown
                error += f". Application shows: {shown}"
                log.emit("info", f"Application shows: {shown}", step_id=sid, idx=idx, level="error")
            if undone:
                error += f". Likely cause, earlier in the run: {undone[-1]}"
        if diag is not None:
            # For the issue log: the page at that moment and what the browser reported during the step.
            diag["page"] = capture_page(page, rdir / "failure", f"{idx + 1:02d}_{sid}")
            diag["browser_log"] = blog.since(t_step)[-50:]
            res["diagnostics"] = diag
            if diag["page"].get("url"):
                log.emit("info", f"Page at the time: {diag['page']['url']} \"{diag['page'].get('title', '')}\"",
                         step_id=sid, idx=idx, level="warn")
            for e in diag["browser_log"][-5:]:
                log.emit("info", f"Browser {e['kind']}: {e['text'][:300]}", step_id=sid, idx=idx, level="warn")

        shot = shots / f"{idx + 1:02d}_{sid}.png"
        try:
            page.screenshot(path=str(shot), timeout=5000)
            shot_url = f"/artifacts/runs/{run_id}/screenshots/{shot.name}"
        except Exception:
            shot_url = None

        observed = (f"Automated: {done} action(s) completed."
                    + (f" Verified on screen: {'; '.join(checks)}." if checks else ""))
        if error:
            observed = f"Failed after {done} action(s): {error}"
        ms = int((time.time() - t_step) * 1000)
        res.update(status=status, observed=observed, duration_ms=ms, screenshot=shot_url, error=error)
        log.emit("step_end", f"{sid} {status.upper()} ({ms / 1000:.1f}s)", step_id=sid, idx=idx,
                 status=status, ms=ms, screenshot=shot_url, error=error,
                 level={"passed": "success", "failed": "error"}.get(status, "warn"))
        storage.save_run(run)
