"""Shared helpers for recorded actions: parameters and readable descriptions."""
import copy
import re
from datetime import datetime

PARAM = re.compile(r"\$\{([^}]+)\}")
TOKEN = re.compile(r"\{(now|date|time|stamp)\}")
MASK = "••••••"
SHORT_VALUE = 4  # values shorter than this ("2", "Yes") match test data by chance
DOUBLE_CLICK_S = 0.5  # two clicks on one element this close together are one double-click


class MissingDataError(KeyError):
    pass


def resolve_tokens(value: str, when: datetime) -> str:
    """{now}, {date}, {time}, {stamp} in Excel test data become fresh values
    each session, e.g. a unique email subject per run."""
    fmt = {"now": "%Y-%m-%d %H:%M:%S", "date": "%Y-%m-%d", "time": "%H:%M:%S", "stamp": "%Y%m%d-%H%M%S"}
    return TOKEN.sub(lambda m: when.strftime(fmt[m.group(1)]), value or "")


def resolved_steps(case_steps: list[dict], when: datetime | None = None) -> list[dict]:
    when = when or datetime.now()
    steps = copy.deepcopy(case_steps)
    for s in steps:
        s["data"] = {k: resolve_tokens(v, when) for k, v in s["data"].items()}
    return steps


NO_START_URL = ("This test has no start URL. Add a line such as 'URL: https://...' to the "
                "test data of the first step in the Excel and upload it again.")


def start_url(case_steps: list[dict]) -> str:
    """The first step's 'URL' test data says which application to open."""
    for s in case_steps:
        for k, v in s["data"].items():
            if k.strip().lower() == "url" and v.strip():
                return v.strip()
    return ""


def case_target(case: dict) -> str:
    """Start URL of a test: given directly (recorded without Excel) or by the Excel URL data.
    Empty when the Excel gives none."""
    return case.get("start_url") or start_url(case["steps"])


def substitute(value: str, data: dict[str, str]) -> str:
    def repl(m):
        key = m.group(1)
        if key not in data:
            raise MissingDataError(f"Test data '{key}' is missing")
        return data[key]
    return PARAM.sub(repl, value or "")


def _plain(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def _names_field(key: str, a: dict) -> bool:
    """The test data key is named after the action's field: 'Quantity' or 'Quantity 2' for field "Quantity"."""
    field = next((c["value"] for c in a.get("locators", []) if c["kind"] in ("label", "placeholder")), "")
    if not field:
        m = re.search(r'"(.+)"', a.get("target", ""))
        field = m.group(1) if m else ""
    return bool(field) and _plain(re.sub(r"\s+\d+$", "", key)) == _plain(field)


def parameterize(rec_steps: list[dict], case_steps: list[dict]) -> int:
    """Replace recorded values that match the Excel test data with ${Key}.

    This is what makes a recording reusable: change the data in Excel and the
    same automation types the new values. Returns how many values were linked.

    A short value is only linked to data named after its own field: "2" chosen in
    Business Unit is not the "2" typed into Quantity, and linking the two makes a
    later change to Quantity pick another business unit.
    """
    by_step = {s["step_id"]: s["data"] for s in case_steps}
    everything = {k: v for s in case_steps for k, v in s["data"].items()}
    linked = 0

    def match(a, value, data):
        value = (value or "").strip()
        return next((k for k, v in data.items() if v and v.strip() == value
                     and (len(value) >= SHORT_VALUE or _names_field(k, a))), None)

    # Longest values first so "Automation test 14:32" wins over a shorter overlap.
    by_length = sorted(((k, v.strip()) for k, v in everything.items() if len(v.strip()) >= SHORT_VALUE),
                       key=lambda kv: len(kv[1]), reverse=True)

    for rs in rec_steps:
        local = by_step.get(rs["step_id"], {})
        for a in rs["actions"]:
            field = {"fill": "value", "select": "value", "expect_text": "text"}.get(a["type"])
            if not field or not a.get(field) or PARAM.search(a[field]):
                continue
            key = match(a, a[field], local) or match(a, a[field], everything)
            if key:
                a["param"] = key
                a[field] = "${" + key + "}"
                linked += 1
            elif a["type"] == "expect_text":
                # A check such as 'Sent "Q3 summary" to x@y' keeps working when the
                # data changes: each test value inside the text becomes ${Key}.
                text, used = a[field], []
                for k, v in by_length:
                    if v in text:
                        text = text.replace(v, "${" + k + "}")
                        used.append(k)
                if used:
                    a[field], a["param"] = text, ", ".join(used)
                    linked += 1
    return linked


def repeated_click(first: dict | None, second: dict) -> bool:
    """`second` is the other half of a double-click that reached the page as two clicks.

    The browser reports a double-click only when both clicks land on the same element.
    When the first click redraws the element or starts opening a page (an Oracle
    springboard tile), only the two clicks arrive. Replayed, the second click finds
    nothing left to click, so it is not kept.
    """
    if not first or first["type"] != "click" or second["type"] != "click":
        return False
    if any(first.get(k) != second.get(k) for k in ("locators", "tab", "frame")):
        return False
    t1, t2 = first.get("recorded_at"), second.get("recorded_at")
    return t1 is not None and t2 is not None and 0 <= t2 - t1 < DOUBLE_CLICK_S


def _shown(value: str, a: dict, data: dict | None) -> str:
    if a.get("masked"):
        return MASK
    if data is not None:
        try:
            value = substitute(value, data)
        except MissingDataError:
            pass
    value = (value or "").replace("\n", " ")
    return value if len(value) <= 70 else value[:67] + "..."


def describe(a: dict, data: dict | None = None) -> str:
    """Human-readable line for logs and the dashboard."""
    t, target = a["type"], a.get("target", "")
    if t == "goto":
        return f"Open {a['url']}"
    if t == "click":
        return f"Click {target}"
    if t == "dblclick":
        return f"Double-click {target}"
    if t == "upload":
        return f"Attach {', '.join(a.get('files', []))} to {target}"
    if t == "fill":
        return f"Type \"{_shown(a.get('value'), a, data)}\" into {target}"
    if t == "press":
        return f"Press {a.get('key')}" + ("" if a.get("global") else f" in {target}")
    if t == "select":
        return f"Select \"{_shown(a.get('value'), a, data)}\" in {target}"
    if t == "expect_text":
        return f"Check that \"{_shown(a.get('text'), a, data)}\" is visible"
    if t == "scroll":
        where = "the page" if a.get("page") else target
        return f"Scroll {where} to {a.get('y', 0)} px" + (f" (across {a['x']} px)" if a.get("x") else "")
    return t


def locator_label(loc: dict | None) -> str:
    if not loc:
        return ""
    if loc["kind"] == "role":
        return f"role={loc['role']} name=\"{loc['value']}\""
    return f"{loc['kind']}=\"{loc['value']}\""
