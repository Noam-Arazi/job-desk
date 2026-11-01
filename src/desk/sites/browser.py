"""Reading pages through the Chrome that Noam leaves open.

28.10.2026. LinkedIn's logged-out detail endpoint answers 429 after a couple
of hundred requests, and 78% of a week's LinkedIn postings reached the store
with no description — and a posting with no description almost never earns a
score. The same page opened in a real browser renders in full, so the detail
requests go there when the window is up.

The window is `deploy/browser/start-chrome.sh`: a profile of its own, because
Chrome refuses a debugging port on the everyday profile. Nothing here logs in,
types, clicks or solves anything. It opens one tab, loads a page at a human
pace, reads the HTML and leaves; the window and its sign-ins are Noam's.

`attach()` returns None when the window is not there, and the caller falls
back to the logged-out path and says so. A morning with the laptop lid shut is
a thinner morning, never a failed one.
"""

from __future__ import annotations

import os
import time
from typing import Any

DEFAULT_PORT = 9222


class BrowserFetcher:
    """A Fetcher over one tab of an already-running Chrome."""

    def __init__(self, playwright: Any, browser: Any, *, pace: float, wait_for: str) -> None:
        self._playwright = playwright
        self._browser = browser
        self._page = browser.contexts[0].new_page()
        self._pace = pace
        self._wait_for = wait_for
        self._last = 0.0

    def get(self, url: str) -> str:
        gap = self._pace - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        self._page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if self._wait_for:
            # Not fatal: a page without the element is still returned, and the
            # parser decides whether it carried anything.
            try:
                self._page.wait_for_selector(self._wait_for, timeout=8_000)
            except Exception:
                pass
        return str(self._page.content())

    def close(self) -> None:
        # The tab is ours, the browser is Noam's: close the one, never the other.
        try:
            self._page.close()
        finally:
            self._playwright.stop()


def attach(*, pace: float = 3.0, wait_for: str = "") -> BrowserFetcher | None:
    """The open window, or None when there is none to attach to."""
    port = int(os.environ.get("DESK_CHROME_PORT", DEFAULT_PORT))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5_000)
        if not browser.contexts:
            raise RuntimeError("no browser context")
        return BrowserFetcher(playwright, browser, pace=pace, wait_for=wait_for)
    except Exception:
        playwright.stop()
        return None
