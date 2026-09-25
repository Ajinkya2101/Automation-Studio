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
from .config import ACTION_TIMEOUT_MS, APP_URL
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
      also gets an include-hidden twin and an [aria-label] CSS twin, used only
      when the element is actually visible.
    """
    out = []
    for c in recorded:
        c = dict(c)
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


def _resolve(page: Page, candidates: list[dict], target: str):
    """First candidate matching exactly one element wins; waits while the page loads.

    Semantic locators (role, label, text) are accepted at once. Structural CSS
    fallbacks only after a grace period, so a CSS path that happens to match
    something while the page is still loading cannot win the race.
    """
    start = time.time()
    deadline = start + ACTION_TIMEOUT_MS / 1000
    while True:
        allow_css = time.time() - start >= STRUCTURAL_GRACE_S
        fallback = None
        for i, c in enumerate(candidates):
            if c["kind"] == "css" and not allow_css:
                continue
            try:
                loc = _build(page, c)
                n = loc.count()
                if n and c.get("must_be_visible"):
                    loc = loc.filter(visible=True)
                    n = loc.count()
            except Exception:  # page mid-navigation
                continue
            if n == 1:
                return loc, i
            if n > 1 and fallback is None:
                fallback = (loc.first, i)
        if fallback:
            return fallback
        if time.time() > deadline:
            raise StepFailed(f"Could not find {target} on the page "
                             f"(tried {len(candidates)} locator(s)).")
        page.wait_for_timeout(250)


def _execute(page: Page, a: dict, data: dict) -> tuple[str, int]:
    """Run one action. Returns (locator used, candidate index)."""
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

    candidates = _candidates(a["locators"])
    loc, idx = _resolve(page, candidates, a.get("target", "element"))
    used = locator_label(candidates[idx]) + (" (under aria-hidden)" if candidates[idx].get("include_hidden") else "")
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
    return used, idx


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
    with sync_playwright() as p:
        browser = open_context(p, target_url or APP_URL, headless=not headed,
                               slow_mo=slow_mo if headed else 0)
        page = first_page(browser)
        page.set_default_timeout(ACTION_TIMEOUT_MS)

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
                        used, cand = _execute(page, a, test_data)
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
