#!/usr/bin/env bash
# Start the virtual screens and their VNC servers, then run the studio, all as the unprivileged
# "studio" user. The studio relays the live view to VNC itself, behind its sign-in.
# Each browser shown at the same time gets a screen of its own (STUDIO_SCREENS of them:
# displays :99, :100, ... with VNC on 5900, 5901, ...), so users never see or click into
# each other's browsers. app/config.py numbers them the same way.
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
SCREENS="${STUDIO_SCREENS:-4}"
VNC_BASE="${STUDIO_VNC_PORT:-5900}"

for i in $(seq 0 $((SCREENS - 1))); do
  n=$((99 + i))
  rm -f "/tmp/.X${n}-lock" "/tmp/.X11-unix/X${n}"
  Xvfb ":${n}" -screen 0 "${SCREEN_SIZE}x24" -nolisten tcp -ac &
  for _ in $(seq 1 50); do [ -e "/tmp/.X11-unix/X${n}" ] && break; sleep 0.1; done
  # VNC listens inside the container only; the studio's /vnc/websockify is the only way in.
  x11vnc -display ":${n}" -rfbport $((VNC_BASE + i)) -localhost -forever -shared -nopw -quiet -noxdamage \
    >"/tmp/x11vnc-${n}.log" 2>&1 &
done

if [ ! -f samples/Outlook_Email.xlsx ]; then
  python samples/make_sample.py
fi

exec python run.py --no-browser
