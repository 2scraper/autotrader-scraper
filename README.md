# autotrader-scraper

[![release](https://img.shields.io/github/v/release/2scraper/autotrader-scraper)](https://github.com/2scraper/autotrader-scraper/releases)
[![tests](https://github.com/2scraper/autotrader-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/autotrader-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/autotrader-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/autotrader-scraper/actions/workflows/canary.yml)
![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
![engines](https://img.shields.io/badge/engines-playwright%20%7C%20selenium%20%7C%20pyppeteer-lightgrey)
![needs](https://img.shields.io/badge/needs-a%20US%20residential%20exit%20%2B%20a%20headful%20browser-orange)

Scrapes [autotrader.com](https://www.autotrader.com) (United States) into
JSON and CSV:

| `--mode` | what | one row per | columns |
|---|---|---|---|
| `listing` (default) | one search's **organic results**: price (fees included), the pre-fee price and the fees, MSRP, the KBB fair price and range, the site's deal rating, VIN, year/make/model/trim, mileage, colours, drivetrain, mpg, and the dealer with its rating, address and distance | car | 50 columns |
| `vehicle` | one **detail page** per listing id: all of the above, plus every listed feature, the seller's description, open recalls, KBB owner reviews and the photo count | car | 56 columns |

A search is given as the site builds it (`--url`, with any of its own
filters), or with `--make/--model/--zip/--radius/--listing-type`. Vehicle
mode takes `--vehicle-id`, a detail-page `--url`, or `--from-listing` with a
listing run's JSON.

Every run writes a `<out>.meta.json` beside the output with the site's own
count, so a file can say "300 of 76,704" rather than only "300".

---

## Start with the part most scrapers bury

**This site needs two things, both, and neither is optional.** Measured
2026-09-24; every row asked for the same results page:

| client | from | answer |
|---|---|---|
| curl, curl's own User-Agent | datacentre (Hetzner, FI) | "page unavailable" |
| curl, a Chrome User-Agent | datacentre | "page unavailable" |
| curl | **US residential** proxy | "page unavailable" |
| headless Chromium | datacentre | "page unavailable" |
| headless Chromium | US residential | "page unavailable" |
| Chromium's new headless mode (full Chrome) | US residential | "page unavailable" |
| **headful** Chromium | datacentre | "page unavailable" |
| **headful** Chromium | **US residential** | **served**: 1.6-3.1 MB, the full results |
| the 2Captcha Scraper API, its own exits | — | "page unavailable" |
| one Scraping Browser profile, `country-us` | — | "page unavailable", 2 of 2 |

"Page unavailable" is a 3.7 KB static page from Akamai, served **under HTTP
200** in place of whatever was asked for, including the site's own JSON
endpoint. The status says nothing, so the engines read the document.

So:

* the engines run **headful by default**. On a server that means a virtual
  display: `xvfb-run -a python playwright_scraper.py …`. Without one, the
  engine says so before launching anything.
* you want a **US residential proxy** (`--proxy`, or `AUTOTRADER_PROXY` in
  `.env`). A 2Captcha residential login with `-region-us` is what was used.
  A US home connection of your own was not tested.
* The Scraping Browser row is **one profile on one day**. It was refused,
  and this repo has not found out what the site keys on in that browser. It
  is recorded, not concluded from.

No captcha stood in front of any page. The refusal page carries a reCAPTCHA
of its own, and it is **not a way in**: it guards a form that files an
unblock request with the site's support staff. This repo never solves or
submits it.

---

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
.venv/bin/playwright install chromium
sudo apt install xvfb        # on a machine with no display
cp .env.example .env         # and set AUTOTRADER_PROXY
```

Install exactly one engine per virtualenv: the three pin mutually
incompatible dependencies.

## Run

```bash
# one search, three pages of 25
xvfb-run -a .venv/bin/python playwright_scraper.py \
  --url "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny" \
  --pages 3

# the same kind of search from flags; 100 a page is a quarter of the navigations
xvfb-run -a .venv/bin/python playwright_scraper.py \
  --listing-type used --make ford --model f150 --zip 90012 --radius 50 \
  --page-size 100 --pages 3 --sort price-asc

# the detail page of every car the listing run found
xvfb-run -a .venv/bin/python playwright_scraper.py --from-listing autotrader_rows.json
```

Measured through one US residential exit, Playwright, 2026-09-24: three
pages of 25 in 22 s; all 300 reachable cars of a search in 19 s at
`--page-size 100 --concurrency 2`; a detail page in 2-8 s.

Sample output, cut from real runs: [`sample_output.json`](sample_output.json)
(listing), [`sample_output_vehicle.json`](sample_output_vehicle.json)
(vehicle), and the `.csv` of each.

---

## Six things about autotrader.com that will look like bugs

### 1. A search stops at 300 cars

The site serves **300 results of any one search**, however many matched:
12 pages of 25, or 3 of 100. A Camry search in New York matched 558; a
search for every car near Beverly Hills (`?zip=90210`) matched 76,704; each
serves 300. A run that
fetched them all is `status: complete` and a sample. The sidecar says so
(`total_results`, `pages_available`, `reachable_max`, `capped_by_site`).
Split the search, by ZIP and radius, price, year or listing type, to reach
more.

Asked for a page past the end, the site does not fail: it **redirects to
its last page**. The page's own store says which page it rendered, and that
is what ends a run here, not the page number asked for.

This repo does not implement the site's JSON search endpoint
(`/rest/lsc/listing`), which answers only inside a served browser session.
Measured: it goes past the 300 (`firstRecord=300` returned 25 more), stops
somewhere before the end (`firstRecord=550` of 558 returned `{}`), and
orders results differently from the page, mixing in paid placements.

### 2. A slug the site does not know widens the search, silently

`/used-cars/ford/f-150/los-angeles-ca` came back as **every used Ford in
Los Angeles** (2,284 of them), HTTP 200, no error: the model slug is
`f150`. The engines compare the path they asked for with the path the site
served, and a widened search writes nothing (exit 2) with both paths in the
message. The site's own reading of the search is in the sidecar
(`site_query`), in its codes (`F150PICKUP`).

### 3. `price` is what the tile shows, and it includes the dealer's fees

`price` is the displayed price; `price_before_fees` and `dealer_fees` are
beside it. Across 476 records measured, the displayed price was the pre-fee
price plus the fees on 452 of 453 priced ones. A listing with no asking
price ("Contact Dealer For Price") carries its **MSRP** as its displayed
price; there `price` is null and `msrp` holds it. A KBB fair price the site
publishes as `0` (new cars it does not value) is null, not zero.

### 4. Paid placements are not rows, and the default ordering favours them

A results page carries paid "spotlight" cars beside the results, in the same
data, and its JSON-LD describes three of them rather than the results. Only
the organic results become rows. A result that is ALSO a paid "premium
spotlight" keeps its row and says so in `premium_spotlight`: on one search,
6 of the first 25 under the site's default `relevance` ordering, against 0
to 2 under every other one. `--sort` is a column on every row, and
`diff_runs.py` refuses to compare two runs sorted differently: under a
300-car cap, the ordering decides which cars are in the file.

### 5. A sold car's detail page is a results page for somewhere else

Asked for a listing that has gone, the site redirects to a results page for
some other location (`redirectExpiredPage=1`): Orangeburg, SC on one try,
Keller, TX on the next. Vehicle mode names it `gone`, writes no row for it,
lists it in the sidecar's `vehicles_gone`, and carries on.

### 6. A detail page states no currency and no distance

A results page states its currency once, in its JSON-LD (`USD`). A detail
page states none anywhere, so a vehicle row's `currency` is null rather than
assumed. Its `distance_miles` is null too: the figure a detail page carries
is measured from somewhere the site chose (1,395.7 miles for a car 3.15
miles from the searched ZIP).

---

## Engines

| | |
|---|---|
| `playwright_scraper.py` | **Primary.** Authenticates a proxy and a remote CDP endpoint. |
| `puppeteer_scraper.py` | pyppeteer is effectively unmaintained; here for parity. Its own Chromium would not start on the test machine, so point `--chromium-path` at Playwright's. Its built-in proxy authentication does not work with current Chromium (the CDP method it uses was removed), so this engine answers the proxy's challenge through the CDP `Fetch` domain itself. Slower: every request is paused and continued. |
| `selenium_scraper.py` | Drives the Chrome you already have. **Cannot authenticate a proxy** (`--proxy-server` takes an address only) or a remote CDP endpoint. On this site that matters: a US residential proxy is nearly always credentialled, so this engine works from a US residential connection of its own, or through a proxy that authorises by source IP. |
| `scraper_api_client.py` | No local browser: the 2Captcha Scraper API fetches the page, both modes. Its own exits were refused (above, $0.0005 a task); `--cdp-url` routes it through a Scraping Browser session instead. |

The page loop (navigation, the readiness poll, retries, rotation, parsing,
the widened-search check, the end of a search) is one implementation in
`page_flow.py` that all three browser engines drive, so they cannot disagree
about a page. All three were run live on 2026-09-24 in both modes, through
a US residential exit (Selenium through a local, credential-free forwarder
to the same exit), and produced identical rows for the same cars.

---

## What the 2Captcha products buy, and when

One key, four separately-billed products ([2captcha.com](https://2captcha.com)):

* **Proxies** (`--proxy`, `--proxy-file`): the one this site needs. A US
  residential exit is the difference between a served page and the refusal.
  A pool spreads the volume; `--concurrency` without one sends N times the
  requests from one address.
* **The Scraping Browser API** (`--cdp-endpoint`): a remote browser you do
  not run, with a chosen exit country. One `country-us` profile was refused
  on 2026-09-24 (2 of 2); that is the measurement, not a verdict on the
  product. One live connection per `pid`, so `--concurrency` is refused with
  it.
* **Captcha solving**: none stood in front of this site's pages, so a
  normal run buys nothing. The reCAPTCHA path is kept as insurance for a
  page that one day carries one. This repo does not implement Akamai's own
  behavioural challenge, which was not seen either.
* **Fingerprints** (`--fingerprint`): a consistent device identity for a
  local browser. Not needed here: the unmodified browser was served. No
  engine sets a User-Agent of its own.

Nothing here integrates a competitor.

---

## Exit codes

| | |
|---|---|
| 0 | rows written |
| 1 | crash |
| 2 | bad usage, including a search the site WIDENED (a slug it does not know) |
| 3 | blocked: the site's "page unavailable", distinct from an empty search |
| 4 | zero rows: the search matched nothing, or every vehicle asked for has gone |
| 5 | the pages were never obtained: a timeout, a dead proxy, a remote API error |
| 6 | partial: some pages came back and some did not |

**A run that finds nothing writes nothing**, so a failure never replaces last
night's good output with `[]`. `--allow-empty` is the opt-out.

`diff_runs.py --old a.json --new b.json` compares two runs of the same mode
and search by `sku`: cars that appeared, vanished (sold, or below the
300-car cut), or changed price, mileage or deal rating.

---

## Configuration

Credentials live in `.env` next to the scripts, never on a command line.
Copy [`.env.example`](.env.example) and fill in what you use;
`python3 env_config.py` prints what was picked up **without printing
secrets**. Precedence: explicit flag → exported environment variable →
`.env` → default.

---

## Tests

```bash
python3 smoke_test.py          # offline, no network, no engine needed
python3 smoke_test.py -v       # every check as it passes
pytest                          # the same suite, one test
```

The fixtures are real pages, cut down to the parts of the site's store the
parser reads and scrubbed of the address and session that fetched them by
`make_fixtures.py`, which proves each one parses identically to its
original. The suite drives the shared page loop end to end with a fake
browser: a three-page search that runs past its end, a refusal, an empty
search, a timeout, a widened search, and a vehicle run with a sold car in
the middle. Each of ten planted faults was confirmed to turn it red on the
check that names it.

The [canary](.github/workflows/canary.yml) runs a real 3-page search and
three detail pages daily, **when the repo has an `AUTOTRADER_PROXY`
secret**. Without one it skips with a notice rather than going red: every
datacentre address measured was refused, and a GitHub runner is one.

---

## Legal

This reads **public data**: search results and vehicle detail pages, as the
site renders them for an anonymous visitor. It contacts no seller, submits
no lead, credit application or other form (including the unblock form on
the refusal page), and reads nothing behind a login. A private seller's
phone number, which the site hides, is never written, and neither is their
personal id.

Rate limits, terms of service and the legality of scraping in your
jurisdiction are your responsibility as the operator. `--delay` defaults to
2 seconds between pages.

MIT licensed. Not affiliated with or endorsed by Autotrader.
