"""Live event logs and the browser sessions (sign-in, recording, run) in progress.

Recording and replay run Playwright's sync API on a worker thread each, so
several users' sessions run side by side. They push events into an EventLog;
the API streams that log to the dashboard over Server-Sent Events.
"""
import asyncio
import json
import secrets
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable

from .config import FIRST_DISPLAY, MAX_SESSIONS, SCREENS, VNC_PORT
from .diagnostics import log as app_log
from .diagnostics import save_session_log


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
    user: str = ""  # who started it
    id: str = field(default_factory=lambda: secrets.token_hex(6))
    status: str = "running"  # running, completed, failed, stopped
    log: EventLog = field(default_factory=EventLog)
    stop_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)
    error: str | None = None  # traceback when the session crashed
    stopped_by: str | None = None
    screen: int | None = None  # the container's virtual screen its browser is shown on, if any

    @property
    def display(self) -> str | None:
        """The X display its browser draws on; None outside the container or for a hidden browser."""
        return f":{FIRST_DISPLAY + self.screen}" if self.screen is not None else None

    @property
    def vnc_port(self) -> int | None:
        return VNC_PORT + self.screen if self.screen is not None else None

    def info(self) -> dict:
        return {"id": self.id, "kind": self.kind, "suite_id": self.suite_id,
                "case_id": self.case_id, "run_id": self.run_id, "status": self.status,
                "started_at": self.started_at, "user": self.user, "stopped_by": self.stopped_by,
                "live_view": self.screen is not None}


class BusyError(RuntimeError):
    pass


_NAMES = {"signin": "sign-in", "record": "recording", "run": "run"}


class SessionManager:
    """Runs browser sessions side by side, so one user's run never waits for another's.

    - One session per user at a time: a user's saved sign-ins live in one browser
      profile, which Chromium cannot open twice.
    - A test case being recorded is not run or recorded by anyone else meanwhile,
      and is not recorded while it runs: the recording is what a run replays.
    - In the container every shown browser gets a virtual screen of its own, so
      the live view shows each user only their own browser, and a recording's
      clicks never land in someone else's run. A hidden browser needs none.
    - At most MAX_SESSIONS browsers at once, so the server is not overloaded.
    """

    def __init__(self, screens: int = SCREENS, limit: int = MAX_SESSIONS):
        self._sessions: dict[str, Session] = {}
        self._screens = range(screens)
        self._limit = limit
        self._lock = threading.Lock()

    def running(self) -> list[Session]:
        return [s for s in list(self._sessions.values()) if s.status == "running"]

    def active(self, user: str) -> Session | None:
        """The user's own session in progress, if any."""
        return next((s for s in self.running() if s.user == user), None)

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def _conflict(self, kind: str, suite_id: str, case_id: str, user: str, shown: bool) -> str | None:
        running = self.running()
        mine = next((s for s in running if s.user == user), None)
        if mine:
            return (f"You already have a {_NAMES[mine.kind]} in progress for {mine.case_id}. "
                    "Wait for it to finish or stop it first.")
        same = next((s for s in running if (s.suite_id, s.case_id) == (suite_id, case_id)
                     and "record" in (kind, s.kind)), None)
        if same:
            return (f"{case_id} has a {_NAMES[same.kind]} in progress, started by {same.user or 'someone else'}. "
                    "A test case cannot be run or recorded while it is being recorded, or recorded while it runs.")
        if len(running) >= self._limit:
            return (f"The studio is already running {len(running)} browsers, its limit "
                    f"(started by {', '.join(sorted({s.user or '-' for s in running}))}). Try again when one finishes.")
        if shown and self._screens and len({s.screen for s in running} - {None}) >= len(self._screens):
            return (f"All {len(self._screens)} live screens are in use. Try again when one finishes, "
                    "or untick 'Show browser' to run in the background.")
        return None

    def start(self, kind: str, target: Callable, *, suite_id: str, case_id: str,
              run_id: str | None = None, user: str = "", **kwargs) -> Session:
        # Sign-in and recording always show the browser; a run does when it is "headed".
        shown = kwargs.get("headed", not kwargs.get("headless", False))
        with self._lock:
            problem = self._conflict(kind, suite_id, case_id, user, shown)
            if problem:
                raise BusyError(problem)
            screen = None
            if shown and self._screens:
                used = {s.screen for s in self.running()}
                screen = next(i for i in self._screens if i not in used)
            s = Session(kind=kind, suite_id=suite_id, case_id=case_id, run_id=run_id, user=user, screen=screen)
            self._sessions[s.id] = s

        def runner():
            app_log.info("%s session %s started for %s/%s%s by %s%s", kind, s.id, suite_id, case_id,
                         f" (run {run_id})" if run_id else "", user or "-",
                         f" on screen {s.display}" if s.display else "")
            try:
                result = target(s, **kwargs)
                if s.status == "running":
                    s.status = result or "completed"
            except Exception as exc:  # surface anything unexpected to the dashboard
                s.status = "failed"
                s.error = traceback.format_exc()
                app_log.error("%s session %s crashed:\n%s", kind, s.id, s.error)
                s.log.emit("error", f"Session failed: {exc}", level="error")
            finally:
                s.log.emit("session_end", f"Session {s.status}", status=s.status)
                save_session_log(s)
                app_log.info("%s session %s ended: %s", kind, s.id, s.status)
                s.log.close()

        threading.Thread(target=runner, name=f"{kind}-{s.id}", daemon=True).start()
        return s


sessions = SessionManager()
