# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

## [0.1.1] — 2026-10-07

> **The listing mode works again.** From 2026-10-13 autotrader.com's results
> page no longer carries `__NEXT_DATA__`: it is a Next.js App Router page
> with the same records inside its `self.__next_f` flight chunks. 0.1.0
> classified that 2.4 MB served page as `unknown`, retried it three times and
> exited 5 ("the pages never arrived"); the daily canary was red on
> 2026-10-13 and 2026-10-14 for that reason.

Also in this release: fixes from a third-party audit of 0.1.0, each reproduced before it was
changed.

> **Behaviour change for `scraper_api_client.py --retries`.** It used to
> count EXTRA attempts (default 1 = two tries) while the browser engines'
> `--retries` counted ATTEMPTS (default 3 = three tries). It now counts
> attempts everywhere, so the client's default of 1 is one try. A refused
> page is still re-fetched once, so the most a refused page costs is
> unchanged: two tasks. And `--retries 0` is now refused (exit 2) in every
> engine, where it used to mean no attempt at all.

### Fixed

- **`--retries 0` made no request and reported "the pages were never
  obtained" (exit 5).** The page loop counts attempts. Reproduced offline:
  0 navigations, exit 5. Now exit 2 with the reason.
- **A moved page store read as an empty search, or as success.** Reproduced
  on the real fixture by renaming one key each:
  - `inventory` → exit 4, "the search matched nothing", beside the site's
    own count of 558. Now `parser_found_nothing`: exit 5 on page 1, exit 6
    (partial, page listed as failed) later on.
  - `vin` → exit 0, VIN empty on every row, one warning in the log.
  - `pricingDetail` → exit 0, every price empty, no warning at all.

  The sidecar now carries `field_coverage` and `fields_below_floor`, the log
  says it as an ERROR, and the canary FAILS on it. Floors: 99% for the
  columns every captured record had, 25% for `price` (the emptiest page
  measured was 64%, under price-desc and year-desc, where "Contact Dealer
  For Price" listings sort first).
- Results pages are read from the App Router flight payload when there is
  no `__NEXT_DATA__`. The records are the same (`inventory`, `owners`), so
  every column is read as before; `data_source` is `next_flight` on those
  rows. Replayed on the canary's captures of 2026-10-13 and 2026-10-14: 25
  of 25 rows each, VIN/price/seller on every row, the site's count (417 and
  410) and 12 pages read. `__NEXT_DATA__` is still read first.
- The Scraper API client's default wait was the text `__NEXT_DATA__`, which
  a served results page no longer carries; it now waits for the site's own
  asset path, `/cars-for-sale/_next/static/`.

### Changed

- **The Scraper API client runs the shared page loop** instead of its own
  copy of the pagination, the status triage and the sidecar. It now gets
  the widened-search check, the served-page check, `gone`, and the same
  sidecar as the browser engines. It does not poll an unknown document for
  repaints, since nothing can repaint it.
- **The sidecar says how the pages were fetched** (`fetch`: engine, headful
  or not, CDP or not, the proxy's host and port without credentials).

### Measured

- Scraper API, its own exits: refused 1 of 1 on 2026-09-24; on 2026-10-07,
  served 1 of 4 (25 rows) and answered HTTP 408 to the other 3 (sync wait
  exceeded, not billed).
- The canary since the proxy secret was set: 11 of 12 green, the red one
  three navigation timeouts at the exit.

### Not verified

- Vehicle (detail) pages: no capture in the new shape exists yet. If a
  detail page has moved too, it reads as `unknown` (exit 5), never as a
  gone listing.

### Not changed, and why

- **A preflight health check before the run** — page 1 already is that
  probe: it is fetched alone, a refusal stops the run after one fresh-browser
  retry, and nothing else is fetched.
- **Lock files / upper bounds** — the daily canary installs current
  releases and is the test of them; pinning would switch that off.
- **Checkpoint / resume, JSONL sinks, a package layout** — family-wide
  product decisions (a shared sink interface is still pending),
  not something to grow in one repo. A run that fails after gathering rows
  already writes them (exit 6).
- **A JSON-LD or DOM fallback parser** — the JSON-LD on a results page
  describes three paid placements, not the results, and the DOM is built
  from the same store; a fallback would return the wrong cars confidently.
- **Adapters for, or a benchmark against, other vendors' scraping APIs** —
  out of scope for this family, which integrates 2Captcha's products only.

## [0.1.0] — 2026-09-24

First release. Two modes over autotrader.com's own page store, three browser
engines over one shared page loop, and the 2Captcha Scraper API client for
both modes.

> **Before your first run:** this site serves its pages only to a **US
> residential exit** with a **headful** browser (measured 2026-09-24; the
> README has the table). The engines are headful by default and want
> `xvfb-run` on a server, and a run needs `--proxy` or `AUTOTRADER_PROXY`.

### Added

- `--mode listing`: one search's organic results, one row per car (50
  columns): price with fees, pre-fee price and fees, MSRP, KBB fair price
  and range, deal rating, VIN, year/make/model/trim, condition, mileage,
  body, colours, fuel, drivetrain, engine, transmission, mpg, days on site,
  stock number, photo, and the dealer or private seller with rating,
  address and distance.
- `--mode vehicle`: one detail page per listing id (56 columns): everything
  above plus features, the seller's description, open recalls, KBB owner
  reviews and the photo count. Ids from `--vehicle-id`, a detail `--url`, or
  `--from-listing` with a listing run's JSON.
- A search from `--url` (with the site's own filters kept) or from
  `--make/--model/--zip/--radius/--listing-type`; `--sort` over the seven
  orderings the site was measured to honour; `--page-size 25|100`.
- The sidecar records the site's own count, the pages it serves, the
  300-result cap (`capped_by_site`, `reachable_max`), the search the site
  says it ran (`site_query`), and in vehicle mode the listings that have
  gone (`vehicles_gone`).

### Measured, and built in

- **Headful and US residential, both.** Headless (old and new) refused from
  a US residential exit; headful refused from a datacentre. Headful is the
  default, and a missing display is said before any launch.
- **A slug the site does not know widens the search silently**
  (`f-150` → every used Ford, HTTP 200). The served path is compared with
  the requested one; a widened search writes nothing, exit 2.
- **The site serves 300 results per search**, and a page past the end is
  its LAST page re-served. The run ends on the page number the site says it
  rendered.
- **Paid placements are not rows**; the page's JSON-LD describes three of
  them rather than the results. `premium_spotlight` marks a result that is
  also paid.
- **`price` needs a real asking price.** A no-price listing's display price
  is its MSRP; `salePrice` means different things on different dealers and
  is not a column. A KBB `0` is null.
- **A sold listing is `gone`**, not 25 cars from the unrelated results page
  the site redirects to.
- **The refusal page's reCAPTCHA files an unblock request with the site's
  staff**, so that page is `blocked` and never solved.
- **pyppeteer's `page.authenticate` does not work with current Chromium**
  (`Network.setRequestInterception` was removed), and its bundled Chromium
  would not start here. The engine answers the proxy through the CDP
  `Fetch` domain, and `--chromium-path` drives Playwright's Chromium.
- **No engine sets a User-Agent.** The browser's own was served.

### Tests

- 360-odd offline checks on fixtures cut from real pages by
  `make_fixtures.py`, which proves each parses identically to its original
  and refuses to write one that still carries the requesting address, a
  session id or a private seller's id.
- Ten planted faults, each confirmed to turn the suite red on the check
  that names it. Two did not on the first attempt, and the suite was
  changed rather than the prediction: a placement check that derived the
  placements from the rows (so it emptied exactly when the filter broke),
  and an end-to-end vehicle run whose sold car came first, where the loop's
  end-of-search check never looks.
