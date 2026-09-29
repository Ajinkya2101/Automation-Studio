"""Record a user performing the Excel steps in a real browser.

Opens Chromium with recorder.js injected. The toolbar in that window shows the
current Excel step; everything the user does is stored against that step.
When the user finishes (or closes the window), values that match the Excel
test data are turned into parameters and the recording is saved.

Also provides the sign-in session: open the target site in the same browser
profile so the user can log in once (MFA included) before recording or runs.
"""
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import Page, sync_playwright

from . import storage
from .actions import case_target, describe, parameterize, resolved_steps
from .browser import first_page, mark_signed_in, open_context
from .excel_io import build_workbook
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


def _blank_step(case_id: str, n: int) -> dict:
    return {"step_id": f"{case_id}.{n}", "name": f"Step {n}", "role": "", "description": "",
            "data": {}, "expected": ""}


def record_session(session: Session, suite: dict, case: dict, start_url: str | None = None,
                   headless: bool = False, on_ready: Callable[[Page], None] | None = None) -> str:
    """on_ready lets the smoke test play the user's part; the app leaves it unset.

    Two modes:
    - Excel test case: the steps come from the workbook; the user follows them.
    - Free-form (case["freeform"]): no workbook. Each 'Next step' starts a new
      step, and when the recording is saved the steps, their test data and an
      Excel test script are built from what the user did.
    """
    log = session.log
    freeform = bool(case.get("freeform"))
    # {now}-style tokens get this session's values
    steps = [_blank_step(case["id"], 1)] if freeform else resolved_steps(case["steps"])
    start_url = start_url or case_target(case, TARGET_START_URL)
    rec_steps = [{"step_id": s["step_id"], "name": s["name"], "actions": []} for s in steps]
    st = {"idx": 0, "finished": False}

    def snapshot() -> dict:
        s = steps[st["idx"]]
        custom = s["name"] if s["name"] != f"Step {st['idx'] + 1}" else ""
        return {"mode": "record", "freeform": freeform, "idx": st["idx"], "total": len(steps),
                "step_id": s["step_id"], "name": s["name"], "custom_name": custom, "description": s["description"],
                "test_data": s["data"], "expected": s["expected"],
                "count": len(rec_steps[st["idx"]]["actions"]), "finished": st["finished"]}

    def name_current(name) -> None:
        """Free-form: the user can name the step on the toolbar."""
        name = (name or "").strip()[:80] if isinstance(name, str) else ""
        if freeform and name and name != steps[st["idx"]]["name"]:
            steps[st["idx"]]["name"] = rec_steps[st["idx"]]["name"] = name
            log.emit("step_named", f"{steps[st['idx']]['step_id']} named \"{name}\"", idx=st["idx"], name=name)

    def add(action: dict) -> None:
        action = {k: v for k, v in action.items() if v not in (None, "")}
        action["recorded_at"] = time.time()
        cur = rec_steps[st["idx"]]["actions"]
        # Typing into the same field, or scrolling the same area, twice in a row keeps only the final one.
        if (action["type"] in ("fill", "scroll") and cur and cur[-1]["type"] == action["type"]
                and cur[-1].get("locators", [])[:1] == action.get("locators", [])[:1]
                and cur[-1].get("page") == action.get("page")):
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

    def on_next(_source, name=None):
        name_current(name)
        if freeform and rec_steps[st["idx"]]["actions"]:
            n = len(steps) + 1
            steps.append(_blank_step(case["id"], n))
            rec_steps.append({"step_id": steps[-1]["step_id"], "name": steps[-1]["name"], "actions": []})
        if st["idx"] < len(steps) - 1:
            st["idx"] += 1
            s = steps[st["idx"]]
            log.emit("step_change", f"Now recording {s['step_id']}: {s['name']}",
                     idx=st["idx"], step_id=s["step_id"], name=s["name"])
        return snapshot()

    def on_finish(_source, name=None):
        name_current(name)
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
        ctx.expose_binding("__recName", lambda _s, name: (name_current(name), snapshot())[1])
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
    if freeform:
        steps, rec_steps = _build_freeform_case(suite, case, steps, rec_steps)
    linked = parameterize(rec_steps, steps)
    total = sum(len(s["actions"]) for s in rec_steps)
    warnings = _review(rec_steps, steps, linked, freeform)
    recording = {
        "suite_id": suite["id"], "case_id": case["id"], "recorded_at": storage.now_iso(),
        "start_url": start_url, "status": status, "linked_parameters": linked,
        "warnings": warnings, "steps": rec_steps,
    }
    storage.save_recording(suite["id"], case["id"], recording)

    for w in warnings:
        log.emit("warning", w, level="warn")
    what = ("value(s) saved as editable test data and an Excel test script written" if freeform
            else "value(s) linked to Excel test data")
    log.emit("recording_saved",
             f"Recording saved: {total} actions across {len(rec_steps)} steps, {linked} {what}.",
             level="success", total=total, linked=linked, status=status)
    return status


def _field_key(action: dict) -> str:
    """Test data key for a typed value: the field's label, e.g. 'Subject'."""
    raw = next((c["value"] for c in action.get("locators", []) if c["kind"] in ("label", "placeholder")), "")
    if not raw:
        m = re.search(r'"(.+)"', action.get("target", ""))
        raw = m.group(1) if m else "Value"
    # Keys must survive a round trip through the Excel "Key: value" lines.
    key = re.sub(r"\s+", " ", re.sub(r"[^\w ./()&-]", " ", raw)).strip()[:36].strip()
    return key if key[:1].isalpha() else f"Field {key}".strip()


def _build_freeform_case(suite: dict, case: dict, steps: list[dict], rec_steps: list[dict]):
    """Turn a free-form recording into test-case steps, test data and an Excel script."""
    kept = ([(i, s, r) for i, (s, r) in enumerate(zip(steps, rec_steps)) if r["actions"]]
            or [(0, steps[0], rec_steps[0])])
    new_steps, new_rec, used = [], [], set()
    for n, (orig, s, r) in enumerate(kept, start=1):
        step_id = f"{case['id']}.{n}"
        data = {}
        for a in r["actions"]:
            if a["type"] == "fill" and a.get("value") and not a.get("masked"):
                key, i = _field_key(a), 2
                base = key
                while key in used:
                    key, i = f"{base} {i}", i + 1
                used.add(key)
                data[key] = a["value"]
        checks = [a["text"] for a in r["actions"] if a["type"] == "expect_text"]
        # Unnamed steps are renumbered after empty ones are dropped; named steps keep their name.
        name = s["name"] if s["name"] != f"Step {orig + 1}" else f"Step {n}"
        new_steps.append({
            "step_id": step_id, "name": name, "role": "",
            "description": "\n".join(f"{i}. {describe(a)}" for i, a in enumerate(r["actions"], start=1)),
            "data": data, "expected": "; ".join(f'"{c}" is shown' for c in checks)})
        new_rec.append({**r, "step_id": step_id, "name": name})
    case["steps"] = new_steps
    stored = storage.load_suite(suite["id"]) or suite
    stored["cases"] = [case if c["id"] == case["id"] else c for c in stored["cases"]]
    storage.save_suite(stored)
    build_workbook(storage.workbook_path(suite["id"]), case)
    return new_steps, new_rec


def signin_session(session: Session, suite: dict, case: dict, headless: bool = False) -> str:
    """Open the test's site so the user can sign in; the profile keeps the session."""
    log = session.log
    url = case_target(case, TARGET_START_URL)
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


def _review(rec_steps: list[dict], steps: list[dict], linked: int, freeform: bool = False) -> list[str]:
    """Plain-language problems with a recording that make later runs unreliable."""
    if freeform:
        # Steps and test data come from the recording itself, so only the checks can be missing.
        return [] if total_checks(rec_steps) else [
            "No checks were added, so a run can only show that the clicks happened, not that the "
            "result appeared. Use '+ Add check' on text that proves a step worked."]
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
