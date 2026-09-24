# Troubleshooting

Find your exit code first (`echo $?` straight after the run), then the
sidecar's `stop_reason` in `<out>.meta.json` if one was written.

## Exit 2 — bad usage

* **"The site did not recognise 'f-150' and served a WIDER search"** — the
  slug is not one the site knows, and the site answers that by quietly
  dropping it: `/used-cars/ford/f-150/…` came back as every used Ford. The
  run writes nothing rather than a file of the wrong cars. Take the slug
  from the site's own URL for that model (`f150`).
* **"This engine runs a HEADFUL browser by default … $DISPLAY is unset"** —
  run it under a virtual display: `xvfb-run -a python playwright_scraper.py …`
  (`apt install xvfb`). `--headless` runs anyway and is refused by the site.
* **"--url already carries the search"** — pass a URL or the search flags,
  not both. Merging them could scrape something neither named.
* **"--sort X is not one of …"** / **"the URL's sortBy=… is not one this repo
  has verified"** — the site answers an unknown ordering with a full page
  in an order of its own choosing, so it is refused before anything is sent.
* **"is AutoTrader.ca (Canada), a different company"** — this repo reads
  autotrader.com only.
* **"The browser did not start"** (pyppeteer) — its own Chromium is old; pass
  `--chromium-path` to Playwright's
  (`~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome`).

## Exit 3 — blocked

The log names what refused the run:

* **`akamai-unavailable`** — the site served its "page unavailable" page
  (HTTP 200, 3.7 KB) instead of the one asked for. Measured 2026-09-24, it
  does that unless BOTH of these hold:
  1. a **US residential** exit (`--proxy`, or `AUTOTRADER_PROXY` in `.env`);
  2. a **headful** browser (the default; under `xvfb-run` on a server).

  Headless Chromium was refused from a US residential address, and headful
  Chromium from a datacentre one. If the log says the run was HEADLESS, that
  is the first thing to change.

  That page carries a reCAPTCHA, and it is not a way in: it guards a form
  that files an unblock request with the site's support staff. This repo
  never solves or submits it.
* **`akamai-denied`** — Akamai's generic "Access Denied" page. Not seen on
  this site; treat it like the one above.

A `<out>_page<N>_debug.html` beside the output holds what came back.

## Exit 4 — zero rows

The search matched nothing (the site's own count said 0), or every vehicle
id asked for has gone. Nothing is written, so an earlier good file is left
alone; `--allow-empty` writes the empty file.

## Exit 5 — the pages never arrived

* **"Gave up on page N"** — a navigation timeout or a dead proxy. The log
  names which. A residential exit can be slow: a 1.8 MB results page took
  3 to 16 seconds through one on 2026-09-24.
* **"could not connect to --cdp-endpoint … HTTP 401"** — a Scraping Browser
  profile's credentials last about a day. Get a fresh endpoint.
* **"… HTTP 500"** — another run still holds that profile (`pid`).

## Exit 6 — partial

Some pages came back and a later one did not. The output holds what was
gathered and the sidecar lists `pages_failed` by number.

## "Vehicle N is no longer listed"

The site answers a detail page for a sold listing by redirecting to a
results page for some other place (`redirectExpiredPage=1`). The run notes it
in the sidecar's `vehicles_gone` and carries on; it is not a failure.

## Only 300 cars from a search that matched thousands

The site serves 300 results of any one search (12 pages of 25, or 3 of
100), whatever it matched. The sidecar says so (`capped_by_site`,
`reachable_max`). Split the search — by ZIP and radius, price, year or
listing type — to reach more.

## A run looks fine but a column is wrong

Re-run with `--dump-html page.html`: it writes the exact document the parser
was given, on success too, so a parsing bug can be told apart from a change
in what the site sends.

## Duplicates dropped, or a car missing between two runs

A search is live. A car that moves across a page boundary while a run reads
the pages is fetched twice (the dedupe drops it and the log says so), or not
at all. That is the site moving, not the scraper.
