"""Central settings for the prototype. Environment variables override the
defaults; the container sets them in start_container.bat."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = BASE_DIR / "app"
STATIC_DIR = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR", BASE_DIR / "data"))
PROFILES_DIR = DATA_DIR / "profiles"
SAMPLES_DIR = BASE_DIR / "samples"
SAMPLE_WORKBOOK = SAMPLES_DIR / "Outlook_Email.xlsx"

# Address the server binds to (0.0.0.0 inside the container).
BIND_HOST = os.environ.get("STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("STUDIO_PORT", "8010"))

# In the container the browser draws on a virtual screen that the dashboard
# shows through noVNC. Natively, the browser opens as a normal window.
LIVE_VIEW_URL = os.environ.get("STUDIO_LIVE_VIEW_URL", "")
IN_CONTAINER = bool(os.environ.get("STUDIO_IN_CONTAINER"))
SCREEN = os.environ.get("STUDIO_SCREEN", "1600x900")

# Slow instances (Oracle Fusion test pods often are): every replay wait limit is
# multiplied by this. Replay still moves on as soon as the screen is ready, so a
# higher factor only costs time when a step really fails.
SLOW_FACTOR = max(float(os.environ.get("STUDIO_SLOW_FACTOR", "1")), 0.1)

# How long replay waits for an element or a check before failing a step.
ACTION_TIMEOUT_MS = int(int(os.environ.get("STUDIO_ACTION_TIMEOUT_MS", "45000")) * SLOW_FACTOR)

# Issue log. Each tester's device is named in everything it exports; in the container the
# launcher passes the PC's name and user, since the container's own are random.
LOGS_DIR = DATA_DIR / "logs"
DEVICE_NAME = os.environ.get("STUDIO_DEVICE_NAME", "")
OS_USER = os.environ.get("STUDIO_OS_USER", "")
# Playwright trace of each run (open with `playwright show-trace trace.zip`):
# "failures" keeps it for runs that did not pass, "always" for every run, "off" records none.
TRACE = os.environ.get("STUDIO_TRACE", "failures").strip().lower()
