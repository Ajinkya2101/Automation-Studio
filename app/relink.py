"""Re-link the test data of tests recorded without Excel, with the current linking rules.

Recordings saved before 12 Nov 2026 can have
- a typed value linked to another field that held the same value (GL_Code_Product_Cat
  typing ${GL_Code_Location} because both were 4111), and
- a field typed twice over (the sign-in page's remembered user name, typed over) saved
  as two values, "Username" and "Username 2", so a run form with "Username 2" left
  empty signs in with no user name.

This rebuilds each such test's data from its recording as a new recording would get it.
It changes nothing unless --apply is given; then the test's folder is backed up first
to data/backups/relink-<time>/.

    python -m app.relink            # show what would change
    python -m app.relink --apply    # change it (as admin; written to the audit log)
"""
import argparse
import copy
import shutil
import sys
from datetime import datetime

from . import audit, storage
from .actions import PARAM, MissingDataError, parameterize, substitute
from .config import DATA_DIR
from .excel_io import build_workbook
from .recorder import freeform_steps, linked_count


def _unlinked(rec_steps: list[dict], data: dict[str, str]) -> list[dict]:
    """The recording as it was typed: every ${Key} replaced by the test's value."""
    out = copy.deepcopy(rec_steps)
    for s in out:
        for a in s["actions"]:
            for field in ("value", "text"):
                if isinstance(a.get(field), str) and PARAM.search(a[field]):
                    a[field] = substitute(a[field], data)
            if a["type"] in ("fill", "select", "expect_text"):
                a.pop("param", None)
                a.pop("recorded_value", None)
    return out


def _links(rec_steps: list[dict]) -> list[str]:
    return [f"{a.get('target', '')} -> ${{{a['param']}}}" for s in rec_steps for a in s["actions"] if a.get("param")]


def relink(suite: dict, case: dict, recording: dict) -> tuple[list[dict], list[dict], list[str]] | None:
    """(new case steps, new recording steps, what changes), or None when nothing changes."""
    data = {k: v for s in case["steps"] for k, v in s["data"].items()}
    rec_steps = _unlinked(recording["steps"], data)
    steps = [{**s, "data": {}} for s in case["steps"]]
    if len(steps) != len(rec_steps):
        raise ValueError("its steps and its recording do not match")
    new_steps, new_rec = freeform_steps(case, steps, rec_steps)
    # Test data added for clicked text (see main.add_text_param) is kept with its step, and so is data
    # that a check or list choice uses without any typing providing it.
    for ns, rs, old in zip(new_steps, new_rec, recording["steps"]):
        for a in rs["actions"]:
            for tp in a.get("text_params", []):
                ns["data"].setdefault(tp["key"], tp["value"])
        for a in old["actions"]:
            if a["type"] != "fill" and a.get("param"):
                for k in (k.strip() for k in a["param"].split(",")):
                    if k in data and not any(k in s["data"] for s in new_steps):
                        ns["data"][k] = data[k]
    parameterize(new_rec, new_steps)

    old_links, new_links = _links(recording["steps"]), _links(new_rec)
    old_keys = {k: v for s in case["steps"] for k, v in s["data"].items()}
    new_keys = {k: v for s in new_steps for k, v in s["data"].items()}
    changes = [f"{o}  becomes  {n.split(' -> ')[1]}" for o, n in zip(old_links, new_links) if o != n]
    if len(old_links) != len(new_links):
        changes.append(f"{len(old_links)} linked actions become {len(new_links)}")
    changes += [f"test data '{k}' ({old_keys[k]!r}) is no longer needed" for k in old_keys if k not in new_keys]
    changes += [f"test data '{k}' = {new_keys[k]!r} is new" for k in new_keys if k not in old_keys]
    changes += [f"test data '{k}': {old_keys[k]!r} becomes {new_keys[k]!r}" for k in new_keys
                if k in old_keys and old_keys[k] != new_keys[k]]
    return (new_steps, new_rec, changes) if changes else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="change the tests (backed up first)")
    ap.add_argument("--user", default="admin", help="who is named in the audit log (default admin)")
    args = ap.parse_args(argv)
    backup = DATA_DIR / "backups" / f"relink-{datetime.now():%Y%m%d-%H%M%S}"
    changed = 0
    for suite in storage.list_suites():
        if suite.get("source") != "recording":
            continue
        for case in suite["cases"]:
            recording = storage.load_recording(suite["id"], case["id"])
            if not case.get("freeform") or not recording:
                continue
            try:
                result = relink(suite, case, recording)
            except (MissingDataError, ValueError) as exc:
                print(f"SKIP  {case['title']} ({suite['id']}): {exc}")
                continue
            if not result:
                continue
            new_steps, new_rec, changes = result
            changed += 1
            print(f"{'FIX ' if args.apply else 'WOULD FIX'}  {case['title']} ({suite['id']} {case['id']})")
            for c in changes:
                print(f"      {c}")
            if not args.apply:
                continue
            shutil.copytree(storage.suite_dir(suite["id"]), backup / suite["id"])
            stored = storage.load_suite(suite["id"])
            target = storage.find_case(stored, case["id"])
            target["steps"] = new_steps
            storage.save_suite(stored)
            build_workbook(storage.workbook_path(suite["id"]), target)
            storage.save_recording(suite["id"], case["id"], {**recording, "steps": new_rec,
                                                             "linked_parameters": linked_count(new_rec),
                                                             "relinked_at": storage.now_iso()})
            audit.record(args.user, "relink_test_data", "", suite_id=suite["id"], case_id=case["id"],
                         title=case["title"], changes=changes)
    if not changed:
        print("Nothing to change.")
    elif args.apply:
        print(f"\n{changed} test(s) changed. Backup: {backup}")
    else:
        print(f"\n{changed} test(s) would change. Run with --apply to change them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
