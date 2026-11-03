"""Dashboard users: a username and a password each, kept in data/users.json.

Only a salted PBKDF2 hash of each password is stored. Manage users from a shell
(in the container: `docker compose exec studio python -m app.users ...`):

  python -m app.users list
  python -m app.users add <username> [password]      password generated when left out
  python -m app.users add-admin <username> [password] an admin also has the Admin screen
  python -m app.users passwd <username> [password]
  python -m app.users remove <username>
  python -m app.users init [count]                   first setup: user01..user10
"""
import hashlib
import hmac
import json
import re
import secrets
import sys
import threading
from datetime import datetime

from .config import DATA_DIR, USERS_FILE

ITERATIONS = 200_000
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
INITIAL_PASSWORDS = DATA_DIR / "initial_passwords.txt"
_lock = threading.Lock()


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


WORDS = ("sun", "sky", "tea", "owl", "fox", "oak", "bee", "ice", "jet", "kit", "map", "pen", "cup", "box",
         "red", "mint", "lime", "rose", "star", "moon", "leaf", "wave", "fern", "gold", "blue", "rain", "snow",
         "bird", "lake", "pine")


def new_password() -> str:
    """Short and quick to type many times a day: a word and two digits, such as mint27.
    The sign-in lockout (5 wrong passwords lock the username for 15 minutes) stops guessing."""
    return f"{secrets.choice(WORDS)}{secrets.randbelow(90) + 10}"


def load() -> dict[str, dict]:
    if not USERS_FILE.exists():
        return {}
    return json.loads(USERS_FILE.read_text(encoding="utf-8")).get("users", {})


def _save(users: dict[str, dict]) -> None:
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"users": users}, indent=2), encoding="utf-8")
    tmp.replace(USERS_FILE)


def get(username: str) -> dict | None:
    return load().get((username or "").strip().lower())


def verify(username: str, password: str) -> str | None:
    """The canonical username when the password is right, else None."""
    name = (username or "").strip().lower()
    user = load().get(name)
    if not user:
        _hash(password or "", "00" * 16)  # same cost as a real check, so timing does not reveal names
        return None
    return name if hmac.compare_digest(_hash(password or "", user["salt"]), user["hash"]) else None


def is_admin(username: str) -> bool:
    return (get(username) or {}).get("role") == "admin"


def set_password(username: str, password: str | None = None, create: bool = False, role: str | None = None) -> str:
    """Sets (or with create, adds a user with) a password; returns the password."""
    name = (username or "").strip().lower()
    if not USERNAME.match(name):
        raise ValueError("Usernames are 2-32 characters: lowercase letters, digits, '.', '_' or '-'.")
    password = password or new_password()
    if len(password) < 4:
        raise ValueError("Passwords need at least 4 characters.")
    with _lock:
        users = load()
        if create and name in users:
            raise ValueError(f"User {name} already exists.")
        if not create and name not in users:
            raise ValueError(f"No user {name}.")
        salt = secrets.token_hex(16)
        users[name] = {**users.get(name, {"created_at": datetime.now().astimezone().isoformat(timespec="seconds")}),
                       "salt": salt, "hash": _hash(password, salt)}
        if role:
            users[name]["role"] = role
        _save(users)
    return password


def remove(username: str) -> None:
    with _lock:
        users = load()
        if users.pop((username or "").strip().lower(), None) is None:
            raise ValueError(f"No user {username}.")
        _save(users)


def init(count: int = 10) -> dict[str, str]:
    """Creates user01..userNN with generated passwords (only when there are no users yet)."""
    if load():
        raise ValueError(f"{USERS_FILE} already has users. Use add / passwd instead.")
    passwords: set[str] = set()
    while len(passwords) < count:  # all different, so nobody's password also opens another account
        passwords.add(new_password())
    return {f"user{i:02d}": set_password(f"user{i:02d}", pw, create=True)
            for i, pw in enumerate(sorted(passwords, key=lambda _: secrets.randbelow(1000)), start=1)}


def ensure_users() -> str | None:
    """First start: create the ten users and an admin, and leave their passwords in
    data/initial_passwords.txt. Returns that file's path when it was just written."""
    if load():
        return None
    created = init()
    created["admin"] = set_password("admin", create=True, role="admin")
    INITIAL_PASSWORDS.write_text(
        "Automation Studio: initial sign-ins. Hand each person one line, then delete this file.\n"
        "Change a password with: python -m app.users passwd <username>\n\n"
        + "".join(f"{u}  {p}\n" for u, p in created.items()), encoding="utf-8")
    return str(INITIAL_PASSWORDS)


def _main(argv: list[str]) -> int:
    cmd, args = (argv[0] if argv else "list"), argv[1:]
    try:
        if cmd == "list":
            for name, u in sorted(load().items()):
                print(f"{name:20} {u.get('role', 'user'):6} created {u.get('created_at', '')}")
        elif cmd == "init":
            for name, pw in init(int(args[0]) if args else 10).items():
                print(f"{name}  {pw}")
        elif cmd in ("add", "add-admin", "passwd") and args:
            pw = set_password(args[0], args[1] if len(args) > 1 else None, create=cmd != "passwd",
                              role="admin" if cmd == "add-admin" else None)
            print(f"{args[0].lower()}  {pw}")
        elif cmd == "remove" and args:
            remove(args[0])
            print(f"Removed {args[0]}.")
        else:
            print(__doc__)
            return 2
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
