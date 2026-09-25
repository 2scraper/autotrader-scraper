# Builds the Playwright engine (the one the README recommends) into a
# container with its own Chromium and a virtual display — for a CI canary run
# or a scheduled job, not required for local development (`pip install`
# directly is simpler there).
#
#   docker build -t autotrader-scraper .
#   docker run --rm -v "$PWD/out:/out" --env-file .env autotrader-scraper \
#     --url "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny" \
#     --pages 3 --out /out/camry
#
# The site serves its pages to a HEADFUL browser only (headless Chromium was
# refused on every measurement, 2026-09-24), so the entrypoint starts a
# virtual display (Xvfb) first: a display inside the container, no window
# anywhere. docker-entrypoint.sh says why it is not `xvfb-run`.
# It also wants a US residential exit: pass AUTOTRADER_PROXY through
# --env-file (never on the command line, where `ps` can read it). Nothing
# here bakes in a credential.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    # Playwright's own apt-get for Chromium's shared-library dependencies —
    # not pip packages, so this has to run as a separate, explicit step.
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: a sibling's image once omitted
# proxy_pool.py, which the engine imports at module level, so the image died
# with ModuleNotFoundError on every invocation INCLUDING `--help` — a broken
# container that nothing in the repo would have noticed.
COPY captcha_solver.py cli.py env_config.py fingerprint_client.py \
     output_writer.py page_flow.py playwright_scraper.py product_parser.py \
     proxy_pool.py diff_runs.py docker-entrypoint.sh ./

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["--help"]
