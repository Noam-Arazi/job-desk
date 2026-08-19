"""What every site module is, and what none of them may assume.

A site module's only job is to turn one job board into `RawPosting` objects.
It does no filtering beyond what the board itself can do server-side, it makes
no model calls, and it never decides whether a posting is relevant — that is
the analyst's job in session 5, working from `spec/search.yaml`.

Three rules hold for all of them:

    a module returns normalized objects, never library types
        Swapping the fetching library is then a local change. Nothing outside
        this package imports Scrapling.

    a module that fails does not fail the run
        `crawl` catches per page. A dead site returns `ok=False` with its
        errors attached, and the other sites still produce a digest.

    every request goes through the throttle
        Rate is per site, read from spec/search.yaml. There is no code path
        that fetches without passing through it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..store import Posting


@dataclass(frozen=True)
class RawPosting:
    """One posting as the board stated it. No interpretation applied.

    `posted_raw` keeps the board's own wording — "לפני 3 שעות", "1 ימים" —
    next to the parsed timestamp. When a date parse is wrong, the trace shows
    what the board actually said rather than only what we made of it.
    """

    site: str
    external_id: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    body: str = ""
    posted_at: str = ""  # ISO 8601, empty when the board's wording did not parse
    posted_raw: str = ""
    work_arrangement: str = ""
    # Some boards state the experience they want as its own field. Where they
    # do, the seniority gate reads it instead of inferring it from prose.
    stated_experience: str = ""

    def to_posting(self) -> Posting:
        return Posting(
            site=self.site,
            external_id=self.external_id,
            title=self.title,
            company=self.company,
            location=self.location,
            url=self.url,
            body=self.body,
            posted_at=self.posted_at,
            stated_experience=getattr(self, "stated_experience", ""),
        )


class SiteError(Exception):
    """A site module gave up. Caught by the caller; never escapes a run."""


@dataclass
class SiteResult:
    site: str
    postings: list[RawPosting] = field(default_factory=list)
    pages_fetched: int = 0
    stopped_because: str = ""
    errors: list[str] = field(default_factory=list)
    # What the module dropped on purpose, by reason. Never empty silently:
    # a scraper that quietly discards half its input reads as complete
    # coverage when it is not.
    skipped: dict[str, int] = field(default_factory=dict)
    # Which search terms reached each posting, by external id. A posting found
    # by several terms is stored once; the terms that found it are a prior for
    # routing it to a CV family later.
    matched_terms: dict[str, list[str]] = field(default_factory=dict)
    # Things the module wants the human to know that are not failures: a board
    # that publishes no dates, an argument that could not reach it. Without
    # somewhere to say them, a module has only the choice between raising over
    # a non-error and staying quiet about something that matters.
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> dict[str, object]:
        return {
            "site": self.site,
            "postings": len(self.postings),
            "pages": self.pages_fetched,
            "skipped": sum(self.skipped.values()),
            "stopped": self.stopped_because,
            "errors": len(self.errors),
        }


class Fetcher(Protocol):
    """The one call a site module is allowed to make against the network."""

    def get(self, url: str) -> str: ...


def search_terms(spec: dict[str, Any]) -> list[str]:
    """Every term the spec declares, in family order, without repeats.

    Site-agnostic on purpose: the terms are the spec's, not a board's, and two
    modules reading two copies of this would drift.

    A term that finds nothing is not pruned here. The spec says the width was
    raised deliberately and comes down on measurement in session 5, not on a
    hunch in the fetching layer.
    """
    terms: list[str] = []
    for family in spec.get("families", {}).values():
        for term in [*family.get("terms_he", []), *family.get("terms_en", [])]:
            if term not in terms:
                terms.append(term)
    return terms


def crawl_terms(
    *,
    site: str,
    fetcher: Fetcher,
    url_for: Callable[[str, int, Any], str],
    parse_page: Callable[[str], dict[str, Any]],
    terms: list[str],
    variants: list[Any],
    cutoff: str,
    max_pages: int,
) -> SiteResult:
    """Search each term until it stops yielding anything inside the window.

    Shared by every site, because the awkward parts are the same everywhere and
    are the parts worth getting right once:

    Boards do not order pages strictly by date and consecutive pages repeat
    listings, so the stop condition cannot be the first old item. It is a page
    that contributed nothing fresh at all.

    A page that raises is recorded and that term moves on. One broken query
    never ends the crawl, and the result still carries what was collected.

    The same posting is reached by several terms. That is not waste — the terms
    that found it are recorded as a prior for routing it to a CV family later —
    and it is still kept once.
    """
    result = SiteResult(site=site)
    found: dict[str, RawPosting] = {}
    matched: dict[str, list[str]] = {}
    skipped: dict[str, int] = {}
    stops: list[str] = []

    for term in terms:
        for variant in variants:
            label = f"{term!r}" + (f" variant {variant}" if variant not in ("", None) else "")
            for page in range(1, max_pages + 1):
                try:
                    parsed = parse_page(fetcher.get(url_for(term, page, variant)))
                except Exception as exc:  # this page's problem, not the run's
                    result.errors.append(f"{label} page {page}: {exc}")
                    break

                result.pages_fetched += 1
                for reason, count in parsed["skipped"].items():
                    skipped[reason] = skipped.get(reason, 0) + count

                fresh = 0
                new_here = 0
                for posting in parsed["postings"]:
                    if posting.posted_at and posting.posted_at < cutoff:
                        continue
                    fresh += 1
                    if posting.external_id not in found:
                        found[posting.external_id] = posting
                        matched[posting.external_id] = []
                        new_here += 1
                    if term not in matched[posting.external_id]:
                        matched[posting.external_id].append(term)

                if not parsed["postings"]:
                    stops.append(f"{label}: empty page {page}")
                    break
                if fresh == 0:
                    stops.append(f"{label}: page {page} was entirely older than the window")
                    break
                # "Nothing new", not "nothing fresh". A board that answers every
                # page number with its last page returns postings that are
                # perfectly fresh and already held, and counting those as
                # progress walks to the page ceiling fetching the same page over
                # and over. One of the three boards here does exactly that, and
                # its own module already stops this way; the shared helper the
                # other two use did not.
                if new_here == 0:
                    stops.append(f"{label}: page {page} added nothing new")
                    break
            else:
                stops.append(f"{label}: hit the {max_pages}-page ceiling")

    result.postings = list(found.values())
    result.matched_terms = matched
    result.stopped_because = " · ".join(stops)
    result.skipped = skipped
    return result


class Throttle:
    """Requests per second, per site. Sleeps rather than dropping.

    The clock and the sleep are injected so a test can assert the pacing
    without spending the wall-clock time it describes.
    """

    def __init__(
        self,
        rps: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if rps <= 0:
            raise ValueError("rps must be positive")
        self.interval = 1.0 / rps
        self._clock = clock
        self._sleep = sleeper
        self._last: float | None = None
        self.waited = 0.0

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self.interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                self.waited += remaining
                now = self._clock()
        self._last = now


class ThrottledFetcher:
    """Binds a fetcher to a throttle so no module can fetch around it."""

    def __init__(self, fetcher: Fetcher, throttle: Throttle) -> None:
        self._fetcher = fetcher
        self._throttle = throttle
        self.urls: list[str] = []

    def get(self, url: str) -> str:
        self._throttle.wait()
        self.urls.append(url)
        return self._fetcher.get(url)
