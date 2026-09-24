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
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import requests

from product_parser import (Query, currency, detect_bot_challenge,
                            detect_page_state, page_facts, parse_page,
                            query_from_url, request_for, site_page_for)
from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from page_flow import pages_to_plan
import env_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_api_client")

API_BASE = "https://scraper.2captcha.com"
SYNC_ENDPOINT = f"{API_BASE}/tasks/sync"

# The API caps `timeout` at 120s and rejects bodies over 10,000 bytes.
MAX_API_TIMEOUT = 120

# Exit codes. Kept distinct from 2 (bad usage) on purpose: a remote API
# failing is not the operator passing wrong arguments, and a harness that
# lumps them together sends you looking in the wrong place. An early run
# reported `exit=2` for an HTTP 422 from the API — which reads as "you called
# it wrong".
#
# Imported rather than redefined: the browser engines return the same code for
# a Scraping Browser that will not accept a connection, and two definitions
# of one exit code is how a family's contract drifts.
from output_writer import EXIT_API_ERROR  # noqa: E402

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


def main() -> int:
    args = parse_args()
    if not args.key:
        logger.error("No 2captcha API key. Pass --key, or better, export TWOCAPTCHA_KEY.")
        return 2
    query = args.query
    rows, seen, pages_done, failed, gone = [], set(), 0, [], []
    stop_reason, blocked, facts1, cur = "completed", False, None, None
    plan = len(query.vehicle_ids) if query.mode == "vehicle" else args.pages
    n = 1
    while n <= plan:
        args.url = request_for(query, n)
        expect = query.vehicle_ids[n - 1] if query.mode == "vehicle" else None
        rc, text, status, final_url = _fetch_once(args, query.mode, expect)
        if rc:
            failed.append(n)
            blocked = rc == 3
            stop_reason = ("blocked_akamai-unavailable" if blocked
                           else "page_load_timeout")
            break
        state = detect_page_state(text, status, final_url or args.url,
                                  query.mode, expect)
        if state == "gone":
            logger.warning("Vehicle %s is no longer listed — no row for it.", expect)
            gone.append(expect)
            pages_done += 1
            n += 1
            continue
        if state not in ("content", "empty"):
            logger.error("Page %d came back as %s (upstream HTTP %s).", n, state, status)
            failed.append(n)
            blocked = state in ("challenge", "blocked")
            stop_reason = "blocked_%s" % (detect_bot_challenge(text) or state) \
                if blocked else "page_load_timeout"
            break
        if query.mode == "listing":
            facts = page_facts(text)
            if n == 1:
                facts1 = facts
                cur = currency(text)
                available = facts.pages_available
                if available is not None:
                    available = max(0, available - query.first_page + 1)
                plan = pages_to_plan(args.pages, available)
            elif facts.served_page != site_page_for(query, n):
                logger.info("Asked for page %d, served page %d — the end of what "
                            "the site serves for this search.",
                            site_page_for(query, n), facts.served_page)
                stop_reason = "end_of_listing"
                break
        got = parse_page(text, query, n, cur)
        pages_done += 1
        logger.info("Parsed %d row(s) from page %d.", len(got), n)
        if query.mode == "listing" and not got:
            stop_reason = "end_of_listing" if n > 1 else "completed"
            break
        rows.extend(dedupe_by_key(got, seen))
        n += 1
        if n <= plan:
            time.sleep(args.delay)

    extra = {"engine": "scraper_api"}
    if query.mode == "listing":
        total = facts1.total_results if facts1 else None
        available = facts1.pages_available if facts1 else None
        reachable = available * query.page_size if available is not None else None
        extra.update({"total_results": total, "pages_available": available,
                      "reachable_max": reachable,
                      "capped_by_site": (bool(total and reachable is not None
                                              and total > reachable)
                                         if total is not None else None),
                      "site_query": facts1.site_query if facts1 else {},
                      "query": {"url": query.url, "sort": query.sort or "relevance",
                                "page_size": query.page_size,
                                "first_page": query.first_page}})
    else:
        extra.update({"vehicles_requested": len(query.vehicle_ids),
                      "vehicles_gone": gone,
                      "query": {"vehicle_ids": len(query.vehicle_ids)}})
    return finish_run(rows, args.out, args.format, args.allow_empty,
                      blocked=blocked, stop_reason=stop_reason,
                      pages_requested=plan if query.mode == "vehicle" else args.pages,
                      pages_completed=pages_done, pages_failed=failed,
                      mode=query.mode, source=SOURCE_DEFAULT,
                      start_url=request_for(query, 1),
                      final_url=request_for(query, max(1, min(pages_done, plan))),
                      extra=extra)


def _fetch_once(args, mode, expect):
    """(rc, html, upstream_status, final_url). rc is 0 on a fetch that
    returned, EXIT_API_ERROR on the API failing, 3 on a refusal after the
    retries."""
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        try:
            html, upstream_status, final_url = fetch_html(args)
        except requests.RequestException as e:
            logger.error("Network error talking to the Scraper API: %s",
                         _redact_debug_header(str(e)))
            return EXIT_API_ERROR, "", None, None
        except RuntimeError as e:
            logger.error("%s", _redact_debug_header(str(e)))
            return EXIT_API_ERROR, "", None, None
        if args.dump_html:
            with open(args.dump_html, "w", encoding="utf-8") as f:
                f.write(html)
            logger.info("Response written to %s", args.dump_html)
        state = detect_page_state(html, upstream_status, final_url or args.url,
                                  mode, expect)
        if state not in ("blocked", "challenge"):
            return 0, html, upstream_status, final_url
        if attempt < attempts:
            logger.info("Refused on attempt %d/%d — retrying in %ds.",
                        attempt, attempts, args.retry_delay)
            time.sleep(args.retry_delay)
    logger.error("The Scraper API's fetch was refused on every attempt (%s). "
                 "autotrader.com serves its pages to a US residential exit "
                 "with a headful browser; route the fetch through a Scraping "
                 "Browser country-us session (--cdp-url), or use a browser "
                 "engine with --proxy.", detect_bot_challenge(html) or state)
    return 3, "", None, None


def parse_args():
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
                           "__NEXT_DATA__, which only a served page carries.")
    wait.add_argument("--wait-element", default=None,
                      help="Wait until this CSS selector is visible.")
    wait.add_argument("--wait-state", choices=["load", "domcontentloaded"], default=None,
                      help="Wait for a page load state instead of specific content")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were parsed.")
    p.add_argument("--retries", type=int, default=1,
                   help="Extra attempts if a refusal comes back. Each attempt is "
                        "a separate billable task, so this defaults to 1.")
    p.add_argument("--retry-delay", type=int, default=10,
                   help="Seconds between retries (default 10)")
    p.add_argument("--dump-html", default=None,
                   help="Also write the page to this path, even on success")
    args = p.parse_args()
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
    if not (args.wait_text or args.wait_element or args.wait_state):
        # A refusal page never carries it, so the wait cannot resolve on one
        # and hand it back as if it were the page.
        args.wait_text = "__NEXT_DATA__"
    args.query = query
    return args


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
