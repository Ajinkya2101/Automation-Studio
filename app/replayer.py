"""Replay a recording as an automated run, step by step, with logs.

No recording is needed at run time beyond the saved actions: each action's
locator candidates are tried in order (most stable first), values come from
the Excel test data, and every step ends with a screenshot.
"""
import re
import time
from datetime import datetime

from playwright.sync_api import Page, expect, sync_playwright

from . import storage
from .actions import (MissingDataError, describe, locator_label, resolve_tokens, resolved_steps,
                      substitute)
from .browser import first_page, open_context
from .config import ACTION_TIMEOUT_MS, APP_URL, SLOW_FACTOR
from .events import Session
from .excel_io import write_results
from . import mockmail


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
        ctx.on("request", self._start)
        ctx.on("requestfinished", self._end)
        ctx.on("requestfailed", self._end)

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


def _candidates(recorded: list[dict]) -> list[dict]:
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
        c = {**c, "must_be_visible": True}
        if c["kind"] == "css":
            if GENERATED_ID.search(c["value"]):
                continue
        elif c["kind"] != "testid":
            c["value"] = _clean(c["value"])
        if c["value"]:
            out.append(c)
    for c in [c for c in out if c["kind"] == "role"]:
        out.append({**c, "include_hidden": True, "must_be_visible": True})
        out.append({"kind": "css", "value": f'[aria-label="{_css_str(c["value"])}"]', "must_be_visible": True})
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


def _resolve(page: Page, candidates: list[dict], target: str, timeout_ms: int = ACTION_TIMEOUT_MS,
             css_grace_s: float = STRUCTURAL_GRACE_S):
    """First candidate matching exactly one visible element wins; waits while the page loads.

    Semantic locators (role, label, text) are accepted at once. Structural CSS
    fallbacks only after a grace period, so a CSS path that happens to match
    something while the page is still loading cannot win the race.
    """
    start = time.time()
    deadline = start + timeout_ms / 1000
    hidden_only = False
    while True:
        allow_css = time.time() - start >= css_grace_s
        fallback = None
        for i, c in enumerate(candidates):
            if c["kind"] == "css" and not allow_css:
                continue
            try:
                loc = _build(page, c)
                n = loc.count()
                if n and c.get("must_be_visible"):
                    loc = loc.filter(visible=True)
                    visible = loc.count()
                    hidden_only = hidden_only or visible == 0
                    n = visible
            except Exception:  # page mid-navigation
                continue
            if n == 1:
                return loc, i
            if n > 1 and fallback is None:
                c["matches"] = n
                fallback = (loc.first, i)
        if fallback:
            return fallback
        if time.time() > deadline:
            if hidden_only:
                raise StepFailed(
                    f"Found {target}, but it stayed hidden (for example inside a collapsed menu group or a closed "
                    "panel). The recording is probably missing the click that opens it: re-record this step "
                    "and click the menu group or panel header before the item.")
            raise StepFailed(f"Could not find {target} on the page (tried {len(candidates)} locator(s)).")
        page.wait_for_timeout(250)


def _scroll(page: Page, a: dict) -> str:
    """Best effort: repeat a recorded scroll so lists that load more items on scroll do so.

    A scroll never fails a step: if the scrolled area cannot be found, the next
    action still gets its chance (Playwright scrolls to elements before clicking).
    """
    x, y = int(a.get("x", 0)), int(a.get("y", 0))
    if a.get("page"):
        page.evaluate("([x, y]) => window.scrollTo(x, y)", [x, y])
        page.wait_for_timeout(300)
        return "page"
    try:
        # Scroll areas rarely have a label or short text, so their CSS path is allowed at once.
        loc, i = _resolve(page, _candidates(a.get("locators", [])), a.get("target", "area"),
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
                                               and not a.get("navigates"))


def _redo(page: Page, a: dict, data: dict) -> None:
    """Repeat an action that had no effect. Typing is repeated key by key, because
    search-as-you-type fields react to keystrokes, not to a pasted value."""
    if a["type"] == "scroll":
        _scroll(page, a)
        return
    loc, _ = _resolve(page, _candidates(a["locators"]), a.get("target", "field"), timeout_ms=5000, css_grace_s=0)
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
    candidates = _candidates(a["locators"])
    target = a.get("target", "element")
    start = time.time()
    deadline = start + ACTION_TIMEOUT_MS / 1000
    recover_at = start + RECOVER_AFTER_S if _repeatable(prev) else None
    while True:
        try:
            loc, idx = _ready(page, candidates, target, min(deadline, recover_at) if recover_at else deadline)
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
    result = _perform(page, a, data, net, prev, note)
    if a["type"] not in ("expect_text", "scroll"):
        _settle(page, net)
    if a["type"] == "fill":
        _check_fill(page, a, data, net)
        _commit(page, a, net, nxt)
    return result


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
        loc, _ = _resolve(page, _candidates(a["locators"]), a.get("target", "field"), timeout_ms=5000, css_grace_s=0)
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
    try:
        loc, _ = _resolve(page, candidates, a.get("target", "field"), timeout_ms=5000, css_grace_s=0)
        if loc.input_value(timeout=2000) == value:
            return
    except Exception:
        return  # not a plain input (e.g. a rich editor); nothing reliable to compare
    loc, _ = _resolve(page, candidates, a.get("target", "field"))
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
            expect(page.get_by_text(text).first).to_be_visible(timeout=ACTION_TIMEOUT_MS)
        except AssertionError:
            raise StepFailed(f"\"{text}\" did not appear within {ACTION_TIMEOUT_MS // 1000}s") from None
        return f'text contains "{text}"', 0
    if t == "scroll":
        return _scroll(page, a), 0

    candidates, loc, idx = _find(page, a, data, net, prev, note)
    used =locator_label(candidates[idx]) + (" (under aria-hidden)" if candidates[idx].get("include_hidden") else "")
    try:
        _act(page, a, data, loc)
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


def _act(page: Page, a: dict, data: dict, loc) -> None:
    t = a["type"]
    if t in ("click", "press") and a.get("navigates"):
        with page.expect_navigation(wait_until="domcontentloaded", timeout=ACTION_TIMEOUT_MS):
            loc.click() if t == "click" else loc.press(a["key"])
    elif t == "click":
        loc.click()
    elif t == "press":
        page.wait_for_timeout(600)  # let pickers/suggestions react to the typed text first
        loc.press(a["key"])
    elif t == "fill":
        loc.fill(substitute(a.get("value", ""), data))
    elif t == "select":
        loc.select_option(substitute(a.get("value", ""), data))


def _page_errors(page: Page) -> str:
    """Error messages the application itself is showing, to explain a failure."""
    try:
        texts = page.locator("[role=alert], .error, .err").all_inner_texts()
    except Exception:
        return ""
    texts = [t.strip() for t in texts if t.strip()]
    return " | ".join(texts)[:300]


def _short(exc: Exception) -> str:
    msg = str(exc).strip().splitlines()
    return msg[0][:300] if msg else exc.__class__.__name__


def run_session(session: Session, suite: dict, case: dict, recording: dict,
                data: dict | None = None, headed: bool = True, slow_mo: int = 250,
                reset_mailbox: bool = True) -> str:
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

    run = {"id": run_id, "suite_id": suite["id"], "case_id": case["id"], "title": case["title"],
           "started_at": storage.now_iso(), "finished_at": None, "status": "running",
           "headed": headed, "data_overrides": sorted((data or {}).keys()),
           "steps": [{"step_id": s["step_id"], "name": names.get(s["step_id"], s["name"]),
                      "status": "pending", "actions": len(s["actions"])} for s in recording["steps"]]}
    storage.save_run(run)
    t_run = time.time()
    log.emit("run_start", f"Run {run_id} started for {case['id']}: {len(run['steps'])} steps.",
             run_id=run_id, steps=[{"step_id": s["step_id"], "name": s["name"]} for s in run["steps"]])

    if reset_mailbox and target_url.startswith(f"{APP_URL}/mail"):
        mockmail.reset_mailbox()
        log.emit("info", "Test data reset: Acme Mail sent items cleared.")
    shown_data = {k: ("••••••" if "pass" in k.lower() else v) for k, v in test_data.items()}
    log.emit("info", "Test data: " + "; ".join(f"{k} = {v}" for k, v in shown_data.items()))

    failed = stopped = False
    prev = None  # the action before the current one, which may need repeating if it had no effect
    following = iter([a for s in recording["steps"] for a in s["actions"]][1:])  # the action after the current one
    with sync_playwright() as p:
        browser = open_context(p, target_url or APP_URL, headless=not headed,
                               slow_mo=slow_mo if headed else 0)
        page = first_page(browser)
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        net = _Network(browser)

        for idx, (rstep, res) in enumerate(zip(recording["steps"], run["steps"])):
            sid = rstep["step_id"]
            if failed or stopped:
                res.update(status="skipped", observed="Not run (an earlier step did not pass).")
                log.emit("step_skipped", f"{sid} skipped", step_id=sid, idx=idx, level="warn")
                continue

            log.emit("step_start", f"{sid} · {res['name']}", step_id=sid, idx=idx)
            t_step = time.time()
            done, checks, error = 0, [], None
            try:
                for a in rstep["actions"]:
                    if session.stop_event.is_set():
                        raise Stopped()
                    text = describe(a, test_data)
                    t_act = time.time()
                    try:
                        used, cand = _execute(page, a, test_data, net, prev,
                                              lambda msg: log.emit("info", msg, step_id=sid, idx=idx, level="warn"),
                                              next(following, None))
                        prev = a
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
            except StepFailed as exc:
                status, failed, error = "failed", True, str(exc)
                log.emit("action", str(exc), step_id=sid, idx=idx, status="fail", level="error")
                shown = _page_errors(page)
                if shown:
                    error += f". Application shows: {shown}"
                    log.emit("info", f"Application shows: {shown}", step_id=sid, idx=idx, level="error")

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
