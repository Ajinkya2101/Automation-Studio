"""Dashboard sign-in: a signed cookie names the user, and repeated wrong passwords lock the
username out for a while.

The cookie holds "username.expiry.session-id.signature". The signature covers part of the user's
password hash, so changing a password (or removing the user) ends that user's existing sessions.
Signing out revokes the session id (kept in data/revoked_sessions.json until it would have
expired), so a copy of the cookie stops working too.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time

from . import users
from .config import DATA_DIR, SECRET_KEY, SESSION_HOURS

COOKIE = "studio_session"
MAX_FAILURES = 5
LOCK_SECONDS = 15 * 60


def _secret() -> bytes:
    if SECRET_KEY:
        return SECRET_KEY.encode()
    path = DATA_DIR / "secret_key"
    if not path.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip().encode()


_SECRET = None


def _sign(payload: str) -> str:
    global _SECRET
    if _SECRET is None:
        _SECRET = _secret()
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


REVOKED_FILE = DATA_DIR / "revoked_sessions.json"
_revoked: dict[str, int] | None = None  # session id -> when its cookie would have expired
_revoked_lock = threading.Lock()


def _revoked_ids() -> dict[str, int]:
    global _revoked
    if _revoked is None:
        try:
            _revoked = json.loads(REVOKED_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _revoked = {}
    return _revoked


def make_token(username: str) -> str:
    user = users.get(username)
    expiry = int(time.time() + SESSION_HOURS * 3600)
    payload = f"{username}.{expiry}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload + '.' + user['hash'][:16])}"


def _parse(token: str | None) -> tuple[str, int, str] | None:
    """(username, expiry, session id) of a genuine, unexpired cookie."""
    try:
        username, expiry, sid, sig = (token or "").rsplit(".", 3)
        if int(expiry) < time.time():
            return None
    except ValueError:
        return None
    user = users.get(username)
    if not user or not hmac.compare_digest(sig, _sign(f"{username}.{expiry}.{sid}.{user['hash'][:16]}")):
        return None
    return username, int(expiry), sid


def user_from_token(token: str | None) -> str | None:
    parsed = _parse(token)
    if not parsed:
        return None
    with _revoked_lock:
        if parsed[2] in _revoked_ids():
            return None
    return parsed[0]


def revoke(token: str | None) -> None:
    """Signing out: this cookie stops working on every device it was copied to."""
    parsed = _parse(token)
    if not parsed:
        return
    with _revoked_lock:
        now = time.time()
        ids = {sid: exp for sid, exp in _revoked_ids().items() if exp > now}
        ids[parsed[2]] = parsed[1]
        _revoked.clear()
        _revoked.update(ids)
        REVOKED_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REVOKED_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(ids), encoding="utf-8")
        tmp.replace(REVOKED_FILE)


class Lockout:
    """Counts wrong passwords per username; after MAX_FAILURES within LOCK_SECONDS it refuses
    further attempts until the oldest failure is LOCK_SECONDS old."""

    def __init__(self):
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> list[float]:
        cutoff = time.time() - LOCK_SECONDS
        recent = [t for t in self._fails.get(key, []) if t > cutoff]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def wait_seconds(self, username: str) -> int:
        with self._lock:
            fails = self._recent(username.lower())
            return int(fails[0] + LOCK_SECONDS - time.time()) + 1 if len(fails) >= MAX_FAILURES else 0

    def failed(self, username: str) -> None:
        with self._lock:
            key = username.lower()
            self._fails[key] = self._recent(key) + [time.time()]

    def clear(self, username: str) -> None:
        with self._lock:
            self._fails.pop(username.lower(), None)


lockout = Lockout()
