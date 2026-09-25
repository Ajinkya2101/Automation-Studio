"""Start Automation Studio: python run.py [--no-browser]

The container runs this too (see docker/entrypoint.sh)."""
import asyncio
import sys
import threading
import webbrowser

import uvicorn

from app.config import BIND_HOST, IN_CONTAINER, PORT


def main() -> None:
    if sys.platform == "win32":
        # Playwright launches browsers as subprocesses, which needs the Proactor loop.
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    url = f"http://localhost:{PORT}"
    print(f"\n  Automation Studio (prototype)  ->  {url}")
    print(f"  Acme Mail (demo target)       ->  {url}/mail/login")
    print("  Press Ctrl+C to stop.\n", flush=True)
    if "--no-browser" not in sys.argv and not IN_CONTAINER:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=BIND_HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
