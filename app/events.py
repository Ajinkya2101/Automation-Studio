"""Live event logs and the single active record/run session.

Recording and replay run Playwright's sync API on a worker thread. They push
events into an EventLog; the API streams that log to the dashboard over
Server-Sent Events.
"""
import asyncio
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


class EventLog:
    def __init__(self):
        self._events: list[dict] = []
        self._lock = threading.Lock()
        self.closed = False

    def emit(self, type: str, message: str = "", level: str = "info", **data) -> dict:
        ev = {"type": type, "message": message, "level": level, "ts": time.time(), **data}
        with self._lock:
            ev["seq"] = len(self._events)
            self._events.append(ev)
        return ev

    def since(self, n: int) -> list[dict]:
        with self._lock:
            return list(self._events[n:])

    def all(self) -> list[dict]:
        return self.since(0)

    def close(self):
        self.closed = True

    async def stream(self, after: int = 0):
        i = after
        idle = 0.0
        while True:
            batch = self.since(i)
            for ev in batch:
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev)}\n\n"
            i += len(batch)
            if self.closed and not self.since(i):
                yield "event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(0.2)
            idle = 0.0 if batch else idle + 0.2
            if idle >= 15:
                yield ": keep-alive\n\n"
                idle = 0.0


@dataclass
class Session:
    kind: str  # "record" or "run"
    suite_id: str
    case_id: str
    run_id: str | None = None
    id: str = field(default_factory=lambda: secrets.token_hex(6))
    status: str = "running"  # running, completed, failed, stopped
    log: EventLog = field(default_factory=EventLog)
    stop_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)

    def info(self) -> dict:
        return {"id": self.id, "kind": self.kind, "suite_id": self.suite_id,
                "case_id": self.case_id, "run_id": self.run_id, "status": self.status,
                "started_at": self.started_at}


class BusyError(RuntimeError):
    pass


class SessionManager:
    """Allows one browser session (recording or run) at a time."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._active: Session | None = None
        self._lock = threading.Lock()

    def active(self) -> Session | None:
        s = self._active
        return s if s and s.status == "running" else None

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def start(self, kind: str, target: Callable, *, suite_id: str, case_id: str,
              run_id: str | None = None, **kwargs) -> Session:
        with self._lock:
            if self.active():
                a = self._active
                raise BusyError(f"A {a.kind} session is already in progress for {a.case_id}.")
            s = Session(kind=kind, suite_id=suite_id, case_id=case_id, run_id=run_id)
            self._sessions[s.id] = s
            self._active = s

        def runner():
            try:
                result = target(s, **kwargs)
                if s.status == "running":
                    s.status = result or "completed"
            except Exception as exc:  # surface anything unexpected to the dashboard
                s.status = "failed"
                s.log.emit("error", f"Session failed: {exc}", level="error")
            finally:
                s.log.emit("session_end", f"Session {s.status}", status=s.status)
                s.log.close()

        threading.Thread(target=runner, name=f"{kind}-{s.id}", daemon=True).start()
        return s


sessions = SessionManager()
