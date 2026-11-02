"""Is the job still open? Asked of the digest's items, just before delivery.

02.11.2026: the top two items were closed when Noam opened them — Migdal on
alljobs, Peak Innovation on jobify. Both boards still served them in search at
04:00, so nothing the fetch reads could have said so. Only the posting's own
page does, in a sentence of the board's own.

Unknown is never closed. A page that would not load, or a site with no known
marker, keeps its item: a board that is down costs the check, not the job.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

# Read off real closed pages, 02.11.2026, and nothing else. A marker that was
# guessed would close jobs on a rewording nobody saw.
MARKERS: dict[str, tuple[str, ...]] = {
    "alljobs": ('סומנה ע"י המעסיק כלא אקטואלית',),
    "jobify": ("המשרה הזאת סגורה כעת",),
}

# Each refill can bring up another closed job. A few rounds clear a bad morning;
# walking the whole ranking one page at a time is not worth the requests.
ROUNDS = 3


def is_closed(site: str, html: str) -> bool | None:
    """True when the page says it is closed, None when this site cannot be judged."""
    markers = MARKERS.get(site)
    if not markers:
        return None
    return any(marker in html for marker in markers)


class _Item(Protocol):
    fingerprint: str
    site: str
    url: str


class _Digest(Protocol):
    @property
    def items(self) -> Any: ...


D = TypeVar("D", bound=_Digest)


def drop_closed(
    build: Callable[[], D],
    *,
    check: Callable[[Any], bool | None],
    close: Callable[[Any], None],
    rounds: int = ROUNDS,
) -> tuple[D, list[Any]]:
    """Build, close what the pages say is closed, and build again until clean.

    `close` has to make the next `build` leave the item out — moving it to the
    `closed` state does that, and keeps it out of tomorrow's digest too.
    """
    gone: list[Any] = []
    checked: set[str] = set()
    for _ in range(rounds):
        today = build()
        found = []
        for item in today.items:
            if item.fingerprint in checked:
                continue
            checked.add(item.fingerprint)
            if check(item) is True:
                found.append(item)
        if not found:
            return today, gone
        for item in found:
            close(item)
        gone.extend(found)
    return build(), gone


def page_checker(fetchers: dict[str, Callable[[str], str]]) -> Callable[[Any], bool | None]:
    """A `check` that reads each item's page with its site's fetcher."""

    def check(item: Any) -> bool | None:
        fetch = fetchers.get(item.site)
        if fetch is None or not item.url or item.site not in MARKERS:
            return None
        try:
            return is_closed(item.site, fetch(item.url))
        except Exception:
            return None

    return check
