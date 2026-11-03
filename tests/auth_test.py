"""Sign-in, audit trail and the deployment safeguards.

Run from the Prototype folder:  .venv\\Scripts\\python tests\\auth_test.py
Uses port 8766 and its own data folder. Checks: everything but the sign-in page needs a user;
wrong passwords and lockout; who did what lands on the suite and in the audit log; requests
from another site, unsafe IDs and oversized uploads are refused; a password change ends
sessions and signing out revokes the cookie; each user sees only their own test cases, runs,
screenshots, sessions, activity and issue logs, while an admin sees everyone's; the live view's
WebSocket needs a user and relays to the VNC port.
"""
import asyncio
import http.cookiejar
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["STUDIO_DATA_DIR"] = tempfile.mkdtemp(prefix="studio-auth-")
os.environ["STUDIO_MAX_UPLOAD_MB"] = "1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


os.environ["STUDIO_VNC_PORT"] = str(VNC_PORT := free_port())

import uvicorn  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from app import audit, events, storage, users  # noqa: E402
from app.excel_io import build_workbook  # noqa: E402
from app.main import app  # noqa: E402

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
failures = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  NoRedirect())

    def call(self, method, path, body=None, headers=None, raw=None, ctype="application/json"):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": ctype} if data is not None else {}
        req = urllib.request.Request(BASE + path, data=data, method=method, headers={**h, **(headers or {})})
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def cookie(self):
        return next((f"{c.name}={c.value}" for c in self.jar), "")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def multipart(filename: str, content: bytes) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def workbook(case_id: str) -> bytes:
    case = {"id": case_id, "sheet": "Case", "title": "Auth test", "description": "", "steps": [
        {"step_id": "S.1", "name": "Open", "role": "User", "description": "1. Open",
         "data": {"URL": f"{BASE}/nowhere"}, "expected": "Opens"}]}
    path = Path(tempfile.mkdtemp()) / "t.xlsx"
    build_workbook(path, case, "Auth test")
    return path.read_bytes()


def fake_vnc_server():
    """Stands in for x11vnc: sends a greeting, then echoes."""
    async def handle(reader, writer):
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", VNC_PORT)
        async with server:
            await server.serve_forever()
    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()


def main():
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    fake_vnc_server()

    print("first start")
    first = Path(os.environ["STUDIO_DATA_DIR"]) / "initial_passwords.txt"
    check(first.exists(), "ten users created with their passwords in initial_passwords.txt")
    lines = [ln.split() for ln in first.read_text().splitlines() if ln.startswith("user")]
    check(len(lines) == 10 and lines[0][0] == "user01", "user01..user10")
    check("hash" in users.get("user01") and lines[0][1] not in Path(users.USERS_FILE).read_text(),
          "users.json holds hashes, not passwords")
    pw1, pw2 = lines[0][1], lines[1][1]
    admin_pw = next((ln.split()[1] for ln in first.read_text().splitlines() if ln.startswith("admin ")), "")
    check(bool(admin_pw) and users.is_admin("admin") and not users.is_admin("user01"), "an admin is created too")

    print("signed out")
    anon = Client()
    st, h, _ = anon.call("GET", "/")
    check(st == 303 and h["location"] == "/login", "dashboard redirects to /login")
    for path in ("/api/cases", "/api/audit", "/api/issue-log", "/api/sample", "/artifacts/runs/x/y.png"):
        check(anon.call("GET", path)[0] == 401, f"{path} needs a user")
    check(anon.call("GET", "/api/health")[0] == 200, "health check is public")
    check(anon.call("GET", "/login")[0] == 200, "sign-in page is public")
    for path in ("/docs", "/openapi.json"):
        check(Client().call("GET", path)[0] in (401, 404), f"{path} is not served")
    st, h, _ = anon.call("GET", "/login")
    check("frame-ancestors 'self'" in h.get("content-security-policy", "") and h["x-content-type-options"] == "nosniff",
          "security headers set")

    print("sign-in")
    check(anon.call("POST", "/api/login", {"username": "user01", "password": "wrong-password"})[0] == 401,
          "wrong password refused")
    check(anon.call("POST", "/api/login", {"username": "nobody", "password": "x" * 10})[0] == 401,
          "unknown user refused")
    a = Client()
    st, h, body = a.call("POST", "/api/login", {"username": "USER01 ", "password": pw1})
    check(st == 200 and json.loads(body)["user"] == "user01", "user01 signs in (username not case sensitive)")
    cookie = h.get("set-cookie", "").lower()
    check("httponly" in cookie and "samesite=lax" in cookie, "cookie is HttpOnly and SameSite=Lax")
    check(json.loads(a.call("GET", "/api/me")[2])["user"] == "user01", "/api/me names the user")
    b = Client()
    check(b.call("POST", "/api/login", {"username": "user02", "password": pw2})[0] == 200, "user02 signs in")

    print("lockout")
    c = Client()
    for _ in range(5):
        c.call("POST", "/api/login", {"username": "user03", "password": "wrong-password"})
    st, _, body = c.call("POST", "/api/login", {"username": "user03", "password": lines[2][1]})
    check(st == 429, "user03 locked after 5 wrong passwords, even with the right one")

    print("who did what")
    body, ctype = multipart("Auth.xlsx", workbook("TS_AUTH1"))
    st, _, res = a.call("POST", "/api/suites", raw=body, ctype=ctype)
    check(st == 200, "user01 uploads a workbook")
    suite_id = json.loads(res)["suite_id"]
    cases = json.loads(a.call("GET", "/api/cases")[2])
    check(cases[0]["created_by"] == "user01", "case list shows created_by user01")
    st, _, res = b.call("POST", "/api/freeform", {"title": "By two", "url": "https://example.com"})
    free = json.loads(res)
    st, _, res = b.call("GET", f"/api/suites/{free['suite_id']}/cases/{free['case_id']}")
    check(json.loads(res)["suite"]["created_by"] == "user02", "free-form test created_by user02")
    # A run of user01's test, with a screenshot, as the replayer leaves it.
    run_id = storage.new_id("run")
    storage.save_run({"id": run_id, "suite_id": suite_id, "case_id": "TS_AUTH1", "title": "Auth test",
                      "started_at": storage.now_iso(), "status": "failed", "run_by": "user01", "steps": []})
    shot = storage.run_dir(run_id) / "screenshots" / "s1.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG fake")

    print("each user sees only their own")
    ids = lambda c: {x["case_id"] for x in json.loads(c.call("GET", "/api/cases")[2])}  # noqa: E731
    check(ids(a) == {"TS_AUTH1"}, "user01's case list: only user01's test")
    check(ids(b) == {free["case_id"]}, "user02's case list: only user02's test")
    case_a = f"/api/suites/{suite_id}/cases/TS_AUTH1"
    for method, path in [("GET", case_a), ("DELETE", case_a), ("POST", case_a + "/record"), ("POST", case_a + "/signin"),
                         ("POST", case_a + "/run"), ("GET", f"/api/suites/{suite_id}/workbook"),
                         ("GET", f"/api/runs/{run_id}"), ("GET", f"/api/runs/{run_id}/results"),
                         ("GET", f"/api/runs/{run_id}/issue-log"),
                         ("GET", f"/artifacts/runs/{run_id}/screenshots/s1.png")]:
        check(b.call(method, path, {} if method == "POST" else None)[0] == 404, f"user02 gets 404 for user01's {method} {path}")
    check(a.call("GET", case_a)[0] == 200, "user01 opens their own test")
    st, h, res = a.call("GET", f"/artifacts/runs/{run_id}/screenshots/s1.png")
    check(st == 200 and res == b"\x89PNG fake" and "no-store" in h.get("cache-control", ""),
          "user01 sees their own screenshot (not cached)")
    for path in (f"/artifacts/runs/{run_id}/run.json", f"/artifacts/runs/{run_id}/..%2F..%2Fusers.json"):
        check(a.call("GET", path)[0] == 404, f"not served: {path}")
    st, _, res = b.call("GET", "/api/issue-log?include_passed=true")
    names = zipfile.ZipFile(io.BytesIO(res)).namelist() if st == 200 else []
    check(st == 200 and not any(run_id in n or "TS_AUTH1" in n or n.endswith("app.log") for n in names),
          "user02's issue-log export: none of user01's runs, no studio log")
    st, _, res = a.call("GET", "/api/issue-log")
    names = zipfile.ZipFile(io.BytesIO(res)).namelist() if st == 200 else []
    check(any(run_id in n for n in names) and not any(n.endswith("app.log") for n in names),
          "user01's issue-log export: their run, no studio log")
    check({e["user"] for e in json.loads(a.call("GET", "/api/audit")[2])} == {"user01"}, "user01's activity: only their own")
    # user01 is running something: user02 sees only that user01 is busy, not with what, and is not blocked.
    busy = events.Session(kind="run", suite_id=suite_id, case_id="TS_AUTH1", user="user01")
    events.sessions._sessions[busy.id] = busy
    theirs = json.loads(b.call("GET", "/api/status")[2])
    mine = json.loads(a.call("GET", "/api/status")[2])
    check(theirs["active"] is None and [o["user"] for o in theirs["others"]] == ["user01"]
          and "case_id" not in theirs["others"][0] and mine["active"]["case_id"] == "TS_AUTH1" and not mine["others"],
          "status: user02 sees user01 is busy, not which test, and has no session of their own")
    check(b.call("POST", f"/api/sessions/{busy.id}/stop")[0] == 404, "user02 cannot stop user01's session")
    check(b.call("GET", f"/api/sessions/{busy.id}/events")[0] == 404, "user02 cannot watch user01's live log")
    busy.status = "completed"

    print("admin")
    check(a.call("GET", "/api/admin/overview")[0] == 403, "a user cannot open the admin overview")
    check(json.loads(a.call("GET", "/api/me")[2])["admin"] is False, "/api/me: user01 is not an admin")
    adm = Client()
    check(adm.call("POST", "/api/login", {"username": "admin", "password": admin_pw})[0] == 200, "admin signs in")
    check(json.loads(adm.call("GET", "/api/me")[2])["admin"] is True, "/api/me: admin is an admin")
    st, _, res = adm.call("GET", "/api/admin/overview")
    overview = json.loads(res) if st == 200 else {}
    mine = [t for t in overview.get("tests", []) if t["case_id"] == free["case_id"]]
    check(st == 200 and mine and mine[0]["created_by"] == "user02" and mine[0]["status"] == "new",
          "admin sees user02's test with its status")
    people = {u["user"]: u for u in overview.get("users", [])}
    check(people.get("user02", {}).get("tests") == 1 and people.get("admin", {}).get("role") == "admin",
          "admin sees what each user created")
    check(ids(adm) == {"TS_AUTH1", free["case_id"]}, "admin's case list: everyone's tests")
    check(adm.call("GET", f"/artifacts/runs/{run_id}/screenshots/s1.png")[0] == 200, "admin sees user01's screenshot")
    st, _, res = adm.call("GET", "/api/issue-log")
    names = zipfile.ZipFile(io.BytesIO(res)).namelist() if st == 200 else []
    check(any(run_id in n for n in names) and any(n.endswith("app.log") for n in names),
          "admin's issue-log export: everyone's runs and the studio log")
    check(adm.call("DELETE", case_a)[0] == 200, "admin deletes user01's test")
    entries = json.loads(adm.call("GET", "/api/audit")[2])
    acts = [(e["user"], e["action"]) for e in entries]
    for want in [("user01", "login"), ("user01", "upload"), ("user02", "create_test"), ("admin", "delete_case"),
                 ("user01", "login_failed"), ("user03", "login_locked")]:
        check(want in acts, f"admin's activity has {want}")
    deleted = next(e for e in entries if e["action"] == "delete_case")
    check(deleted["created_by"] == "user01" and deleted["case_id"] == "TS_AUTH1", "delete entry names case and creator")
    check(all("password" not in json.dumps(e).lower() or e["action"].startswith("login") for e in entries)
          and pw1 not in audit.AUDIT_LOG.read_text(), "no password in the audit log")

    print("refused requests")
    st, _, _ = a.call("POST", "/api/freeform", {"url": "https://example.com"}, headers={"Origin": "https://evil.example"})
    check(st == 403, "change sent from another site refused")
    st, _, _ = a.call("POST", "/api/freeform", {"url": "https://example.com"}, headers={"Origin": BASE})
    check(st == 200, "change from the studio's own page accepted")
    for path in ("/api/runs/..%2F..%2Fusers", "/api/runs/run-1", "/api/suites/..%2Fx/cases/y",
                 "/api/suites/..%2F..%2Fx/workbook"):
        check(a.call("GET", path)[0] in (404, 400), f"unsafe id refused: {path}")
    body, ctype = multipart("Evil.xlsx", workbook("../../escape"))
    st, _, res = a.call("POST", "/api/suites", raw=body, ctype=ctype)
    check(st == 400 and b"cannot be used" in res, "Test Case ID with ../ refused at upload")
    body, ctype = multipart("Big.xlsx", b"x" * (1024 * 1024 + 10))
    check(a.call("POST", "/api/suites", raw=body, ctype=ctype)[0] == 413, "upload over the limit refused")

    print("live view")
    try:
        connect(f"ws://127.0.0.1:{PORT}/vnc/websockify", open_timeout=5).close()
        check(False, "live view refused without a user")
    except InvalidStatus:
        check(True, "live view refused without a user")
    try:
        connect(f"ws://127.0.0.1:{PORT}/vnc/websockify", additional_headers={
            "Cookie": a.cookie(), "Origin": BASE}, open_timeout=5).close()
        check(False, "live view refused to a user with no shown browser of their own")
    except InvalidStatus:
        check(True, "live view refused to a user with no shown browser of their own")
    # user01's recording is shown on the first screen, whose VNC server the fake stands in for.
    shown = events.Session(kind="record", suite_id=suite_id, case_id="TS_AUTH1", user="user01", screen=0)
    events.sessions._sessions[shown.id] = shown
    try:
        connect(f"ws://127.0.0.1:{PORT}/vnc/websockify", additional_headers={
            "Cookie": b.cookie(), "Origin": BASE}, open_timeout=5).close()
        check(False, "user02 cannot watch user01's screen")
    except InvalidStatus:
        check(True, "user02 cannot watch user01's screen")
    with connect(f"ws://127.0.0.1:{PORT}/vnc/websockify", subprotocols=["binary"],
                 additional_headers={"Cookie": a.cookie(), "Origin": BASE}, open_timeout=5) as ws:
        greeting = ws.recv(timeout=5)
        ws.send(b"ping")
        echo = ws.recv(timeout=5)
        shown.status = "completed"
        try:
            while True:
                ws.recv(timeout=5)
        except ConnectionClosed:
            closed = True
        except TimeoutError:
            closed = False
    check(greeting == b"RFB 003.008\n" and echo == b"ping", "live view relays to the screen of the user's own session")
    check(closed, "the live view closes when the session ends, before the screen goes to someone else")
    try:
        connect(f"ws://127.0.0.1:{PORT}/vnc/websockify", additional_headers={
            "Cookie": a.cookie(), "Origin": "https://evil.example"}, open_timeout=5).close()
        check(False, "live view refused from another site")
    except InvalidStatus:
        check(True, "live view refused from another site")

    print("sessions end")
    users.set_password("user02", "a-new-password")
    check(b.call("GET", "/api/cases")[0] == 401, "changing user02's password ends their session")
    copied = a.cookie()
    check(a.call("POST", "/api/logout")[0] == 200 and a.call("GET", "/api/cases")[0] == 401, "sign out ends the session")
    check(Client().call("GET", "/api/cases", headers={"Cookie": copied})[0] == 401,
          "a copy of the signed-out cookie no longer works")
    check(Client().call("POST", "/api/logout", headers={"Cookie": copied})[0] == 200,
          "signing out again with a stale cookie still clears it")

    server.should_exit = True
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
