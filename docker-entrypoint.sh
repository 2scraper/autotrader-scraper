#!/bin/sh
# The image's entrypoint: a virtual display, then the Playwright engine.
#
# The engine is HEADFUL by default because autotrader.com refused headless
# Chromium on every measurement, so it needs a display, and a container has
# none. This starts Xvfb itself rather than using `xvfb-run`, and that is a
# fix, not a preference: xvfb-run waits for Xvfb to signal SIGUSR1 when it
# is ready, and run as PID 1 in a container (no --init) that wait never
# ended — the first CI build hung on `docker run image --help` for fifteen
# minutes before it was cancelled.
#
#   docker run --rm image [engine flags]        runs playwright_scraper.py
#   docker run --rm image --exec CMD [ARGS]     runs CMD under the display
set -e
Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:99
# Wait (bounded, 5 s) for the display's socket, so a browser launched at
# once does not race the server.
i=0
while [ ! -e /tmp/.X11-unix/X99 ] && [ "$i" -lt 50 ]; do
  sleep 0.1
  i=$((i + 1))
done
if [ "$1" = "--exec" ]; then
  shift
  exec "$@"
fi
exec python3 playwright_scraper.py "$@"
