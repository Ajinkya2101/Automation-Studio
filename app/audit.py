"""Who did what: one JSON line per change or export in data/logs/audit.jsonl.

The file is only ever appended to. The dashboard's Activity view reads it back.
"""
import json
import threading
from collections import deque

from .config import LOGS_DIR
from .diagnostics import log as app_log
from .storage import now_iso

AUDIT_LOG = LOGS_DIR / "audit.jsonl"
_lock = threading.Lock()


def record(user: str, action: str, ip: str = "", **details) -> None:
    entry = {"ts": now_iso(), "user": user, "action": action, "ip": ip, **details}
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    app_log.info("audit %s", line)


def recent(limit: int = 500) -> list[dict]:
    """The newest entries first."""
    if not AUDIT_LOG.exists():
        return []
    with _lock, AUDIT_LOG.open(encoding="utf-8") as f:
        lines = deque(f, maxlen=limit)
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
