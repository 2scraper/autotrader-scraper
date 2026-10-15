#!/usr/bin/env python3
"""
autotrader-scraper — 2captcha Scraper API edition (fourth engine)
=================================================================

A fourth way to run this scraper. Unlike the three browser engines, this one
manages **no browser and no CDP session of its own**: it POSTs a URL to
2captcha's separate **Scraper API** (https://scraper.2captcha.com, a
different product from the Scraping Browser API the other three reach
through --cdp-endpoint), gets the page back over plain HTTPS, and feeds it
to this project's product_parser.

Why you would want it: no Chromium to install, runs from a tiny container or
a lambda.

WHAT THIS SITE NEEDS — READ THIS FIRST
--------------------------------------
autotrader.com serves its pages only to a US residential exit AND a headful
browser (see playwright_scraper.py's header). What the Scraper API's own
exits get is recorded in the README with its date; `--cdp-url` routes the
fetch through a Scraping Browser session of your choosing instead, which is
the way to pick a US exit for it.

Both modes are GETs of a URL, so both are implemented: a search's results
(`--url` of a /cars-for-sale search) and detail pages (`--vehicle-id`, or a
/cars-for-sale/vehicle/{id} `--url`).

Usage
-----
    python3 scraper_api_client.py \\
        --url "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny"

    # the key comes from $TWOCAPTCHA_KEY and a CDP endpoint from
    # $AUTOTRADER_CDP_ENDPOINT, so neither needs to be typed — a secret in
    # argv is readable by anything that can run `ps`

Requires: pip install -r requirements.txt
          (no playwright/selenium/pyppeteer needed for this engine)
"""

import argparse
import copy
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import Query, query_from_url
import page_flow
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes come from the shared page loop, as in the browser engines: an
# API failure is a TransportError there, which ends as exit 5 ("the pages
# were never obtained"), not 2 — a remote API failing is not the operator
# passing wrong arguments.

def _mask_credentials(url: str) -> str:
    """Never print a username:password embedded in a ws://... or http://... URL."""
    if "@" not in url:
        return url
    scheme_sep = url.find("://")
    if scheme_sep == -1:
        return url
    scheme, rest = url[:scheme_sep + 3], url[scheme_sep + 3:]
    _, _, host_part = rest.partition("@")
    return f"{scheme}***:***@{host_part}"


# Credentials embedded ANYWHERE in a blob of text, not just in a string that
# is entirely a URL — and every occurrence, not the first. A masker that
# handles one occurrence prints the password the other four times and looks
# like it is working.
_CREDS_IN_TEXT_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s'\"@]+@", re.IGNORECASE)
# Same shape as captcha_solver's and fingerprint_client's. A third copy is
# one too many and they should be unified in a family pass; reaching into
# another module's private name to avoid it would be worse.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)


def _redact_debug_header(value: str) -> str:
    """The x-debug header, safe to log.

    SECURITY.md names this header as one of three places credentials reach a
    log unmasked, and it was logged verbatim: the API echoes back the task it
    ran, so a run driven through a credentialed CDP endpoint put that
    endpoint's username and password into the log, and a key passed as a
    query parameter would go the same way.

    Redaction rather than an allowlist of fields, deliberately: the header is
    the API's own metadata and its shape is not ours to pin, so an allowlist
    would silently drop the cost and timing figures this is logged FOR the
    first time the API adds a field.
    """
    return _KEY_IN_TEXT_RE.sub(r"\1=***",
                               _CREDS_IN_TEXT_RE.sub(r"\1***:***@", value))


def _build_wait_for(args) -> Optional[dict]:
    """`waitFor` is an OBJECT. Measured 2026-09-23 against the live
    /tasks/sync endpoint: the JSON-encoded string form this client used to
    send was answered with HTTP 422 ("params.waitFor must be an object")
    and was still billed ($0.0005); the same request with an object
    answered HTTP 200. The earlier note here, that the API wanted a
    double-encoded string, no longer describes the API.

    Default (no flag): wait for the DOM. On a challenge-protected page
    that resolves instantly against the challenge page itself — which is
    exactly the trap documented in this module's docstring, so
    --wait-text/--wait-element exist to wait on something only the real
    page can contain."""
    if args.wait_text:
        return {"text": args.wait_text}
    if args.wait_element:
        return {"element": args.wait_element, "checkVisible": True}
    if args.wait_state:
        return {"state": args.wait_state}
    return None


def fetch_html(args):
    payload = {
        "task_type": "scrape",
        "url": args.url,
        "data_format": "raw",   # we want HTML; product_parser does the rest
        "format": "json",       # {"status": verdict, "http_code": target status, "headers", "body"}
        "timeout": min(args.timeout, MAX_API_TIMEOUT),
    }

    wait_for = _build_wait_for(args)
    if wait_for:
        payload["waitFor"] = wait_for
        logger.info("waitFor: %s", json.dumps(wait_for))

    if args.cdp_url:
        payload["cdpurl"] = args.cdp_url
        logger.info("Routing through an existing browser session: %s",
                    _mask_credentials(args.cdp_url))

    logger.info("POST %s (url=%s)", SYNC_ENDPOINT, args.url)
    resp = requests.post(
        SYNC_ENDPOINT,
        headers={"Authorization": f"Bearer {args.key}", "Content-Type": "application/json"},
        json=payload,
        # Give the HTTP call more headroom than the API-side task timeout,
        # otherwise a task that legitimately runs the full 120s looks like
        # a client-side network failure.
        timeout=min(args.timeout, MAX_API_TIMEOUT) + 30,
    )

    # The API returns its own per-task metadata (price, timings, status)
    # in an x-debug header — worth logging, it's the only place the real
    # cost of the call shows up.
    debug = resp.headers.get("x-debug")
    if debug:
        logger.info("x-debug: %s", _redact_debug_header(debug))

    if resp.status_code != 200:
        # 422 = task ran but errored (this is what a bad/unreachable
        # cdpurl produces: "CDP connect failed (user cdpurl) after N
        # attempts"); 402 = out of balance; 408 = sync wait exceeded.
        raise RuntimeError(
            f"Scraper API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    body = resp.json()
    html = body.get("body") or ""
    # The TARGET's HTTP status is `http_code`. `status` is the API's own
    # verdict string ("success"), measured 2026-09-23 -- passing it on
    # handed the page classifier a string, so a target 403/503 was never
    # seen. Fall back to `status` only if it is itself an integer.
    upstream_status = body.get("http_code")
    if not isinstance(upstream_status, int):
        legacy = body.get("status")
        upstream_status = legacy if isinstance(legacy, int) and not isinstance(legacy, bool) else None
    final_url = body.get("url") if isinstance(body.get("url"), str) else None
    logger.info("Upstream page HTTP status %s, %d bytes of HTML.", upstream_status, len(html))
    # The STATUS is returned alongside the HTML, not thrown away. It used to
    # be, and that cost this engine the family's central distinction. On this
    # site a refusal carries no markup at all — nothing a challenge check
    # on it, so the challenge check below finds nothing and the run fell
    # through to "0 products" and exit 4. A pipeline branching on the exit
    # code then reads a block as an empty category. See detect_page_state,
    # which the three browser engines already reach through page_flow.
    #
    # On autotrader the refusal arrives under HTTP 200, so the status says
    # little here; the body is what is classified. The final URL is handed
    # on when the API reports one, because a detail page for a sold car is a
    # REDIRECT to a results page, and that is how it is told apart.
    return html, upstream_status, final_url


class _Ops:
    """The Scraper API as page_flow's named operations, so this engine runs
    the SAME page loop as the three browser engines: one `goto` is one API
    task. Before 0.1.1 this file carried its own copy of the pagination,
    the status triage and the sidecar, and an audit (2026-10-07) pointed out
    the two could drift; the copy is gone rather than kept in step.

    `runs_scripts = False`: the loop's readiness poll re-reads a document a
    browser may still be replacing. This one has nothing running in it, so
    the poll would re-read the same bytes for eight seconds and call them
    unknown anyway (a trap a sibling repo met in its own browserless
    engine).
    """

    runs_scripts = False

    def __init__(self, args):
        self.args = args
        self.pool = None
        self.html = ""

    def goto(self, url: str):
        task = copy.copy(self.args)
        task.url = url
        try:
            html, status, final_url = fetch_html(task)
        except (requests.RequestException, RuntimeError) as e:
            raise page_flow.TransportError(_redact_debug_header(str(e))) from None
        self.html = html or ""
        return status, final_url or url

    def document_text(self) -> str:
        return self.html

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        # The API's browser is not ours to drive; a challenge it meets is
        # reported, not paid for here.
        return False

    def relaunch(self) -> None:
        # Every task is a fresh browser at the API already.
        self.html = ""

    def proxy_failure(self, text: str) -> str:
        return ""

    def close(self) -> None:
        pass


def main() -> int:
    args = parse_args()
    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2
    rc = page_flow.run_pages(lambda: _Ops(args), lambda ops: ops.close(),
                             lambda pages, cur: ([], pages, False),
                             args, None, args.query, 1, _redact_debug_header)
    if rc == 3:
        logger.error("The Scraper API's own exits were refused (measured "
                     "2026-09-24: the site's 'page unavailable', $0.0005 a "
                     "task). Route the fetch through a Scraping Browser "
                     "country-us session (--cdp-url), or use a browser engine "
                     "with a US residential --proxy.")
    return rc


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="autotrader.com scraper — 2captcha Scraper API edition (no "
                    "local browser). Both modes: a search's results, or "
                    "vehicle detail pages.")
    # NOT required: prefer the TWOCAPTCHA_KEY env var (a key in argv is
    # visible to anyone who can run `ps`).
    p.add_argument("--key", default=os.environ.get("TWOCAPTCHA_KEY"),
                   help="2captcha.com API key (sent as a Bearer token). "
                        "Defaults to $TWOCAPTCHA_KEY, which is the safer way to pass it.")
    p.add_argument("--mode", choices=["listing", "vehicle"], default=None,
                   help="Inferred from --url or --vehicle-id.")
    p.add_argument("--url", default=None,
                   help="An autotrader.com search URL, or a /cars-for-sale/"
                        "vehicle/{id} page. Also read from AUTOTRADER_URL.")
    p.add_argument("--vehicle-id", action="append", default=None, metavar="ID",
                   help="A listing id, for --mode vehicle. Repeatable.")
    p.add_argument("--pages", type=int, default=1,
                   help="Results pages (default 1). Each is a separate billable task.")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Seconds between pages (default 1.0)")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default="autotrader_rows_scraperapi", help="Output file prefix")
    p.add_argument("--timeout", type=int, default=90,
                   help=f"API-side task timeout in seconds (1-{MAX_API_TIMEOUT}, default 90)")
    p.add_argument("--cdp-url", default=None,
                   help="Route the fetch through an existing browser session over CDP "
                        "(sent as the API's `cdpurl` param), e.g. ws://user:pass@host:port")
    wait = p.add_mutually_exclusive_group()
    wait.add_argument("--wait-text", default=None,
                      help="Wait until this string appears on the page. Default: "
                           "the site's own asset path, /cars-for-sale/_next/static/, "
                           "which only a served page carries.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible.")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were parsed.")
    p.add_argument("--retries", type=int, default=1,
                   help="ATTEMPTS per page on a failed task, the first one "
                        "included, as in the browser engines (default 1: no "
                        "retry, since each attempt is a billable task). A "
                        "refused page is re-fetched once regardless. Before "
                        "0.1.1 this flag counted EXTRA attempts here and "
                        "total attempts in the browser engines.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds before a retry (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the page to this path, even on success")
    args = p.parse_args(argv)
    # This client uses --key and --cdp-url rather than --twocaptcha-key and
    # --cdp-endpoint, so the env mapping is spelled out instead of defaulted.
    env_config.apply(args, keys={
        "TWOCAPTCHA_KEY": "key",
        "AUTOTRADER_CDP_ENDPOINT": "cdp_url",
        "AUTOTRADER_URL": "url",
    })
    if not args.url and not args.vehicle_id:
        p.error("pass --url (a search or a detail page) or --vehicle-id")
    if args.url:
        query, why = query_from_url(args.url)
        if query is None:
            p.error(why)
        if args.vehicle_id:
            p.error("pass --url or --vehicle-id, not both")
    else:
        query = Query(mode="vehicle", vehicle_ids=tuple(args.vehicle_id))
    if args.mode and args.mode != query.mode:
        p.error("--mode %s disagrees with what was asked for (%s)" % (args.mode, query.mode))
    why = query.validate()
    if why:
        p.error(why)
    if args.pages < 1:
        p.error("--pages must be at least 1")
    if args.retries < 1:
        p.error("--retries is the number of ATTEMPTS per page, the first one "
                "included, so it must be at least 1.")
    if not (args.wait_text or args.wait_element or args.wait_state):
        # A refusal page never carries it, so the wait cannot resolve on one
        # and hand it back as if it were the page. Not "__NEXT_DATA__": the
        # results page stopped carrying that on 2026-10-13, and a wait for it
        # would time out on a page that was served. The asset path is on
        # every served page in both payload shapes (19 to 69 times) and on
        # neither refusal captured (0).
        args.wait_text = "/cars-for-sale/_next/static/"
    args.query = query
    args.mode = query.mode
    # What page_flow reads, for an engine that has no browser of its own.
    args.engine = "scraper_api"
    args.headless = False
    args.cdp_endpoint = args.cdp_url
    args.concurrency = 1
    args.proxy_block_retries = 0
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
