"""One place that decides how the automated browser is launched.

Each user has their own persistent browser profile per target site, under
data/profiles/users/<user>/<host>. Signing in once (e.g. to Outlook, including
MFA) is kept there and reused by that user's recordings and runs; no password
is stored by the app. Without the studio sign-in (one person on their own PC)
the profile is data/profiles/<host>.
"""
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Playwright

from .config import IN_CONTAINER, PROFILES_DIR, SCREEN


def profile_dir(url: str, user: str = "") -> Path:
    """Users never share a profile: Chromium cannot open one twice, so two users' browsers on
    the same site would collide, and each would be signed in to the site as the other."""
    host = (urlparse(url).hostname or "default").replace(":", "_")
    if not user or user == "local":
        return PROFILES_DIR / host
    return PROFILES_DIR / "users" / re.sub(r"[^a-z0-9._-]", "_", user.lower()) / host


SIGNIN_MARKER = ".studio-signin"


def mark_signed_in(url: str, when: str, user: str = "") -> None:
    (profile_dir(url, user) / SIGNIN_MARKER).write_text(when, encoding="utf-8")


def signed_in_at(url: str, user: str = "") -> str | None:
    """When the user last clicked 'Save sign-in' for this site, if ever."""
    marker = profile_dir(url, user) / SIGNIN_MARKER
    return marker.read_text(encoding="utf-8").strip() if marker.exists() else None


def _clear_stale_locks(path: Path) -> None:
    # A profile last used by a previous container (different hostname) keeps
    # Singleton* lock files that make Chromium refuse to start.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (path / name).unlink(missing_ok=True)
        except OSError:
            pass


def open_context(p: Playwright, url: str, headless: bool, slow_mo: int = 0, user: str = "",
                 display: str | None = None) -> BrowserContext:
    """The user's browser for this site; shown on X `display` (the session's own screen) when given."""
    path = profile_dir(url, user)
    path.mkdir(parents=True, exist_ok=True)
    _clear_stale_locks(path)
    options = {"headless": headless, "slow_mo": slow_mo, "bypass_csp": True}
    if display and not headless:
        options["env"] = {**os.environ, "DISPLAY": display}
    if headless:
        options["viewport"] = {"width": 1280, "height": 800}
    elif IN_CONTAINER:
        w, h = SCREEN.split("x")[:2]
        options.update(no_viewport=True, args=["--window-position=0,0", f"--window-size={w},{h}"])
    else:
        options.update(no_viewport=True, args=["--start-maximized"])
    return p.chromium.launch_persistent_context(str(path), **options)


def first_page(ctx: BrowserContext):
    """A persistent context opens with one blank tab; use it instead of adding another."""
    return ctx.pages[0] if ctx.pages else ctx.new_page()
