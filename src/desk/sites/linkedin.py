"""LinkedIn — the fifth site module, read at a human pace and logged out.

What is *not* done here, and the distinction is the whole point: no login, no
cookie, no session, no TLS impersonation, no anti-bot bypass, no CAPTCHA
solving, and nothing behind an authentication wall. The endpoints below are the
ones LinkedIn serves to a logged-out browser, and they are read at the spec's
rate. `stealth: false` in the spec is not decoration — the fetching layer has no
bypass to switch on.

**Two endpoints, no key.** Verified by hand on 2026-08-19::

    search  /jobs-guest/jobs/api/seeMoreJobPostings/search
    detail  /jobs-guest/jobs/api/jobPosting/<id>

**Freshness is server-side, and that inverts the crawl.** `f_TPR=r<seconds>`
is applied by LinkedIn, so everything that comes back is already inside the
window. Measured on one term: `r604800` returned exactly 2026-08-12 through
2026-08-19 and nothing older. That is the opposite of every other board here,
where the window is ours to enforce after the fact.

It matters because the unfiltered feed **is not ordered by date at all** — a
posting four months old came back on the first page of a plain search. The
shared `crawl_terms` helper stops on the first page that is entirely older than
the window, which on an unordered feed would stop on page one and call it a
clean run. This module therefore does its own walk, and asks the server for the
window rather than sorting it out afterwards.

**The end of the feed is honest, which is rare here.** Paging past the last
result returns HTTP 200 with a 26-byte body — a doctype and an empty comment.
Drushim answers an unknown page with page one, GotFriends answers it with the
last page, and both traps cost a module each. LinkedIn simply says there is
nothing more. Anything under `END_OF_RESULTS_BYTES` is that sentinel and not a
parse failure, and the distinction is pinned by a test: counting the sentinel as
a broken parse would make an exhausted feed look like a broken scraper.

**Offsets overlap, so ids decide.** `start` steps by ten, but consecutive
offsets return overlapping sets — one measured page brought six new ids out of
ten. So "this page added nothing new" is not a stop condition here, only the
sentinel is, and every posting is keyed by its `urn:li:jobPosting` id.

**The card carries an exact date.** `<time datetime="2026-08-17">` — an ISO
date, stated by the board, on every card. No other module here gets one:
AllJobs and Drushim publish relative Hebrew wording that has to be resolved
against a clock, and GotFriends publishes nothing at all.

**The description is not on the card, and it is worth one request.** A card
states title, company, location, date and URL. The body — which is what the
resolver scores on and what both prose-reading gates need — is only on the
detail endpoint, one request per posting. That cost is real, so it is bounded
by `max_details` and every posting left without a body is counted in
`skipped` rather than returned as a thin row that looks complete.

**The seniority band is passed through as-is, and it is not a year count.**
The detail page states "Entry level" / "Mid-Senior level" / "Not Applicable".
Only the first of those is a fact the seniority gate can use — it reads as a
floor of zero — and the rest carry no figure, so the gate falls through to the
prose exactly as it does on AllJobs. Passing a band into a field named for
years is worth stating: the gate consults `stated_experience` first and would
trust a number found there without the proximity test it applies to prose.
There is no number in these strings to trust, and a test pins that.

**Tracking parameters are stripped from the stored URL.** The href on a card
carries `?position=&pageNum=&refId=&trackingId=`, and the last two are minted
per request. Stored as-is, the same posting would look like a different row on
every run, and the store's own identity checks would be arguing with LinkedIn's
telemetry.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import http
from .base import Fetcher, RawPosting, SiteResult, search_terms

SITE = "linkedin"

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

# The offset step LinkedIn actually serves. Asking for more per call does not
# change it, so it is stated once here rather than assumed at three call sites.
PAGE_SIZE = 10

# Paging past the last result is HTTP 200 with a doctype and an empty comment,
# 26 bytes on the day this was measured. The threshold sits well above that and
# well below any page carrying even one card (~8KB for three).
END_OF_RESULTS_BYTES = 200

CARD = "div.base-card, div.job-search-card, li div[data-entity-urn]"

FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("h3.base-search-card__title", "h3.base-search-card__title span", "h3"),
    "company": ("h4.base-search-card__subtitle a", "h4.base-search-card__subtitle", "h4"),
    "location": ("span.job-search-card__location", "div.base-search-card__metadata span"),
    "date": ("time.job-search-card__listdate--new", "time.job-search-card__listdate", "time"),
    "link": ("a.base-card__full-link", "a[href*='/jobs/view/']"),
}

DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "body": ("div.show-more-less-html__markup", "div.description__text"),
    "criteria_key": ("h3.description__job-criteria-subheader",),
    "criteria_value": ("span.description__job-criteria-text",),
}

_URN_ID = re.compile(r"urn:li:jobPosting:(\d+)")
_URL_ID = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d+)")

# The one criterion the gates can read. LinkedIn's other three — employment
# type, job function, industries — are kept in the body text rather than given
# their own fields, because nothing downstream asks for them by name.
_SENIORITY_KEY = "seniority level"


def _first(card: Any, selectors: tuple[str, ...]) -> Any:
    for selector in selectors:
        found = card.css(selector)
        if found:
            return found[0]
    return None


def _text(card: Any, selectors: tuple[str, ...]) -> str:
    node = _first(card, selectors)
    if node is None:
        return ""
    return " ".join(str(node.get_all_text()).split())


def clean_url(href: str) -> str:
    """The posting's address without LinkedIn's per-request telemetry.

    `refId` and `trackingId` are minted fresh on every response, so keeping
    them would make one posting look like a new row each run. The path already
    identifies the posting; nothing in the query does.
    """
    if not href:
        return ""
    parts = urlsplit(href)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _identity(card: Any, href: str) -> tuple[str, str]:
    """The posting's id, and where it was read from.

    The URN on the card first — it is LinkedIn's own identifier and it is on
    the element that defines the card. The trailing number of the view URL
    second, for a card whose attribute is missing rather than dropping a
    posting that is perfectly readable.
    """
    urn = _URN_ID.search(str(card.attrib.get("data-entity-urn", "")))
    if urn:
        return urn.group(1), "urn"

    in_url = _URL_ID.search(href)
    if in_url:
        return in_url.group(1), "url"

    return "", "none"


def parse(html: str) -> dict[str, Any]:
    """Pure. No network, no store, no clock.

    No clock because unlike AllJobs and Drushim there is nothing to resolve:
    the board states an ISO date and it is taken as it is. A module that
    accepted a `now` it never used would suggest otherwise.
    """
    page = http.selector(html)
    postings: list[RawPosting] = []
    skipped: dict[str, int] = {}
    missing: dict[str, int] = {}
    seen: set[str] = set()

    for card in page.css(CARD):
        link = _first(card, FIELDS["link"])
        href = str(link.attrib.get("href", "")) if link is not None else ""

        external_id, source = _identity(card, href)
        if not external_id:
            skipped["no id on the card"] = skipped.get("no id on the card", 0) + 1
            continue
        if source != "urn":
            missing["urn"] = missing.get("urn", 0) + 1
        if external_id in seen:
            # One response repeating a posting inside itself. Counted rather
            # than left to the store's UNIQUE(site, external_id) to swallow.
            skipped["a second card with the same id"] = (
                skipped.get("a second card with the same id", 0) + 1
            )
            continue
        seen.add(external_id)

        title = _text(card, FIELDS["title"])
        company = _text(card, FIELDS["company"])
        location = _text(card, FIELDS["location"])

        date_node = _first(card, FIELDS["date"])
        posted_at = str(date_node.attrib.get("datetime", "")) if date_node is not None else ""
        posted_raw = (
            " ".join(str(date_node.get_all_text()).split()) if date_node is not None else ""
        )

        for name, value in (
            ("title", title),
            ("company", company),
            ("location", location),
            ("posted_at", posted_at),
        ):
            if not value:
                missing[name] = missing.get(name, 0) + 1

        postings.append(
            RawPosting(
                site=SITE,
                external_id=external_id,
                title=title,
                company=company,
                location=location,
                url=clean_url(href),
                # Empty here by design: the card has no description and this
                # function makes no requests. `crawl` fills it from the detail
                # endpoint, and counts the ones it could not.
                body="",
                posted_at=posted_at,
                posted_raw=posted_raw,
            )
        )

    return {"postings": postings, "skipped": skipped, "missing": missing}


def parse_detail(html: str) -> dict[str, str]:
    """The description, and the criteria list under it.

    The criteria are folded into the body text rather than dropped: employment
    type and industry are exactly the kind of thing the analyst reads, and the
    board states them in a clean list instead of burying them in prose.

    Only the seniority band comes back as its own field, and the docstring at
    the top of this module says what it is and is not.
    """
    page = http.selector(html)

    body = _text(page, DETAIL_FIELDS["body"])
    keys = [
        " ".join(str(node.get_all_text()).split())
        for node in page.css(DETAIL_FIELDS["criteria_key"][0])
    ]
    values = [
        " ".join(str(node.get_all_text()).split())
        for node in page.css(DETAIL_FIELDS["criteria_value"][0])
    ]

    seniority = ""
    criteria: list[str] = []
    # Not strict: LinkedIn has been seen to render a heading whose value cell
    # is empty, and one malformed criterion is not a reason to lose the
    # description it sits under.
    for key, value in zip(keys, values, strict=False):
        if not key or not value:
            continue
        criteria.append(f"{key}: {value}")
        if key.strip().lower() == _SENIORITY_KEY:
            seniority = value

    if criteria:
        body = (body + "\n" + "\n".join(criteria)).strip()

    return {"body": body, "stated_experience": seniority}


def url_for(term: str, start: int, *, days: int, location: str) -> str:
    """One search request: a term, a place, a window, an offset.

    The window is a parameter and not a constant because the daily run and the
    first backfill want different ones, and both are the spec's numbers.
    """
    params = {"keywords": term, "location": location, "start": max(0, int(start))}
    if days > 0:
        params["f_TPR"] = f"r{int(days) * 86400}"
    return f"{SEARCH_URL}?{urlencode(params)}"


def detail_url(job_id: str) -> str:
    return DETAIL_URL.format(job_id=job_id)


def settings_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The module holds no policy. Location and window come from the spec."""
    for entry in spec.get("sites", []):
        if entry.get("id") == SITE:
            return {
                "location": str(entry.get("location", "Israel")),
                "max_details": int(entry.get("max_details", 200)),
            }
    return {"location": "Israel", "max_details": 200}


def crawl(
    fetcher: Fetcher,
    *,
    spec: dict[str, Any],
    max_pages: int = 6,
    terms: list[str] | None = None,
    days: int = 7,
    with_details: bool = True,
    max_details: int | None = None,
    **_ignored: Any,
) -> SiteResult:
    """Walk each term's offsets until LinkedIn says there is nothing more.

    The stop condition is the sentinel and only the sentinel. It is not "a page
    with nothing new" — offsets here overlap, so a page can legitimately repeat
    most of what came before while more still waits behind it. And it is not
    "the first old posting", because the window was applied by the server and
    an unfiltered feed is not in date order anyway.

    A term that raises is recorded and the walk moves on. One broken query
    never ends the crawl.
    """
    settings = settings_from_spec(spec)
    location = settings["location"]
    detail_budget = settings["max_details"] if max_details is None else max_details

    result = SiteResult(site=SITE)
    found: dict[str, RawPosting] = {}
    matched: dict[str, list[str]] = {}
    skipped: dict[str, int] = {}
    stops: list[str] = []

    for term in terms if terms is not None else search_terms(spec):
        for page in range(max_pages):
            url = url_for(term, page * PAGE_SIZE, days=days, location=location)
            try:
                html = fetcher.get(url)
            except Exception as exc:  # this term's problem, not the run's
                result.errors.append(f"{term!r} offset {page * PAGE_SIZE}: {exc}")
                break

            result.pages_fetched += 1

            # The sentinel is checked before parsing, not after. A 26-byte body
            # parses to zero cards, which is indistinguishable from a page this
            # module failed to read — and the two mean opposite things.
            if len(html.strip()) < END_OF_RESULTS_BYTES:
                stops.append(f"{term!r}: end of results at offset {page * PAGE_SIZE}")
                break

            parsed = parse(html)
            for reason, count in parsed["skipped"].items():
                skipped[reason] = skipped.get(reason, 0) + count

            if not parsed["postings"]:
                stops.append(f"{term!r}: offset {page * PAGE_SIZE} carried no readable card")
                break

            for posting in parsed["postings"]:
                if posting.external_id not in found:
                    found[posting.external_id] = posting
                    matched[posting.external_id] = []
                if term not in matched[posting.external_id]:
                    matched[posting.external_id].append(term)
        else:
            stops.append(f"{term!r}: hit the {max_pages}-offset ceiling")

    if with_details:
        wanted = [p for p in found.values() if not p.body]
        for posting in wanted[:detail_budget]:
            try:
                detail = parse_detail(fetcher.get(detail_url(posting.external_id)))
            except Exception as exc:
                result.errors.append(f"detail {posting.external_id}: {exc}")
                skipped["a detail request that failed"] = (
                    skipped.get("a detail request that failed", 0) + 1
                )
                continue
            result.pages_fetched += 1
            found[posting.external_id] = RawPosting(
                site=posting.site,
                external_id=posting.external_id,
                title=posting.title,
                company=posting.company,
                location=posting.location,
                url=posting.url,
                body=detail["body"],
                posted_at=posting.posted_at,
                posted_raw=posting.posted_raw,
                stated_experience=detail["stated_experience"],
            )
        if len(wanted) > detail_budget:
            # Said out loud rather than left as a silently thin slice of rows.
            # A posting with no body is invisible to the resolver and to both
            # prose gates, so a truncated batch is not a smaller result — it is
            # a result some of whose rows cannot be judged.
            skipped["a body left unfetched at the detail ceiling"] = len(wanted) - detail_budget
            result.notes.append(
                f"{len(wanted) - detail_budget} postings kept their card only: "
                f"the detail ceiling is {detail_budget}"
            )
    else:
        result.notes.append("details were not fetched; every posting carries its card only")

    result.postings = list(found.values())
    result.matched_terms = matched
    result.stopped_because = " · ".join(stops)
    result.skipped = skipped
    return result
