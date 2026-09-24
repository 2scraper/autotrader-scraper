#!/usr/bin/env python3
"""
autotrader-scraper — Selenium edition (secondary engine)
========================================================

The same scrape as playwright_scraper.py, driven through Selenium. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money. The page loop that decides all three lives in page_flow.py and
is shared, so this file is browser plumbing and nothing else.

    --mode listing   (default)  one search's organic results
    --mode vehicle              one detail page per listing id

Three limits of this engine, stated here rather than left to be discovered.
None is a bug in this code and none can be fixed from here:

  * **Selenium cannot authenticate a proxy at all.** `--proxy-server=`
    accepts an address only; credentials are stripped and a warning says
    so. ON THIS SITE THAT MATTERS MORE THAN ANYWHERE ELSE IN THE FAMILY:
    autotrader.com serves its pages only to a US residential exit, and a
    residential proxy is almost always a credentialled one. This engine
    therefore works from a US residential connection of its own, or
    through a proxy that authorises by source IP instead of by password.
  * **Selenium cannot use an authenticated remote CDP endpoint.**
    chromedriver's `debuggerAddress` takes a bare `host:port`, so a
    credentialled --cdp-endpoint (the Scraping Browser API) is refused with
    exit 2 rather than connected to and silently failing.
  * **Selenium reports no HTTP status for a navigation.** Nothing is lost
    here: the site's refusal arrives under HTTP 200, so the classifier
    reads the document in all three engines anyway.

Usage
-----
    xvfb-run -a python selenium_scraper.py \\
        --url "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny"

Requires: pip install -r requirements.txt -r requirements-selenium.txt
          Selenium 4 fetches a matching chromedriver itself; a local Chrome
          or Chromium must be installed.
"""

import argparse
import logging
import queue
import re
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

from captcha_solver import detect_recaptcha_v3, solve_recaptcha
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ProxyError,
                        split_credentials)
from cli import (add_common_args, add_search_args, display_problem,
                 finish_args)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("selenium_scraper")

PAGE_LOAD_TIMEOUT = page_flow.NAV_TIMEOUT_MS // 1000
SCRIPT_TIMEOUT = 30

# Chromium's own names for "the proxy is the problem, not the site" (§8).
_PROXY_ERROR_MARKERS = (
    "ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_PROXY_AUTH_REQUESTED",
    "ERR_UNEXPECTED_PROXY_AUTH", "ERR_PROXY_CERTIFICATE_INVALID",
    "ERR_NO_SUPPORTED_PROXIES", "ERR_SOCKS_CONNECTION_FAILED",
)

# Selenium's dialect of the token hand-off: a function BODY with its
# argument in `arguments[0]`, where the twins pass a `(token) => …` function.
INJECT_TOKEN_BODY = """
var token = arguments[0];
var el = document.getElementById('g-recaptcha-response');
if (!el) {
  el = document.createElement('textarea');
  el.id = 'g-recaptcha-response'; el.name = 'g-recaptcha-response';
  el.style.display = 'none'; document.body.appendChild(el);
}
el.value = token; el.innerHTML = token;
return true;
"""


# Every `scheme://user:pass@` in a string, however many times it occurs (§8).
_CREDENTIALS_IN_URL_RE = re.compile(r"([a-z][a-z0-9+.\-]*://)[^\s/@]+:[^\s/@]+@",
                                    re.IGNORECASE)


def _mask_credentials(text: str) -> str:
    """`text` with any username:password in an embedded URL replaced."""
    return _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text or "")


def _proxy_failure(text) -> str:
    text = str(text)
    for marker in _PROXY_ERROR_MARKERS:
        if marker in text:
            return marker
    return ""


def _cdp_host_port(endpoint: str) -> str:
    """`host:port` for chromedriver's debuggerAddress, or exit 2 with a reason."""
    parts = urlsplit(endpoint if "//" in endpoint else f"//{endpoint}")
    if parts.username or parts.password:
        logger.error(
            "This --cdp-endpoint carries credentials (%s), and Selenium cannot "
            "send them: chromedriver's debuggerAddress is a bare host:port. "
            "Use playwright_scraper.py or puppeteer_scraper.py for a "
            "credentialed endpoint such as the Scraping Browser API — both "
            "authenticate on the WebSocket upgrade.",
            _mask_credentials(endpoint))
        sys.exit(2)
    host = parts.hostname or endpoint
    port = f":{parts.port}" if parts.port else ""
    return f"{host}{port}"


class _Ops:
    """One Chrome driver, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser (§8).
    """

    def __init__(self, args, pool):
        self.args, self.pool = args, pool
        self.remote = bool(args.cdp_endpoint)
        self.driver = None

    def open(self):
        options = Options()
        if self.remote:
            options.debugger_address = _cdp_host_port(self.args.cdp_endpoint)
            logger.info("Attaching to an existing browser at %s.",
                        options.debugger_address)
            # No UA, no proxy, no fingerprint on this path: the remote browser
            # brings its own (§8).
            self.driver = webdriver.Chrome(options=options)
            self._apply_timeouts()
            return self

        if self.args.headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1600,1000")
        options.add_argument(f"--lang={self.args.locale}")
        if self.pool:
            scrubbed, credentials = split_credentials(self.pool.current)
            options.add_argument(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(self.pool.current))
            if credentials:
                logger.warning(
                    "This proxy has credentials and SELENIUM CANNOT SEND "
                    "THEM: --proxy-server accepts an address only. They have "
                    "been stripped, so the exit will most likely refuse the "
                    "requests. Use playwright_scraper.py or "
                    "puppeteer_scraper.py for an authenticated proxy — on "
                    "this site, which wants a US residential exit, that is "
                    "usually the only kind available.")

        # No User-Agent override. The browser's own agrees with its TLS and
        # client hints; a bare CDP override is what a sibling site behind
        # the same vendor scored after one page (§24).
        self.driver = webdriver.Chrome(options=options)
        self._apply_timeouts()
        if self.args.fingerprint:
            self._apply_fingerprint()
        return self

    def _apply_timeouts(self):
        # Explicit, because a driver that stops answering otherwise hangs
        # the run (§8).
        self.driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        self.driver.set_script_timeout(SCRIPT_TIMEOUT)

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install."""
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                            {"userAgent": ua})
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": playwright_init_script(fp)})
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except WebDriverException as e:
            logger.warning("Could not apply the fingerprint over CDP (%s) — "
                           "continuing without it.", e)

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            self.driver.get(url)
        except WebDriverException as e:
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        try:
            final = self.driver.current_url
        except WebDriverException:
            final = url
        # No status from a Selenium navigation: see the module docstring.
        return None, final

    def document_text(self) -> str:
        try:
            return self.driver.page_source or ""
        except WebDriverException:
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self, self.args)

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        if self.remote:
            return
        self.close()
        self.open()

    def close(self):
        try:
            if self.driver is not None:
                # quit(), not close(): close() leaves the driver process
                # running, which a per-page rotation would leak once a page.
                self.driver.quit()
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during driver teardown: %s", e)


def handle_captcha_if_present(ops, args) -> bool:
    """Solve a reCAPTCHA on the current document. Mirrors
    playwright_scraper.handle_captcha_if_present, which says why it exists
    on a site where no captcha has been observed."""
    try:
        html = ops.driver.page_source
        url = ops.driver.current_url
    except WebDriverException:
        return False
    challenge = detect_recaptcha_v3(html, url)
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
    logger.warning("reCAPTCHA (%s) on %s — attempting to solve.", challenge.kind, url)
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                api_version=args.captcha_api,
                                min_score=args.min_score)
    except Exception as e:  # noqa: BLE001 — a solver error is a warning (§8)
        logger.error("Solving the reCAPTCHA failed (%s) — continuing.",
                     _mask_credentials(str(e)))
        return False
    try:
        ops.driver.execute_script(INJECT_TOKEN_BODY, token)
        time.sleep(1.5)
        ops.driver.refresh()
    except WebDriverException as e:
        logger.error("Could not hand the token to the page (%s).",
                     _mask_credentials(str(e)))
        return False
    return True


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int,
                              currency_hint: Optional[str] = None):
    """Fetch `page_nums` across `concurrency` workers, each with its own
    driver and exit; the page loop is page_flow.worker_loop."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        try:
            ops = _Ops(args, page_flow.worker_pool(pool, index)).open()
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
    return page_flow.run_pages(
        lambda: _Ops(args, pool).open(),
        lambda ops: ops.close(),
        lambda pages, cur: _fetch_pages_concurrently(args, pool, args.query,
                                                     pages, concurrency, cur),
        args, pool, args.query, concurrency, _mask_credentials)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="autotrader.com scraper — search results and vehicle "
                    "detail pages (Selenium edition)")
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
                       "remote browser supplies its own fingerprint.")
    problem = display_problem(args)
    if problem:
        logger.error("%s", problem)
        return 2
    try:
        return scrape(args)
    except ProxyError as e:
        logger.error("%s", e)
        return 2
    except WebDriverException as e:
        # A remote browser that will not accept the attachment is a REMOTE
        # failure (exit 5), not a crash in this code (exit 1).
        text = _mask_credentials(str(e))
        if args.cdp_endpoint and ("cannot connect" in text.lower()
                                  or "debugger" in text.lower()):
            logger.error("Could not attach to --cdp-endpoint: %s", text)
            return EXIT_API_ERROR
        raise


if __name__ == "__main__":
    sys.exit(main())
