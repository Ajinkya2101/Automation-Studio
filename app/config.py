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
SAMPLE_WORKBOOK = SAMPLES_DIR / "Automation_Email.xlsx"
OUTLOOK_WORKBOOK = SAMPLES_DIR / "Outlook_Email.xlsx"

# Address the server binds to (0.0.0.0 inside the container) and the address
# the automated browser, running next to the server, uses to reach it.
BIND_HOST = os.environ.get("STUDIO_HOST", "127.0.0.1")
PORT = int(os.environ.get("STUDIO_PORT", "8010"))
APP_URL = f"http://127.0.0.1:{PORT}"

# Default application under test when the Excel does not give a URL.
TARGET_START_URL = f"{APP_URL}/mail/login"

# In the container the browser draws on a virtual screen that the dashboard
# shows through noVNC. Natively, the browser opens as a normal window.
LIVE_VIEW_URL = os.environ.get("STUDIO_LIVE_VIEW_URL", "")
IN_CONTAINER = bool(os.environ.get("STUDIO_IN_CONTAINER"))
SCREEN = os.environ.get("STUDIO_SCREEN", "1600x900")

# How long replay waits for an element or a check before failing a step.
ACTION_TIMEOUT_MS = int(os.environ.get("STUDIO_ACTION_TIMEOUT_MS", "20000"))
