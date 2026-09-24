#!/usr/bin/env python3
"""
smoke_test.py — the offline suite for autotrader-scraper.

One file of plain functions. `tests/test_smoke.py` wraps it as a single
pytest test so `pytest` works as an entry point without a second copy of the
checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and imports that engine by name.

THE FIXTURES ARE IN `fixtures_generated.json`, NOT INLINE. They were cut from
real pages captured 2026-09-24 through a US residential exit with headful
Chromium, by `make_fixtures.py`, which proves each one parses identically to
its untrimmed original. Not verbatim: each fixture is a minimal document
rebuilt from the parts of the page's store the parser reads, and phone
numbers are 555-01xx placeholders (see make_fixtures.py for why each).
"""

import argparse
import ast
import copy
import csv
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
from dataclasses import asdict, fields

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


FIXTURES_PATH = os.path.join(HERE, "fixtures_generated.json")
FIXTURES = json.load(open(FIXTURES_PATH, encoding="utf-8"))


def fx(name) -> str:
    return FIXTURES[name]


ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")
DRIVER_IMPORTS = {"playwright_scraper": "playwright",
                  "selenium_scraper": "selenium",
                  "puppeteer_scraper": "pyppeteer"}
SERVED = ("srp_camry_p1", "srp_camry_p2", "srp_camry_past_end", "srp_price_desc",
          "srp_new_crv", "srp_by_owner", "srp_widened_ford", "srp_zero",
          "vdp_camry", "vdp_gone")


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def _src(module):
    return open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()


def _listing(name, page=1, **kw):
    import product_parser as P
    return P.parse_listing(fx(name), P.Query(mode="listing", **kw), page)


# ---------------------------------------------------------------------------
# The fixtures themselves
# ---------------------------------------------------------------------------

def check_fixture_corpus_is_real_and_scrubbed():
    expected = set(SERVED) | {"refused_unavailable", "refused_over_cdp"}
    missing = expected - set(FIXTURES)
    check("every fixture the suite uses is in fixtures_generated.json",
          not missing, "missing %s" % sorted(missing))
    blob = json.dumps(FIXTURES)
    check("the corpus is not empty (a scan of nothing passes for the wrong reason)",
          len(blob) > 50000, "%d bytes" % len(blob))
    for needle, what in (('clientIp', "the requesting address"),
                         ('atcCookie', "a session id"),
                         ('consumerId', "a private seller's personal id"),
                         ('dataIsland', "the analytics island"),
                         ('"birf"', "the analytics store")):
        check("the corpus carries no %s (%s)" % (needle, what), needle not in blob)
    check("no IPv4 address survived", not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", blob))
    phones = set(re.findall(r'\\"value\\": \\"(\d{7,11})\\"', blob))
    check("phone numbers are 555-01xx placeholders (not vacuous)",
          phones and all(p.startswith("55501") for p in phones), repr(sorted(phones)[:5]))
    hexes = re.findall(r"(.{0,40})\b[0-9a-f]{32}\b(.{0,6})", blob)
    check("every 32-hex in the corpus is a photo file name on the site's image host",
          hexes and all("images.autotrader.com/hn/c/" in a and b.startswith(".jp")
                        for a, b in hexes), repr(hexes[:2]))


# ---------------------------------------------------------------------------
# The parser, asserted on VALUES rather than on coverage (§10)
# ---------------------------------------------------------------------------

def check_a_results_page_parses_to_the_captured_values():
    rows = _listing("srp_camry_p1")
    equal("4 kept results become 4 rows", len(rows), 4)
    r = rows[0]
    equal("sku is the listing id", r.sku, "787910153")
    equal("the canonical detail URL, without the search's tracking",
          r.url, "https://www.autotrader.com/cars-for-sale/vehicle/787910153")
    equal("title", r.title, "Used 2020 Toyota Camry LE")
    equal("VIN", r.vin, "4T1C31AK5LU528569")
    equal("year / make / model / trim", (r.year, r.make, r.model, r.trim),
          (2020, "Toyota", "Camry", "LE"))
    equal("condition is the site's listing type", r.condition, "USED")
    equal("mileage is an INT parsed from the string '72,258'", r.mileage, 72258)
    equal("price is the DISPLAYED price, fees included", r.price, 19675)
    equal("...and the pre-fee price and the fees beside it",
          (r.price_before_fees, r.dealer_fees), (19500, 175))
    equal("KBB fair price and range", (r.kbb_fair_price, r.kbb_fair_price_low,
                                       r.kbb_fair_price_high), (20740, 19640, 21840))
    equal("the site's deal rating", r.deal_rating, "Great")
    equal("currency is the one the page's JSON-LD states", r.currency, "USD")
    equal("body style is the site's CODE", r.body_style, "SEDAN")
    equal("fuel / drivetrain / transmission",
          (r.fuel_type, r.drivetrain, r.transmission),
          ("Hybrid Gas/Electric", "FWD", "Automatic"))
    equal("mpg", (r.mpg_city, r.mpg_highway), (51, 53))
    equal("the dealer", (r.seller_id, r.seller_name, r.private_seller),
          ("100021156", "Paragon Acura", False))
    equal("the dealer's place", (r.seller_city, r.seller_state, r.seller_zip),
          ("Woodside", "NY", "11377"))
    equal("the dealer's rating and how many reviews it rests on",
          (r.seller_rating, r.seller_review_count), (4.7, 2781))
    equal("distance from the searched ZIP", r.distance_miles, 3.15)
    equal("the paid-placement flag is written through", r.premium_spotlight, True)
    equal("the ordering is on the row", r.sort, "relevance")
    equal("provenance", r.data_source, "next_data")
    equal("positions count the rows emitted", [x.position for x in rows], [1, 2, 3, 4])


def check_the_displayed_price_includes_the_dealers_fees():
    """452 of 453 priced records measured: displayPrice ==
    preFeeDerivedPrice + dealerFeesTotal, or == salePrice with no fees.
    `salePrice` itself is NOT a stable meaning (19,500 before a fee on one
    dealer, 15,994 after one on another), so it is not a column."""
    from output_writer import Vehicle
    names = {f.name for f in fields(Vehicle)}
    check("no `sale_price` column: its meaning changes from dealer to dealer",
          "sale_price" not in names)
    priced = [r for name in SERVED if name.startswith("srp_")
              for r in _listing(name) if r.price is not None]
    check("the fixtures hold priced rows (not vacuous)", len(priced) >= 12, str(len(priced)))
    ok = [r for r in priced
          if r.price == (r.price_before_fees or 0) + (r.dealer_fees or 0)]
    equal("price == price_before_fees + dealer_fees on every priced fixture row",
          len(ok), len(priced))


def check_a_listing_with_no_asking_price_is_not_given_its_msrp():
    """'Contact Dealer For Price': the site fills displayPrice with the MSRP
    (48,250 = msrp 48,250). That is not a price anybody asked."""
    rows = _listing("srp_price_desc", sort="price-desc")
    first = rows[0]
    equal("no asking price: price is null", first.price, None)
    equal("...and so is the pre-fee price", first.price_before_fees, None)
    equal("...while the MSRP keeps its column", first.msrp, 48250)
    check("...and a priced row on the same page still has a price",
          any(r.price for r in rows))


def check_zero_is_not_a_price():
    """KBB publishes 0 as the fair price of a new car it does not value (§21)."""
    rows = _listing("srp_new_crv")
    equal("a 0 KBB fair price is null", {r.kbb_fair_price for r in rows}, {None})
    equal("new cars carry an MSRP", rows[0].msrp, 39305)
    equal("...and the condition says NEW", rows[0].condition, "NEW")


def check_private_sellers_have_no_phone_and_no_personal_id():
    rows = _listing("srp_by_owner")
    equal("the by-owner search holds private sellers", {r.private_seller for r in rows}, {True})
    equal("a private seller's phone is never written", {r.seller_phone for r in rows}, {None})
    from output_writer import Vehicle, VehicleDetail
    for cls in (Vehicle, VehicleDetail):
        check("%s has no consumer id column" % cls.__name__,
              not any(re.search(r"consumer_?id", f.name) for f in fields(cls)))


def check_the_json_ld_is_a_decoy_and_placements_are_not_rows():
    """Five JSON-LD blocks on a results page, three of them Product/Car, and
    those three are the PAID spotlight cars rather than results (§24). The
    fixture keeps one placement per list that is not a result, so this is a
    real test of the filter."""
    import product_parser as P
    doc = fx("srp_camry_p1")
    facts = P.page_facts(doc)
    rows = _listing("srp_camry_p1")
    skus = {r.sku for r in rows}
    # From the STORE, not from the rows: "spotlights minus rows" is empty
    # exactly when the filter is broken, which made this check pass against
    # a planted fault (the control that found it is in the CHANGELOG).
    organic = {str(i) for i in P.store(doc)["srp_results"]["activeResults"]}
    placements = set(facts.spotlight_ids) - organic
    check("the fixture carries a placement that is not a result (not vacuous)",
          bool(placements), repr(facts.spotlight_ids))
    check("...and it is in the store's inventory, product-shaped",
          all(i in P.store(doc)["inventory"] for i in placements))
    check("no placement becomes a row", not (placements & skus))
    ld_urls = re.findall(r"cars-for-sale/vehicle/(\d+)", "".join(
        m.group(1) for m in P._LD_RE.finditer(doc)))
    check("the JSON-LD's own car is not one of the rows",
          ld_urls and not (set(ld_urls) & skus), repr(ld_urls))


def check_a_detail_page_parses_and_agrees_with_its_listing_row():
    import product_parser as P
    rows = P.parse_vehicle(fx("vdp_camry"), "787910153")
    equal("one row", len(rows), 1)
    d = rows[0]
    listing = _listing("srp_camry_p1")[0]
    equal("same car as the listing's first row", d.sku, listing.sku)
    shared = [f.name for f in fields(type(listing))]
    skip_cols = {"scraped_at", "page", "position", "mode", "sort", "currency",
                 "distance_miles", "days_on_site", "premium_spotlight"}
    differ = {c: (getattr(listing, c), getattr(d, c)) for c in shared
              if c not in skip_cols and getattr(listing, c) != getattr(d, c)}
    equal("a listing row and a detail row of one car agree on every shared column",
          differ, {})
    equal("condition is upper-cased in both modes ('Used' on a detail page)",
          d.condition, "USED")
    check("features are a list", isinstance(d.features, list) and len(d.features) > 20)
    equal("open recalls", d.open_recalls, 0)
    equal("KBB owner reviews of the MODEL", (d.kbb_consumer_rating, d.kbb_review_count),
          (3.9, 349))
    equal("a detail page states no currency, so none is invented (§4)", d.currency, None)
    equal("distance is null: a detail page has no search to measure from",
          d.distance_miles, None)
    check("the description is text", d.description and "<br" not in d.description.lower())
    equal("mode", d.mode, "vehicle")


def check_description_html_becomes_text():
    import product_parser as P
    equal("<br> becomes a line break, entities decode, tags go",
          P._plain_text("Recent Arrival!<br><br>2022 Camry &amp; more<b>!</b>"),
          "Recent Arrival!\n\n2022 Camry & more!")
    equal("empty stays null", P._plain_text("  "), None)


def check_a_gone_listing_is_named_gone_and_yields_nothing():
    import product_parser as P
    doc = fx("vdp_gone")
    equal("a detail URL answered with a results page is `gone`",
          P.detect_page_state(doc, 200, "https://www.autotrader.com/cars-for-sale/"
                              "all-cars/orangeburg-sc?redirectExpiredPage=1",
                              "vehicle", "100000001"), "gone")
    equal("...and its 25 unrelated cars are NOT read as the vehicle",
          P.parse_vehicle(doc, "100000001"), [])
    equal("the same page in LISTING mode is simply a results page",
          P.detect_page_state(doc, 200, "", "listing"), "content")


def check_page_states_on_real_captures():
    import product_parser as P
    for name in SERVED:
        mode = "vehicle" if name.startswith("vdp") else "listing"
        want = {"srp_zero": "empty", "vdp_gone": "gone"}.get(name, "content")
        equal("%s is %s" % (name, want), P.detect_page_state(fx(name), 200, "", mode), want)
    for name in ("refused_unavailable", "refused_over_cdp"):
        for mode in ("listing", "vehicle"):
            equal("%s is blocked (%s mode)" % (name, mode),
                  P.detect_page_state(fx(name), 200, "", mode), "blocked")
        equal("%s is named" % name, P.detect_bot_challenge(fx(name)), "akamai-unavailable")
    equal("the empty search's own count is 0", P.page_facts(fx("srp_zero")).total_results, 0)


def check_the_refusal_is_blocked_and_never_a_challenge_to_solve():
    """The refusal page carries a reCAPTCHA (`recaptcha/api.js?render=explicit`,
    a real CHALLENGE marker) for its own support form, which files an unblock
    REQUEST with the site's staff. Solving it would submit a ticket, not let
    the run in, so the page must classify as blocked, which policy never
    solves."""
    import page_flow as F
    import product_parser as P
    doc = fx("refused_unavailable")
    check("the refusal page carries the reCAPTCHA loader (not vacuous)",
          "recaptcha/api.js" in doc and P.has_challenge(doc))
    equal("...and is still classified as blocked", P.detect_page_state(doc, 200), "blocked")
    check("...a state the policy never solves", not F.should_solve("blocked"))


def check_markers_do_not_match_a_served_page():
    """§18: count a marker on a page you know is good. And the Scraping
    Browser's injected hunters must not read as a challenge (§24): the CDP
    refusal fixture carries 16 of them."""
    import product_parser as P
    for name in SERVED:
        equal("no refusal marker on %s" % name, P.detect_bot_challenge(fx(name)), None)
        check("no challenge marker on %s" % name, not P.has_challenge(fx(name)))
    cdp = fx("refused_over_cdp")
    check("the CDP fixture carries the extension's injections (not vacuous)",
          cdp.count("chrome-extension://") >= 10 and "<captcha-widgets>" in cdp)
    injected = "\n".join(re.findall(r"<script[^>]*chrome-extension://[^>]*>", cdp))
    check("no challenge marker matches an injected hunter",
          not P.has_challenge(injected), injected[:200])
    check("no marker is a bare word", all("/" in m or "-" in m for m in P.CHALLENGE_MARKERS))


def check_a_marker_survives_both_encodings():
    """§20: a marker must match the raw bytes AND the browser's re-serialised
    DOM; the head of the document is unescaped before matching."""
    import product_parser as P
    escaped = fx("refused_unavailable").replace("unavailable", "unavail&#97;ble")
    equal("an entity-escaped refusal is still named", P.detect_bot_challenge(escaped),
          "akamai-unavailable")
    ref = "<title>Access Denied</title>Reference  #18.536a645f.1790000000.2c1e"
    equal("Akamai's generic denial, two spaces and all", P.detect_bot_challenge(ref),
          "akamai-denied")


# ---------------------------------------------------------------------------
# Pagination and what a page says about itself
# ---------------------------------------------------------------------------

def check_the_site_states_its_own_page_count_and_cap():
    import product_parser as P
    f = P.page_facts(fx("srp_camry_p1"))
    equal("the site's count of matches", f.total_results, 558)
    equal("the pages it will serve (12, however many matched)", f.pages_available, 12)
    equal("the page it served", f.served_page, 1)
    equal("the search the SITE says it ran", (f.site_query.get("makeCode"),
                                              f.site_query.get("modelCode"),
                                              f.site_query.get("zip")),
          ("TOYOTA", "CAMRY", "10065"))
    equal("page 2 says it is page 2", P.page_facts(fx("srp_camry_p2")).served_page, 2)
    equal("asked for page 99, the site REDIRECTED to its last page, and says so",
          P.page_facts(fx("srp_camry_past_end")).served_page, 12)
    import page_flow as F
    equal("the plan never exceeds what the site serves", F.pages_to_plan(50, 12), 12)
    equal("...and never goes below one", F.pages_to_plan(3, 0), 1)


def check_page_and_position_are_unique_across_pages():
    rows = _listing("srp_camry_p1", 1) + _listing("srp_camry_p2", 2)
    pp = [(r.page, r.position) for r in rows]
    equal("(page, position) is unique across a multi-page run", len(set(pp)), len(pp))
    equal("the two pages hold different cars", len({r.sku for r in rows}), len(rows))


def check_url_shapes():
    import product_parser as P
    q, why = P.query_from_url("https://www.autotrader.com/cars-for-sale/vehicle/787910153")
    equal("a detail URL is vehicle mode", (q.mode, q.vehicle_ids), ("vehicle", ("787910153",)))
    q, why = P.query_from_url("https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/"
                              "new-york-ny?searchRadius=50&sortBy=derivedpriceASC"
                              "&numRecords=100&page=3")
    equal("a search URL: sort, page size and starting page are read out of it",
          (q.mode, q.sort, q.page_size, q.first_page), ("listing", "price-asc", 100, 3))
    check("...and the site's own filters are kept", "searchRadius=50" in q.url)
    check("...and the paging parameters are not", "page=" not in q.url and "sortBy" not in q.url)
    equal("the run's page 1 asks the site for page 3", P.site_page_for(q, 1), 3)
    check("...and says so in the URL", "page=3" in P.request_for(q, 1))
    check("the run's page 2 is the site's page 4", "page=4" in P.request_for(q, 2))
    q2, _ = P.query_from_url("https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny")
    equal("page 1 of a default search carries no paging parameter at all",
          P.request_for(q2, 1),
          "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny")
    q, why = P.query_from_url("https://www.autotrader.ca/cars/on/toronto/")
    check("autotrader.ca is refused BY NAME (a different company)",
          q is None and "AutoTrader.ca" in why and "different company" in why)
    q, why = P.query_from_url("https://www.autotrader.com/research/")
    check("a non-search page is refused with the reason", q is None and "search results" in why)
    q, why = P.query_from_url("https://www.autotrader.com/cars-for-sale/all-cars?sortBy=BOGUS")
    check("an unverified sortBy is refused (the site answers BOGUS with some order)",
          q is None and "sortBy=BOGUS" in why)


def check_search_flags_build_the_sites_own_url():
    import product_parser as P
    url, why = P.search_url("new", "honda", "cr-v", "60601", 50)
    equal("flags build a search URL",
          url, "https://www.autotrader.com/cars-for-sale/new-cars/honda/cr-v?zip=60601&searchRadius=50")
    check("a bad ZIP is refused", P.search_url(zip_code="6060")[0] is None)
    check("--model without --make is refused", P.search_url(model="camry")[0] is None)
    check("a radius the site does not offer is refused", P.search_url(radius=33)[0] is None)
    check("a slug with a space is refused", P.search_url(make="land rover")[0] is None)


def check_a_widened_search_is_detected():
    """/used-cars/ford/f-150/los-angeles-ca came back as
    /cars-for-sale/ford/los-angeles-ca: every used Ford, HTTP 200."""
    import product_parser as P
    base = "https://www.autotrader.com/cars-for-sale"
    equal("the dropped model slug is named",
          P.dropped_segments(base + "/used-cars/ford/f-150/los-angeles-ca",
                             base + "/ford/los-angeles-ca"), ["f-150"])
    equal("a listing-type segment moved into the site's query is not a narrowing lost",
          P.dropped_segments(base + "/used-cars/ford/f150/los-angeles-ca",
                             base + "/ford/f150/los-angeles-ca"), [])
    equal("a city slug the site ADDS for a ZIP is not a loss",
          P.dropped_segments(base + "/all-cars/toyota/camry?zip=90210",
                             base + "/all-cars/toyota/camry/beverly-hills-ca?zip=90210"), [])
    equal("by-owner inserted for sellerType=p is not a loss",
          P.dropped_segments(base + "/all-cars/toyota/camry/new-york-ny?sellerType=p",
                             base + "/all-cars/by-owner/toyota/camry/new-york-ny"), [])
    f = P.page_facts(fx("srp_widened_ford"))
    check("the widened capture's own query has no model at all",
          f.site_query.get("makeCode") == "FORD" and "modelCode" not in f.site_query)


# ---------------------------------------------------------------------------
# page_flow: the policy, the query, and the loop
# ---------------------------------------------------------------------------

def _args(**kw):
    ns = types.SimpleNamespace(
        url=None, mode=None, make=None, model=None, zip=None, radius=None,
        listing_type=None, sort=None, page_size=None, vehicle_id=None,
        from_listing=None, pages=1)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


class _Refused(Exception):
    pass


def _err(msg):
    raise _Refused(msg)


def _build(**kw):
    import page_flow
    try:
        return page_flow.build_query(_args(**kw), _err), None
    except _Refused as e:
        return None, str(e)


def check_the_query_is_built_and_contradictions_refused():
    q, why = _build(make="toyota", model="camry", zip="10065")
    equal("flags make a listing query", q and q.mode, "listing")
    q, why = _build(url="https://www.autotrader.com/cars-for-sale/all-cars", make="ford")
    check("--url and a search flag together are refused", q is None and "--make" in why)
    q, why = _build(url="https://www.autotrader.com/cars-for-sale/vehicle/787910153",
                    mode="listing")
    check("--mode disagreeing with the URL is refused", q is None and "disagrees" in why)
    q, why = _build(vehicle_id=["787910153", "791592297"])
    equal("--vehicle-id makes a vehicle query", q and q.vehicle_ids, ("787910153", "791592297"))
    q, why = _build(vehicle_id=["abc"])
    check("a non-numeric listing id is refused", q is None and "not a listing id" in why)
    q, why = _build(url="https://www.autotrader.com/cars-for-sale/all-cars?sortBy=relevance",
                    sort="price-asc")
    check("--sort disagreeing with the URL's sortBy is refused", q is None)
    q, why = _build()
    check("nothing asked for is refused with what to pass", q is None and "--url" in why)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "rows.json")
        json.dump([asdict(r) for r in _listing("srp_camry_p1")], open(path, "w"))
        q, why = _build(from_listing=path)
        equal("--from-listing reads a listing run's skus, in order",
              q and list(q.vehicle_ids), [r.sku for r in _listing("srp_camry_p1")])


def check_state_policy():
    import page_flow as F
    equal("every state has a policy", sorted(F.STATE_POLICY),
          ["blocked", "challenge", "content", "empty", "gone", "throttled", "unknown"])
    check("content and empty are parsed, never retried or blocked",
          all(F.should_parse(s) and not F.should_retry(s) and not F.counts_as_blocked(s)
              for s in ("content", "empty")))
    check("gone: not retried, not solved, not blocked, not parsed",
          not F.should_retry("gone") and not F.should_solve("gone")
          and not F.counts_as_blocked("gone") and not F.should_parse("gone"))
    check("blocked: retried, never solved (the refusal's reCAPTCHA files a ticket)",
          F.should_retry("blocked") and not F.should_solve("blocked")
          and F.counts_as_blocked("blocked"))
    check("throttled: retried, NOT blocked (§24)",
          F.should_retry("throttled") and not F.counts_as_blocked("throttled"))
    equal("at most one solve per page", F.SOLVES_PER_PAGE, 1)
    advice = F.refusal_advice("akamai-unavailable")
    check("the refusal's advice names BOTH conditions", "residential" in advice
          and "headful" in advice)
    check("...and a headless run is told so first",
          F.refusal_advice("akamai-unavailable", headless=True).startswith("This run was HEADLESS"))
    check("a CDP 401 is explained as expired credentials",
          "expired" in F.cdp_connect_hint("WebSocket error: 401 Unauthorized"))
    check("...and a 500 as a held pid", "pid" in F.cdp_connect_hint("HTTP 500"))


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code."""
    src = _src("page_flow")
    engines = "".join(_src(m) for m in ENGINES)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL", "SOLVES_PER_PAGE",
                     "THROTTLE_RETRIES", "THROTTLE_WAIT_S", "READY_WAIT_MS",
                     "READY_POLL_MS", "NAV_TIMEOUT_MS", "CORE_FIELD_FLOOR",
                     "CDP_CONNECT_ATTEMPTS", "CDP_LOCKED_WAIT_S"):
        uses = len(re.findall(r"\b%s\b" % constant, src))
        check("page_flow.%s is READ, not only defined" % constant,
              uses >= 2 or constant in engines, "%d occurrence(s)" % uses)


class _FakeOps:
    """page_flow's named operations, answering from fixtures.

    `answers` maps a URL to a list of (status, text, final_url) served in
    turn; the last one repeats."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.pool = None
        self.gotos, self.relaunches, self.solves = [], 0, 0
        self.text = ""

    def goto(self, url):
        import page_flow
        self.gotos.append(url)
        queue = self.answers.get(url)
        if not queue:
            raise page_flow.TransportError("net::ERR_TIMED_OUT at " + url)
        status, text, final = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(text, Exception):
            raise text
        self.text = text
        return status, final or url

    def document_text(self):
        return self.text

    def wait_ms(self, ms):
        pass

    def solve_captcha(self):
        self.solves += 1
        return False

    def relaunch(self):
        self.relaunches += 1

    def proxy_failure(self, text):
        return "ERR_PROXY_CONNECTION_FAILED" if "ERR_PROXY" in text else ""

    def close(self):
        pass


def _run(answers, query, pages=3, **extra):
    import page_flow
    with tempfile.TemporaryDirectory() as tmp:
        args = types.SimpleNamespace(
            pages=pages, retries=2, retry_delay=0, delay=0, headless=False,
            proxy_block_retries=2, out=os.path.join(tmp, "out"), format="json",
            allow_empty=False, dump_html=None, url=None, cdp_endpoint=None,
            concurrency=1)
        for k, v in extra.items():
            setattr(args, k, v)
        ops = _FakeOps(answers)
        rc = page_flow.run_pages(lambda: ops, lambda o: None,
                                 lambda pages, cur: ([], [], False), args, None, query, 1)
        meta_path = args.out + ".meta.json"
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else None
        rows = json.load(open(args.out + ".json")) if os.path.exists(args.out + ".json") else None
    return rc, meta, rows, ops


SEARCH = "https://www.autotrader.com/cars-for-sale/all-cars/toyota/camry/new-york-ny"


def _q(url=SEARCH):
    import product_parser as P
    return P.query_from_url(url)[0]


def check_the_shared_loop_end_to_end():
    ok = lambda name: [(200, fx(name), None)]  # noqa: E731
    answers = {SEARCH: ok("srp_camry_p1"), SEARCH + "?page=2": ok("srp_camry_p2"),
               SEARCH + "?page=3": [(200, fx("srp_camry_past_end"), SEARCH + "?page=12")]}
    rc, meta, rows, ops = _run(answers, _q(), pages=5)
    equal("three pages, the third past the end: exit 0", rc, 0)
    equal("...complete", meta and meta["status"], "complete")
    equal("...stopped on the site's own statement of which page it served",
          meta and meta["stop_reason"], "end_of_listing")
    equal("...rows of pages 1-2 only (page 3's are the LAST page's, re-served)",
          len(rows or []), 7)
    equal("rows are in page order", [r["page"] for r in rows or []], [1] * 4 + [2] * 3)
    equal("the sidecar carries the site's count", meta and meta["total_results"], 558)
    equal("...and its cap", (meta or {}).get("capped_by_site"), True)
    equal("...and what that cap reaches", (meta or {}).get("reachable_max"), 300)
    equal("...and the search the site says it ran",
          (meta or {}).get("site_query", {}).get("modelCode"), "CAMRY")

    rc, meta, rows, ops = _run({SEARCH: ok("refused_unavailable")}, _q())
    equal("a refused page 1: exit 3", rc, 3)
    equal("...re-fetched once from a fresh browser (no pool)", ops.relaunches, 1)
    equal("...no solve was attempted on the refusal's own reCAPTCHA", ops.solves, 0)
    equal("...and nothing written", (meta, rows), (None, None))

    rc, meta, rows, ops = _run({SEARCH: ok("srp_zero")}, _q())
    equal("a search that matched nothing: exit 4, nothing written", (rc, rows), (4, None))

    rc, meta, rows, ops = _run({}, _q())
    equal("a navigation that never completes: exit 5, not 4", rc, 5)
    equal("...tried --retries times", len(ops.gotos), 2)

    rc, meta, rows, ops = _run({SEARCH: [(200, "<html><body></body></html>", None),
                                         (200, fx("srp_camry_p1"), None)]}, _q(), pages=1)
    equal("an unknown document, then the page: exit 0", rc, 0)

    widened = "https://www.autotrader.com/cars-for-sale/used-cars/ford/f-150/los-angeles-ca"
    rc, meta, rows, ops = _run(
        {widened: [(200, fx("srp_widened_ford"),
                    "https://www.autotrader.com/cars-for-sale/ford/los-angeles-ca")]},
        _q(widened))
    equal("a search the site WIDENED: exit 2, and nothing written", (rc, rows), (2, None))
    equal("...page 1 only", len(ops.gotos), 1)


def check_vehicle_mode_end_to_end():
    import product_parser as P
    vdp = "https://www.autotrader.com/cars-for-sale/vehicle/"
    # The gone one in the MIDDLE: the loop checks for an end of listing from
    # page 2 on, so a gone page 1 never exercised that check (a planted
    # fault stayed green here until this order).
    second = fx("vdp_camry").replace("787910153", "787910999")
    q = P.Query(mode="vehicle", vehicle_ids=("787910153", "100000001", "787910999"))
    answers = {vdp + "100000001": [(200, fx("vdp_gone"), "https://www.autotrader.com/"
                                    "cars-for-sale/all-cars/orangeburg-sc?redirectExpiredPage=1")],
               vdp + "787910153": [(200, fx("vdp_camry"), None)],
               vdp + "787910999": [(200, second, None)]}
    rc, meta, rows, ops = _run(answers, q, pages=1)
    equal("one gone between two served: exit 0", rc, 0)
    equal("...two rows", [r["sku"] for r in rows or []], ["787910153", "787910999"])
    equal("...a GONE vehicle does not end the run", len(ops.gotos), 3)
    equal("...complete", meta and meta["status"], "complete")
    equal("...and the sidecar names the gone one", meta and meta["vehicles_gone"], ["100000001"])
    equal("--pages does not cap vehicle mode", meta and meta["pages_requested"], 3)
    q = P.Query(mode="vehicle", vehicle_ids=("100000001",))
    rc, meta, rows, ops = _run({vdp + "100000001": answers[vdp + "100000001"]}, q)
    equal("every vehicle gone: exit 4 (answered, and the answer is nothing)", rc, 4)


def check_every_engine_implements_the_operations_page_flow_uses():
    """The loop is shared, so an engine missing ONE operation fails only when
    a live run reaches it. The set is DERIVED from page_flow's own source."""
    tree = ast.parse(_src("page_flow"))
    used = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "ops"}
    check("page_flow drives the engines through named operations (not vacuous)",
          {"goto", "document_text", "solve_captcha", "relaunch", "pool"} <= used,
          repr(sorted(used)))
    check("...and the fake driver this suite uses implements every one",
          all(hasattr(_FakeOps(), name) for name in used),
          repr(sorted(n for n in used if not hasattr(_FakeOps(), n))))
    for module in ENGINES:
        tree = ast.parse(_src(module))
        ops_cls = next((n for n in tree.body
                        if isinstance(n, ast.ClassDef) and n.name == "_Ops"), None)
        if ops_cls is None:
            check("%s defines _Ops" % module, False)
            continue
        methods = {n.name for n in ops_cls.body if isinstance(n, ast.FunctionDef)}
        attrs = {t.attr for n in ast.walk(ops_cls) if isinstance(n, ast.Assign)
                 for target in n.targets for t in ast.walk(target)
                 if isinstance(t, ast.Attribute)
                 and isinstance(t.value, ast.Name) and t.value.id == "self"}
        missing = sorted(used - methods - attrs)
        check("%s._Ops provides every operation page_flow uses" % module,
              not missing, "missing %s" % missing)


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

def check_row_schema():
    from output_writer import Vehicle, VehicleDetail, ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES
    prefix = ["source", "scraped_at", "url", "sku", "title", "price", "currency"]
    for cls in (Vehicle, VehicleDetail):
        names = [f.name for f in fields(cls)]
        equal("%s: the family prefix is byte-identical and in order (§9)" % cls.__name__,
              names[:7], prefix)
        check("%s: the run-describing tail is present" % cls.__name__,
              {"page", "position", "mode", "sort", "data_source"} <= set(names))
    v = [f.name for f in fields(Vehicle)]
    d = [f.name for f in fields(VehicleDetail)]
    equal("a detail row is a listing row plus columns at the end", d[:len(v)], v)
    check("image_count is detail-only (a results page carries ONE image per car)",
          "image_count" in d and "image_count" not in v)
    equal("every mode maps to its row class", sorted(ROW_CLASS_BY_MODE), ["listing", "vehicle"])
    equal("both modes are one row per sku", sorted(UNIQUE_BY_SKU_MODES), ["listing", "vehicle"])


def check_csv_and_json_writers():
    from output_writer import VehicleDetail, write_csv, write_json
    import product_parser as P
    rows = P.parse_vehicle(fx("vdp_camry"), "787910153")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.csv")
        write_csv(rows, path, row_cls=VehicleDetail)
        reader = list(csv.reader(open(path, encoding="utf-8")))
        equal("CSV header matches the dataclass, in order", reader[0],
              [f.name for f in fields(VehicleDetail)])
        check("no Python list repr leaked into the CSV",
              not any(cell.startswith("['") for row in reader[1:] for cell in row))
        empty = os.path.join(tmp, "empty.csv")
        write_csv([], empty, row_cls=VehicleDetail)
        equal("an EMPTY csv still carries its header",
              len(list(csv.reader(open(empty, encoding="utf-8")))), 1)
        jpath = os.path.join(tmp, "out.json")
        write_json(rows, jpath)
        loaded = json.load(open(jpath, encoding="utf-8"))
        check("features stay a real list in JSON", isinstance(loaded[0]["features"], list))
        check("the listing id stays a string", isinstance(loaded[0]["sku"], str))


def check_exit_codes():
    import output_writer as O
    equal("3 blocked / 4 empty / 5 never obtained / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_FETCH_FAILED, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("end_of_listing is a COMPLETE stop reason (§24)",
          "end_of_listing" in O.COMPLETE_STOP_REASONS)


def check_a_run_that_finds_nothing_writes_nothing():
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        equal("an empty run exits 4", save([], prefix, "json", allow_empty=False), 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(), '[{"sku": "yesterday"}]')


def check_diff_runs_tracks_the_real_columns():
    import diff_runs as D
    for mode in ("listing", "vehicle"):
        check("%s: tracked columns are derived and non-empty" % mode,
              len(D.tracked_fields(mode)) >= 10, repr(D.tracked_fields(mode)))
    t = D.tracked_fields("listing")
    check("price and mileage are tracked", "price" in t and "mileage" in t)
    for col in ("position", "days_on_site", "distance_miles", "premium_spotlight"):
        check("%s is NOT tracked" % col, col not in t)
    old = [asdict(r) for r in _listing("srp_camry_p1")]
    new = copy.deepcopy(old)
    new[0]["price"] = 18999
    new[0]["days_on_site"] = 31
    del new[1]
    result = D.diff_products(old, new)
    equal("one changed, and only by its price",
          [(c["sku"], list(c["changes"])) for c in result["changed"]],
          [(old[0]["sku"], ["price"])])
    equal("one removed", len(result["removed"]), 1)
    import product_parser as P
    with tempfile.TemporaryDirectory() as tmp:
        a, b = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        json.dump(old, open(a, "w"))
        json.dump([asdict(r) for r in P.parse_vehicle(fx("vdp_camry"), "787910153")],
                  open(b, "w"))
        check("two different MODES are refused",
              not D._check_comparable(types.SimpleNamespace(old=a, new=b)))
        json.dump(new, open(b, "w"))
        json.dump({"status": "complete", "query": {"sort": "relevance"}},
                  open(a[:-5] + ".meta.json", "w"))
        json.dump({"status": "complete", "query": {"sort": "price-asc"}},
                  open(b[:-5] + ".meta.json", "w"))
        check("two different ORDERINGS are refused (different samples, §21)",
              not D._check_comparable(types.SimpleNamespace(old=a, new=b)))


# ---------------------------------------------------------------------------
# The engines — the checks CLAUDE.md §17 says to steal
# ---------------------------------------------------------------------------

def check_engines_import_their_driver_at_module_level():
    for module, driver in DRIVER_IMPORTS.items():
        tree = ast.parse(_src(module))
        top = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver), driver in top,
              "top-level imports: %s" % sorted(top))
    tree = ast.parse(_src("cli"))
    imported = {(n.module or "").split(".")[0] for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom)} | {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)
        for a in n.names}
    check("the shared CLI imports no browser library",
          not (imported & set(DRIVER_IMPORTS.values())), repr(sorted(imported)))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1. A name that does not exist FAILS (§22). A name bound
    in the calling file shadows a same-named module."""
    import captcha_solver
    import cli
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "captcha_solver": captcha_solver,
               "proxy_pool": proxy_pool, "cli": cli}
    bound = 0
    for module in ENGINES + ("scraper_api_client", "diff_runs", "page_flow", "cli",
                             "make_fixtures"):
        tree = ast.parse(_src(module))
        local_names = {n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)}
        direct = {}
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (targets[node.module], alias.name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in targets:
                        aliases[alias.asname or alias.name] = targets[alias.name]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func, owner, attr = node.func, None, None
            if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                    and func.value.id in aliases and func.value.id not in local_names):
                owner, attr = aliases[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)" % (owner.__name__, attr,
                      module, node.lineno), False, "AttributeError on a live run")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            try:
                sig = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            if any(kw.arg is None for kw in node.keywords) or any(
                    isinstance(a, ast.Starred) for a in node.args):
                continue
            try:
                sig.bind(*[None] * len(node.args), **{kw.arg: None for kw in node.keywords})
                bound += 1
            except TypeError as e:
                check("%s:%d %s.%s(...) binds against its real signature"
                      % (module, node.lineno, owner.__name__, attr), False,
                      "%s; signature is %s" % (e, sig))
    check("the binding walk checked something (%d calls)" % bound, bound > 50,
          "only %d calls were bound — is the walk finding them?" % bound)


def _flags_in(module_name, parser_names=("p", "g", "wait")):
    tree = ast.parse(_src(module_name))
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parser_names):
            flags.update(a.value for a in node.args if isinstance(a, ast.Constant)
                         and isinstance(a.value, str) and a.value.startswith("--"))
    return flags


def _engine_flags(module):
    """An engine's flags: its own, plus the shared CLI's, but ONLY if it
    calls the functions that add them (checked by name, from its AST)."""
    tree = ast.parse(_src(module))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    flags = _flags_in(module)
    cli_tree = ast.parse(_src("cli"))
    for fn in cli_tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name in called:
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "add_argument"):
                    flags.update(a.value for a in node.args if isinstance(a, ast.Constant)
                                 and isinstance(a.value, str) and a.value.startswith("--"))
    return flags


CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless", "--headful",
    "--fingerprint", "--fp-country", "--fp-tags", "--locale", "--mode",
}
SITE_FLAGS = {"--make", "--model", "--zip", "--radius", "--listing-type", "--sort",
              "--page-size", "--vehicle-id", "--from-listing"}


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both
    ways. The exception list IS the documentation."""
    sets = {m: _engine_flags(m) for m in ENGINES}
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | SITE_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    DOCUMENTED_DIFFERENCES = {"puppeteer_scraper": {"--chromium-path"}}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b), not only_a and not only_b,
              "only in %s: %s; only in %s: %s" % (a, sorted(only_a), b, sorted(only_b)))
    check("the documented difference still exists (closing it must be a decision)",
          "--chromium-path" in sets["puppeteer_scraper"])


def check_headful_is_the_default_and_a_missing_display_is_said_up_front():
    import cli
    p = argparse.ArgumentParser()
    cli.add_search_args(p)
    cli.add_common_args(p)
    args = p.parse_args(["--url", SEARCH])
    equal("HEADFUL by default: headless was refused on every measurement",
          args.headless, False)
    equal("--headless still exists", p.parse_args(["--headless"]).headless, True)
    before = dict(os.environ)
    try:
        os.environ.pop("DISPLAY", None)
        os.environ.pop("WAYLAND_DISPLAY", None)
        args.cdp_endpoint = None
        msg = cli.display_problem(args)
        if sys.platform.startswith("linux"):
            check("no display on Linux: the fix is named before any launch",
                  msg and "xvfb-run" in msg)
        os.environ["DISPLAY"] = ":99"
        equal("...and a display silences it", cli.display_problem(args), None)
        args.headless = True
        os.environ.pop("DISPLAY")
        equal("headless needs no display", cli.display_problem(args), None)
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_no_engine_overrides_the_user_agent():
    """A bare UA override was scored after one page on a sibling site behind
    the same vendor (§24); a fingerprint, which supplies the whole identity,
    is the only path allowed to set one."""
    for module in ENGINES:
        tree = ast.parse(_src(module))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name == "_apply_fingerprint":
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                        and node.func.attr in ("setUserAgent",):
                    check("%s: %s does not set a UA" % (module, fn.name), False)
                if isinstance(node, ast.Constant) and node.value == "Network.setUserAgentOverride":
                    check("%s: %s does not set a UA" % (module, fn.name), False)
                if isinstance(node, ast.keyword) and node.arg == "user_agent":
                    check("%s: %s does not set a UA" % (module, fn.name), False)
        check("%s defines no hand-built UA" % module, "_chrome_ua" not in _src(module))


def check_pyppeteer_authenticates_a_proxy_through_the_fetch_domain():
    """page.authenticate is built on Network.setRequestInterception, which
    current Chromium no longer has: the first live run died on it."""
    src = _src("puppeteer_scraper")
    tree = ast.parse(src)
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    check("pyppeteer's page.authenticate is not called", "authenticate" not in calls)
    for needle in ('"Fetch.enable"', '"handleAuthRequests": True', '"Fetch.continueWithAuth"',
                   '"Fetch.continueRequest"', 'source == "Proxy"'):
        check("the Fetch-domain auth carries %s" % needle, needle in src)


def check_banned_and_removed_flags():
    """`--country` is banned on a scraper (it could disagree with the URL)."""
    for module in ENGINES + ("cli",):
        source = _src(module)
        for flag in ("--antidetect", "--country", "--country-code"):
            check("%s does not define %s" % (module, flag), '"%s"' % flag not in source)


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE."""
    import builtins
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved, repr(unresolved))


def check_no_statement_is_unreachable():
    for filename in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        dead = []
        for node in ast.walk(tree):
            for fld in ("body", "orelse", "finalbody"):
                block = getattr(node, fld, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
                        dead.append(block[i + 1].lineno)
                        break
        check("%s: no statement the control flow can never reach" % filename,
              not dead, "first at line %d" % min(dead) if dead else "")


def _import_graph(entrypoint):
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, todo = set(), [entrypoint]
    while todo:
        name = todo.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(_src(name))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                todo.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                todo.append(node.module.split(".")[0])
    return seen


def check_dockerfile_copies_everything_the_entrypoint_imports():
    dockerfile = open(os.path.join(HERE, "Dockerfile"), encoding="utf-8").read()
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    missing = sorted(_import_graph("playwright_scraper") - copied)
    check("the Dockerfile COPYs every module playwright_scraper.py imports",
          not missing, "missing %s" % missing)
    check("the image does not carry the fixture generator or the suite",
          not ({"smoke_test", "make_fixtures"} & copied))
    check("the image can run a HEADFUL browser (xvfb)", "xvfb" in dockerfile.lower())


def check_pyproject_lists_every_module():
    text = open(os.path.join(HERE, "pyproject.toml"), encoding="utf-8").read()
    m = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S)
    listed = set(re.findall(r'"([a-z_]+)"', m.group(1))) if m else set()
    needed = _import_graph("playwright_scraper") | _import_graph("scraper_api_client")
    missing = sorted(needed - listed)
    check("pyproject's py-modules lists every module an entry point imports",
          not missing, "missing %s" % missing)


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, re.M))
    equal("the example and the loader name the same variables",
          sorted(documented), sorted(env_config.ENV_KEYS))
    check("the per-site variables carry the AUTOTRADER_ prefix",
          {"AUTOTRADER_CDP_ENDPOINT", "AUTOTRADER_PROXY", "AUTOTRADER_URL"}
          <= set(env_config.ENV_KEYS))


def check_a_copied_env_example_reads_as_UNSET():
    import env_config
    text = open(os.path.join(HERE, ".env.example"), encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    credentials = {"TWOCAPTCHA_KEY", "AUTOTRADER_CDP_ENDPOINT", "AUTOTRADER_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in credentials:
                check("a copied .env.example leaves %s unset" % name, got is None, repr(got))
            else:
                check("...while %s stays a usable default" % name, got == raw.strip(), repr(got))
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # Inside the Docker image, which copies no .github at all. Triggered
        # by the WHOLE directory being absent, never by one file in it (§22).
        skip("ci_checks", "no .github directory (the image)")
        return
    check("the credential scan exists as a script", os.path.exists(script))
    workflow = open(os.path.join(HERE, ".github", "workflows", "tests.yml"),
                    encoding="utf-8").read()
    check("CI INVOKES the script rather than reimplementing it", "ci_checks.py" in workflow)
    result = subprocess.run([sys.executable, script, "--secret-check", "--sample-check"],
                            cwd=HERE, capture_output=True, text=True)
    check("the credential scan and sample check pass on this tree",
          result.returncode == 0, (result.stdout + result.stderr)[-600:])


def check_no_workflow_imports_the_code_inline():
    wf_dir = os.path.join(HERE, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        skip("workflows", "no .github directory (the image)")
        return
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    pattern = re.compile(r"^\s*(?:from|import)\s+(%s)\b" % "|".join(sorted(local)), re.M)
    for name in sorted(os.listdir(wf_dir)):
        hits = pattern.findall(open(os.path.join(wf_dir, name), encoding="utf-8").read())
        check("%s imports no local module inline" % name, not hits, repr(hits))


def check_the_hex_exemption_is_one_context_only():
    """SITE_PUBLIC_IDS forgives a 32-hex inside a photo address on the
    site's image host and NOTHING else. Planted, not assumed."""
    if not os.path.isdir(os.path.join(HERE, ".github")):
        skip("ci_checks", "no .github directory (the image)")
        return
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks as C
    hexkey = "0123456789abcdef" * 2
    photo = "https://images.autotrader.com/hn/c/%s.jpg" % hexkey
    check("inside a photo address: forgiven", not C.HEX32.search(C._without_site_ids(photo)))
    check("the same value elsewhere: still caught",
          bool(C.HEX32.search(C._without_site_ids('"key": "%s"' % ("fedcba9876543210" * 2)))))
    check("on another autotrader path: still caught",
          bool(C.HEX32.search(C._without_site_ids("https://www.autotrader.com/x/" + hexkey))))
    check("on the image host without the extension: still caught",
          bool(C.HEX32.search(C._without_site_ids("https://images.autotrader.com/hn/c/" + hexkey))))


# Assembled from pieces, so this file can be scanned like every other rather
# than exempted (§22).
BANNED_WORDING = (
    "cloud" + " browser", "anti" + "detect browser", "2scraper " + "Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)


def check_banned_wording():
    """§12, enforced by this test rather than by review."""
    scanned = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache",
                                                "live", "captures", ".claude", ".venv")
                   and not os.path.exists(os.path.join(root, d, "pyvenv.cfg"))]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example", ".json")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            scanned += 1
            for phrase in BANNED_WORDING:
                if phrase.lower() in text:
                    check("%s contains no banned phrase #%d" % (
                        os.path.relpath(path, HERE), BANNED_WORDING.index(phrase)), False)
    check("the banned-wording scan read the repo (%d files)" % scanned, scanned > 20)


def check_concurrency_with_the_browser_stubbed():
    """§10: driven through the SHARED worker loop with the fetch replaced."""
    import page_flow
    import product_parser as P
    import queue as queue_mod
    fetched, lock = [], threading.Lock()
    real = page_flow.fetch_one_page

    def fake(ops, args, pool, query, page_num, mask=None, currency_hint=None):
        with lock:
            fetched.append(page_num)
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [] if page_num >= 6 else [object()]
        o.state = "empty" if page_num >= 6 else "content"
        return o

    work = queue_mod.Queue()
    for n in range(2, 51):
        work.put(n)
    results, rlock, exhausted = [], threading.Lock(), threading.Event()
    args = types.SimpleNamespace(delay=0)
    page_flow.fetch_one_page = fake
    try:
        threads = [threading.Thread(target=page_flow.worker_loop,
                                    args=(_FakeOps(), args, P.Query(mode="listing"), work,
                                          results, rlock, exhausted, "w%d" % i))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    finally:
        page_flow.fetch_one_page = real
    check("every page fetched was fetched exactly once", len(fetched) == len(set(fetched)))
    check("dispatch STOPPED at the end of the listing", exhausted.is_set())
    check("...so 49 queued pages cost far fewer fetches", len(fetched) < 15,
          "fetched %d" % len(fetched))
    equal("attempted + unattempted covers the whole queue", len(set(fetched)) + work.qsize(), 49)

    fetched.clear()
    work = queue_mod.Queue()
    for n in range(2, 8):
        work.put(n)
    exhausted.clear()
    page_flow.fetch_one_page = lambda *a, **k: page_flow.PageOutcome(
        page_num=fetched.append(a[4]) or a[4], url="u", gone=True, state="gone")
    try:
        page_flow.worker_loop(_FakeOps(), args, P.Query(mode="vehicle", vehicle_ids=("1",) * 8),
                              work, [], rlock, exhausted, "w")
    finally:
        page_flow.fetch_one_page = real
    equal("vehicle mode: a gone car does NOT stop dispatch", sorted(fetched), list(range(2, 8)))


def check_a_dead_worker_neither_hangs_nor_loses_its_siblings():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    import page_flow
    import product_parser as P

    def exploding(ops, args, pool, query, page_num, mask=None, currency_hint=None):
        if page_num == 3:
            raise RuntimeError("worker died")
        o = page_flow.PageOutcome(page_num=page_num, url="u")
        o.products = [object()]
        o.state = "content"
        return o

    class FakeOps(_FakeOps):
        def __init__(self, *a, **k):
            super().__init__()

        def open(self):
            return self

    class FakePlaywright:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    real = (page_flow.fetch_one_page, engine._Ops, engine.sync_playwright)
    page_flow.fetch_one_page = exploding
    engine._Ops = FakeOps
    engine.sync_playwright = lambda: FakePlaywright()
    try:
        results, unattempted, exhausted = engine._fetch_pages_concurrently(
            types.SimpleNamespace(delay=0), None, P.Query(mode="listing"),
            list(range(2, 8)), 3)
    finally:
        page_flow.fetch_one_page, engine._Ops, engine.sync_playwright = real
    check("the dead worker's siblings still delivered their pages",
          len(results) >= 3, "%d results" % len(results))
    check("page 3 is not reported as a success", 3 not in [o.page_num for o in results])


def check_worker_pools_start_on_different_exits():
    import page_flow
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    equal("three workers start on three different exits",
          len({page_flow.worker_pool(pool, i).current for i in range(3)}), 3)
    equal("a missing pool stays missing", page_flow.worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from fingerprint_client import playwright_context_kwargs
    import playwright.sync_api as pw_api
    sample = {"id": "x", "country": "US", "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US", "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown, repr(unknown))


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: wait_for_function evaluates a string, which a CSP without
    unsafe-eval kills."""
    for module in ENGINES:
        tree = ast.parse(_src(module))
        called = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called)


def check_credentials_never_reach_a_log():
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module, "supersecret" not in masked, masked)
        check("%s keeps host and port" % module, "h1:9222" in masked and "h2:9222" in masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_scraper_api_sends_waitfor_as_an_object_and_reads_http_code():
    """Measured 2026-09-23 in a sibling: a STRING waitFor is answered 422 and
    billed; the target's status is `http_code`."""
    try:
        import scraper_api_client as sac
    except ImportError as e:
        skip("scraper_api_client", str(e))
        return
    sent = {}

    class Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 200, "headers": {},
                    "body": FIXTURES["refused_unavailable"]}

    def post(url, **kw):
        sent.update(kw.get("json") or {})
        return Resp()

    args = types.SimpleNamespace(url=SEARCH, key="k" * 8, timeout=60, cdp_url=None,
                                 wait_text="__NEXT_DATA__", wait_element=None,
                                 wait_state=None)
    real = sac.requests.post
    sac.requests.post = post
    try:
        html, status, final = sac.fetch_html(args)
    finally:
        sac.requests.post = real
    equal("--wait-text sends waitFor as an OBJECT", sent.get("waitFor"), {"text": "__NEXT_DATA__"})
    equal("the target status handed onward is http_code", status, 200)
    check("the default wait is a string only a SERVED page carries",
          '"__NEXT_DATA__"' in _src("scraper_api_client"))


def check_x_debug_header_is_redacted():
    try:
        import scraper_api_client as sac
    except ImportError:
        return
    pw = "SeCr" + "EtPw"
    key = "abcdef01" * 4
    raw = ("cdpurl=ws://acct-zone-scraping_browser-pid-7:" + pw
           + "@cb.2captcha.com:9222 cost=0.0005 key=" + key + " status=200")
    out = sac._redact_debug_header(raw)
    check("x-debug: the credential and the key are gone", pw not in out and key not in out)
    check("x-debug: the cost, host and status survive",
          "cost=0.0005" in out and "cb.2captcha.com:9222" in out and "status=200" in out)


def check_captcha_capability_claims_match_the_code():
    """§19: the most expensive bug this family can ship is a SENTENCE."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read().lower()
    for phrase in ("cannot be solved", "can't be solved", "is not solvable",
                   "solver is inapplicable", "no solver can", "unsolvable captcha"):
        check("README: no %r — write 'this repo does not implement X'" % phrase,
              phrase not in readme)
    check("the README says what this repo does not implement, in those words",
          "does not implement" in readme)


def check_readme_numbers_are_not_stale():
    """§17's check #4: a column count claimed in the README is a class's."""
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    from output_writer import ROW_CLASS_BY_MODE
    sizes = {len(fields(c)) for c in ROW_CLASS_BY_MODE.values()}
    claims = re.findall(r"(\d+)\s+columns", readme)
    check("the README states its column counts (not vacuous)", bool(claims))
    for number in claims:
        check("the README's '%s columns' is a row class's size" % number,
              int(number) in sizes, "sizes are %s" % sorted(sizes))


_TREE_BEFORE = None


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines() if not line.endswith(".pyc"))


def check_no_test_mutates_the_working_tree():
    if _TREE_BEFORE is None:
        skip("git status", "not a git repository")
        return
    changed = sorted(set(_tree_state()) - set(_TREE_BEFORE))
    check("the suite itself changed nothing in the working tree", not changed, repr(changed))


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")]


def main():
    global VERBOSE, _TREE_BEFORE
    parser = argparse.ArgumentParser(description="autotrader-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    VERBOSE = parser.parse_args().verbose
    _TREE_BEFORE = _tree_state()
    for fn in CHECKS:
        if fn is check_no_test_mutates_the_working_tree:
            continue
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()
    check_no_test_mutates_the_working_tree()
    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
