"""One place that decides how the automated browser is launched.

Each target site gets its own persistent browser profile under
data/profiles/<host>. Signing in once (e.g. to Outlook, including MFA) is kept
there and reused by recordings and runs; no password is stored by the app.
"""
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Playwright

from .config import IN_CONTAINER, PROFILES_DIR, SCREEN


def profile_dir(url: str) -> Path:
    host = urlparse(url).hostname or "default"
    return PROFILES_DIR / host.replace(":", "_")


SIGNIN_MARKER = ".studio-signin"


def mark_signed_in(url: str, when: str) -> None:
    (profile_dir(url) / SIGNIN_MARKER).write_text(when, encoding="utf-8")


def signed_in_at(url: str) -> str | None:
    """When 'Save sign-in' was last clicked for this site, if ever."""
    marker = profile_dir(url) / SIGNIN_MARKER
    return marker.read_text(encoding="utf-8").strip() if marker.exists() else None


def _clear_stale_locks(path: Path) -> None:
    # A profile last used by a previous container (different hostname) keeps
    # Singleton* lock files that make Chromium refuse to start.
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (path / name).unlink(missing_ok=True)
        except OSError:
            pass


def open_context(p: Playwright, url: str, headless: bool, slow_mo: int = 0) -> BrowserContext:
    path = profile_dir(url)
    path.mkdir(parents=True, exist_ok=True)
    _clear_stale_locks(path)
    options = {"headless": headless, "slow_mo": slow_mo, "bypass_csp": True}
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
