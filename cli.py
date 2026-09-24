"""
cli.py
------
The command line every engine shares: what to scrape, how, and what to do
after parsing. One definition, so the three CLIs cannot drift apart (§17's
flag-parity check in smoke_test.py still asserts it).

No browser library is imported here, so the Selenium and pyppeteer engines
can use it without Playwright installed.
"""

import os
import sys
from typing import Optional

import env_config
import page_flow
from product_parser import LISTING_TYPES, PAGE_SIZES, RADII, SORTS
from proxy_pool import ROTATE_MODES


def display_problem(args) -> Optional[str]:
    """Why a headful launch cannot work here, or None.

    Headful is the default because headless was refused on every
    measurement, and a Linux server without a display fails the launch with
    a long Playwright traceback about a missing X server. Said once, up
    front, with the fix.
    """
    if args.headless or args.cdp_endpoint:
        return None
    if not sys.platform.startswith("linux"):
        return None
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return None
    return ("This engine runs a HEADFUL browser by default, because "
            "autotrader.com refused headless Chromium on every measurement, "
            "and there is no display here ($DISPLAY is unset). Run it under a "
            "virtual one: xvfb-run -a python %s ... (apt install xvfb). "
            "--headless runs anyway, and will most likely be refused."
            % os.path.basename(sys.argv[0] or "playwright_scraper.py"))


def add_search_args(p):
    """The flags every engine takes to say WHAT to scrape. Shared so the
    three CLIs cannot drift (§17's flag-parity check still asserts it)."""
    p.add_argument("--mode", choices=["listing", "vehicle"], default=None,
                   help="listing (default): one search's organic results. "
                        "vehicle: one detail page per listing id. Inferred "
                        "from --url when that is given.")
    p.add_argument("--url", default=None,
                   help="An autotrader.com search URL as the site builds it "
                        "(www.autotrader.com/cars-for-sale/...), with any of "
                        "the site's own filters in its query string; or a "
                        "/cars-for-sale/vehicle/{id} detail page. Also read "
                        "from AUTOTRADER_URL.")
    g = p.add_argument_group("search (instead of --url)")
    g.add_argument("--make", default=None,
                   help="The make's URL slug, as in the site's own URLs "
                        "(toyota, land-rover).")
    g.add_argument("--model", default=None,
                   help="The model's URL slug (camry, f150, cr-v). A slug the "
                        "site does not recognise is not an error there: it "
                        "silently widens the search to the whole make. This "
                        "run compares the served path and refuses to write "
                        "a widened result.")
    g.add_argument("--zip", default=None, help="Five-digit US ZIP code.")
    g.add_argument("--radius", type=int, default=None, choices=RADII,
                   metavar="MILES",
                   help="Search radius in miles: %s (0 = any distance)."
                        % ", ".join(map(str, RADII)))
    g.add_argument("--listing-type", choices=list(LISTING_TYPES), default=None,
                   help="all (default), used, new or certified.")
    p.add_argument("--category", default=None,
                   help="Not a separate option on this site: a body style is "
                        "part of the search URL (…/cars-for-sale/suv/…). "
                        "Accepted for the family's CLI contract and refused "
                        "with that explanation.")
    p.add_argument("--sort", choices=list(SORTS), default=None,
                   help="Ordering (default: the site's own, relevance). Not "
                        "cosmetic: the site serves 300 cars of a search, so "
                        "the ordering decides WHICH cars are in the file. "
                        "relevance puts more paid 'premium spotlight' "
                        "listings near the top (a column on every row).")
    p.add_argument("--page-size", type=int, choices=PAGE_SIZES, default=None,
                   help="Results per page: 25 (the site's default) or 100. "
                        "100 is a quarter of the navigations.")
    g = p.add_argument_group("vehicle mode")
    g.add_argument("--vehicle-id", action="append", default=None, metavar="ID",
                   help="A listing id (the number in /cars-for-sale/vehicle/"
                        "{id}). Repeatable.")
    g.add_argument("--from-listing", default=None, metavar="JSON",
                   help="A listing run's JSON output: one detail page per "
                        "row, in its order.")


def add_common_args(p, out_default="autotrader_rows"):
    p.add_argument("--pages", type=int, default=1,
                   help="Listing mode: results pages to fetch (25 or 100 cars "
                        "each). Planned against the page count the site states "
                        "on page 1, which is 12 at most. Vehicle mode fetches "
                        "every id given and ignores this.")
    p.add_argument("--delay", type=float, default=2.0,
                   help="Delay between pages, seconds (default %(default)s)")
    p.add_argument("--concurrency", type=int, default=1, metavar="N",
                   help="Fetch pages through N parallel workers (default 1). "
                        "Each worker runs its own headful browser and holds "
                        "its own proxy exit. Ignored with --cdp-endpoint.")
    p.add_argument("--retries", type=int, default=3,
                   help="Attempts per page on a transport failure (default 3). "
                        "The pause doubles each time.")
    p.add_argument("--retry-delay", type=float, default=3.0,
                   help="Seconds before the first retry, doubling thereafter.")
    p.add_argument("--format", choices=["json", "csv", "both"], default="both")
    p.add_argument("--out", default=out_default, help="Output file prefix")
    p.add_argument("--locale", default="en-US",
                   help="Browser locale (default en-US). The site is in "
                        "English whatever the browser claims.")
    p.add_argument("--proxy", default=None,
                   help="Proxy URL, e.g. http://ACCOUNT:PASSWORD@HOST:9999 "
                        "(2captcha.com/proxy). This site wants a US "
                        "RESIDENTIAL exit.")
    p.add_argument("--proxy-file", default=None,
                   help="File with one proxy URL per line to rotate across. "
                        "Wins over --proxy.")
    p.add_argument("--proxy-rotate", choices=list(ROTATE_MODES), default="per-run",
                   help="per-run (default): one exit for the whole run. "
                        "per-page: a new exit, and a fresh browser, per page.")
    p.add_argument("--proxy-shuffle", action="store_true",
                   help="Shuffle the pool at startup.")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="When a page is refused, retry it from this many OTHER "
                        "exits (default 2). Needs a pool of more than one.")
    p.add_argument("--twocaptcha-key", default=None, help="2captcha.com API key")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write output files even when 0 rows were found.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Apply a browser fingerprint from 2captcha's "
                        "Fingerprint API. Needs --twocaptcha-key. Ignored with "
                        "--cdp-endpoint. Not needed on this site: the browser's "
                        "own identity was served.")
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag for the fingerprint filter: "
                        "Windows, Microsoft Windows or Android. NOT a list — "
                        "Chrome, Desktop and Mobile are each rejected by the "
                        "API with 400. (default: Windows)")
    p.add_argument("--fp-country", default=None,
                   help="Fingerprint country, ISO 3166-1 alpha-2. Match it to "
                        "your proxy's exit country (US).")
    p.add_argument("--captcha-api", choices=["v2", "v1"], default="v2",
                   help="Which 2captcha solver API to use (v2: createTask).")
    p.add_argument("--solve-captcha", choices=["when-blocked", "always"],
                   default="when-blocked",
                   help="when-blocked (default): solve a reCAPTCHA only when it "
                        "stands between the run and the page. No captcha has "
                        "been seen on this site, so 'always' behaves the same.")
    p.add_argument("--min-score", type=float, default=0.7,
                   help="reCAPTCHA v3 minimum score (0.3, 0.7 or 0.9).")
    p.add_argument("--cdp-endpoint", default=None,
                   help="Connect to an already-running browser over CDP "
                        "instead of launching Chromium, e.g. the Scraping "
                        "Browser API endpoint ws://user:pass@host:port with a "
                        "country-us segment. --proxy and --headless/--headful "
                        "are ignored.")
    p.add_argument("--dump-html", default=None, metavar="PATH",
                   help="Save the exact document the parser is given, on "
                        "success as well as failure.")
    p.add_argument("--headless", action="store_true", default=False,
                   help="Run the browser headless. NOT the default: the site "
                        "refused headless Chromium on every measurement.")
    p.add_argument("--headful", dest="headless", action="store_false",
                   help="Run the browser with a window (the default). Needs a "
                        "display; use xvfb-run on a server.")


def finish_args(p, args):
    """Everything after parsing that the three engines do identically."""
    env_config.apply(args)
    if args.category is not None:
        p.error("--category is not a separate option on autotrader.com: a body "
                "style is part of the search URL itself (e.g. "
                "/cars-for-sale/suv/denver-co). Build the search on the site "
                "and pass it as --url.")
    args.query = page_flow.build_query(args, p.error)
    args.mode = args.query.mode
    return args
