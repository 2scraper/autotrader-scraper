#!/usr/bin/env python3
"""
autotrader-scraper — pyppeteer edition (secondary engine)
=========================================================

The same scrape as playwright_scraper.py, driven through pyppeteer. It must
agree with its twins on exit codes, run status, and whether a run crashes or
spends money. The page loop that decides all three lives in page_flow.py and
is shared, so this file is browser plumbing and nothing else.

    --mode listing   (default)  one search's organic results
    --mode vehicle              one detail page per listing id

See playwright_scraper.py's header for why the browser is headful by
default and wants a US residential exit.

Two things to know before choosing this engine:

  * **pyppeteer is effectively unmaintained** and its own README points at
    Playwright. It is here for parity, and for anyone who already has it.
    Its bundled Chromium is old; `--chromium-path` drives another one.
  * Unlike the Selenium engine, it CAN authenticate a remote CDP endpoint
    (`browserWSEndpoint` takes a full `ws://user:pass@host:port`) and a
    proxy (`page.authenticate`), so it works through the credentialled US
    residential exit this site wants.

Usage
-----
    xvfb-run -a python puppeteer_scraper.py --proxy "$US_RESIDENTIAL" \\
        --url "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny"

Requires: pip install -r requirements.txt -r requirements-puppeteer.txt
          (pyppeteer downloads its own Chromium on first run)
"""

import argparse
import asyncio
import concurrent.futures
import logging
import queue
import re
import sys
import threading
import time
from typing import Optional

# At module level, deliberately, and not inside the launch path. The offline
# suite guards `import puppeteer_scraper` behind try/except ImportError and
# REPORTS the skip, and CI's engine-smoke job imports it by name. That only
# works if importing this module actually requires the driver (CLAUDE.md §10).
from pyppeteer import launch, connect
from pyppeteer.errors import BrowserError

from captcha_solver import INJECT_TOKEN_JS, detect_recaptcha_v3, solve_recaptcha
from output_writer import EXIT_API_ERROR
import page_flow
from proxy_pool import (from_args as proxy_pool_from_args, mask, ProxyError,
                        split_credentials)
from cli import (add_common_args, add_search_args, display_problem,
                 finish_args)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

# Every await in this file goes through the bridge below with a timeout, so a
# hung remote call ends the operation instead of the run. pyppeteer provides
# no connect timeout of its own (§8).
DEFAULT_OP_TIMEOUT = 120
CONNECT_TIMEOUT = 30

# Chromium's own names for a proxy that could not be used (§8).
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


class _AsyncBridge:
    """Runs pyppeteer's coroutines on a private event loop, synchronously.

    Lets this engine drive page_flow's synchronous fetch loop unchanged,
    and gives every call an explicit, enforced timeout: `.result(timeout)`
    returns control even when the browser never answers, which pyppeteer's
    own API does not offer.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # pyppeteer leaves CDP calls in flight when a browser closes, and the
        # loop then logs each as an ERROR after a successful run has printed
        # its results. Only that shape is swallowed; anything else still gets
        # the default handler.
        message = " | ".join(
            str(context.get(k)) for k in ("exception", "message")
            if context.get(k))
        if any(m in message for m in (
                "Target closed", "Connection closed",
                "Task was destroyed but it is pending",
                "Future exception was never retrieved",
                "No session with given id",
                # A rejected --cdp-endpoint handshake, raised by websockets in
                # a task pyppeteer never awaits, AFTER the connect has already
                # timed out and been reported with the reason.
                "server rejected WebSocket connection",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, coro, timeout: Optional[float] = DEFAULT_OP_TIMEOUT):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING what it still has in flight, on the loop
        thread, so asyncio does not print a traceback per pending task after
        a successful run."""
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            self.loop.stop()

        self.loop.call_soon_threadsafe(_cancel_and_stop)
        self._thread.join(timeout=5)


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


class _Ops:
    """One pyppeteer browser + page, exposed as page_flow's named operations.

    Same contract as playwright_scraper._Ops, including the rule that a
    rotation means a genuinely FRESH browser (§8).
    """

    def __init__(self, bridge: _AsyncBridge, args, pool):
        self.bridge, self.args, self.pool = bridge, args, pool
        self.remote = bool(args.cdp_endpoint)
        self.browser = self.page = None

    def open(self):
        if self.remote:
            logger.info("Connecting to an existing browser over CDP: %s",
                        _mask_credentials(self.args.cdp_endpoint))
            err = None
            for attempt in range(1, page_flow.CDP_CONNECT_ATTEMPTS + 1):
                try:
                    self.browser = self.bridge.run(
                        connect(browserWSEndpoint=self.args.cdp_endpoint,
                                ignoreHTTPSErrors=True),
                        timeout=page_flow.CDP_CONNECT_TIMEOUT_S)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001 — see below
                    err = e
                    if (attempt < page_flow.CDP_CONNECT_ATTEMPTS
                            and page_flow.cdp_should_retry(str(e))):
                        logger.warning("The Scraping Browser profile did not "
                                       "accept the connection (attempt %d/%d) "
                                       "— it may still be locked by a previous "
                                       "run; retrying in %.0fs.", attempt,
                                       page_flow.CDP_CONNECT_ATTEMPTS,
                                       page_flow.CDP_LOCKED_WAIT_S)
                        time.sleep(page_flow.CDP_LOCKED_WAIT_S)
                        continue
                    break
            if err is not None:
                # websockets' message ("server rejected WebSocket connection:
                # HTTP 500") names neither the endpoint nor the reason.
                # Re-raised masked, with the meaning spelled out, so main()
                # can map it onto exit 5.
                raise RuntimeError(
                    "could not connect to --cdp-endpoint %s: %s\n%s"
                    % (_mask_credentials(self.args.cdp_endpoint),
                       _mask_credentials(str(err)),
                       page_flow.cdp_connect_hint(str(err)))) from None
            self.page = self.bridge.run(self.browser.newPage())
            self._enable_autosolve()
            return self

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage",
                       f"--lang={self.args.locale}"]
        launch_kwargs = {}
        if self.args.chromium_path:
            launch_kwargs["executablePath"] = self.args.chromium_path
        credentials = None
        if self.pool:
            exit_url = self.pool.current
            # Credentials go through page.authenticate(), never onto the
            # command line: --proxy-server= is part of the browser's argv,
            # readable by anything that can run `ps` (§8).
            scrubbed, credentials = split_credentials(exit_url)
            launch_args.append(f"--proxy-server={scrubbed}")
            logger.info("Using proxy exit %s", mask(exit_url))
        # Signal handlers off: pyppeteer installs them inside launch(), and
        # `signal.signal` raises off the main thread, which is where this
        # event loop lives. close() handles teardown instead.
        self.browser = self.bridge.run(
            launch(headless=self.args.headless, args=launch_args,
                   ignoreHTTPSErrors=True, handleSIGINT=False,
                   handleSIGTERM=False, handleSIGHUP=False, **launch_kwargs),
            timeout=CONNECT_TIMEOUT * 2)
        self.page = self.bridge.run(self.browser.newPage())
        # No User-Agent override (the donor set one). A sibling site behind
        # the same vendor served page 1 to a pyppeteer UA override and then
        # denied pages 2-4 of the same session, while the unmodified browser
        # was served 4 of 4 (§24). The browser's own UA is left alone.
        if self.args.fingerprint:
            self._apply_fingerprint()
        if credentials:
            self._authenticate_proxy(*credentials)
        return self

    def _authenticate_proxy(self, username: str, password: str):
        """Answer the proxy's 407 through the CDP `Fetch` domain.

        pyppeteer's own `page.authenticate` is built on
        `Network.setRequestInterception`, which current Chromium no longer
        has: measured here, Chromium from Playwright's current download
        answered it with "'Network.setRequestInterception' wasn't found" and
        the run died before its first navigation. pyppeteer's bundled
        Chromium, which still has the method, would not start on this
        machine at all. `Fetch.enable` with `handleAuthRequests` is what
        replaced it, and what Playwright itself uses.

        Every request is paused and continued unchanged; only an auth
        challenge is answered, and only with the proxy's credentials, which
        never leave this process's memory (§8).
        """
        async def _enable():
            session = await self.page.target.createCDPSession()

            def _paused(event):
                asyncio.ensure_future(session.send(
                    "Fetch.continueRequest", {"requestId": event["requestId"]}))

            def _auth(event):
                source = (event.get("authChallenge") or {}).get("source")
                response = ({"response": "ProvideCredentials",
                             "username": username, "password": password}
                            if source == "Proxy" else {"response": "Default"})
                asyncio.ensure_future(session.send(
                    "Fetch.continueWithAuth",
                    {"requestId": event["requestId"],
                     "authChallengeResponse": response}))

            session.on("Fetch.requestPaused", _paused)
            session.on("Fetch.authRequired", _auth)
            await session.send("Fetch.enable", {"handleAuthRequests": True,
                                                "patterns": [{"urlPattern": "*"}]})
            return session

        self._auth_session = self.bridge.run(_enable(), timeout=30)

    def _enable_autosolve(self):
        """The Scraping Browser API's own CAPTCHA domain, as the Playwright
        engine enables it."""
        async def _enable():
            # One coroutine for both calls: pyppeteer's CDPSession.send
            # returns a Future rather than a coroutine, and the bridge's
            # run_coroutine_threadsafe accepts only the latter (§26).
            session = await self.page.target.createCDPSession()
            await session.send("Captcha.setAutoSolve",
                               {"autoSolve": True, "options": [{"type": "*"}]})

        try:
            self.bridge.run(_enable(), timeout=30)
            logger.info("Scraping Browser API Captcha.setAutoSolve enabled.")
        except Exception as e:  # noqa: BLE001 — a non-Scraping-Browser endpoint
            logger.info("Captcha.setAutoSolve not available on this "
                        "--cdp-endpoint (%s) — relying on this script's own "
                        "detect+solve logic instead.", e)

    def _apply_fingerprint(self):
        """The SAME init script the other two engines install, so no engine
        applies a different half of one fingerprint."""
        from fingerprint_client import get_fingerprint, playwright_init_script
        fp = get_fingerprint(self.args.twocaptcha_key, tags=self.args.fp_tags,
                             country=self.args.fp_country)
        ua = (fp.get("userAgent") or {}).get("value")
        try:
            if ua:
                self.bridge.run(self.page.setUserAgent(ua))
            self.bridge.run(
                self.page.evaluateOnNewDocument(playwright_init_script(fp)))
            logger.info("Using 2captcha fingerprint %s (%s)", fp.get("id"),
                        fp.get("country"))
        except Exception as e:  # noqa: BLE001 — a fingerprint is not the run
            logger.warning("Could not apply the fingerprint (%s) — continuing "
                           "without it.", e)

    # ---- page_flow's operations -----------------------------------------

    def goto(self, url: str):
        try:
            resp = self.bridge.run(self.page.goto(
                url, waitUntil="domcontentloaded",
                timeout=page_flow.NAV_TIMEOUT_MS),
                timeout=page_flow.NAV_TIMEOUT_MS / 1000 + 30)
        except Exception as e:  # noqa: BLE001 — pyppeteer raises several types
            raise page_flow.TransportError(_mask_credentials(str(e))) from None
        return (resp.status if resp is not None else None), self.page.url

    def document_text(self) -> str:
        try:
            return self.bridge.run(self.page.content()) or ""
        except Exception:  # noqa: BLE001 — the document was being replaced
            return ""

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def solve_captcha(self) -> bool:
        return handle_captcha_if_present(self, self.args)

    def proxy_failure(self, text: str) -> str:
        return _proxy_failure(text)

    def relaunch(self):
        if self.remote:
            try:
                self.bridge.run(self.page.close(), timeout=30)
            except Exception:  # noqa: BLE001
                pass
            self.page = self.bridge.run(self.browser.newPage())
            return
        try:
            self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error while closing browser: %s", e)
        self.open()

    def close(self):
        """Close the page, and on a REMOTE browser disconnect too: closing
        only the page leaves the websocket open, and its unwinding prints
        tracebacks after the output is written. The remote BROWSER is left
        running; it is not ours."""
        try:
            if self.remote:
                self.bridge.run(self.page.close(), timeout=30)
                self.bridge.run(self.browser.disconnect(), timeout=30)
            else:
                self.bridge.run(self.browser.close(), timeout=30)
        except Exception as e:  # noqa: BLE001
            logger.debug("Ignoring error during browser teardown: %s", e)


def handle_captcha_if_present(ops, args) -> bool:
    """Solve a reCAPTCHA on the current document. Mirrors
    playwright_scraper.handle_captcha_if_present, which says why it exists
    on a site where no captcha has been observed."""
    try:
        html = ops.bridge.run(ops.page.content())
        url = ops.page.url
    except Exception:  # noqa: BLE001
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
        ops.bridge.run(ops.page.evaluate(INJECT_TOKEN_JS, token))
        time.sleep(1.5)
        ops.bridge.run(ops.page.reload(waitUntil="domcontentloaded",
                                       timeout=page_flow.NAV_TIMEOUT_MS),
                       timeout=page_flow.NAV_TIMEOUT_MS / 1000 + 30)
    except Exception as e:  # noqa: BLE001
        logger.error("Could not hand the token to the page (%s).",
                     _mask_credentials(str(e)))
        return False
    return True


def _fetch_pages_concurrently(args, pool, query, page_nums, concurrency: int,
                              currency_hint: Optional[str] = None):
    """Fetch `page_nums` across `concurrency` workers, each with its own event
    loop, browser and exit; the page loop is page_flow.worker_loop."""
    work = queue.Queue()
    for n in page_nums:
        work.put(n)
    results, results_lock = [], threading.Lock()
    exhausted = threading.Event()

    def worker(index: int):
        name = f"worker-{index + 1}"
        bridge = _AsyncBridge()
        try:
            ops = _Ops(bridge, args, page_flow.worker_pool(pool, index)).open()
            try:
                page_flow.worker_loop(ops, args, query, work, results,
                                      results_lock, exhausted, name,
                                      _mask_credentials, currency_hint)
            finally:
                ops.close()
        except Exception:  # noqa: BLE001 — a dead worker must not hang the run
            logger.exception("[%s] died; its pages will be reported as failed.", name)
        finally:
            bridge.close()

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
    bridge = _AsyncBridge()
    try:
        return page_flow.run_pages(
            lambda: _Ops(bridge, args, pool).open(),
            lambda ops: ops.close(),
            lambda pages, cur: _fetch_pages_concurrently(args, pool, args.query,
                                                         pages, concurrency, cur),
            args, pool, args.query, concurrency, _mask_credentials)
    finally:
        bridge.close()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="autotrader.com scraper — search results and vehicle "
                    "detail pages (pyppeteer edition)")
    add_search_args(p)
    add_common_args(p)
    p.add_argument("--chromium-path", default=None,
                   help="A Chrome/Chromium binary to drive instead of the one "
                        "pyppeteer downloads. That one is old (Chromium 1181205), "
                        "and a site behind a bot manager may score an old "
                        "browser; Playwright's Chromium works "
                        "(~/.cache/ms-playwright/chromium-*/chrome-linux/chrome).")
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
    except BrowserError as e:
        # The browser never started, which is this machine rather than the
        # site. Measured here: pyppeteer's own download closed on launch with
        # an empty message, and Playwright's Chromium started fine under
        # --chromium-path. Said plainly rather than as a traceback.
        logger.error("The browser did not start (%s). pyppeteer's own Chromium "
                     "is old and often will not run on a current system; point "
                     "--chromium-path at another one, e.g. Playwright's "
                     "(~/.cache/ms-playwright/chromium-*/chrome-linux/chrome).",
                     _mask_credentials(str(e)).strip() or "no reason given")
        return 1
    except RuntimeError as e:
        text = _mask_credentials(str(e))
        if "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            return EXIT_API_ERROR
        raise


if __name__ == "__main__":
    sys.exit(main())
