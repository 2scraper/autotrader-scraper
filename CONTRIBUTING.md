# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

autotrader.com changing its pages is the normal way this stops working, and
it has its own issue template. The parser reads no markup and no JSON-LD
(the JSON-LD on a results page describes three PAID placements, not the
results): every row comes out of the page's own Next.js store,
`__NEXT_DATA__` → `props.pageProps.__eggsState`. So there are four things
that can break, and each is loud or guarded:

1. **The store's slices.** `srp_results.activeResults` (the organic ids),
   `inventory` (one record per id), `owners`. A page whose store has no
   `srp_results` classifies as `unknown` and is retried, then reported.
2. **A record's own field names** (`pricingDetail.displayPrice`,
   `mileage.value`, `vin`, ...). This is the one that can be QUIET: the row
   still writes, with that column null. `page_flow.CORE_FIELDS` is the
   guard, a coverage floor of 99% on the columns every captured record
   carried.
3. **The page no longer being server-rendered.** A results page whose store
   arrives client-side would read as `unknown`; the readiness poll
   (`page_flow.READY_WAIT_MS`) covers a slow document, not a missing one.
4. **The gate moving.** Today it is "US residential AND headful". If a run
   with both is refused, that is the report.

If you are reporting a break, say which of those four it is, and attach the
one inventory record that is wrong, NOT the whole `--dump-html` output: a
served page carries the address that fetched it (`clientIp`).

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once.
   It needs the `AUTOTRADER_PROXY` secret (a US residential exit), because
   the site refused every datacentre address measured; without the secret
   it skips with a notice rather than going red. Dispatch it once both
   ways, and confirm the branch you expect ran.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions. Its fixtures are real pages, trimmed and scrubbed, in
`fixtures_generated.json`, which `make_fixtures.py` regenerates from a
capture directory and proves parse identically to their originals. Copy the
nearest existing check and edit it.

Seven properties in this repo exist because they were measured against
expectation and cost real time. Tests pin all seven, so a PR that breaks one
fails rather than silently regressing:

- **The site wants a US residential exit AND a headful browser.** Headless
  was refused from a residential address, headful from a datacentre one.
  So headful is the default, and the CLI says up front when there is no
  display to run it in.
- **A slug the site does not know WIDENS the search, silently.**
  `/used-cars/ford/f-150/…` came back as every used Ford, HTTP 200. Page 1's
  served path is compared with the requested one, and a widened search
  writes nothing (exit 2).
- **Placements are not rows.** A results page carries paid spotlight cars
  beside the results, in the same `inventory`, and its JSON-LD describes
  them. Only `srp_results.activeResults` become rows.
- **`price` is what the tile shows, fees included**, and needs a real asking
  price behind it. A "Contact Dealer For Price" listing carries its MSRP as
  its display price; that is `msrp`, not `price`. `salePrice` means
  different things on different dealers, so it is not a column.
- **Past the end, the site re-serves its LAST page.** `?page=99` of a
  12-page search answered with page 12. The response states which page it
  rendered, and that is what ends a run, not the page number asked for.
- **A sold listing's detail page is a results page for somewhere else.**
  Classified `gone`, no row, and the run carries on.
- **The refusal page carries a reCAPTCHA that is not a way in.** It guards a
  form filing an unblock request with the site's staff. The page is
  classified `blocked`, which is never solved.

Plus the family's own invariants, which are not negotiable:

- **A run that finds nothing writes nothing.** It must not replace a good
  output file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked, `4` zero rows — including a query that genuinely
  matched nothing, which is a correct answer — `5` the pages were never obtained (a dead
  proxy, a timeout, a remote API error), `6` partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A query
  that matched nothing was served exactly as asked.
- **Credentials never reach argv or a log, and an exception message is a
  log.** The masker is global rather than first-occurrence: a Playwright
  connection error repeats the endpoint five times.
- **Merge in page order, not arrival order**, so concurrency cannot change
  the output.

### If your change needs a live run

Most do not: the suite covers the parser, the writers, the classifier and
the CLI contract against real, trimmed pages. If yours genuinely needs
autotrader.com, say in the PR what you ran (engine, mode, search), from
which kind of exit, and what you got, including the sidecar's
`total_results`.

Two things about running this live that are specific to this site:

* **You need a US residential exit and a display.** On a server, run the
  engine under `xvfb-run -a`. Selenium cannot authenticate a proxy, and a
  residential proxy is nearly always a credentialled one, so test with
  Playwright or pyppeteer unless your exit authorises by source IP.
* **A search is capped at 300 results** however many matched. A run that
  "only" got 300 of 2,000 is complete; the sidecar says it is a sample.

**Run more than the primary engine.** "Mirror them exactly" is a design
rule, not a verification. The fetch loop is shared (`page_flow.run_pages`),
but each engine's driver plumbing is its own, and only running it proves it.

## Scope

This repo reads **public data** on autotrader.com: search results and
vehicle detail pages, exactly as the site renders them for an anonymous
visitor.

Out of scope: anything behind a login, anything that contacts a seller,
submits a lead, a credit application, a trade-in or any other form —
including the unblock-request form on the site's refusal page — and
anything that defeats a protection rather than being served the way an
ordinary browser is.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
