"""A small multi-page site for the end-to-end tests to record and replay against.

Only the tests mount it (app.include_router(demo_site.router)); the studio itself
ships no application under test. Records live in memory.
"""
import asyncio
import html
import itertools
import re
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(prefix="/demo", include_in_schema=False)

USERS = {"demo.user": "demo123"}
COOKIE = "demo_site_session"
_sessions: set[str] = set()
_ids = itertools.count(1)
_suppliers: list[dict] = []


def reset() -> None:
    """Clear saved suppliers so every automated run starts from the same state."""
    _suppliers.clear()


def _e(s) -> str:
    return html.escape(str(s or ""))


def _page(title: str, body: str, nav: bool = True) -> HTMLResponse:
    links = ("<nav><a href='/demo/suppliers'>Suppliers</a> | <a href='/demo/suppliers/new'>Add supplier</a></nav>"
             if nav else "")
    return HTMLResponse(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{_e(title)}</title></head>"
                        f"<body style='font-family:sans-serif;padding:20px'>{links}{body}</body></html>")


def _signed_in(request: Request) -> bool:
    return request.cookies.get(COOKIE, "") in _sessions


LOGIN = RedirectResponse("/demo/login", 303)


@router.get("/login")
def login_page(request: Request):
    error = "<p role='alert'>Incorrect user name or password.</p>" if request.query_params.get("error") else ""
    return _page("Sign in", f"""<h1>Sign in</h1>{error}
<form method='post' action='/demo/login'>
  <p><label for='username'>User Name</label> <input id='username' name='username' autocomplete='off'></p>
  <p><label for='password'>Password</label> <input id='password' name='password' type='password'></p>
  <button type='submit'>Sign in</button>
</form>""", nav=False)


@router.post("/login")
def login(username: str = Form(""), password: str = Form("")):
    if USERS.get(username.strip()) != password:
        return RedirectResponse("/demo/login?error=1", 303)
    token = secrets.token_hex(16)
    _sessions.add(token)
    resp = RedirectResponse("/demo/suppliers", 303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax")
    return resp


@router.get("/suppliers")
def suppliers(request: Request):
    if not _signed_in(request):
        return LOGIN
    rows = "".join(f"<tr><td><a href='/demo/suppliers/{s['id']}'><span class='name'>{_e(s['name'])}</span></a></td>"
                   f"<td>{_e(s['phone'])}</td></tr>" for s in reversed(_suppliers))
    return _page("Suppliers", f"<h1>Suppliers</h1>{f'<table>{rows}</table>' if rows else '<p>No suppliers.</p>'}")


def _form(name="", phone="", notes="", error=""):
    err = f"<p role='alert'>{_e(error)}</p>" if error else ""
    return _page("New supplier", f"""<h1>New supplier</h1>{err}
<form method='post' action='/demo/suppliers'>
  <p><label for='name'>Name</label> <input id='name' name='name' value='{_e(name)}' autocomplete='off'></p>
  <p><label for='phone'>Phone</label> <input id='phone' name='phone' value='{_e(phone)}' autocomplete='off'></p>
  <p><label for='notes'>Notes</label><br><textarea id='notes' name='notes' rows='5' cols='60'>{_e(notes)}</textarea></p>
  <button type='submit'>Save</button> <a href='/demo/suppliers'>Cancel</a>
</form>""")


@router.get("/suppliers/new")
def new_supplier(request: Request):
    return _form() if _signed_in(request) else LOGIN


@router.post("/suppliers")
async def save(request: Request, name: str = Form(""), phone: str = Form(""), notes: str = Form("")):
    if not _signed_in(request):
        return LOGIN
    if not name.strip():
        return _form(name, phone, notes, "Please enter a name.")
    if not re.fullmatch(r"[\d +()-]{5,}", phone.strip()):
        return _form(name, phone, notes, "Please enter a valid phone number.")
    await asyncio.sleep(0.8)  # a little latency, like a real application saving a record
    s = {"id": next(_ids), "name": name.strip(), "phone": phone.strip(), "notes": notes}
    _suppliers.append(s)
    return RedirectResponse(f"/demo/suppliers/{s['id']}?flash=Supplier+saved", 303)


@router.get("/suppliers/{sid}")
def supplier(request: Request, sid: int):
    if not _signed_in(request):
        return LOGIN
    s = next((x for x in _suppliers if x["id"] == sid), None)
    if not s:
        return RedirectResponse("/demo/suppliers", 303)
    flash = request.query_params.get("flash")
    return _page(s["name"], f"""{f"<p role='status'>{_e(flash)}</p>" if flash else ""}
<h1>{_e(s['name'])}</h1><p><b>Phone:</b> {_e(s['phone'])}</p><p style='white-space:pre-wrap'>{_e(s['notes'])}</p>""")
