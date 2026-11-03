#!/usr/bin/env bash
# Start the virtual screen and the VNC server, then run the studio, all as the unprivileged
# "studio" user. The studio relays the live view to VNC itself, behind its sign-in.
set -euo pipefail

if [ "$(id -u)" = 0 ]; then
  # data/ may be a fresh volume or a host folder created by root: hand it to the studio user,
  # then start again as that user.
  mkdir -p data
  if ! setpriv --reuid=studio --regid=studio --init-groups test -w data; then
    chown -R studio:studio data 2>/dev/null || true
  fi
  export HOME=/home/studio
  exec setpriv --reuid=studio --regid=studio --init-groups "$0" "$@"
fi

SCREEN_SIZE="${STUDIO_SCREEN:-1600x900}"
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

Xvfb :99 -screen 0 "${SCREEN_SIZE}x24" -nolisten tcp -ac &
for _ in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done

# VNC listens inside the container only; the studio's /vnc/websockify is the only way in.
x11vnc -display :99 -rfbport "${STUDIO_VNC_PORT:-5900}" -localhost -forever -shared -nopw -quiet -noxdamage \
  >/tmp/x11vnc.log 2>&1 &

if [ ! -f samples/Outlook_Email.xlsx ]; then
  python samples/make_sample.py
fi

exec python run.py --no-browser
