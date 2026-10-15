#!/usr/bin/env python3
"""
make_fixtures.py
----------------
Cut the offline suite's fixtures out of real captures, scrub them, and prove
each one parses IDENTICALLY to the untrimmed page it came from.

    python3 make_fixtures.py            reads captures/, writes fixtures_generated.json

The captures are 1.1-3.1 MB Next.js pages, fetched 2026-09-24 through a US
residential exit with headful Chromium, and they are NOT committed
(`captures/` is ignored). Each carries things that do not belong in a public
repository, all of them in the page's own __NEXT_DATA__:

    props.clientIp                        the address that fetched the page
    pageProps.dataIsland / birf.pageData  the same address again, and a
                                          session id (`atcCookie`)
    inventory[*].consumerId               a PRIVATE seller's personal id

So a fixture is not a trimmed page. It is a NEW, minimal document built from
the parts of the store the parser reads: `srp_results` and the pagination
links, the placement lists, the router query, and the inventory and owner
records of the first few results. Everything else is dropped rather than
scrubbed, which is the stronger arrangement: a field that is not copied
cannot leak, whatever a future capture puts in it.

Of what IS copied, three kinds of value are replaced, and the suite says so:

    phone numbers         -> 555-01xx, the range reserved for fiction
    a seller's description -> cut to its first 300 characters
    images                -> kept, one per record (the parser reads one)

Dealer names, cities and ratings are kept: they are businesses, shown on
every tile. A private seller's name on this site is "Private Seller
Exchange", the site's own brand for that programme, not the person.

Every fixture is then parsed, and so is the page it was cut from, and the
rows must agree on every column except the replaced ones. A fixture that
does not is not written.
"""

import json
import os
import re
import sys
from dataclasses import asdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import product_parser as P  # noqa: E402

CAPTURES = os.path.join(HERE, "captures")
OUT = os.path.join(HERE, "fixtures_generated.json")

# fixture name -> (capture file, kind, how many results to keep)
PLAN = {
    "srp_camry_p1":       ("cap_p1.html", "srp", 4),
    "srp_camry_p2":       ("cap_p2.html", "srp", 3),
    "srp_camry_past_end": ("cap_p99.html", "srp", 2),
    "srp_price_desc":     ("cap_s_pd.html", "srp", 3),   # no-price listings first
    "srp_new_crv":        ("cap_new.html", "srp", 3),    # msrp, kbb 0
    "srp_by_owner":       ("cap_priv.html", "srp", 4),   # private sellers
    "srp_widened_ford":   ("cap_la.html", "srp", 2),     # f-150 served as all Fords
    "srp_zero":           ("cap_zero.html", "srp", 0),
    "vdp_camry":          ("cap_vdp.html", "vdp", 1),
    "vdp_gone":           ("cap_gone.html", "srp", 1),   # a gone id's redirect
    # The App Router results page the site has served since 2026-10-13: no
    # __NEXT_DATA__, the store inside `self.__next_f` flight chunks. Cut
    # from the canary's own capture of 2026-10-14 (2.4 MB, US residential).
    "srp_flight_camry":   ("cap_flight.html", "flight", 4),
}
# Kept whole: 4.4 KB of static Akamai page, the same bytes for every URL,
# carrying nothing about the request; and the same page as a Scraping
# Browser session served it (2026-09-24, country-us), with the 16 scripts
# the auto-solve extension injects into every page it loads (§24). Read for
# anything request-specific before committing: the extension id and the
# script paths are the same for everyone.
VERBATIM = {"refused_unavailable": "p_hl.html",
            "refused_over_cdp": "cdp_refused.html"}

INVENTORY_KEYS = (
    "id", "title", "listingTitle", "vin", "year", "make", "model", "trim",
    "listingType", "type", "mileage", "bodyStyles", "bodyStyleCodes", "color",
    "fuelType", "driveType", "engine", "transmission", "mpgCity", "mpgHighway",
    "pricingDetail", "isReducedPrice", "daysOnSite", "stockId", "stockNumber",
    "premiumSpotlight", "images", "ownerId", "ownerName", "phone",
    "marketExtension", "owner", "features", "fullDescription", "description",
    "safetyRecall", "kbbConsumerRatings", "kbbConsumerReviewCount",
)
OWNER_KEYS = ("id", "name", "privateSeller", "location", "rating", "phone",
              "distanceFromSearch")
STORE_KEYS = ("srp_results", "srp_srpPaginationLinks", "srp_spotlight",
              "srp_primeSpotlight", "srp_boost", "query")
DESCRIPTION_CHARS = 300
# The columns a fixture is allowed to disagree with its original on, and why.
REPLACED_COLUMNS = {"seller_phone", "description", "image_count"}

_phone_counter = [0]
_phone_map = {}


def _fake_phone(real):
    if real not in _phone_map:
        _phone_counter[0] += 1
        _phone_map[real] = "55501%02d" % (_phone_counter[0] % 100)
    return _phone_map[real]


def _scrub_phone(node):
    if isinstance(node, dict) and node.get("value"):
        node = dict(node)
        node["value"] = _fake_phone(node["value"])
    return node


def _scrub_owner(owner):
    o = {k: owner[k] for k in OWNER_KEYS if k in owner}
    if "phone" in o:
        o["phone"] = _scrub_phone(o["phone"])
    loc = (o.get("location") or {}).get("address") or {}
    o["location"] = {"address": {k: loc[k] for k in ("city", "state", "zip") if k in loc}}
    return o


def _scrub_record(rec):
    r = {k: rec[k] for k in INVENTORY_KEYS if k in rec}
    if "phone" in r:
        r["phone"] = _scrub_phone(r["phone"])
    if isinstance(r.get("images"), dict):
        imgs = r["images"]
        primary = imgs.get("primary") if isinstance(imgs.get("primary"), int) else 0
        sources = imgs.get("sources") or []
        keep = sources[primary:primary + 1] or sources[:1]
        r["images"] = {"primary": 0, "sources": keep}
    if isinstance(r.get("owner"), dict):
        r["owner"] = _scrub_owner(r["owner"])
    for key in ("fullDescription", "description"):
        if isinstance(r.get(key), str):
            r[key] = r[key][:DESCRIPTION_CHARS]
    if isinstance(r.get("safetyRecall"), dict):
        r["safetyRecall"] = {"count": r["safetyRecall"].get("count")}
    return r


def _ld_blocks(doc):
    """The JSON-LD blocks the parser reads anything from: one `offers` block
    for the currency. The other four are not copied."""
    out = []
    for m in P._LD_RE.finditer(doc):
        try:
            block = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(block, dict) and isinstance(block.get("offers"), dict):
            offers = block["offers"]
            out.append({"@context": "http://schema.org/", "@type": block.get("@type"),
                        "name": block.get("name"),
                        "offers": {"@type": "Offer",
                                   "priceCurrency": offers.get("priceCurrency"),
                                   "price": offers.get("price"),
                                   "url": offers.get("url")}})
            break
    return out


def build(doc, kind, keep):
    data = P.next_data(doc)
    pp = P.page_props(data)
    store = P.store(data)
    new_store = {}
    for key in STORE_KEYS:
        if key in store:
            new_store[key] = json.loads(json.dumps(store[key]))
    ids = []
    if kind == "srp":
        results = new_store.get("srp_results") or {}
        ids = [str(i) for i in (results.get("activeResults") or [])[:keep]]
        results["activeResults"] = [int(i) for i in ids]
        results.pop("stats", None)
        spots = []
        for key in ("srp_spotlight", "srp_primeSpotlight", "srp_boost"):
            if key in new_store:
                lst = [str(i) for i in (new_store[key].get("activeResults") or [])]
                # One placement per list that is NOT a kept result, so the
                # suite can prove a placement never becomes a row.
                extra = next((i for i in lst if i not in ids), None)
                kept = [i for i in lst if i in ids] + ([extra] if extra else [])
                new_store[key] = {"activeResults": [int(i) for i in kept]}
                spots.extend(i for i in kept if i not in ids)
        wanted = ids + spots
    else:
        wanted = [str(k) for k in (store.get("inventory") or {})]
    inv = store.get("inventory") or {}
    owners = store.get("owners") or {}
    new_store["inventory"] = {i: _scrub_record(inv[i]) for i in wanted if i in inv}
    owner_ids = {str(new_store["inventory"][i].get("ownerId"))
                 for i in new_store["inventory"]}
    new_store["owners"] = {o: _scrub_owner(owners[o]) for o in owner_ids if o in owners}
    new_pp = {"pageType": pp.get("pageType"), "__eggsState": new_store}
    if "showExpiredListingAlert" in pp:
        new_pp["showExpiredListingAlert"] = pp["showExpiredListingAlert"]
    new_data = {"props": {"pageProps": new_pp}, "page": data.get("page"),
                "query": data.get("query") or {}}
    ld = "".join('<script type="application/ld+json">%s</script>' % json.dumps(b)
                 for b in _ld_blocks(doc))
    title = re.search(r"<title[^>]*>(.*?)</title>", doc, re.S)
    return ("<!DOCTYPE html><html><head><title>%s</title>%s</head><body>"
            '<script id="__NEXT_DATA__" type="application/json">%s</script>'
            "</body></html>" % (title.group(1).strip() if title else "", ld,
                                json.dumps(new_data)))


_FLIGHT_ROW_ID = "6"


def _flight_props(doc):
    """The raw (unresolved) props object of the row holding `pageData`, and
    that row's id, from a real App Router capture."""
    rows = P._flight_rows(P._flight_text(doc))
    for rid, raw in rows.items():
        if '"pageData"' in raw and raw[:1] in "[{":
            props = P._find_page_props(json.loads(raw))
            if props is not None:
                return rid, props
    raise SystemExit("no pageData row in the capture")


def build_flight(doc, keep):
    """A minimal App Router document: the parts of `pageData` the parser
    reads, as a flight payload split across two chunks, with a `T` row and
    the original's `$` references kept where their target is kept.

    Not copied: `initialContext` apart from its `query` (it carries the
    requesting address as `clientIp`), `dataIsland` (the address again and a
    session id), and every other row of the payload.
    """
    rid, props = _flight_props(doc)
    pd = props["pageData"]
    ids = [str(i) for i in pd["inventoryIds"][:keep]]
    spot = [str(i) for i in pd.get("spotlightIds") or []]
    extra = next((i for i in spot if i not in ids), None)
    wanted = ids + ([extra] if extra else [])
    inv = pd["inventory"]
    # A record whose phone is a reference to another record's: keep the
    # target too, so the reference resolves as it does on the real page.
    # The page's inventory holds records that are neither results nor
    # placements, and on the capture only those carry such a reference; the
    # first one is kept (not a row) so the suite can resolve it.
    ref = re.compile(r"^\$%s:props:pageData:inventory:(\d+):" % re.escape(rid))
    referring = next((k for k, r in inv.items() if k not in wanted
                      and isinstance(r, dict) and ref.match(str(r.get("phone", "")))), None)
    if referring:
        wanted.append(referring)
    for i in list(wanted):
        m = ref.match(str((inv.get(i) or {}).get("phone", "")))
        if m and m.group(1) not in wanted:
            wanted.append(m.group(1))
    new_inv = {i: _scrub_record(inv[i]) for i in wanted if i in inv}
    owners = pd.get("owners") or {}
    owner_ids = {str(r.get("ownerId")) for r in new_inv.values()}
    new_pd = {
        "inventory": new_inv,
        "owners": {o: _scrub_owner(owners[o]) for o in owner_ids if o in owners},
        "inventoryIds": [int(i) for i in ids],
        "spotlightIds": [int(i) for i in spot if i in wanted],
        "boostListingIds": pd.get("boostListingIds") or [],
        "totalResultCount": pd.get("totalResultCount"),
        "currentPage": pd.get("currentPage"),
        "paginationLinks": pd.get("paginationLinks"),
        "sort": pd.get("sort"),
    }
    query = (props.get("initialContext") or {}).get("query") or {}
    element = ["$", "$L37", None, {"initialContext": {"query": query}, "pageData": new_pd}]
    note = "A raw text row, as the real payload carries:\nmore than one line."
    flight = ("1:\"$Sreact.fragment\"\n"
              "19:T%x,%s" % (len(note.encode("utf-8")), note)
              + "%s:%s\n" % (rid, json.dumps(element, separators=(",", ":"))))
    half = len(flight) // 2
    pushes = "".join("<script>self.__next_f.push([1,%s])</script>" % json.dumps(part)
                     for part in (flight[:half], flight[half:]))
    assets = "".join('<script src="/cars-for-sale/_next/static/chunks/%s.js" async=""></script>' % n
                     for n in ("webpack", "main-app", "page"))
    ld = "".join('<script type="application/ld+json">%s</script>' % json.dumps(b)
                 for b in _ld_blocks(doc))
    title = re.search(r"<title[^>]*>(.*?)</title>", doc, re.S)
    return ("<!DOCTYPE html><html><head><title>%s</title>%s%s</head><body>"
            "<script>(self.__next_f=self.__next_f||[]).push([0])</script>%s"
            "</body></html>" % (title.group(1).strip() if title else "", assets, ld, pushes))


def _rows(doc, kind, first_ids):
    if kind == "vdp":
        return [asdict(r) for r in P.parse_vehicle(doc)]
    rows = [asdict(r) for r in P.parse_listing(doc, P.Query(mode="listing"), 1)]
    return [r for r in rows if r["sku"] in first_ids]


def verify(name, original, fixture, kind, keep):
    """The fixture's rows equal the original's first `keep`, column by column,
    except the columns this file replaces on purpose."""
    fx_rows = _rows(fixture, kind, None) if kind == "vdp" else None
    if kind in ("srp", "flight"):
        fx_rows = [asdict(r) for r in P.parse_listing(fixture, P.Query(mode="listing"), 1)]
        ids = [r["sku"] for r in fx_rows]
        orig_rows = _rows(original, kind, set(ids))
    else:
        orig_rows = _rows(original, kind, None)
    assert len(fx_rows) == len(orig_rows) == (keep if kind in ("srp", "flight") else len(orig_rows)), \
        "%s: %d rows from the fixture, %d from the original" % (name, len(fx_rows), len(orig_rows))
    for a, b in zip(orig_rows, fx_rows):
        for col in a:
            if col in ("scraped_at",) or col in REPLACED_COLUMNS:
                continue
            assert a[col] == b[col], "%s: %s differs (%r vs %r)" % (name, col, a[col], b[col])
        if a.get("description"):
            assert a["description"].startswith(b["description"][:200]), name
    assert P.detect_page_state(original, 200, "", "vehicle" if kind == "vdp" else "listing") == \
        P.detect_page_state(fixture, 200, "", "vehicle" if kind == "vdp" else "listing"), name
    fo, ff = P.page_facts(original), P.page_facts(fixture)
    assert (fo.served_page, fo.total_results, fo.pages_available, fo.site_query) == \
        (ff.served_page, ff.total_results, ff.pages_available, ff.site_query), name
    assert P.currency(original) == P.currency(fixture), name


# What must never survive into the committed corpus.
FORBIDDEN = (
    (re.compile(r'"clientIp"'), "the requesting address"),
    (re.compile(r'"atcCookie"'), "a session id"),
    (re.compile(r'"consumerId"'), "a private seller's personal id"),
    (re.compile(r'"ip"\s*:\s*"\d'), "an address"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "an IPv4 address"),
)


def main():
    # `--only NAME ...` rebuilds those fixtures and keeps the rest of the
    # committed corpus as it is, for when only some captures are at hand.
    only = sys.argv[sys.argv.index("--only") + 1:] if "--only" in sys.argv else None
    out = {}
    if only:
        out = json.load(open(OUT, encoding="utf-8"))
    for name, (fname, kind, keep) in PLAN.items():
        if only and name not in only:
            continue
        original = open(os.path.join(CAPTURES, fname), encoding="utf-8").read()
        fixture = (build_flight(original, keep) if kind == "flight"
                   else build(original, kind, keep))
        verify(name, original, fixture, kind, keep)
        out[name] = fixture
        print("  %-20s %8d -> %6d bytes, verified" % (name, len(original), len(fixture)))
    for name, fname in VERBATIM.items():
        if only and name not in only:
            continue
        out[name] = open(os.path.join(CAPTURES, fname), encoding="utf-8").read()
        print("  %-20s verbatim, %d bytes" % (name, len(out[name])))
    blob = json.dumps(out)
    for pattern, what in FORBIDDEN:
        hit = pattern.search(blob)
        assert not hit, "the corpus still carries %s: %r" % (what, hit.group(0))
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    print("Wrote %s (%d bytes)." % (os.path.relpath(OUT, HERE), os.path.getsize(OUT)))


if __name__ == "__main__":
    main()
