# Automation Studio (prototype) container.
# Runs the dashboard plus a real Chromium on a virtual screen (Xvfb). The screen
# is shared through noVNC so the dashboard can show it live: you sign in,
# record and watch runs from your own browser.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# The code is copied so the image works on its own; start_container.bat also
# mounts the project folder over /app so edits apply on restart.
COPY . .
# Installed outside /app so the bind mount never hides it; CRLF stripped in case
# the file was saved on Windows.
RUN sed 's/\r$//' docker/entrypoint.sh > /usr/local/bin/studio-entrypoint \
 && chmod +x /usr/local/bin/studio-entrypoint

ENV STUDIO_HOST=0.0.0.0 \
    STUDIO_PORT=8010 \
    STUDIO_IN_CONTAINER=1 \
    STUDIO_SCREEN=1600x900

EXPOSE 8010 6080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/health', timeout=2)"
ENTRYPOINT ["studio-entrypoint"]
