#!/usr/bin/env bash
# Start the virtual screen, share it over noVNC, then run the studio.
set -euo pipefail

SCREEN_SIZE="${STUDIO_SCREEN:-1600x900}"
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 "${SCREEN_SIZE}x24" -nolisten tcp -ac &
for _ in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done

# VNC listens inside the container only; noVNC (websockify) is the published entry point.
x11vnc -display :99 -rfbport 5900 -localhost -forever -shared -nopw -quiet -noxdamage >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

if [ ! -f samples/Automation_Email.xlsx ] || [ ! -f samples/Outlook_Email.xlsx ]; then
  python samples/make_sample.py
fi

exec python run.py --no-browser
