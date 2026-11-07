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
The detail page states one of a fixed set — "Entry level", "Associate",
"Mid-Senior level", "Director", "Internship", "Not Applicable", all six seen
in one real run. Only the first of those is a fact the seniority gate can use — it reads as a
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
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from . import http
from .base import Fetcher, RawPosting, SiteResult, search_terms

SITE = "linkedin"

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
# The same posting as a page, for the attached browser. It renders the same
# description markup as the guest fragment, so `parse_detail` reads both.
VIEW_URL = "https://www.linkedin.com/jobs/view/{job_id}/"

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
    # The third is the signed-in page the attached browser sees (28.10.2026).
    "body": (
        "div.show-more-less-html__markup",
        "div.description__text",
        "[data-testid='expandable-text-box']",
    ),
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


RATE_LIMIT_RETRY_SECONDS = 30


def _is_rate_limit(exc: Exception) -> bool:
    return str(exc).startswith("429 ")


def _get(fetcher: Fetcher, url: str, *, first_page: bool, pause: Callable[[float], None]) -> str:
    """A term's first page is worth one more try after a 429; a deeper one is not.

    Missing page one loses the whole term for the day. Missing page four loses
    a handful of postings the narrow window has already mostly delivered.
    """
    try:
        return fetcher.get(url)
    except Exception as exc:
        if not (first_page and _is_rate_limit(exc)):
            raise
        pause(RATE_LIMIT_RETRY_SECONDS)
        return fetcher.get(url)


def window_days(last_fetch: datetime | None, now: datetime, *, ceiling: float = 7) -> float:
    """How far back to ask LinkedIn for: the time since the last fetch, plus six hours.

    07.11.2026. The guest feed is not ordered by date and ignores `sortBy=DD`
    (measured: identical cards with and without it), so a seven-day window read
    six pages deep sampled the week instead of covering what was new. A two-day
    window still filled all six offsets on broad terms, so the window is the
    exact gap — `f_TPR` takes seconds — with a margin for a run that started
    late. A missed morning widens the next window instead of losing a day.
    """
    if last_fetch is None:
        return ceiling
    elapsed = (now - last_fetch).total_seconds() / 86400 + 0.25
    return max(0.25, min(ceiling, elapsed))


def url_for(term: str, start: int, *, days: float, location: str) -> str:
    """One search request: a term, a place, a window, an offset.

    The window is a parameter and not a constant because the daily run and the
    first backfill want different ones, and both are the spec's numbers.
    """
    params = {"keywords": term, "location": location, "start": max(0, int(start))}
    if days > 0:
        params["f_TPR"] = f"r{round(days * 86400)}"
    return f"{SEARCH_URL}?{urlencode(params)}"


def in_direction(title: str, spec: dict[str, Any]) -> bool:
    """Whether the title alone routes to a family that is switched on. Free:
    the router runs without a model when `ask` is left out."""
    from ..analyst import families
    from ..gates.chain import Candidate

    family = families.route(Candidate(title=title, body="", site=SITE), spec=spec).family
    config = (spec.get("families") or {}).get(family)
    return config is not None and config.get("enabled", True) is not False


def detail_url(job_id: str) -> str:
    return DETAIL_URL.format(job_id=job_id)


def view_url(job_id: str) -> str:
    return VIEW_URL.format(job_id=job_id)


def settings_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The module holds no policy. Location and window come from the spec."""
    for entry in spec.get("sites", []):
        if entry.get("id") == SITE:
            return {
                "location": str(entry.get("location", "Israel")),
                "max_details": int(entry.get("max_details", 200)),
                "max_details_browser": int(entry.get("max_details_browser", 400)),
            }
    return {"location": "Israel", "max_details": 200, "max_details_browser": 400}


def crawl(
    fetcher: Fetcher,
    *,
    spec: dict[str, Any],
    max_pages: int = 6,
    terms: list[str] | None = None,
    days: float = 7,
    with_details: bool = True,
    max_details: int | None = None,
    detail_fetcher: Fetcher | None = None,
    have_body: set[str] | None = None,
    pause: Callable[[float], None] = time.sleep,
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

    `detail_fetcher` is the attached browser when Noam's window is up: details
    are read there, from the page view, under the larger browser budget.
    `have_body` names the external ids the store already holds a description
    for, which are not asked for again — the budget goes to the ones missing.
    """
    settings = settings_from_spec(spec)
    location = settings["location"]
    via_browser = detail_fetcher is not None
    default_budget = settings["max_details_browser" if via_browser else "max_details"]
    detail_budget = default_budget if max_details is None else max_details
    details = detail_fetcher or fetcher
    url_of = view_url if via_browser else detail_url
    known = have_body or set()

    result = SiteResult(site=SITE)
    found: dict[str, RawPosting] = {}
    matched: dict[str, list[str]] = {}
    skipped: dict[str, int] = {}
    stops: list[str] = []

    for term in terms if terms is not None else search_terms(spec):
        for page in range(max_pages):
            url = url_for(term, page * PAGE_SIZE, days=days, location=location)
            try:
                html = _get(fetcher, url, first_page=page == 0, pause=pause)
            except Exception as exc:  # this term's problem, not the run's
                if page > 0 and _is_rate_limit(exc):
                    # 07.11.2026 — the window is now only the days since the
                    # last fetch, so the pages already read hold what is new.
                    # A 429 deeper in is the end of this term, not a failure:
                    # it used to mark every morning failed while delivering.
                    stops.append(f"{term!r}: rate-limited at offset {page * PAGE_SIZE}")
                    break
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
        wanted = [p for p in found.values() if not p.body and p.external_id not in known]
        # The budget is spent on the postings a title already places in a
        # direction Noam is pursuing, and only then on the rest. Scan order
        # used to decide it, and most of the 200 went to roles no family wants.
        wanted.sort(key=lambda p: not in_direction(p.title, spec))
        if known:
            result.notes.append(
                f"{sum(1 for i in found if i in known)} postings already had a body in the store"
            )
        result.notes.append(
            f"details via {'the attached browser' if via_browser else 'the logged-out endpoint'}"
        )
        for posting in wanted[:detail_budget]:
            try:
                detail = parse_detail(details.get(url_of(posting.external_id)))
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
