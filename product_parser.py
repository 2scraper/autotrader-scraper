"""
product_parser.py
-----------------
Everything this repo knows about autotrader.com. The engines, page_flow and
output_writer know nothing about the site beyond what is imported from here.

What the site serves, measured 2026-09-24
-----------------------------------------
Every search results page (SRP) and vehicle detail page (VDP) is a Next.js
document whose `<script id="__NEXT_DATA__">` carries the site's own redux
store, `props.pageProps.__eggsState`. That store is the source here (and
since 2026-10-13 a results page carries it in App Router flight chunks
instead; `flight_data` maps that onto the same shape):

    srp_results.activeResults    the ORGANIC results of this page, as listing
                                 ids, in the order the page shows them
    srp_results.count            the site's own count of what matched
    srp_srpPaginationLinks       the pages the site will serve: 12 at most
    srp_spotlight,
    srp_primeSpotlight           PAID placements, beside the results
    inventory                    one full record per listing id, for the
                                 results AND the placements
    owners                       one record per dealer or private seller

Three things that are easy to get wrong, each measured:

* **The JSON-LD is a decoy.** A results page carries five
  `application/ld+json` blocks: a BreadcrumbList, a CollectionPage with no
  items, and three `Product`/`Car` blocks, which are the three PAID
  spotlight listings rather than results. 3 against 25 on every page
  captured (a sibling site has the same decoy). It is read for exactly one
  thing, the currency, which is stated nowhere else on the page.
* **`inventory` is not the result list.** It holds the placements as well
  (46 records beside 25 results on one page), and on a search with no
  results it still holds 12. The results are `srp_results.activeResults`,
  looked up in `inventory`, and nothing else becomes a row.
* **The site rewrites a search it does not understand, silently.**
  `/used-cars/ford/f-150/los-angeles-ca` was answered with all 2,284 used
  Fords in Los Angeles (the model slug is `f150`), HTTP 200, no error.
  `dropped_segments()` compares the path asked for with the path served.

A detail page for a listing that has gone is not an error either: it
redirects to a results page for some OTHER location (an id asked for from a
New York exit landed on Orangeburg, SC) with `redirectExpiredPage=1`. Read
as a detail page, that is 25 unrelated cars. `detect_page_state` calls it
`gone`.
"""

import html as html_lib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from output_writer import Vehicle, VehicleDetail

BASE = "https://www.autotrader.com"
SEARCH_ROOT = "/cars-for-sale"
VDP_PATH = SEARCH_ROOT + "/vehicle/%s"

# The hosts this repo reads. autotrader.ca and autotrader.co.uk are separate
# companies with different sites, so they are refused BY NAME rather than
# with "not an autotrader URL", which would be false.
SUPPORTED_HOSTS = ("www.autotrader.com", "autotrader.com")
OTHER_AUTOTRADERS = {
    "autotrader.ca": "AutoTrader.ca (Canada)",
    "autotrader.co.uk": "Auto Trader UK",
    "autotrader.com.au": "Autotrader Australia",
    "autotrader.co.za": "AutoTrader South Africa",
}

# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------

# The first path segment after /cars-for-sale that names the listing type.
# Measured: the site drops `used-cars` from the path when it redirects and
# keeps it as `listingType: "USED"` in its own query, so these are never
# reported as dropped (see dropped_segments).
LISTING_TYPES = {"all": "all-cars", "used": "used-cars", "new": "new-cars",
                 "certified": "certified-cars"}
LISTING_TYPE_SEGMENTS = frozenset(LISTING_TYPES.values()) | {"by-owner"}

# `sortBy` values, by the name this repo offers. Each was fetched on
# 2026-09-24 and gave its own ordering. The ALLOWLIST matters: the site
# answered `sortBy=BOGUS` with HTTP 200 and a full page under an ordering
# of its own choosing, so a typo would be a healthy-looking run in an order
# nobody asked for.
SORTS = {
    "relevance": "relevance",          # the site's default ("Best Match")
    "price-asc": "derivedpriceASC",
    "price-desc": "derivedpriceDESC",
    "mileage-asc": "mileageASC",
    "year-desc": "yearDESC",
    "newest-listed": "datelistedDESC",
    "distance": "distanceASC",
}
DEFAULT_SORT = "relevance"

# Rows per results page. 25 is the site's default. `numRecords=100` was
# honoured on an HTML page (100 results, 2026-09-24) and is offered because
# it is a quarter of the navigations, each of which costs a headful browser
# several seconds through a residential exit.
PAGE_SIZES = (25, 100)
DEFAULT_PAGE_SIZE = 25

# The highest page number this repo will ask for. The site's own ceiling is
# lower and is read from each response (srp_srpPaginationLinks); this only
# bounds a malformed one.
MAX_PAGES = 400

# Radii the site's own filter offers, in miles. 0 is "any distance".
RADII = (0, 10, 25, 50, 75, 100, 150, 200, 300, 400, 500)

_ZIP_RE = re.compile(r"^\d{5}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_VDP_RE = re.compile(r"^/cars-for-sale/vehicle/(\d+)$")
_ID_RE = re.compile(r"^\d{6,12}$")


@dataclass
class Query:
    """What a run asks for.

    `listing`: one search, as a /cars-for-sale URL. Its query string is
    kept whole: every filter the site offers is a parameter of that URL,
    and this repo does not need to know them to pass them on.

    `vehicle`: a list of listing ids, one detail page each.
    """
    mode: str = "listing"
    url: str = ""
    sort: Optional[str] = None
    page_size: int = DEFAULT_PAGE_SIZE
    # The page the URL itself asked for. A run started on `?page=3` calls
    # that page 1 of the RUN while asking the site for page 3, and the
    # out-of-range check compares against the latter.
    first_page: int = 1
    vehicle_ids: Tuple[str, ...] = ()

    def validate(self) -> Optional[str]:
        if self.mode == "listing":
            if self.sort is not None and self.sort not in SORTS:
                return ("--sort %r is not one of: %s. The site answers an "
                        "unknown sortBy with a full page under an ordering of "
                        "its own choosing, so it is refused here."
                        % (self.sort, ", ".join(SORTS)))
            if self.page_size not in PAGE_SIZES:
                return "--page-size must be one of %s" % (PAGE_SIZES,)
            if not self.url:
                return "a listing run needs --url or --make/--zip"
            return None
        if self.mode == "vehicle":
            if not self.vehicle_ids:
                return ("--mode vehicle needs listing ids: --url of a "
                        "/cars-for-sale/vehicle/{id} page, --vehicle-id, or "
                        "--from-listing with a listing run's JSON.")
            return check_ids(self.vehicle_ids)
        return "unknown mode %r" % self.mode


def _host_refusal(host: str) -> Optional[str]:
    host = (host or "").lower()
    if host in SUPPORTED_HOSTS:
        return None
    for other, name in OTHER_AUTOTRADERS.items():
        if host == other or host.endswith("." + other):
            return ("%s is %s, a different company with a different site. "
                    "This repo reads autotrader.com (United States) only."
                    % (host, name))
    return "%s is not autotrader.com" % (host or "that URL")


def query_from_url(url: str) -> Tuple[Optional[Query], Optional[str]]:
    """The Query a --url names, or (None, why it is refused)."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("http", "https"):
        return None, "--url must be an http(s) URL"
    refusal = _host_refusal(u.hostname or "")
    if refusal:
        return None, refusal
    path = re.sub(r"/+$", "", u.path) or "/"
    m = _VDP_RE.match(path)
    if m:
        return Query(mode="vehicle", vehicle_ids=(m.group(1),)), None
    if not (path == SEARCH_ROOT or path.startswith(SEARCH_ROOT + "/")):
        return None, ("%s is not a search results page. Pass a URL under "
                      "%s/ (a search as the site builds it), or a "
                      "%s/vehicle/{id} detail page." % (path, SEARCH_ROOT,
                                                       SEARCH_ROOT))
    params = parse_qsl(u.query, keep_blank_values=True)
    first_page = 1
    for k, v in params:
        if k == "page" and v.isdigit() and int(v) >= 1:
            first_page = int(v)
    sort = next((v for k, v in params if k == "sortBy"), None)
    size = next((v for k, v in params if k == "numRecords"), None)
    q = Query(mode="listing", first_page=first_page)
    if sort is not None:
        alias = {v: k for k, v in SORTS.items()}.get(sort)
        if alias is None:
            return None, ("the URL's sortBy=%s is not one this repo has "
                          "verified (%s)." % (sort, ", ".join(SORTS.values())))
        q.sort = alias
    if size is not None:
        if not size.isdigit() or int(size) not in PAGE_SIZES:
            return None, ("the URL's numRecords=%s is not one of %s."
                          % (size, PAGE_SIZES))
        q.page_size = int(size)
    kept = [(k, v) for k, v in params
            if k not in ("page", "sortBy", "numRecords", "firstRecord")]
    q.url = urlunparse(("https", "www.autotrader.com", path, "",
                        urlencode(kept), ""))
    return q, None


def search_url(listing_type: str = "all", make: Optional[str] = None,
               model: Optional[str] = None, zip_code: Optional[str] = None,
               radius: Optional[int] = None) -> Tuple[Optional[str], Optional[str]]:
    """A search URL built from flags, or (None, why not).

    Make and model are the site's own path SLUGS (`toyota`, `camry`,
    `f150`). A wrong slug is not an error on the site, it widens the search
    silently, which is why the served path is checked against this one on
    page 1 (dropped_segments).
    """
    if listing_type not in LISTING_TYPES:
        return None, "--listing-type must be one of %s" % ", ".join(LISTING_TYPES)
    if model and not make:
        return None, "--model needs --make"
    segs = [LISTING_TYPES[listing_type]]
    for name, slug in (("--make", make), ("--model", model)):
        if slug is None:
            continue
        slug = slug.strip().lower()
        if not _SLUG_RE.match(slug):
            return None, ("%s %r is not a URL slug (lowercase letters, digits "
                          "and dashes, as in the site's own URLs)" % (name, slug))
        segs.append(slug)
    params = []
    if zip_code is not None:
        if not _ZIP_RE.match(zip_code):
            return None, "--zip must be a five-digit US ZIP code"
        params.append(("zip", zip_code))
    if radius is not None:
        if radius not in RADII:
            return None, ("--radius must be one of %s (miles; 0 is any "
                          "distance), the values the site's own filter "
                          "offers" % ", ".join(map(str, RADII)))
        params.append(("searchRadius", str(radius)))
    return (BASE + SEARCH_ROOT + "/" + "/".join(segs)
            + ("?" + urlencode(params) if params else "")), None


def site_page_for(query: Query, n: int) -> int:
    """The page number the SITE is asked for on the run's n-th page."""
    return query.first_page + n - 1


def request_for(query: Query, n: int) -> str:
    """The URL of the n-th page of the RUN (1-based)."""
    if query.mode == "vehicle":
        return BASE + VDP_PATH % query.vehicle_ids[n - 1]
    u = urlparse(query.url)
    params = parse_qsl(u.query, keep_blank_values=True)
    if query.sort and query.sort != DEFAULT_SORT:
        params.append(("sortBy", SORTS[query.sort]))
    if query.page_size != DEFAULT_PAGE_SIZE:
        params.append(("numRecords", str(query.page_size)))
    site_page = site_page_for(query, n)
    if site_page > 1:
        params.append(("page", str(site_page)))
    return urlunparse((u.scheme, u.netloc, u.path, "", urlencode(params), ""))


def dropped_segments(requested: str, served: str) -> List[str]:
    """Path segments of `requested` that the served page's path lacks.

    The site answers a slug it does not recognise by removing it and
    serving the wider search: `/used-cars/ford/f-150/los-angeles-ca` came
    back as `/cars-for-sale/ford/los-angeles-ca`, all used Fords. A listing
    TYPE segment is not counted: the site moves it out of the path into its
    own `listingType` and keeps honouring it. Segments the site ADDS (a
    city slug for a ZIP, `by-owner` for `sellerType=p`) narrow nothing and
    are ignored.
    """
    def segs(url: str) -> List[str]:
        parts = [s for s in urlparse(url or "").path.split("/") if s]
        if parts and parts[0] == SEARCH_ROOT.strip("/"):
            parts = parts[1:]
        return parts
    if not served:
        return []
    have = set(segs(served))
    return [s for s in segs(requested)
            if s not in have and s not in LISTING_TYPE_SEGMENTS]


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S)
_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I)


def next_data(doc: Optional[str]) -> Optional[Dict[str, Any]]:
    """The page's store, in the __NEXT_DATA__ shape, or None.

    Two payloads carry it. Until 2026-10-12 every results page had a
    `<script id="__NEXT_DATA__">`. From 2026-10-13 the results page is a
    Next.js App Router document instead: no __NEXT_DATA__ at all, and the
    same records inside the `self.__next_f.push([1, "..."])` flight chunks
    (see `flight_data`). Both are returned in the old shape, so everything
    below reads one store whichever the site served.
    """
    m = _NEXT_DATA_RE.search(doc or "")
    if not m:
        return flight_data(doc)
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# The App Router flight payload (results pages from 2026-10-13)
# ---------------------------------------------------------------------------
#
# Measured on the canary's own captures of 2026-10-13 and 2026-10-14 (2.4 MB
# each, served through a US residential exit): three `push([1, "..."])`
# chunks whose strings, concatenated, are a React Server Components payload
# of ~280 rows, `<hex id>:<JSON>` one per line, plus `T` rows (`<id>:T<hex
# byte length>,<text>`) that carry raw text with newlines in it. One row is
# the element whose props hold the page's data:
#
#     props.pageData.inventory         one record per listing id (results
#                                      AND placements), same keys as the old
#                                      store's `inventory`
#     props.pageData.owners            one record per seller, ditto
#     props.pageData.inventoryIds      the ORGANIC results, in page order
#                                      (was srp_results.activeResults)
#     props.pageData.totalResultCount  the site's own count (was .count)
#     props.pageData.paginationLinks   was srp_srpPaginationLinks.links
#     props.pageData.spotlightIds,
#     props.pageData.boostListingIds   PAID placements
#     props.pageData.currentPage       the page the server rendered
#     props.initialContext.query       the search the site ran (was `query`)
#
# Values may be references: "$undefined" is undefined, "$$..." is a literal
# string starting "$" (prices printed "$$22,363"), and "$<id>:<path>" points
# into another row or this one ("$6:props:pageData:inventory:N:phone" is a
# dealer's phone shared between its cars). They are resolved before the
# records are read. `initialContext` also carries the requesting address
# (`clientIp`), which is never read.

_FLIGHT_PUSH_RE = re.compile(r"self\.__next_f\.push\(\[1,\s*")
_FLIGHT_ROW_RE = re.compile(r"([0-9a-f]+):")
_FLIGHT_REF_RE = re.compile(r"^\$([0-9a-f]+)((?::[^:]+)*)$")
_FLIGHT_CACHE: Dict[int, Tuple[str, Optional[Dict[str, Any]]]] = {}


def _flight_text(doc: str) -> str:
    decoder = json.JSONDecoder()
    parts = []
    for m in _FLIGHT_PUSH_RE.finditer(doc):
        try:
            value, _ = decoder.raw_decode(doc, m.end())
        except ValueError:
            continue
        if isinstance(value, str):
            parts.append(value)
    return "".join(parts)


def _flight_rows(text: str) -> Dict[str, str]:
    """{row id: its raw JSON text}. `T` rows are skipped by their length,
    which counts UTF-8 bytes."""
    rows: Dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        m = _FLIGHT_ROW_RE.match(text, i)
        if not m:
            j = text.find("\n", i)
            if j < 0:
                break
            i = j + 1
            continue
        rid, i = m.group(1), m.end()
        if text.startswith("T", i):
            comma = text.find(",", i)
            try:
                size = int(text[i + 1:comma], 16)
            except ValueError:
                break
            chunk = text[comma + 1:comma + 1 + size].encode("utf-8")[:size]
            i = comma + 1 + len(chunk.decode("utf-8", "ignore"))
            continue
        j = text.find("\n", i)
        if j < 0:
            j = n
        rows[rid] = text[i:j]
        i = j + 1
    return rows


class _Flight:
    def __init__(self, rows: Dict[str, str]):
        self.rows = rows
        self.parsed: Dict[str, Any] = {}

    def row(self, rid: str) -> Any:
        if rid not in self.parsed:
            try:
                self.parsed[rid] = json.loads(self.rows.get(rid, "null"))
            except ValueError:
                self.parsed[rid] = None
        return self.parsed[rid]

    def follow(self, rid: str, path: List[str], depth: int) -> Any:
        node = self.row(rid)
        for seg in path:
            if (isinstance(node, list) and len(node) >= 4 and node[0] == "$"
                    and seg == "props"):
                node = node[3]
            elif isinstance(node, list):
                try:
                    node = node[int(seg)]
                except (ValueError, IndexError):
                    return None
            elif isinstance(node, dict):
                node = node.get(seg)
            else:
                return None
        return self.resolve(node, depth + 1)

    def resolve(self, v: Any, depth: int = 0) -> Any:
        if depth > 12:
            return None
        if isinstance(v, dict):
            return {k: self.resolve(x, depth) for k, x in v.items()}
        if isinstance(v, list):
            return [self.resolve(x, depth) for x in v]
        if isinstance(v, str) and v.startswith("$"):
            if v == "$undefined":
                return None
            if v.startswith("$$"):
                return v[1:]
            m = _FLIGHT_REF_RE.match(v)
            if m:
                path = [p for p in m.group(2).split(":") if p]
                return self.follow(m.group(1), path, depth)
        return v


def _find_page_props(node: Any, depth: int = 0) -> Optional[Dict[str, Any]]:
    """The props object holding `pageData` with an `inventory`."""
    if depth > 40:
        return None
    if isinstance(node, dict):
        pd = node.get("pageData")
        if isinstance(pd, dict) and "inventory" in pd:
            return node
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find_page_props(child, depth + 1)
        if found is not None:
            return found
    return None


def flight_data(doc: Optional[str]) -> Optional[Dict[str, Any]]:
    """The store of an App Router results page, in the __NEXT_DATA__ shape,
    or None when the document carries no such payload.

    Only a RESULTS page is mapped (`inventoryIds` present). Whether the
    detail page has moved too has not been measured, and guessing its shape
    would turn a parse failure into wrong rows.
    """
    doc = doc or ""
    if "self.__next_f" not in doc:
        return None
    key = id(doc)
    cached = _FLIGHT_CACHE.get(key)
    if cached is not None and cached[0] is doc:
        return cached[1]
    result = _flight_data(doc)
    _FLIGHT_CACHE.clear()  # one document at a time; the engines parse in turn
    _FLIGHT_CACHE[key] = (doc, result)
    return result


def _flight_data(doc: str) -> Optional[Dict[str, Any]]:
    flight = _Flight(_flight_rows(_flight_text(doc)))
    props = None
    for rid, raw in flight.rows.items():
        if '"pageData"' in raw and raw[:1] in "[{":
            props = _find_page_props(flight.row(rid))
            if props is not None:
                break
    if props is None:
        return None
    pd = props["pageData"]
    if not isinstance(pd.get("inventoryIds"), list):
        return None
    get = lambda k: flight.resolve(pd.get(k))  # noqa: E731
    context = props.get("initialContext") if isinstance(props.get("initialContext"), dict) else {}
    query = flight.resolve(context.get("query"))
    store_ = {
        "srp_results": {"activeResults": get("inventoryIds") or [],
                        "count": get("totalResultCount")},
        "srp_srpPaginationLinks": {"links": get("paginationLinks") or []},
        "srp_spotlight": {"activeResults": get("spotlightIds") or []},
        "srp_boost": {"activeResults": get("boostListingIds") or []},
        "inventory": get("inventory") or {},
        "owners": get("owners") or {},
        "query": query if isinstance(query, dict) else {},
    }
    page = get("currentPage")
    return {"props": {"pageProps": {"pageType": "srp", "__eggsState": store_}},
            "query": {"page": page} if isinstance(page, int) else {},
            "_payload": "next_flight"}


def payload_kind(doc: Optional[str]) -> str:
    """Which payload a row came from, for the `data_source` column."""
    data = next_data(doc)
    return (data or {}).get("_payload") or "next_data"


def page_props(doc_or_data: Any) -> Dict[str, Any]:
    data = doc_or_data if isinstance(doc_or_data, dict) else next_data(doc_or_data)
    props = (data or {}).get("props") or {}
    pp = props.get("pageProps") if isinstance(props, dict) else None
    return pp if isinstance(pp, dict) else {}


def store(doc_or_data: Any) -> Dict[str, Any]:
    """The redux store the page was rendered from (`__eggsState`)."""
    s = page_props(doc_or_data).get("__eggsState")
    return s if isinstance(s, dict) else {}


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

# What a refused request gets, measured 2026-09-24: HTTP 200 and a 3.7 KB
# static page from `AkamaiNetStorage`, last modified in 2016, whose whole
# text is "We're sorry for any inconvenience, but the site is currently
# unavailable." It is served IN PLACE of every URL (listing, detail page and
# the site's JSON endpoint alike) to plain curl, to headless Chromium (old
# and new headless), and to headful Chromium from a datacentre address.
# Counted: "page unavailable" once on each of 4 refusals, 0 times on each of
# 11 served pages. The status is 200, so the body is the only signal.
UNAVAILABLE_MARKERS = ("Autotrader - page unavailable",
                       "the site is currently unavailable")

# Akamai's generic edge denial ("Access Denied ... Reference #18.x.y"). NOT
# OBSERVED on this site, which answers with the page above instead. It is
# here because it is the same edge; the id's SHAPE is matched with any
# whitespace, since the bytes carry two spaces and a browser re-serialises
# them.
_AKAMAI_REF_RE = re.compile(r"Reference\s*#\s*\d+\.[0-9a-f]+\.\d+\.[0-9a-f]+", re.I)

# Only the head of a document is unescaped and searched for refusal
# markers: a refusal is a few KB, and a 1.8 MB results page could carry a
# marker-shaped string deep in a dealer's description.
_REFUSAL_SCAN_CHARS = 12_000

# A reCAPTCHA, or Akamai's behavioural challenge page. NEITHER has been
# observed on this site: no captcha of any kind appeared on any page
# captured, served or refused. They are named so that a page carrying one
# is reported as a challenge rather than as an unexplained parse failure.
# Specific loader paths only: a bare "captcha" or "recaptcha" is not a
# marker anywhere a Scraping Browser extension can inject its hunters.
CHALLENGE_MARKERS = ("recaptcha/api.js", "recaptcha/api2/anchor",
                     "recaptcha/enterprise.js", "sec-if-cpt-container",
                     "/_sec/cp_challenge/")


def detect_bot_challenge(doc: Optional[str], url: str = "") -> Optional[str]:
    """The name of what refused the request, or None."""
    head = html_lib.unescape((doc or "")[:_REFUSAL_SCAN_CHARS])
    if any(m in head for m in UNAVAILABLE_MARKERS):
        return "akamai-unavailable"
    if _AKAMAI_REF_RE.search(head) and "Access Denied" in head:
        return "akamai-denied"
    return None


def has_challenge(doc: Optional[str]) -> bool:
    return any(m in (doc or "") for m in CHALLENGE_MARKERS)


def detect_page_state(doc: Optional[str], status: Optional[int] = None,
                      url: str = "", mode: str = "listing",
                      expect_id: Optional[str] = None) -> str:
    """Name what the site answered with. page_flow's STATE_POLICY says what
    each name means for a retry, a solve and the exit code.

    Ordered by how much each signal PROVES: the site's own store
    first, since no refusal page carries one; then the refusal markers;
    then the status, which is 200 on the refusal this site actually sends.
    """
    doc = doc or ""
    data = next_data(doc)
    pp = page_props(data)
    if data is not None and pp:
        s = store(data)
        page_type = pp.get("pageType")
        if mode == "vehicle":
            if page_type == "vdp":
                inv = s.get("inventory") or {}
                if expect_id is None or str(expect_id) in {str(k) for k in inv}:
                    return "content"
                return "unknown"
            if (page_type == "srp" and data.get("_payload") == "next_flight"
                    and _VDP_RE.match(urlparse(url or "").path or "")):
                # A flight payload mapped as a results page while the
                # browser is still ON the detail URL. Whether the detail
                # page itself has moved to that payload is not measured, so
                # this is not taken as "gone": a wrong "gone" drops a live
                # car silently.
                return "unknown"
            if page_type == "srp":
                # A detail URL answered with a results page: the listing
                # has gone. `showExpiredListingAlert` and the
                # `redirectExpiredPage=1` parameter both say so; the page
                # type alone is enough, because a live listing is never
                # answered with a results page.
                return "gone"
            return "unknown"
        results = s.get("srp_results")
        if page_type == "srp" or isinstance(results, dict):
            if not isinstance(results, dict):
                return "unknown"
            if results.get("activeResults"):
                return "content"
            if results.get("count") == 0:
                return "empty"
            # A count and no ids. Not seen; worth one more try.
            return "unknown"
        return "unknown"
    if detect_bot_challenge(doc, url):
        return "blocked"
    if has_challenge(doc):
        return "challenge"
    if status == 429:
        return "throttled"
    if status in (401, 403):
        return "blocked"
    if served_by_site(doc):
        # The site's own page, built from its own assets, with no refusal on
        # it and no store this parser can read. Retrying fetches the same
        # 2.4 MB again and gets the same answer, and calling it "never
        # arrived" sends the reader to check a proxy that worked. Measured
        # 2026-10-13/14: the results page moved to a different payload and
        # was retried three times as `unknown`, then reported exit 5 with
        # "a dead proxy or a timeout".
        return "unreadable"
    return "unknown"


# The site's own static assets, under its own path. Counted on served pages
# and refusals (2026-09-24 and 2026-10-14): 19 to 69 on every served results
# page, in both payload shapes; 0 on Akamai's "page unavailable" and 0 on
# the same refusal fetched over the Scraping Browser. A browser's own error
# page names the host in its title but references none of these.
SITE_ASSET_MARKER = "/cars-for-sale/_next/static/"
SITE_ASSET_MIN = 3


def served_by_site(doc: Optional[str]) -> bool:
    """Whether a document was built out of the site's own assets."""
    return (doc or "").count(SITE_ASSET_MARKER) >= SITE_ASSET_MIN


# ---------------------------------------------------------------------------
# What a results page says about itself
# ---------------------------------------------------------------------------

@dataclass
class PageFacts:
    """The site's own statements about one results page."""
    # The page the server actually rendered. Asked for page 99 of a
    # 12-page search, the site REDIRECTS to page 12 (HTTP 200, 25 real
    # results), so the page number asked for proves nothing about the rows
    # (unlike sibling sites, which re-serve the FIRST page).
    served_page: int = 1
    total_results: Optional[int] = None
    pages_available: Optional[int] = None
    spotlight_ids: List[str] = field(default_factory=list)
    # The site's own reading of the search: make, model, zip, radius,
    # listing type. Recorded in the sidecar because it is what the rows are
    # a sample OF, and it can differ from what was asked (dropped_segments).
    site_query: Dict[str, Any] = field(default_factory=dict)


def page_facts(doc: Optional[str]) -> PageFacts:
    data = next_data(doc)
    s = store(data)
    facts = PageFacts()
    router_q = (data or {}).get("query") or {}
    try:
        facts.served_page = int(router_q.get("page") or 1)
    except (TypeError, ValueError, AttributeError):
        facts.served_page = 1
    results = s.get("srp_results") or {}
    if isinstance(results.get("count"), int):
        facts.total_results = results["count"]
    links = (s.get("srp_srpPaginationLinks") or {}).get("links") or []
    pages = [l.get("page") for l in links if isinstance(l, dict)
             and isinstance(l.get("page"), int)]
    if pages:
        facts.pages_available = max(pages)
    elif facts.total_results is not None:
        # One page of results carries no pagination links at all.
        facts.pages_available = 1 if facts.total_results else 0
    spot: List[str] = []
    for key in ("srp_spotlight", "srp_primeSpotlight", "srp_boost"):
        for i in (s.get(key) or {}).get("activeResults") or []:
            if str(i) not in spot:
                spot.append(str(i))
    facts.spotlight_ids = spot
    q = s.get("query")
    if isinstance(q, dict):
        facts.site_query = {k: q.get(k) for k in
                            ("makeCode", "modelCode", "zip", "city", "state",
                             "searchRadius", "listingType")
                            if q.get(k) is not None}
    return facts


def currency(doc: Optional[str]) -> Optional[str]:
    """The currency the page itself states, or None.

    Stated in exactly one place: `offers.priceCurrency` in the JSON-LD of a
    results page. Those blocks describe the spotlight cars, but a currency
    is a fact about the page rather than about which cars it lists. The
    redux records carry bare numbers, and a detail page carries no JSON-LD
    and no "USD" anywhere (0 occurrences, 2026-09-24), so a detail row's
    currency is null rather than a default.
    """
    for m in _LD_RE.finditer(doc or ""):
        try:
            block = json.loads(m.group(1))
        except ValueError:
            continue
        for node in (block if isinstance(block, list) else [block]):
            if not isinstance(node, dict):
                continue
            offers = node.get("offers")
            for o in (offers if isinstance(offers, list) else [offers]):
                if isinstance(o, dict):
                    c = o.get("priceCurrency")
                    if isinstance(c, str) and re.match(r"^[A-Z]{3}$", c):
                        return c
    return None


# ---------------------------------------------------------------------------
# Records -> rows
# ---------------------------------------------------------------------------

def _d(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _name(v: Any) -> Optional[str]:
    """A `{code, name}` node's name, or a bare string."""
    if isinstance(v, dict):
        n = v.get("name")
        return n.strip() if isinstance(n, str) and n.strip() else None
    return v.strip() if isinstance(v, str) and v.strip() else None


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.replace(",", "").strip()
        if re.match(r"^-?\d+$", s):
            return int(s)
    return None


def _positive(v: Any) -> Optional[int]:
    """A positive whole number, or None. ZERO IS NOT A PRICE here:
    `kbbFppAmount` is 0 on a new car the valuation does not cover, and
    written through it would drag every average a consumer computes.
    The same holds for an mpg of 0."""
    n = _int(v)
    return n if n and n > 0 else None


def _float(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _bool(v: Any) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def vehicle_url(listing_id: Any) -> str:
    """The listing's canonical address: the path the site's own JSON-LD and
    detail page use, without the search's tracking parameters."""
    return BASE + VDP_PATH % listing_id


def _mileage(rec: Dict[str, Any]) -> Optional[int]:
    """`mileage` is `{"label": "Mileage", "value": "72,258"}`: a STRING with
    a thousands comma. A new car carries a delivery figure ("10")."""
    m = rec.get("mileage")
    if isinstance(m, dict):
        return _int(m.get("value"))
    return _int(m)


def prices(rec: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """The price columns, from `pricingDetail`.

    `displayPrice` is what the tile shows, and it INCLUDES the dealer's
    fees: measured over 476 distinct records on 2026-09-24,

        345   displayPrice == preFeeDerivedPrice + dealerFeesTotal
        107   no fees, no preFeeDerivedPrice: displayPrice == salePrice
         23   no asking price at all: displayPrice == msrp
          1   none of the above (a new car with a dealer incentive)

    `salePrice` is NOT a stable meaning and is not a column: on one dealer
    it is the price before fees (19,500 + 175 = 19,675 displayed), on
    another it is the displayed price itself (15,994, with 14,995 before a
    999 fee). The pre-fee price is `preFeeDerivedPrice`, and where a dealer
    charges no fees it is the displayed price.

    The 23: a listing with no asking price ("Contact Dealer For Price")
    carries its MSRP as `displayPrice`. Written through as `price`, that is
    a number the dealer never asked, so `price` needs a sale or pre-fee
    price behind it and the MSRP keeps its own column.

    `dealerDiscountedPrice` is left out on purpose: it was above the
    displayed price on some records and below it on others, and a column
    whose meaning cannot be stated should not exist.
    """
    p = _d(rec.get("pricingDetail"))
    sale = _positive(p.get("salePrice"))
    pre = _positive(p.get("preFeeDerivedPrice"))
    fees = _positive(p.get("dealerFeesTotal"))
    asked = sale is not None or pre is not None
    if pre is None and sale is not None and fees is None:
        pre = sale
    return {
        "price": _positive(p.get("displayPrice")) if asked else None,
        "price_before_fees": pre if asked else None,
        "dealer_fees": fees,
        "msrp": _positive(p.get("msrp")),
        "kbb_fair_price": _positive(p.get("kbbFppAmount")),
        "kbb_fair_price_low": _positive(p.get("kbbFppLowAmount")),
        "kbb_fair_price_high": _positive(p.get("kbbFppHighAmount")),
    }


def _primary_image(rec: Dict[str, Any]) -> Tuple[Optional[str], int]:
    imgs = _d(rec.get("images"))
    sources = [s for s in imgs.get("sources") or [] if isinstance(s, dict)
               and s.get("src")]
    if not sources:
        return None, 0
    i = imgs.get("primary") if isinstance(imgs.get("primary"), int) else 0
    if not 0 <= i < len(sources):
        i = 0
    return sources[i]["src"], len(sources)


def _seller(rec: Dict[str, Any], owner: Dict[str, Any]) -> Dict[str, Any]:
    """Who sells it.

    A private seller's phone is hidden by the site (`visible: false`) and is
    never written. A dealer's is a business number the site prints on the
    tile. A private seller's `consumerId` is a personal id and is not a
    column at all.
    """
    private = _bool(owner.get("privateSeller"))
    addr = _d(_d(owner.get("location")).get("address"))
    rating = _d(owner.get("rating"))
    phone = _d(rec.get("phone")) or _d(owner.get("phone"))
    phone_value = None
    if private is not True and phone.get("visible") is not False:
        phone_value = _str(phone.get("value"))
    distance = _d(rec.get("marketExtension")).get("distance")
    if distance is None:
        distance = owner.get("distanceFromSearch")
    # A seller with no reviews: both columns null together, never a 0.0
    # grade beside a count of 0.
    rating_count = _positive(rating.get("count"))
    return {
        "seller_id": _str(rec.get("ownerId") or owner.get("id")),
        "seller_name": _str(rec.get("ownerName") or owner.get("name")),
        "private_seller": private,
        "seller_city": _str(addr.get("city")),
        "seller_state": _str(addr.get("state")),
        "seller_zip": _str(addr.get("zip")),
        "seller_phone": phone_value,
        "seller_rating": _float(rating.get("value")) if rating_count else None,
        "seller_review_count": rating_count,
        "distance_miles": _float(distance),
    }


def parse_record(rec: Dict[str, Any], owner: Dict[str, Any],
                 row_cls=Vehicle, **extra) -> Any:
    """One inventory record as a row. Used by both modes, so a listing row
    and a detail row of the same car agree on every shared column."""
    rid = _str(rec.get("id"))
    color = _d(rec.get("color"))
    image, image_count = _primary_image(rec)
    # The body style's CODE ("SEDAN"), in both modes. A results page names
    # it ({"code": "SEDAN", "name": "Sedan"}) and a detail page gives only
    # the code, so the name would make the same car read "Sedan" in one
    # file and "SEDAN" in the other.
    body = rec.get("bodyStyles")
    body_style = None
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body_style = _str(body[0].get("code"))
    elif isinstance(rec.get("bodyStyleCodes"), list) and rec["bodyStyleCodes"]:
        body_style = _str(rec["bodyStyleCodes"][0])
    # "USED" on a results page (`listingType`), "Used" on a detail page
    # (`type`): upper-cased so one car reads the same in both.
    condition = _str(rec.get("listingType") or rec.get("type"))
    cols = dict(
        url=vehicle_url(rid) if rid else "",
        sku=rid,
        title=_str(rec.get("title") or rec.get("listingTitle")),
        vin=_str(rec.get("vin")),
        year=_int(rec.get("year")),
        make=_name(rec.get("make")),
        model=_name(rec.get("model")),
        trim=_name(rec.get("trim")),
        condition=condition.upper() if condition else None,
        mileage=_mileage(rec),
        body_style=body_style,
        exterior_color=_str(color.get("exteriorColor")),
        interior_color=_str(color.get("interiorColor")),
        fuel_type=_name(rec.get("fuelType")),
        drivetrain=_name(rec.get("driveType")),
        engine=_name(rec.get("engine")),
        transmission=_name(rec.get("transmission")),
        mpg_city=_positive(rec.get("mpgCity")),
        mpg_highway=_positive(rec.get("mpgHighway")),
        deal_rating=_str(_d(rec.get("pricingDetail")).get("dealIndicator")),
        price_reduced=_bool(rec.get("isReducedPrice")),
        days_on_site=_int(rec.get("daysOnSite")),
        stock_number=_str(rec.get("stockId") or rec.get("stockNumber")),
        premium_spotlight=_bool(rec.get("premiumSpotlight")),
        image_url=image,
    )
    if row_cls is VehicleDetail:
        # A results-page record carries ONE image (1 of 75 on every row of a
        # live run); the detail page carries them all (37 on one car). So
        # the count is a detail-only column: on a listing row it would say
        # 1 about a car with 37 photos.
        cols["image_count"] = image_count or None
    cols.update(prices(rec))
    cols.update(_seller(rec, owner))
    cols.update(extra)
    return row_cls(**cols)


def result_ids(doc: Optional[str]) -> List[str]:
    """The organic result ids the page itself lists, distinct, in order —
    what a parsed page's row count is checked against."""
    ids: List[str] = []
    for i in (store(doc).get("srp_results") or {}).get("activeResults") or []:
        if str(i) not in ids:
            ids.append(str(i))
    return ids


def parse_listing(doc: str, query: Query, page: int = 1,
                  currency_hint: Optional[str] = None) -> List[Vehicle]:
    """The organic results of one results page, in the page's order.

    Placements are NOT rows: they are paid slots beside the results, and
    counting them would make the row count depend on how many the page
    happened to carry (the same search showed 13 on one fetch and 15 on
    another). A result that is also a paid placement keeps its row, and
    says so in `premium_spotlight`.

    `position` counts the rows EMITTED on this page, so it does not move
    when the placements do.
    """
    s = store(doc)
    inv = s.get("inventory") or {}
    owners = s.get("owners") or {}
    ids = (s.get("srp_results") or {}).get("activeResults") or []
    cur = currency(doc) or currency_hint
    source = payload_kind(doc)
    rows: List[Vehicle] = []
    seen = set()
    for lid in ids:
        rec = inv.get(str(lid))
        if not isinstance(rec, dict) or str(lid) in seen:
            continue
        seen.add(str(lid))
        owner = owners.get(str(rec.get("ownerId"))) or {}
        rows.append(parse_record(
            rec, owner, Vehicle, currency=cur, page=page,
            position=len(rows) + 1, mode="listing",
            sort=query.sort or DEFAULT_SORT, data_source=source))
    return rows


def _features(rec: Dict[str, Any]) -> Optional[List[str]]:
    """Every feature the listing names, de-duplicated in the site's order.
    `features` is a dict of category -> list, and the categories overlap."""
    out: List[str] = []
    for group in _d(rec.get("features")).values():
        for f in group if isinstance(group, list) else []:
            if isinstance(f, str) and f.strip() and f.strip() not in out:
                out.append(f.strip())
    return out or None


_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _plain_text(v: Any) -> Optional[str]:
    """A seller's description as text. Dealers write it as HTML
    ("Recent Arrival!<br><br>2022 Toyota Camry LE ..."), and a CSV cell full
    of `<br>` is neither readable nor what the page shows."""
    s = _str(v)
    if s is None:
        return None
    s = html_lib.unescape(_TAG_RE.sub("", _BR_RE.sub("\n", s)))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip() or None


def parse_vehicle(doc: str, listing_id: Optional[str] = None,
                  page: int = 1) -> List[VehicleDetail]:
    """The one car a detail page describes, as a one-row list (or [])."""
    s = store(doc)
    inv = s.get("inventory") or {}
    if listing_id is not None:
        rec = inv.get(str(listing_id))
    else:
        rec = next(iter(inv.values()), None) if len(inv) == 1 else None
    if not isinstance(rec, dict):
        return []
    owner = _d(rec.get("owner"))
    reviews = _positive(rec.get("kbbConsumerReviewCount"))
    # `distance_miles` is null here on purpose. A detail page has no search
    # to measure from, and the figure it carries is from somewhere the site
    # chose: 1,395.7 miles on a car that was 3.15 miles from the searched
    # ZIP on the results page, measured on the same car minutes apart.
    return [parse_record(
        rec, owner, VehicleDetail, currency=currency(doc), page=page,
        position=1, mode="vehicle", sort=None, data_source="next_data",
        distance_miles=None,
        features=_features(rec),
        description=_plain_text(rec.get("fullDescription") or rec.get("description")),
        open_recalls=_int(_d(rec.get("safetyRecall")).get("count")),
        kbb_consumer_rating=_float(rec.get("kbbConsumerRatings")) if reviews else None,
        kbb_review_count=reviews,
    )]


def parse_page(doc: str, query: Query, page: int = 1,
               currency_hint: Optional[str] = None) -> List[Any]:
    if query.mode == "vehicle":
        return parse_vehicle(doc, query.vehicle_ids[page - 1], page)
    return parse_listing(doc, query, page, currency_hint)


def check_ids(ids: Sequence[str]) -> Optional[str]:
    bad = [i for i in ids if not _ID_RE.match(str(i))]
    return ("not a listing id: %s" % ", ".join(map(str, bad[:5]))) if bad else None


def listing_ids_from_file(path: str) -> Tuple[List[str], Optional[str]]:
    """The `sku` column of a listing run's JSON output, in order."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError) as e:
        return [], "could not read %s: %s" % (path, e)
    if not isinstance(rows, list):
        return [], "%s is not a JSON list of rows" % path
    ids: List[str] = []
    for r in rows:
        sku = r.get("sku") if isinstance(r, dict) else None
        if sku is not None and str(sku) not in ids:
            ids.append(str(sku))
    if not ids:
        return [], "%s holds no rows with a `sku`" % path
    return ids, None
