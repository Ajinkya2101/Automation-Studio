# Automation Studio (prototype) container.
# Runs the dashboard plus a real Chromium on a virtual screen (Xvfb). The screen
# is shared through VNC, which the dashboard relays to noVNC behind its sign-in:
# you sign in, record and watch runs from your own browser.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

# novnc is only needed for its web page (/usr/share/novnc); the studio relays the connection.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Separate layers so a failed Chromium download does not redo the pip install.
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# The studio runs as this user, not root (see docker/entrypoint.sh).
RUN useradd --create-home --uid 1000 studio \
 && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# The code is copied so the image works on its own (docker-compose.yml mounts only data/);
# start_container.bat also mounts the project folder over /app so edits apply on restart.
COPY --chown=studio:studio . .
# Installed outside /app so the bind mount never hides it; CRLF stripped in case
# the file was saved on Windows.
RUN sed 's/\r$//' docker/entrypoint.sh > /usr/local/bin/studio-entrypoint \
 && chmod +x /usr/local/bin/studio-entrypoint

ENV STUDIO_HOST=0.0.0.0 \
    STUDIO_PORT=8010 \
    STUDIO_IN_CONTAINER=1 \
    STUDIO_SCREEN=1600x900 \
    STUDIO_LIVE_VIEW_URL="/vnc/vnc.html?autoconnect=true&resize=scale&reconnect=true&path=vnc/websockify"

EXPOSE 8010
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/health', timeout=2)"
ENTRYPOINT ["studio-entrypoint"]
