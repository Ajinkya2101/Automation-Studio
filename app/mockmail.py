"""Acme Mail: a small stand-in webmail used as the application under test.

It plays the role Oracle Fusion will play later: a separate web application the
recorder watches and the runner drives. Mailboxes live in memory; nothing is
really sent.
"""
import asyncio
import html
import itertools
import secrets
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/mail", include_in_schema=False)

USERS = {"demo.user": {"password": "demo123", "email": "demo.user@acme.test", "name": "Demo User"}}
COOKIE = "acme_mail_session"
_sessions: dict[str, str] = {}
_ids = itertools.count(1)
_sent: list[dict] = []
_inbox: list[dict] = []


def _seed_inbox():
    _inbox.clear()
    for sender, subject, body in [
        ("it.support@acme.test", "Scheduled maintenance this weekend",
         "Systems will be unavailable on Saturday from 22:00 to 02:00."),
        ("finance.team@acme.test", "Month-end close checklist",
         "Please review the attached checklist before Friday."),
        ("hr@acme.test", "Welcome to Acme Mail", "Your mailbox is ready to use."),
    ]:
        _inbox.append({"id": next(_ids), "from": sender, "to": USERS["demo.user"]["email"],
                       "cc": "", "subject": subject, "body": body,
                       "at": datetime.now().strftime("%d %b %H:%M")})


_seed_inbox()


def reset_mailbox() -> None:
    """Clear sent items so every automated run starts from the same state."""
    _sent.clear()
    _seed_inbox()


def _user(request: Request):
    return USERS.get(_sessions.get(request.cookies.get(COOKIE, ""), ""))


def _e(s) -> str:
    return html.escape(str(s or ""))


CSS = """
*{box-sizing:border-box}body{margin:0;font:14px/1.45 "Segoe UI",system-ui,sans-serif;color:#1f2937;background:#eef1f5}
a{color:inherit}
.top{display:flex;align-items:center;justify-content:space-between;background:#0f4c81;color:#fff;padding:0 20px;height:52px}
.brand{font-weight:700;font-size:17px;letter-spacing:.2px}.brand span{opacity:.75;font-weight:400}
.who{display:flex;gap:14px;align-items:center;font-size:13px}.who button{background:transparent;border:1px solid #ffffff80;color:#fff;border-radius:4px;padding:4px 10px;cursor:pointer}
.shell{display:grid;grid-template-columns:210px 1fr;min-height:calc(100vh - 52px)}
nav.side{background:#fff;border-right:1px solid #dde3ea;padding:16px 12px}
.compose{display:block;text-align:center;background:#0f4c81;color:#fff;text-decoration:none;border-radius:6px;padding:10px;font-weight:600;margin-bottom:16px}
.folder{display:flex;justify-content:space-between;align-items:center;padding:8px 10px;border-radius:6px;margin-bottom:2px}
.folder a{text-decoration:none;flex:1}.folder.on{background:#e3eefa;font-weight:600}.badge{font-size:12px;color:#5b6b7c}
main{padding:22px 26px;max-width:980px}
h1{font-size:20px;margin:0 0 14px}
.flash{background:#e7f6ec;border:1px solid #9fd5b0;color:#1d6b39;padding:10px 14px;border-radius:6px;margin-bottom:16px;font-weight:600}
.err{background:#fdecec;border:1px solid #f2b3b3;color:#9b1c1c;padding:10px 14px;border-radius:6px;margin-bottom:14px}
table.list{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dde3ea;border-radius:8px;overflow:hidden}
.list td{padding:11px 14px;border-bottom:1px solid #eef1f5}.list tr:hover td{background:#f6f9fc}
.list a{text-decoration:none;display:block}.who-col{width:230px;color:#5b6b7c}.at{width:110px;text-align:right;color:#5b6b7c;font-size:12px}
.empty{background:#fff;border:1px dashed #cbd5e1;border-radius:8px;padding:30px;text-align:center;color:#64748b}
.card{background:#fff;border:1px solid #dde3ea;border-radius:8px;padding:20px 22px}
.field{display:grid;grid-template-columns:90px 1fr;align-items:center;gap:10px;margin-bottom:12px}
.field label{color:#475569;font-weight:600}
input,textarea{font:inherit;padding:9px 11px;border:1px solid #c5cfda;border-radius:6px;width:100%}
textarea{min-height:200px;resize:vertical}.field.tall{align-items:start}
.actions{display:flex;gap:10px;margin-top:16px}
.btn{background:#0f4c81;color:#fff;border:0;border-radius:6px;padding:9px 20px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none}
.btn.ghost{background:#fff;color:#334155;border:1px solid #c5cfda}
.meta{color:#5b6b7c;margin-bottom:14px}.meta div{margin:2px 0}.body{white-space:pre-wrap;border-top:1px solid #eef1f5;padding-top:14px}
.login{max-width:360px;margin:9vh auto}.login .card{padding:28px}.login h1{text-align:center}
.login .field{grid-template-columns:1fr;gap:4px}.login .btn{width:100%;padding:11px}
.hint{text-align:center;color:#64748b;font-size:12px;margin-top:14px}
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
                        f"<title>{_e(title)} - Acme Mail</title><style>{CSS}</style></head>"
                        f"<body>{body}</body></html>")


def _shell(user: dict, active: str, content: str) -> str:
    def folder(name, href, count=None):
        on = " on" if name == active else ""
        badge = f"<span class='badge'>{count}</span>" if count else ""
        return f"<div class='folder{on}'><a href='{href}'>{name}</a>{badge}</div>"

    return f"""
<header class='top'><div class='brand'>Acme Mail <span>| Web</span></div>
  <div class='who'><span>{_e(user['name'])} &lt;{_e(user['email'])}&gt;</span>
  <form method='post' action='/mail/logout' style='margin:0'><button type='submit'>Sign out</button></form></div></header>
<div class='shell'><nav class='side'>
  <a class='compose' href='/mail/compose'>Compose</a>
  {folder('Inbox', '/mail/inbox', len(_inbox))}{folder('Sent', '/mail/sent', len(_sent))}
</nav><main>{content}</main></div>"""


def _flash(request: Request) -> str:
    msg = request.query_params.get("flash")
    return f"<div class='flash' role='status'>{_e(msg)}</div>" if msg else ""


def _list(items: list[dict], who_key: str) -> str:
    if not items:
        return "<div class='empty'>No messages.</div>"
    rows = "".join(
        f"<tr><td class='who-col'>{_e(m[who_key])}</td>"
        f"<td><a href='/mail/message/{m['id']}'><span class='subject'>{_e(m['subject'])}</span></a></td>"
        f"<td class='at'>{_e(m['at'])}</td></tr>" for m in reversed(items))
    return f"<table class='list'>{rows}</table>"


@router.get("")
@router.get("/")
def home(request: Request):
    return RedirectResponse("/mail/inbox" if _user(request) else "/mail/login", 303)


@router.get("/login")
def login_page(request: Request):
    error = "<div class='err'>Incorrect user name or password.</div>" if request.query_params.get("error") else ""
    return _page("Sign in", f"""
<div class='login'><div class='card'><h1>Sign in to Acme Mail</h1>{error}
<form method='post' action='/mail/login'>
  <div class='field'><label for='username'>User Name</label><input id='username' name='username' autocomplete='off'></div>
  <div class='field'><label for='password'>Password</label><input id='password' name='password' type='password'></div>
  <button class='btn' type='submit'>Sign in</button>
</form></div><p class='hint'>Demo account: demo.user / demo123</p></div>""")


@router.post("/login")
def login(username: str = Form(""), password: str = Form("")):
    user = USERS.get(username.strip())
    if not user or user["password"] != password:
        return RedirectResponse("/mail/login?error=1", 303)
    token = secrets.token_hex(16)
    _sessions[token] = username.strip()
    resp = RedirectResponse("/mail/inbox", 303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
def logout(request: Request):
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    resp = RedirectResponse("/mail/login", 303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/inbox")
def inbox(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/mail/login", 303)
    return _page("Inbox", _shell(user, "Inbox", f"{_flash(request)}<h1>Inbox</h1>{_list(_inbox, 'from')}"))


@router.get("/sent")
def sent(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/mail/login", 303)
    return _page("Sent", _shell(user, "Sent", f"{_flash(request)}<h1>Sent</h1>{_list(_sent, 'to')}"))


def _compose_form(user, to="", cc="", subject="", body="", error=""):
    err = f"<div class='err' role='alert'>{_e(error)}</div>" if error else ""
    return _page("New message", _shell(user, "", f"""
<h1>New message</h1>{err}<div class='card'><form method='post' action='/mail/send'>
  <div class='field'><label for='to'>To</label><input id='to' name='to' value='{_e(to)}' autocomplete='off'></div>
  <div class='field'><label for='cc'>Cc</label><input id='cc' name='cc' value='{_e(cc)}' autocomplete='off'></div>
  <div class='field'><label for='subject'>Subject</label><input id='subject' name='subject' value='{_e(subject)}' autocomplete='off'></div>
  <div class='field tall'><label for='body'>Message</label><textarea id='body' name='body'>{_e(body)}</textarea></div>
  <div class='actions'><button class='btn' type='submit'>Send</button><a class='btn ghost' href='/mail/inbox'>Discard</a></div>
</form></div>"""))


@router.get("/compose")
def compose(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/mail/login", 303)
    return _compose_form(user)


@router.post("/send")
async def send(request: Request, to: str = Form(""), cc: str = Form(""),
               subject: str = Form(""), body: str = Form("")):
    user = _user(request)
    if not user:
        return RedirectResponse("/mail/login", 303)
    if not to.strip() or "@" not in to:
        return _compose_form(user, to, cc, subject, body, "Please enter a valid recipient in To.")
    if not subject.strip():
        return _compose_form(user, to, cc, subject, body, "Please enter a subject.")
    await asyncio.sleep(0.8)  # a little latency, like a real mail server
    msg = {"id": next(_ids), "from": user["email"], "to": to.strip(), "cc": cc.strip(),
           "subject": subject.strip(), "body": body, "at": datetime.now().strftime("%d %b %H:%M")}
    _sent.append(msg)
    return RedirectResponse(f"/mail/message/{msg['id']}?flash=Message+sent", 303)


@router.get("/message/{mid}")
def message(request: Request, mid: int):
    user = _user(request)
    if not user:
        return RedirectResponse("/mail/login", 303)
    m = next((x for x in _inbox + _sent if x["id"] == mid), None)
    if not m:
        return RedirectResponse("/mail/inbox", 303)
    folder = "Sent" if m in _sent else "Inbox"
    cc = f"<div><b>Cc:</b> {_e(m['cc'])}</div>" if m["cc"] else ""
    return _page(m["subject"], _shell(user, folder, f"""{_flash(request)}
<div class='card'><h1>{_e(m['subject'])}</h1><div class='meta'>
<div><b>From:</b> {_e(m['from'])}</div><div><b>To:</b> {_e(m['to'])}</div>{cc}<div><b>Date:</b> {_e(m['at'])}</div>
</div><div class='body'>{_e(m['body'])}</div></div>"""))
