"""
page_flow.py
------------
The retry / solve / blocked decision, as DATA rather than as three copies of
an if-chain (CLAUDE.md §1), and the page loop itself, once, for all three
engines.

autotrader.com answers one of this repo's navigations in five ways, measured
2026-09-24, and they want four different responses:

    a results or detail page (the site's own __NEXT_DATA__) -> parse
    a results page whose own count is 0                     -> parse, it is
                                                               an answer
    a detail URL answered with a results page elsewhere     -> "gone": the
                                                               listing was
                                                               sold; no row,
                                                               not a failure
    Akamai's "page unavailable", HTTP 200                   -> blocked: the
                                                               exit or the
                                                               browser is
                                                               refused
    anything else                                           -> retry

Three states are named and have NOT been observed: a challenge (reCAPTCHA
or Akamai's behavioural page), a 429, and a 403. They are here because a
scraper that cannot name a refusal reports it as success.

Nothing here imports a browser, and **no JavaScript crosses this boundary**
(§1). Each engine answers a handful of named operations in its own driver's
dialect; see "The page loop" below.
"""

import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from output_writer import dedupe_by_key, finish_run, SOURCE_DEFAULT
from product_parser import (DEFAULT_PAGE_SIZE, DEFAULT_SORT, MAX_PAGES,
                            Query, check_ids, currency, detect_bot_challenge,
                            detect_page_state, dropped_segments,
                            listing_ids_from_file, page_facts, parse_page,
                            query_from_url, request_for, search_url,
                            site_page_for)

log = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

# How long one navigation may take. The largest page measured was 3.1 MB
# (100 results), which arrived in 8 to 16 seconds through a residential
# exit. The bound exists because §8 requires every remote call to have one.
NAV_TIMEOUT_MS = 60_000

# How long to keep reading a document that came back as neither a page nor
# a refusal, before calling it unknown. Both kinds of page are server
# rendered, so this is rarely spent; it covers a navigation that returned
# before the document had been replaced.
READY_WAIT_MS = 8_000
READY_POLL_MS = 1_000

# How long to wait at the SAME exit after a 429. NOT OBSERVED: dozens of
# navigations a few seconds apart never met one.
THROTTLE_WAIT_S = 20.0
THROTTLE_RETRIES = 2


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "", mode: str = "listing",
             expect_id: Optional[str] = None) -> str:
    """Name what the site answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every caller passes
    `classify(html, status, url, mode, expect_id)`. A sibling repo shipped
    `classify(html, url=...)` in two of three engines against a callee that
    took `status` second, and both crashed on their first fetch (§17).
    """
    return detect_page_state(html or "", status, url, mode, expect_id)


STATE_POLICY = {
    "content":    {"retry": False, "solve": False, "blocked": False, "parse": True},
    # A search that matched nothing. The site served exactly what was asked
    # for, so this is EXIT_NO_PRODUCTS rather than EXIT_BLOCKED.
    "empty":      {"retry": False, "solve": False, "blocked": False, "parse": True},
    # A detail page for a listing that no longer exists. The site answers
    # with a results page for some other location; asking again gets the
    # same answer. No row, and not a failure of the run: the question was
    # answered.
    "gone":       {"retry": False, "solve": False, "blocked": False, "parse": False},
    # NOT OBSERVED. A reCAPTCHA can be bought an answer to; Akamai's
    # behavioural page cannot (this repo does not implement it), and a fresh
    # exit is what changes it. Hence retry.
    "challenge":  {"retry": True,  "solve": True,  "blocked": True,  "parse": False},
    # NOT OBSERVED. Waited out at the same exit, and NOT counted as blocked:
    # calling a throttle a block reports exit 3 for a page that was about to
    # come back (§24).
    "throttled":  {"retry": True,  "solve": False, "blocked": False, "parse": False},
    "blocked":    {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    "unknown":    {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the loop
# below, so setting it False really does stop the retry (§17).
RETRY_ON_BLOCKED = True

# How many times to re-fetch a blocked page when there is no proxy pool to
# rotate into. One, from a fresh browser: the refusal is a decision about
# the address and the client, and was the same answer on every repeat
# measured, so a second request from both unchanged costs a navigation and
# confirms it. WITH a pool the loop retries once per remaining exit
# instead, because there the retry changes what the refusal depends on.
BLOCK_RETRIES_WITHOUT_POOL = 1

# At most one solve per page, counted across every call site: a challenge
# that survives a solved token is not one this run can pass, and a second
# solve is a second charge for the same answer (§23).
SOLVES_PER_PAGE = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given what page 1 said is there.

    The site serves 12 pages of a search at most (srp_srpPaginationLinks)
    however many cars matched, and asked for more it REDIRECTS to the last
    one, so walking off the end re-collects the last page rather than
    failing. The plan is therefore made from the site's own number.
    """
    ceiling = MAX_PAGES if pages_available is None else min(pages_available, MAX_PAGES)
    return max(1, min(int(pages_requested), ceiling))


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (§7).
    """
    return 1 if cdp_endpoint else None


# ---------------------------------------------------------------------------
# Refusals: how they are named and what the reader is told
# ---------------------------------------------------------------------------

def refusal_advice(name: str, headless: bool = False) -> str:
    """One paragraph on what changes the answer. Kept here so the three
    engines cannot give three different pieces of advice."""
    if name == "akamai-unavailable":
        text = ("autotrader.com served its 'page unavailable' page in place of "
                "the one asked for. Measured 2026-09-24 it does that unless "
                "BOTH hold: a US residential exit, and a headful browser. "
                "Headless Chromium (old and new headless) was refused from a "
                "US residential address, and headful Chromium from a "
                "datacentre one. Use --proxy with a US residential exit and "
                "run headful (the default; under xvfb-run on a server), or "
                "--cdp-endpoint with a Scraping Browser country-us profile.")
        if headless:
            text = ("This run was HEADLESS, which was refused on every "
                    "measurement. Drop --headless. " + text)
        return text
    if name == "challenge":
        return ("A challenge page stood in front of the listing. If it is a "
                "reCAPTCHA, set TWOCAPTCHA_KEY so it can be solved. A "
                "different exit (--proxy-file) is the other thing that "
                "changes it.")
    return ("The site refused this request. A different exit is what changes "
            "that: --proxy / --proxy-file, or --cdp-endpoint.")


def stop_reason_for(outcome) -> str:
    """The run's stop_reason when `outcome` is the page that ended it."""
    if getattr(outcome, "blocked_by", None):
        return "blocked_%s" % outcome.blocked_by
    if getattr(outcome, "state", None) == "throttled":
        return "throttled"
    return "page_load_timeout"


# ---------------------------------------------------------------------------
# The query, and the end of a run
# ---------------------------------------------------------------------------

# The search flags, by the argparse dest they land in. A flag left at None
# was not typed, which is how build_query tells a user's value from a
# default.
SEARCH_FLAGS = (("make", "--make"), ("model", "--model"), ("zip", "--zip"),
                ("radius", "--radius"), ("listing_type", "--listing-type"))


def build_query(args, error: Callable[[str], None]) -> Query:
    """The Query a run sends, from --url or from the flags, validated.

    --url and the search flags are two ways to say the same thing. A flag
    the user typed that disagrees with the URL would silently scrape
    something neither of them named, so the combination is refused rather
    than merged (the family's --country rule, §10). `error` is argparse's
    `p.error`, so a refusal is exit 2 with the usage line.
    """
    typed = [flag for dest, flag in SEARCH_FLAGS
             if getattr(args, dest, None) is not None]
    ids = list(getattr(args, "vehicle_id", None) or [])
    if getattr(args, "from_listing", None):
        more, why = listing_ids_from_file(args.from_listing)
        if why:
            error(why)
        ids.extend(i for i in more if i not in ids)

    if args.url:
        query, why = query_from_url(args.url)
        if query is None:
            error(why)
        if typed:
            error("--url already carries the search; %s would have to agree "
                  "with it and nothing checks that they do. Pass a URL or the "
                  "flags, not both." % ", ".join(typed))
        if query.mode == "vehicle" and ids:
            error("--url names one vehicle; pass further ids with "
                  "--vehicle-id alone.")
    elif ids:
        if typed:
            error("%s describe a search; --vehicle-id/--from-listing name "
                  "cars. Pick one." % ", ".join(typed))
        why = check_ids(ids)
        if why:
            error(why)
        query = Query(mode="vehicle", vehicle_ids=tuple(ids))
    elif typed:
        url, why = search_url(args.listing_type or "all", args.make,
                              args.model, args.zip, args.radius)
        if url is None:
            error(why)
        query, _ = query_from_url(url)
    else:
        error("say what to scrape: --url, or --make/--model/--zip, or "
              "--vehicle-id / --from-listing for --mode vehicle.")

    if args.mode and args.mode != query.mode:
        error("--mode %s disagrees with what was asked for, which is a %s run."
              % (args.mode, query.mode))
    if query.mode == "listing":
        if getattr(args, "sort", None) is not None:
            if query.sort is not None and query.sort != args.sort:
                error("--sort %s disagrees with the URL's own sortBy." % args.sort)
            query.sort = args.sort
        if getattr(args, "page_size", None) is not None:
            query.page_size = args.page_size
    why = query.validate()
    if why:
        error(why)
    if args.pages < 1:
        error("--pages must be at least 1")
    return query


def query_summary(query: Query) -> dict:
    """The query as the sidecar records it."""
    if query.mode == "vehicle":
        return {"vehicle_ids": len(query.vehicle_ids)}
    return {"url": query.url, "sort": query.sort or DEFAULT_SORT,
            "page_size": query.page_size, "first_page": query.first_page}


# ---------------------------------------------------------------------------
# The page loop, driven through named operations
# ---------------------------------------------------------------------------
#
# Everything about fetching one page lives here, once: navigating, reading
# the document, retrying a transport failure, paying for a challenge,
# waiting out a throttle, rotating on a refusal, and parsing what came back.
# The three engines differ only in HOW they ask their driver:
#
#     ops.goto(url)           -> (status, final_url). Raises TransportError.
#     ops.document_text()     -> the current document's markup
#     ops.wait_ms(ms)
#     ops.solve_captcha()     -> True if a challenge was solved and reloaded
#     ops.relaunch()          -> a fresh browser (on the pool's current exit)
#     ops.proxy_failure(text) -> the driver's proxy-error name in text, or ""
#     ops.pool                -> the worker's ProxyPool, or None
#
# The family carried three copies of this loop for nine sites, and §6 says
# the three must agree on exit codes and on whether a run spends money. One
# copy is how that holds by construction (§26).


class TransportError(Exception):
    """A navigation that did not complete: a timeout, a dead proxy."""


@dataclass
class PageOutcome:
    """What one page produced.

    Collected per page and merged afterwards, in page order, rather than
    folded into shared state as the loop goes, so the output cannot depend
    on which page happened to finish first (§8).
    """
    page_num: int
    url: str
    products: List = field(default_factory=list)
    blocked_by: Optional[str] = None
    load_failed: bool = False
    state: Optional[str] = None
    final_url: str = ""
    # Listing: the site served a different page than the one asked for,
    # which on this site means the search ended (it redirects past-the-end
    # requests to its last page).
    out_of_range: bool = False
    # Vehicle: the listing no longer exists.
    gone: bool = False
    # Listing, page 1: segments of the search the site threw away.
    dropped: List[str] = field(default_factory=list)
    total_available: Optional[int] = None
    pages_available: Optional[int] = None
    spotlights: int = 0
    site_query: dict = field(default_factory=dict)
    currency: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.load_failed and self.blocked_by is None


# The columns the site filled on EVERY record of every capture, per mode
# (259 results across eight captures, 2026-09-24). Below this share the
# payload has moved rather than the data being unusual. Deliberately NOT
# here: `price` (a no-price listing is legitimate), `deal_rating` (127 of
# 259), `mpg_city` (209 of 259), `image_url` (241 of 259).
CORE_FIELD_FLOOR = 99
CORE_FIELDS = {
    "listing": ("sku", "title", "vin", "year", "make", "model", "mileage",
                "seller_name"),
    "vehicle": ("sku", "title", "vin", "year", "make", "model", "mileage"),
}


def _core_field_warnings(rows: List, mode: str, page_num: int) -> None:
    if not rows:
        return
    for name in CORE_FIELDS.get(mode, ()):
        filled = sum(1 for r in rows if getattr(r, name, None) not in (None, "", []))
        share = 100.0 * filled / len(rows)
        if share < CORE_FIELD_FLOOR:
            log.warning("Only %.0f%% of page %d carries `%s`, against a "
                        "measured floor of %d%%. Every record of every capture "
                        "had one, so the page's store has moved — re-run with "
                        "--dump-html.", share, page_num, name, CORE_FIELD_FLOOR)


def _dump(args, page_num: int, text: str) -> None:
    """Write the exact document the parser was given, on success too (§9)."""
    if not args.dump_html:
        return
    path = args.dump_html if args.pages == 1 and page_num == 1 else \
        f"{args.dump_html}.page{page_num}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    log.info("Saved the document the parser sees to %s (%d bytes).",
             path, len(text))


def _save_debug(args, page_num: int, text: str) -> str:
    path = f"{args.out}_page{page_num}_debug.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    return path


def _navigate(ops, url: str, mode: str, expect_id: Optional[str],
              solves: List[int]):
    """One navigation, read until it is something. Returns
    (state, text, final_url, error)."""
    try:
        status, final_url = ops.goto(url)
    except TransportError as e:
        return "load_failed", "", "", str(e)
    final_url = final_url or url
    text = ops.document_text()
    state = classify(text, status, final_url, mode, expect_id)
    waited = 0
    while state == "unknown" and waited < READY_WAIT_MS:
        ops.wait_ms(READY_POLL_MS)
        waited += READY_POLL_MS
        text = ops.document_text()
        state = classify(text, None, final_url, mode, expect_id)
    if state == "challenge" and should_solve(state) and solves[0] < SOLVES_PER_PAGE:
        solves[0] += 1
        if ops.solve_captcha():
            text = ops.document_text()
            state = classify(text, None, final_url, mode, expect_id)
    return state, text, final_url, None


def fetch_one_page(ops, args, pool, query: Query, page_num: int,
                   mask: Callable[[str], str] = lambda s: s,
                   currency_hint: Optional[str] = None) -> PageOutcome:
    """Fetch and parse one page. Retries, rotations and debug dumps live here.

    Never raises for an EXPECTED failure: a timeout, a refusal and a dead
    exit are all recorded on the outcome, because what the run should do
    about them differs between the sequential and the concurrent paths.
    """
    url = request_for(query, page_num)
    outcome = PageOutcome(page_num=page_num, url=url)
    mode = query.mode
    expect_id = query.vehicle_ids[page_num - 1] if mode == "vehicle" else None

    has_pool = bool(pool and len(pool) > 1)
    block_retries = 0 if not RETRY_ON_BLOCKED else (
        args.proxy_block_retries if has_pool else BLOCK_RETRIES_WITHOUT_POOL)
    throttles = 0
    solves = [0]
    state, text, final_url, last_error, exit_failed = "unknown", "", "", None, None

    total = len(query.vehicle_ids) if mode == "vehicle" else args.pages
    for block_attempt in range(block_retries + 1):
        log.info("Fetching %s %d/%d: %s", "vehicle" if mode == "vehicle" else "page",
                 page_num, total, url)
        exit_failed = None
        attempt = 0
        while attempt < args.retries:
            attempt += 1
            state, text, final_url, last_error = _navigate(
                ops, url, mode, expect_id, solves)
            if last_error:
                exit_failed = ops.proxy_failure(last_error) or None
                if exit_failed:
                    break  # a different exit is the only thing that helps
            if state == "throttled" and throttles < THROTTLE_RETRIES:
                throttles += 1
                attempt -= 1  # a throttle wait spends its own budget (§24)
                pause = THROTTLE_WAIT_S * throttles
                log.warning("Rate-limited on page %d (HTTP 429) — waiting %.0fs "
                            "at the same exit (%d/%d).", page_num, pause,
                            throttles, THROTTLE_RETRIES)
                ops.wait_ms(int(pause * 1000))
                continue
            if state in ("load_failed", "unknown") and attempt < args.retries:
                pause = args.retry_delay * (2 ** (attempt - 1))
                log.warning("Page %d came back %s (attempt %d/%d)%s — retrying "
                            "in %.1fs.", page_num, state, attempt, args.retries,
                            f": {mask(last_error)}" if last_error else "", pause)
                ops.wait_ms(int(pause * 1000))
                continue
            break

        if exit_failed and has_pool and block_attempt < block_retries:
            log.warning("Exit %s is unusable (%s) — rotating to another one "
                        "(%d/%d).", mask(pool.current), exit_failed,
                        block_attempt + 1, block_retries)
            pool.advance(f"unusable exit: {exit_failed}")
            ops.relaunch()
            continue
        if (counts_as_blocked(state) and should_retry(state)
                and block_attempt < block_retries):
            name = detect_bot_challenge(text) or state
            if has_pool:
                log.warning("Page %d refused (%s) at %s — rotating to another "
                            "exit (%d/%d).", page_num, name, mask(pool.current),
                            block_attempt + 1, block_retries)
                pool.advance(f"refused: {name}")
            else:
                log.warning("Page %d refused (%s) — re-fetching once from a "
                            "fresh browser.", page_num, name)
            ops.relaunch()
            continue
        break

    outcome.state = state
    outcome.final_url = final_url
    if state == "load_failed" or exit_failed:
        outcome.load_failed = True
        log.error("Gave up on page %d: %s", page_num,
                  mask(last_error or "the navigation never completed"))
        return outcome
    if counts_as_blocked(state):
        outcome.blocked_by = detect_bot_challenge(text) or state
        debug = _save_debug(args, page_num, text)
        log.error("Blocked by %s on page %d — saved to %s. This is exit 3, "
                  "distinct from an empty search (exit 4). %s",
                  outcome.blocked_by, page_num, debug,
                  refusal_advice(outcome.blocked_by, bool(args.headless)))
        return outcome
    if state == "gone":
        outcome.gone = True
        log.warning("Vehicle %s is no longer listed: the site answered its "
                    "detail page with a results page for another location "
                    "(%s). No row for it.", expect_id, final_url)
        return outcome
    if not should_parse(state):
        # throttled past its budget, or never a page of the site at all
        outcome.load_failed = True
        debug = _save_debug(args, page_num, text)
        log.error("Page %d never came back as one of the site's pages (%s) — "
                  "saved to %s.", page_num, state, debug)
        return outcome

    _dump(args, page_num, text)
    if mode == "listing":
        facts = page_facts(text)
        outcome.total_available = facts.total_results
        outcome.pages_available = facts.pages_available
        outcome.spotlights = len(facts.spotlight_ids)
        outcome.site_query = facts.site_query
        outcome.currency = currency(text) or currency_hint
        asked = site_page_for(query, page_num)
        if state == "content" and facts.served_page != asked:
            outcome.out_of_range = True
            log.info("Asked for page %d, the site served page %d — past the "
                     "end of what it will serve for this search. Its rows are "
                     "not kept (they were already collected).", asked,
                     facts.served_page)
            return outcome
        if page_num == 1:
            outcome.dropped = dropped_segments(url, final_url)
    rows = parse_page(text, query, page_num, outcome.currency)
    outcome.products = rows
    log.info("Parsed %d row(s) from page %d.", len(rows), page_num)
    if mode == "listing" and page_num == 1 and outcome.total_available is not None:
        log.info("The site reports %d match(es), and serves %s page(s) of "
                 "them.", outcome.total_available, outcome.pages_available)
    _core_field_warnings(rows, mode, page_num)
    return outcome


def finish(args, query: Query, outcomes: List[PageOutcome], stop_reason: str,
           blocked: bool) -> int:
    """Merge the pages in PAGE order, write the output, return the exit code.

    One implementation for the three engines, so the merge order, the
    dedupe and the sidecar cannot differ between them (§6).
    """
    rows, seen = [], set()
    for oc in sorted(outcomes, key=lambda o: o.page_num):
        fresh = dedupe_by_key(oc.products, seen, key="sku")
        if len(fresh) < len(oc.products):
            log.info("Page %d: dropped %d duplicate row(s) — the search moved "
                     "between page fetches.", oc.page_num,
                     len(oc.products) - len(fresh))
        rows.extend(fresh)

    ok_pages = [o for o in outcomes if o.ok]
    failed_pages = sorted(o.page_num for o in outcomes if not o.ok)
    last_ok = max([o.page_num for o in ok_pages] or [1])
    extra = {"query": query_summary(query)}
    if query.mode == "listing":
        first = next((o for o in outcomes if o.page_num == 1), None)
        total = getattr(first, "total_available", None)
        available = getattr(first, "pages_available", None)
        reachable = (available * query.page_size) if available is not None else None
        extra.update({
            "total_results": total,
            "pages_available": available,
            "reachable_max": reachable,
            "capped_by_site": (bool(total and reachable is not None
                                    and total > reachable)
                               if total is not None else None),
            "site_query": getattr(first, "site_query", {}) or {},
            "spotlights_seen": sum(o.spotlights for o in outcomes),
        })
        if rows and total:
            log.info("The site reports %d match(es); this run holds %d (%.1f%%).",
                     total, len(rows), 100.0 * len(rows) / total)
        if extra["capped_by_site"]:
            log.info("The site serves at most %d of them for one search (%s "
                     "pages of %d). Narrow the search, or split it by ZIP, "
                     "price or year, to reach the rest.", reachable, available,
                     query.page_size)
    else:
        gone = [query.vehicle_ids[o.page_num - 1] for o in outcomes if o.gone]
        extra.update({"vehicles_requested": len(query.vehicle_ids),
                      "vehicles_gone": gone})
    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=blocked, stop_reason=stop_reason,
        pages_requested=(len(query.vehicle_ids) if query.mode == "vehicle"
                         else args.pages),
        pages_completed=len(ok_pages), pages_failed=failed_pages,
        mode=query.mode, source=SOURCE_DEFAULT,
        start_url=args.url or request_for(query, 1),
        final_url=request_for(query, last_ok), extra=extra)


def _ends_listing(query: Query, outcome: PageOutcome) -> bool:
    """Whether an OK page says the search has nothing further. Never true in
    vehicle mode, where a gone listing is one car rather than the end."""
    return query.mode == "listing" and (outcome.out_of_range
                                         or not outcome.products)


def run_pages(open_ops, close_ops, run_concurrently, args, pool,
              query: Query, concurrency: int,
              mask: Callable[[str], str] = lambda s: s) -> int:
    """The whole run after argument handling, shared by the three engines.

    `open_ops()` returns a ready ops object on `pool`, `close_ops(ops)`
    tears it down, and `run_concurrently(page_nums, currency_hint)` returns
    (outcomes, unattempted, exhausted) for pages 2..N fetched by workers.
    The engines supply those three because a browser's lifecycle (and on
    Playwright, its thread) is the one thing that cannot be shared.
    """
    outcomes: List[PageOutcome] = []
    blocked, stop_reason = False, "completed"
    ops = open_ops()
    try:
        # Page 1 is always fetched alone: it says how many pages there are,
        # and whether the site understood the search at all (§7).
        first = fetch_one_page(ops, args, pool, query, 1, mask)
        outcomes.append(first)
        if first.dropped:
            log.error("The site did not recognise %s and served a WIDER search "
                      "instead: asked for %s, served %s. That is a different "
                      "sample from the one asked for, so nothing is written. "
                      "Check the slug against the site's own URL for that "
                      "make or model (e.g. f150, not f-150).",
                      ", ".join(repr(s) for s in first.dropped), first.url,
                      first.final_url)
            return 2
        if not first.ok:
            stop_reason = stop_reason_for(first)
            blocked = first.blocked_by is not None
        else:
            if query.mode == "vehicle":
                plan = len(query.vehicle_ids)
            else:
                available = first.pages_available
                if available is not None:
                    available = max(0, available - query.first_page + 1)
                plan = pages_to_plan(args.pages, available)
                if plan < args.pages:
                    log.info("Asked for %d page(s); the site serves %s from "
                             "here. Fetching all of them.", args.pages, plan)
            more_pages = (query.mode == "vehicle"
                          or (first.products and not first.out_of_range))
            rest = list(range(2, plan + 1)) if more_pages else []
            if query.mode == "listing" and not first.products:
                stop_reason = "completed"
            if rest and concurrency > 1:
                close_ops(ops)
                ops = None
                log.info("Fetching pages 2-%d across %d workers%s.", plan,
                         concurrency, f" over {len(pool)} exit(s)" if pool else "")
                more, unattempted, exhausted = run_concurrently(rest, first.currency)
                outcomes.extend(more)
                failed = [o for o in more if not o.ok]
                if failed:
                    stop_reason = stop_reason_for(min(failed, key=lambda o: o.page_num))
                    blocked = any(o.blocked_by for o in more)
                elif exhausted:
                    stop_reason = "end_of_listing"
                elif unattempted:
                    stop_reason = "pages_unattempted"
            else:
                for page_num in rest:
                    ops.wait_ms(int(args.delay * 1000))
                    if pool and pool.rotates_per_page():
                        pool.advance(f"per-page rotation, page {page_num}")
                        ops.relaunch()
                    outcome = fetch_one_page(ops, args, pool, query, page_num,
                                             mask, first.currency)
                    outcomes.append(outcome)
                    if not outcome.ok:
                        stop_reason = stop_reason_for(outcome)
                        blocked = outcome.blocked_by is not None
                        break
                    if _ends_listing(query, outcome):
                        # A property of the DATA, and complete (§7).
                        log.info("Page %d ended the search.", page_num)
                        stop_reason = "end_of_listing"
                        break
    finally:
        if ops is not None:
            close_ops(ops)
    return finish(args, query, outcomes, stop_reason, blocked)


def worker_loop(ops, args, query: Query, work, results, results_lock,
                exhausted, name: str, mask: Callable[[str], str] = lambda s: s,
                currency_hint: Optional[str] = None):
    """One concurrent worker's page loop, after its engine opened `ops`.

    Takes pages until the queue is empty or a page ends the search, which
    sets `exhausted` so the other workers stop taking work too.
    """
    first = True
    while not exhausted.is_set():
        try:
            page_num = work.get_nowait()
        except Exception:  # queue.Empty
            break
        if not first:
            ops.wait_ms(int(args.delay * 1000))
        first = False
        outcome = fetch_one_page(ops, args, ops.pool, query, page_num, mask,
                                 currency_hint)
        with results_lock:
            results.append(outcome)
        if outcome.ok and _ends_listing(query, outcome):
            log.info("[%s] page %d ended the search; stopping dispatch.",
                     name, page_num)
            exhausted.set()


def concurrency_for(args, pool) -> int:
    """How many workers this run may use, with the warnings said once for
    all three engines."""
    concurrency = max(1, args.concurrency)
    if concurrency <= 1:
        return 1
    if concurrency_limit(args.cdp_endpoint) == 1:
        log.warning("--concurrency is ignored with --cdp-endpoint: the Scraping "
                    "Browser API allows one live connection per profile, and "
                    "several workers would collide on it (profile_locked). Use "
                    "several pids instead.")
        return 1
    if not pool:
        log.warning("--concurrency %d with no proxy pool: every worker leaves "
                    "from the SAME address, which is N times the request rate "
                    "from it. Pass --proxy-file to spread the load.",
                    concurrency)
    if concurrency > 4:
        log.warning("--concurrency %d means %d HEADFUL browsers at once "
                    "(~300-500 MB each on this site's pages).", concurrency,
                    concurrency)
    return concurrency


def worker_pool(pool, worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Workers start on distinct exits and share no mutable state, so rotation
    needs no lock (§7).
    """
    if not pool:
        return None
    from proxy_pool import ProxyPool
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


# ---------------------------------------------------------------------------
# The Scraping Browser connection
# ---------------------------------------------------------------------------

def cdp_connect_hint(error_text: str) -> str:
    """What a failed --cdp-endpoint connection means, from its status.

    Two answers that want opposite fixes. A profile's credentials last about
    a day, so a 401 is almost always an expired endpoint; a 500 is almost
    always a pid another run still holds (§26).
    """
    if "401" in (error_text or ""):
        return ("HTTP 401: the endpoint's credentials were refused. A Scraping "
                "Browser profile's credentials last about a day, so an "
                "endpoint copied from an older .env has usually expired. "
                "Get a fresh one from your 2Captcha dashboard.")
    return ("A Scraping Browser profile allows ONE live connection at a time, "
            "so an HTTP 500 here usually means another run still holds this "
            "`pid`. Wait for it to finish, or use a different pid.")


# A profile stays `profile_locked` for 1.6-1.9 s after a clean disconnect
# (measured in a sibling repo, 3 of 3). Three attempts 3 s apart ride that
# out, and a profile genuinely held by another run still fails, after ~9 s,
# with the pid explanation.
CDP_CONNECT_ATTEMPTS = 3
CDP_LOCKED_WAIT_S = 3.0
# pyppeteer does not surface the 500 at all: its connect() waits on a future
# the rejected handshake never resolves, so only a timeout ends it.
CDP_CONNECT_TIMEOUT_S = 10


def cdp_should_retry(error_text: str) -> bool:
    """Whether a failed --cdp-endpoint connection is worth another attempt:
    a locked profile (500) or a connect that never answered. A 401 is not:
    expired credentials stay expired."""
    text = error_text or ""
    if "401" in text:
        return False
    return ("profile_locked" in text or " 500" in text or "HTTP 500" in text
            or "did not return within" in text)
