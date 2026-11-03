"""Start Automation Studio: python run.py [--no-browser]

The container runs this too (see docker/entrypoint.sh)."""
import asyncio
import sys
import threading
import webbrowser

import uvicorn

from app.config import BIND_HOST, IN_CONTAINER, PORT, TRUSTED_PROXIES


def main() -> None:
    if sys.platform == "win32":
        # Playwright launches browsers as subprocesses, which needs the Proactor loop.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    url = f"http://localhost:{PORT}"
    print(f"\n  Automation Studio (prototype)  ->  {url}")
    print("  Press Ctrl+C to stop.\n", flush=True)
    if "--no-browser" not in sys.argv and not IN_CONTAINER:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    # Behind a reverse proxy (HTTPS), take the client's address and https from its headers,
    # so the audit log names the right IP and the sign-in cookie is marked Secure.
    uvicorn.run("app.main:app", host=BIND_HOST, port=PORT, log_level="warning",
                proxy_headers=True, forwarded_allow_ips=TRUSTED_PROXIES)


if __name__ == "__main__":
    main()
