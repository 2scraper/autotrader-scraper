# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
follows [Semantic Versioning](https://semver.org/) as closely as a CLI
toolkit can: a patch release means **fixes**, not that every flag and
default is frozen. A default that changes behaviour for an existing user is
said so at the top of its release notes.

## [Unreleased]

> **The listing mode works again.** From 2026-10-13 autotrader.com's results
> page no longer carries `__NEXT_DATA__`: it is a Next.js App Router page
> with the same records inside its `self.__next_f` flight chunks. 0.1.0
> classified that 2.4 MB served page as `unknown`, retried it three times and
> exited 5 ("the pages never arrived"); the daily canary was red on
> 2026-10-13 and 2026-10-14 for that reason.

### Fixed

- Results pages are read from the App Router flight payload when there is
  no `__NEXT_DATA__`. The records are the same (`inventory`, `owners`), so
  every column is read as before; `data_source` is `next_flight` on those
  rows. Replayed on the canary's captures of 2026-10-13 and 2026-10-14: 25
  of 25 rows each, VIN/price/seller on every row, the site's count (417 and
  410) and 12 pages read. `__NEXT_DATA__` is still read first.
- The Scraper API client's default wait was the text `__NEXT_DATA__`, which
  a served results page no longer carries; it now waits for the site's own
  asset path, `/cars-for-sale/_next/static/`.

### Not verified

- Vehicle (detail) pages: no capture in the new shape exists yet. If a
  detail page has moved too, it reads as `unknown` (exit 5), never as a
  gone listing.

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
