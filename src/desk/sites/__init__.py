"""The site modules, and the one place that knows which of them exist.

Sites are added one at a time, in the order `spec/search.yaml` declares. A
module registered here is still not fetched unless the spec enables it: the
spec decides what runs, this file only decides what is available to run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import alljobs, drushim, gotfriends, xplace
from .base import (
    Fetcher,
    RawPosting,
    SiteError,
    SiteResult,
    Throttle,
    ThrottledFetcher,
    search_terms,
)
from .http import FetchError, FixtureFetcher, HttpFetcher

Crawler = Callable[..., SiteResult]

MODULES: dict[str, Crawler] = {
    alljobs.SITE: alljobs.crawl,
    drushim.SITE: drushim.crawl,
    gotfriends.SITE: gotfriends.crawl,
    xplace.SITE: xplace.crawl,
}


def available() -> list[str]:
    return sorted(MODULES)


def rate_limit(spec: dict[str, Any], site: str) -> float:
    for entry in spec.get("sites", []):
        if entry["id"] == site:
            return float(entry.get("rate_limit_rps", 0.5))
    raise KeyError(f"{site} is not in the spec")


__all__ = [
    "FetchError",
    "Fetcher",
    "FixtureFetcher",
    "HttpFetcher",
    "MODULES",
    "RawPosting",
    "SiteError",
    "SiteResult",
    "Throttle",
    "ThrottledFetcher",
    "alljobs",
    "drushim",
    "gotfriends",
    "xplace",
    "search_terms",
    "available",
    "rate_limit",
]
