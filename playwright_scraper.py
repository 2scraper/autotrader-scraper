#!/usr/bin/env python3
"""
autotrader-scraper — Playwright edition (primary engine)
========================================================

Scrapes two things autotrader.com publishes:

    --mode listing   (default)  one search's ORGANIC results: price, KBB fair
                                price and deal rating, VIN, mileage, trim,
                                and the dealer with its rating and address
    --mode vehicle              one detail page per listing id: everything
                                above, plus features, the seller's
                                description, open recalls and KBB reviews

Three engines ship in this repo and they must agree on exit codes, run
status, and whether a run crashes or spends money. The page loop is ONE
implementation in page_flow.py; this file only answers how Playwright
navigates, reads a document and relaunches.

What is different about autotrader.com
--------------------------------------
* **It wants a US residential exit AND a headful browser**, both. Measured
  2026-09-24, every one of these got a 3.7 KB "page unavailable" page under
  HTTP 200 in place of the page asked for: plain curl (with and without a
  Chrome User-Agent), headless Chromium, the new headless mode, and all of
  those through a US residential proxy; and headful Chromium from a
  datacentre address. Headful Chromium through the US residential exit was
  served, every time. So this engine is HEADFUL BY DEFAULT, and on a
  server it wants a display (`xvfb-run -a python playwright_scraper.py …`).
* **The data is the page's own store**, not its markup and not its JSON-LD
  (which describes three paid placements). See product_parser.
* **The site serves 12 pages of a search**, 300 cars at 25 a page, however
  many matched, and redirects a page past the end to its last one. The run
  plans against the site's own page count and records the cap.

Usage
-----
    xvfb-run -a python playwright_scraper.py --proxy "$US_RESIDENTIAL" \\
        --url "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny" \\
        --pages 3

    python playwright_scraper.py --make honda --model cr-v --zip 60601 \\
        --radius 50 --listing-type new --sort price-asc

    python playwright_scraper.py --mode vehicle --from-listing autotrader_rows.json

Requires: pip install -r requirements.txt -r requirements-playwright.txt
          then: playwright install chromium   (only if NOT using --cdp-endpoint)
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from typing import Optional

from playwright.sync_api import (sync_playwright, Error as PWError,
                                 TimeoutError as PWTimeout)

from captcha_solver import (INJECT_TOKEN_JS, detect_recaptcha_v3,
                            solve_recaptcha)
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ProxyError)
from cli import (add_common_args, add_search_args, display_problem,
                 finish_args)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("playwright_scraper")


# Chromium's own names for a proxy that could not be used. Distinguished
# from a timeout because the two want opposite responses (CLAUDE.md §8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_PROXY_AUTH_REQUESTED",
    "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_MANDATORY_PROXY_CONFIGURATION_FAILED",
)


def _proxy_failure(exc) -> str:
    """The Chromium proxy-error name in `exc`, or "" if it is not one."""
    text = str(exc)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _launch_local(pw, args, pool):
    """Launch our own Chromium on `pool`'s current exit; return (browser, context, page).

    A proxy rotation tears the whole browser down and calls this again.
    Cookies a bot manager issued against one exit, replayed from another,
    are a stronger signal than either address alone (§8), and Akamai Bot
    Manager is what fronts this site.

    No User-Agent is set. The browser's own is consistent with its TLS and
    client hints by construction, and a bare UA override is what a sibling
    site behind the same vendor scored after one page (§24).
    """
    launch_kwargs = {"headless": args.headless}
    proxy = to_playwright(pool.current) if pool else None
    if proxy:
        launch_kwargs["proxy"] = proxy
        logger.info("Using proxy exit %s", mask(pool.current))

    browser = pw.chromium.launch(**launch_kwargs)
    ctx_kwargs = {"locale": args.locale}
    init_script = None
    if args.fingerprint:
        from fingerprint_client import (get_fingerprint,
                                        playwright_context_kwargs,
                                        playwright_init_script)
        fp = get_fingerprint(args.twocaptcha_key,
                             tags=args.fp_tags, country=args.fp_country)
        ctx_kwargs.update(playwright_context_kwargs(fp))
        init_script = playwright_init_script(fp)
        logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"), fp.get("country"))

    context = browser.new_context(**ctx_kwargs)
    if init_script:
        context.add_init_script(init_script)
    return browser, context, context.new_page()


class _Ops:
    """One browser + context + page, exposed as page_flow's named operations.

    page_flow owns the page loop for all three engines. This class answers
    only HOW Playwright does each step.
    """

    def __init__(self, pw, args, pool, remote: bool = False):
        self.pw, self.args, self.pool, self.remote = pw, args, pool, remote
        self.browser = self.context = self.page = None

    def open(self):
        if self.remote:
            self.browser, self.context, self.page = _connect_remote(self.pw, self.args)
        else:
            self.browser, self.context, self.page = _launch_local(
                self.pw, self.args, self.pool)
        return self

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded",
                                  timeout=page_flow.NAV_TIMEOUT_MS)
        except (PWTimeout, PWError) as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        return (resp.status if resp is not None else None), self.page.url

    def document_text(self) -> str:
        try:
            return self.page.content()
        except PWError:
            # The document was being replaced mid-read; the caller polls.
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self.page, self.args)

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        """A fresh browser on the pool's current exit. On a remote browser
        a fresh page is all this engine can change: its exit is not ours."""
        if self.remote:
            try:
                self.page.close()
            except Exception:  # noqa: BLE001
                pass
            self.page = self.context.new_page()
            return
        try:
            self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser for rotation: %s", e)
        self.open()

    def close(self):
        try:
            if self.remote:
                self.page.close()  # leave the remote browser app running
            else:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def _connect_remote(pw, args):
    """Attach to an already-running browser over CDP; return (browser, context, page)."""
    logger.info("Connecting to existing browser over CDP: %s",
                _mask_credentials(args.cdp_endpoint))
    browser, e = None, None
    for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
        try:
            browser = pw.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30000)
            break
        except (PWError, PWTimeout) as err:
            e = err
            if attempt < page_flow.CDP_CONNECT_ATTEMPTS and page_flow.cdp_should_retry(str(err)):
                logger.warning("The Scraping Browser profile is still locked "
                               "(attempt %d/%d) — a previous run may be "
                               "releasing it; retrying in %.0fs.", attempt,
                               page_flow.CDP_CONNECT_ATTEMPTS,
                               page_flow.CDP_LOCKED_WAIT_S)
                time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                continue
            break
    if browser is None:
        # The endpoint carries a password, and Playwright repeats it five
        # times in its error text (§8). Rewritten with it masked, keeping
        # host and port, which are the useful half.
        raise PWError(
            f"could not connect to --cdp-endpoint "
            f"{_mask_credentials(args.cdp_endpoint)}: "
            f"{_mask_credentials(str(e))}\n"
            f"{page_flow.cdp_connect_hint(str(e))}"
        ) from None
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    # The Scraping Browser API's own CAPTCHA domain
    # (https://2captcha.com/scraper/browser-api/api). No challenge has been
    # seen on this site; if one appears, the extension can clear it before
    # the local solver gets a turn.
    try:
        cdp_session = context.new_cdp_session(page)
        cdp_session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
        cdp_session.on("Captcha.detected", lambda *_: logger.info("[Scraping Browser] CAPTCHA detected on page."))
        cdp_session.on("Captcha.solveFinished", lambda *_: logger.info("[Scraping Browser] CAPTCHA solved automatically."))
        cdp_session.on("Captcha.solveFailed", lambda *_: logger.warning("[Scraping Browser] CAPTCHA auto-solve failed."))
        logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
    except Exception as e:  # noqa: BLE001
        logger.info("Captcha.setAutoSolve not available on this --cdp-endpoint (%s) — "
                    "relying on this script's own detect+solve logic instead.", e)
    return browser, context, page


# Every `scheme://user:pass@` in a string, however many times it occurs.
# Matching GLOBALLY is the point: a Playwright connection error repeats the
# endpoint five times (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def handle_captcha_if_present(page, args) -> bool:
    """Solve a reCAPTCHA on the current document. True if one was solved.

    NOT OBSERVED on this site: no captcha of any kind appeared on any page
    captured on 2026-09-24, served or refused. The refusal it does send has
    no widget on it, so there is nothing to buy an answer to (§19:
    "unsolvable" is a property of a page). This path is the insurance for a
    page that one day carries a reCAPTCHA, and page_flow calls it only for
    a page it has classified as a challenge.
    """
    try:
        html = page.content()
    except PWError:
        return False
    challenge = detect_recaptcha_v3(html, page.url)
    if challenge is None:
        logger.info("A challenge page with no reCAPTCHA on it — nothing to "
                    "send to the solver. This repo does not implement Akamai's "
                    "own behavioural challenge; a different exit is what "
                    "changes it.")
        return False
    if not args.twocaptcha_key:
        logger.warning("A reCAPTCHA stands in front of the page and there is no "
                       "2captcha key to solve it with. Set TWOCAPTCHA_KEY in .env.")
        return False
    logger.warning("reCAPTCHA (%s) on %s — attempting to solve.", challenge.kind, page.url)
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver error is a warning (§8)
        logger.error("Solving the reCAPTCHA failed (%s) — continuing.",
                     _mask_credentials(str(e)))
        return False
    try:
        page.evaluate(INJECT_TOKEN_JS, token)
        page.wait_for_timeout(1500)
        page.reload(wait_until="domcontentloaded", timeout=page_flow.NAV_TIMEOUT_MS)
    except (PWError, PWTimeout) as e:
        logger.error("Could not hand the token to the page (%s).", e)
        return False
    return True


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int,
                              currency_hint: Optional[str] = None):
    """Fetch `page_nums` across `concurrency` workers.

    Each worker owns its own Playwright instance, browser and exit: with the
    sync API a browser belongs to the thread that made it, so sharing one is
    not an option even in principle (§7). The page loop itself is
    page_flow.worker_loop, shared by all three engines.
    """
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            with sync_playwright() as pw:
                ops = _Ops(pw, args, page_flow.worker_pool(pool, index)).open()
                try:
                    page_flow.worker_loop(ops, args, query, work, results,
                                          results_lock, exhausted, name,
                                          _mask_credentials, currency_hint)
                finally:
                    ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)

    threads = [threading.Thread(target=worker, args=(i,), name=f"page-worker-{i + 1}")
               for i in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unattempted = []
    while True:
        try:
            unattempted.append(work.get_nowait())
        except queue.Empty:
            break
    return results, sorted(unattempted), exhausted.is_set()


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    if pool and args.cdp_endpoint:
        logger.warning("Ignoring --proxy/--proxy-file: with --cdp-endpoint the "
                       "remote browser has its own exit, and layering a second "
                       "proxy on top would contradict it.")
        pool = None
    concurrency = page_flow.concurrency_for(args, pool)
    with sync_playwright() as pw:
        return page_flow.run_pages(
            lambda: _Ops(pw, args, pool, remote=bool(args.cdp_endpoint)).open(),
            lambda ops: ops.close(),
            lambda pages, cur: _fetch_pages_concurrently(args, pool, args.query,
                                                         pages, concurrency, cur),
            args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="autotrader.com scraper — search results and vehicle "
                    "detail pages (Playwright edition)")
    add_search_args(p)
    add_common_args(p)
    return finish_args(p, p.parse_args(argv))


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint API "
                     "uses the same key, though it's a separate subscription "
                     "from solving).")
        return 2
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint.")
    problem = display_problem(args)
    if problem:
        logger.error("%s", problem)
        return 2
    try:
        return scrape(args)
    except ProxyError as e:
        logger.error("%s", e)
        return 2
    except PWError as e:
        # A remote browser refusing the connection is a REMOTE API failure
        # (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            return EXIT_API_ERROR
        raise


if __name__ == "__main__":
    sys.exit(main())
