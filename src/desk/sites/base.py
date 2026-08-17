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
from typing import Protocol

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
