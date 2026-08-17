"""The only place Scrapling is imported.

Two reasons it is isolated here rather than used directly in a site module.

The library is an optional dependency, so the offline replay path stays light
and a clean clone runs the demo without it. And swapping it later is then a
change to this file, not to every parser.

Scrapling is used for two distinct things and it is worth keeping them apart:
the HTTP engine here, and adaptive element relocation in the parsers, which
re-finds an element by structural similarity after a layout change instead of
letting a selector break silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class FetchError(Exception):
    pass


def _scrapling() -> Any:
    try:
        from scrapling.fetchers import Fetcher
    except ImportError as exc:  # pragma: no cover - exercised by hand, not in CI
        raise FetchError(
            "the fetch extra is not installed. `uv sync --extra fetch`"
        ) from exc
    return Fetcher


def selector(html: str) -> Any:
    """Parse HTML. Kept here so no parser imports the library directly."""
    try:
        from scrapling import Selector
    except ImportError as exc:  # pragma: no cover
        raise FetchError("the fetch extra is not installed. `uv sync --extra fetch`") from exc
    return Selector(html)


class HttpFetcher:
    """Plain HTTP. No stealth, no fingerprint impersonation, no anti-bot bypass.

    That restraint is deliberate and it is the project's position, not an
    oversight: these boards are read at a polite rate with an honest client.
    The one site that forbids scraping is reached through the human's own
    logged-in browser instead, in a module that can be switched off.
    """

    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout

    def get(self, url: str) -> str:
        fetcher = _scrapling()
        response = fetcher.get(url, timeout=self.timeout)
        status = getattr(response, "status", 0)
        if status != 200:
            raise FetchError(f"{status} for {url}")
        return str(response.html_content)


class FixtureFetcher:
    """Serves saved pages. The parser tests and the offline path use this.

    Unknown URLs raise rather than returning an empty page: a test that
    silently parses nothing passes for the wrong reason.
    """

    def __init__(self, pages: dict[str, Path | str]) -> None:
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url: str) -> str:
        self.requested.append(url)
        for key, page in self.pages.items():
            if key in url:
                # Typed, not sniffed. Asking whether a whole HTML document
                # "exists" as a path returns False on macOS and raises
                # ENAMETOOLONG on Linux, so sniffing passed here and failed
                # in CI.
                return page.read_text(encoding="utf-8") if isinstance(page, Path) else str(page)
        raise FetchError(f"no fixture for {url}")
