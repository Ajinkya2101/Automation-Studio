"""Shared helpers for recorded actions: parameters and readable descriptions."""
import copy
import re
from datetime import datetime

PARAM = re.compile(r"\$\{([^}]+)\}")
TOKEN = re.compile(r"\{(now|date|time|stamp)\}")
MASK = "••••••"


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


def start_url(case_steps: list[dict], default: str) -> str:
    """The first step's 'URL' test data says which application to open."""
    for s in case_steps:
        for k, v in s["data"].items():
            if k.strip().lower() == "url" and v.strip():
                return v.strip()
    return default


def substitute(value: str, data: dict[str, str]) -> str:
    def repl(m):
        key = m.group(1)
        if key not in data:
            raise MissingDataError(f"Test data '{key}' is missing")
        return data[key]
    return PARAM.sub(repl, value or "")


def parameterize(rec_steps: list[dict], case_steps: list[dict]) -> int:
    """Replace recorded values that match the Excel test data with ${Key}.

    This is what makes a recording reusable: change the data in Excel and the
    same automation types the new values. Returns how many values were linked.
    """
    by_step = {s["step_id"]: s["data"] for s in case_steps}
    everything = {k: v for s in case_steps for k, v in s["data"].items()}
    linked = 0

    def match(value, data):
        return next((k for k, v in data.items() if v and v.strip() == (value or "").strip()), None)

    # Longest values first so "Automation test 14:32" wins over a shorter overlap.
    by_length = sorted(((k, v.strip()) for k, v in everything.items() if len(v.strip()) >= 4),
                       key=lambda kv: len(kv[1]), reverse=True)

    for rs in rec_steps:
        local = by_step.get(rs["step_id"], {})
        for a in rs["actions"]:
            field = {"fill": "value", "select": "value", "expect_text": "text"}.get(a["type"])
            if not field or not a.get(field) or PARAM.search(a[field]):
                continue
            key = match(a[field], local) or match(a[field], everything)
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
    if t == "fill":
        return f"Type \"{_shown(a.get('value'), a, data)}\" into {target}"
    if t == "press":
        return f"Press {a.get('key')} in {target}"
    if t == "select":
        return f"Select \"{_shown(a.get('value'), a, data)}\" in {target}"
    if t == "expect_text":
        return f"Check that \"{_shown(a.get('text'), a, data)}\" is visible"
    return t


def locator_label(loc: dict | None) -> str:
    if not loc:
        return ""
    if loc["kind"] == "role":
        return f"role={loc['role']} name=\"{loc['value']}\""
    return f"{loc['kind']}=\"{loc['value']}\""
