"""Record a user performing the Excel steps in a real browser.

Opens Chromium with recorder.js injected. The toolbar in that window shows the
current Excel step; everything the user does is stored against that step.
When the user finishes (or closes the window), values that match the Excel
test data are turned into parameters and the recording is saved.

Also provides the sign-in session: open the target site in the same browser
profile so the user can log in once (MFA included) before recording or runs.
"""
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page, sync_playwright

from . import storage
from .actions import describe, parameterize, resolved_steps, start_url as case_start_url
from .browser import first_page, mark_signed_in, open_context
from .config import TARGET_START_URL
from .events import Session

RECORDER_JS = (Path(__file__).parent / "recorder.js").read_text(encoding="utf-8")


def _wait_until(ctx, session: Session, done: Callable[[], bool]) -> None:
    """Keep Playwright responsive (bindings fire here) until finished or closed."""
    while not done() and not session.stop_event.is_set():
        open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
        if not open_pages:
            break
        try:
            open_pages[-1].wait_for_timeout(200)
        except Exception:  # window closed by the user
            break


def record_session(session: Session, suite: dict, case: dict, start_url: str | None = None,
                   headless: bool = False, on_ready: Callable[[Page], None] | None = None) -> str:
    """on_ready lets the smoke test play the user's part; the app leaves it unset."""
    log = session.log
    steps = resolved_steps(case["steps"])  # {now}-style tokens get this session's values
    start_url = start_url or case_start_url(steps, TARGET_START_URL)
    rec_steps = [{"step_id": s["step_id"], "name": s["name"], "actions": []} for s in steps]
    st = {"idx": 0, "finished": False}

    def snapshot() -> dict:
        s = steps[st["idx"]]
        return {"mode": "record", "idx": st["idx"], "total": len(steps), "step_id": s["step_id"],
                "name": s["name"], "description": s["description"], "test_data": s["data"],
                "expected": s["expected"], "count": len(rec_steps[st["idx"]]["actions"]),
                "finished": st["finished"]}

    def add(action: dict) -> None:
        action = {k: v for k, v in action.items() if v not in (None, "")}
        action["recorded_at"] = time.time()
        cur = rec_steps[st["idx"]]["actions"]
        # Typing into the same field twice in a row keeps only the final value.
        if (action["type"] == "fill" and cur and cur[-1]["type"] == "fill"
                and cur[-1]["locators"][:1] == action["locators"][:1]):
            cur[-1] = action
        else:
            cur.append(action)
        step = rec_steps[st["idx"]]
        log.emit("recorded", describe(action), step_id=step["step_id"], idx=st["idx"],
                 action=_public(action))

    def on_action(_source, action):
        if not st["finished"]:
            add(action)
        return snapshot()

    def on_next(_source):
        if st["idx"] < len(steps) - 1:
            st["idx"] += 1
            s = steps[st["idx"]]
            log.emit("step_change", f"Now recording {s['step_id']}: {s['name']}",
                     idx=st["idx"], step_id=s["step_id"])
        return snapshot()

    def on_finish(_source):
        st["finished"] = True
        return snapshot()

    def on_navigated(frame) -> None:
        # Remember which clicks load a new page, so replay can wait for it.
        if frame.parent_frame is not None:
            return
        cur = rec_steps[st["idx"]]["actions"] or (rec_steps[st["idx"] - 1]["actions"] if st["idx"] else [])
        if cur and cur[-1]["type"] in ("click", "press") and time.time() - cur[-1]["recorded_at"] < 5:
            cur[-1]["navigates"] = True

    log.emit("recording_started", f"Recording {case['id']} on {start_url}. Perform each step in the "
             "browser and use the toolbar to move to the next step.",
             steps=[{"step_id": s["step_id"], "name": s["name"]} for s in steps])

    with sync_playwright() as p:
        ctx = open_context(p, start_url, headless=headless)
        ctx.expose_binding("__recAction", on_action)
        ctx.expose_binding("__recState", lambda _s: snapshot())
        ctx.expose_binding("__recNext", on_next)
        ctx.expose_binding("__recFinish", on_finish)
        ctx.add_init_script(RECORDER_JS)
        ctx.on("page", lambda pg: pg.on("framenavigated", on_navigated))
        page = first_page(ctx)
        page.on("framenavigated", on_navigated)  # harmless if the context hook fired too
        add({"type": "goto", "url": start_url, "target": start_url, "locators": []})
        page.goto(start_url)
        if on_ready:
            on_ready(page)
        _wait_until(ctx, session, lambda: st["finished"])
        if st["finished"]:
            time.sleep(1.2)  # let the "Recording saved" toolbar show briefly
        try:
            ctx.close()
        except Exception:
            pass

    status = "completed" if st["finished"] else "stopped"
    linked = parameterize(rec_steps, steps)
    total = sum(len(s["actions"]) for s in rec_steps)
    warnings = _review(rec_steps, steps, linked)
    recording = {
        "suite_id": suite["id"], "case_id": case["id"], "recorded_at": storage.now_iso(),
        "start_url": start_url, "status": status, "linked_parameters": linked,
        "warnings": warnings, "steps": rec_steps,
    }
    storage.save_recording(suite["id"], case["id"], recording)

    for w in warnings:
        log.emit("warning", w, level="warn")
    log.emit("recording_saved",
             f"Recording saved: {total} actions across {len(rec_steps)} steps, "
             f"{linked} value(s) linked to Excel test data.",
             level="success", total=total, linked=linked, status=status)
    return status


def signin_session(session: Session, suite: dict, case: dict, headless: bool = False) -> str:
    """Open the test's site so the user can sign in; the profile keeps the session."""
    log = session.log
    url = case_start_url(case["steps"], TARGET_START_URL)
    st = {"finished": False}

    def snapshot(_source=None):
        return {"mode": "signin", "url": url, "finished": st["finished"]}

    def on_finish(_source):
        st["finished"] = True
        return snapshot()

    log.emit("signin_started", f"Opening {url}. Sign in there (including any MFA prompt), "
             "then click 'Save sign-in' on the toolbar.", url=url)
    with sync_playwright() as p:
        ctx = open_context(p, url, headless=headless)
        ctx.expose_binding("__recState", snapshot)
        ctx.expose_binding("__recAction", lambda _s, _a: snapshot())
        ctx.expose_binding("__recNext", lambda _s: snapshot())
        ctx.expose_binding("__recFinish", on_finish)
        ctx.add_init_script(RECORDER_JS)
        first_page(ctx).goto(url)
        _wait_until(ctx, session, lambda: st["finished"])
        if st["finished"]:
            time.sleep(1.0)
        try:
            ctx.close()  # flushes cookies and storage into the profile
        except Exception:
            pass
    status = "completed" if st["finished"] else "stopped"
    if st["finished"]:
        mark_signed_in(url, storage.now_iso())
    log.emit("signin_saved" if st["finished"] else "warning",
             "Sign-in saved for this site. Recordings and runs will reuse it."
             if st["finished"] else "Sign-in window closed. Whatever was completed is kept in the profile.",
             level="success" if st["finished"] else "warn", url=url,
             saved_at=datetime.now().isoformat(timespec="seconds"))
    return status


def _review(rec_steps: list[dict], steps: list[dict], linked: int) -> list[str]:
    """Plain-language problems with a recording that make later runs unreliable."""
    notes = []
    with_actions = [s for s in rec_steps if any(a["type"] != "goto" for a in s["actions"])]
    if len(rec_steps) > 1 and len(with_actions) == 1:
        notes.append(f"Everything was recorded under {with_actions[0]['step_id']}. Click 'Next step' on the "
                     "toolbar as you finish each step, so each Excel step gets its own actions and result.")
    else:
        empty = [s["step_id"] for s in rec_steps if not s["actions"]]
        if empty:
            notes.append(f"No actions were recorded for {', '.join(empty)}.")
    typed = [a for s in rec_steps for a in s["actions"] if a["type"] == "fill" and not a.get("masked")]
    has_data = any(v for s in steps for k, v in s["data"].items() if k.lower() != "url")
    if typed and has_data and linked == 0:
        notes.append("None of the typed values match the Excel test data, so every run will type exactly "
                     "what you typed and changes to the Excel data will have no effect. Type the values "
                     "shown on the toolbar to link them.")
    used = {k.strip() for s in rec_steps for a in s["actions"] for k in a.get("param", "").split(",") if k.strip()}
    unused = sorted({k for s in steps for k, v in s["data"].items() if v and k.lower() != "url"} - used)
    if linked and unused:
        notes.append(f"Excel test data not used by any recorded action: {', '.join(unused)}. "
                     "If the test should type these (e.g. the message body), they were not captured; re-record "
                     "and type the values shown on the toolbar.")
    partial = [a["value"] for a in typed if not a.get("param") and len(a.get("value", "")) >= 3
               and any(v.startswith(a["value"]) and v != a["value"] for s in steps for v in s["data"].values())]
    if partial:
        notes.append(f"'{partial[0]}' looks like the start of an Excel value (e.g. a recipient picked from "
                     "suggestions). Type the full value so the automation doesn't depend on what the "
                     "suggestion list shows.")
    if total_checks(rec_steps) == 0:
        notes.append("No checks were added, so a run can only show that the clicks happened, not that the "
                     "result appeared (e.g. the email in Sent Items). Use '+ Add check'.")
    return notes


def total_checks(rec_steps: list[dict]) -> int:
    return sum(1 for s in rec_steps for a in s["actions"] if a["type"] == "expect_text")


def _public(action: dict) -> dict:
    a = dict(action)
    if a.get("masked") and "value" in a:
        a["value"] = "••••••"
    a.pop("recorded_at", None)
    return a
