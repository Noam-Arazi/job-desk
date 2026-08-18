"""Where the job is — read out of a field that is usually not one place.

Measured against the store before this was written, and the field is messier
than its name suggests. Of 191 AllJobs rows, 101 read

    מספר מקומות חיפה תל אביב הרצליה ...

— an unpunctuated run of city names — and 43 name no city at all and say only
"עבודה מהבית". So this gate cannot compare a location to a region. It tokenizes,
collects every city it recognises, and passes if ANY of them is in an accepted
region, because a role offered in Haifa and in Beer Sheva is a role in Haifa.

The city lists live in `spec/search.yaml`, not here. There is no commute ceiling
anywhere in this module: the spec deliberately filters on region only and leaves
distance to be reported per item, so the human decides.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .result import GateResult, Verdict
from .text import quote, readable

GATE = "geography"

# How the boards say "no fixed place". These are wordings, not criteria — the
# criterion (remote is acceptable when it is Israel-based) is the spec's.
_REMOTE_MARKERS = (
    "עבודה מהבית",
    "עבודה מרחוק",
    "מהבית",
    "היברידי",
    "hybrid",
    "remote",
    "work from home",
)

# A remote posting on an Israeli board is an Israeli remote posting. The spec
# excludes international remote, and none of the enabled sites list it.
_ISRAELI_BOARDS = ("alljobs", "drushim", "gotfriends", "jobify", "xplace", "linkedin")


def city_index(spec: Mapping[str, Any]) -> dict[str, str]:
    """Every city the spec names, mapped to its region.

    Empty when the spec states no city data, which is a real state and not an
    error: this gate then reports `unknown` rather than inventing a geography.
    """
    cities = (spec.get("geography") or {}).get("cities") or {}
    index: dict[str, str] = {}
    for region, names in cities.items():
        for name in names or ():
            index[readable(str(name))] = str(region)
    return index


def _found_cities(text: str, index: Mapping[str, str]) -> list[tuple[str, str, int]]:
    """Every known city in the text, longest name first.

    Longest first so "קרית ביאליק" is consumed before the "קרית" inside it and
    the row is not credited to two cities it names once.
    """
    found: list[tuple[str, str, int]] = []
    consumed: list[tuple[int, int]] = []
    for city in sorted(index, key=len, reverse=True):
        start = text.find(city)
        while start != -1:
            end = start + len(city)
            if not any(s <= start and end <= e for s, e in consumed):
                consumed.append((start, end))
                found.append((city, index[city], start))
                break
            start = text.find(city, end)
    return sorted(found, key=lambda f: f[2])


def check(
    *,
    spec: Mapping[str, Any],
    location: str,
    title: str = "",
    body: str = "",
    site: str = "",
) -> GateResult:
    geography = spec.get("geography") or {}
    accepted = {str(r) for r in geography.get("regions") or ()}
    excluded = {str(r) for r in geography.get("exclude_regions") or ()}
    index = city_index(spec)

    if not index:
        return GateResult(
            GATE,
            Verdict.UNKNOWN,
            reason="the spec names regions but no cities, so no location can be placed",
        )

    # The location field first, and only then the prose. A city named in a
    # sentence about the company's other offices is not where the job is.
    where = readable(location)
    hits = _found_cities(where, index)
    read_from = "location"
    if not hits:
        where = readable(title, body)
        hits = _found_cities(where, index)
        read_from = "title and body"

    in_accepted = [h for h in hits if h[1] in accepted]
    in_excluded = [h for h in hits if h[1] in excluded]

    if in_accepted:
        city, region, at = in_accepted[0]
        return GateResult(
            GATE,
            Verdict.PASS,
            reason=f"{city} is in {region}",
            evidence=quote(where, at, at + len(city)),
            details={
                "read_from": read_from,
                "cities": [h[0] for h in hits],
                "regions": sorted({h[1] for h in hits}),
                "accepted_regions": sorted({h[1] for h in in_accepted}),
            },
        )

    if in_excluded:
        city, region, at = in_excluded[0]
        named = ", ".join(sorted({h[1] for h in in_excluded}))
        return GateResult(
            GATE,
            Verdict.BLOCK,
            reason=f"every city named is in an excluded region ({named})",
            evidence=quote(where, at, at + len(city)),
            details={
                "read_from": read_from,
                "cities": [h[0] for h in hits],
                "regions": sorted({h[1] for h in hits}),
            },
        )

    remote = _remote_marker(readable(location, title, body))
    if remote:
        if site and site not in _ISRAELI_BOARDS:
            return GateResult(
                GATE,
                Verdict.UNKNOWN,
                reason=f"remote, and {site} is not known to be Israel-based",
                evidence=remote,
            )
        return GateResult(
            GATE,
            Verdict.PASS,
            reason="no city named, and the posting is remote",
            evidence=remote,
            details={"remote": True},
        )

    return GateResult(
        GATE,
        Verdict.UNKNOWN,
        reason="no city this gate recognises and no remote wording",
        evidence=readable(location)[:80],
    )


def _remote_marker(text: str) -> str:
    for marker in _REMOTE_MARKERS:
        if marker in text:
            return marker
    return ""


def unknown_cities(spec: Mapping[str, Any], locations: Iterable[str]) -> list[str]:
    """Location strings this gate could place nothing in.

    Not used by the gate itself. It exists so a run can report what the spec's
    city lists are missing, instead of the lists silently going stale as the
    boards start naming towns nobody wrote down.
    """
    index = city_index(spec)
    return [loc for loc in locations if loc and not _found_cities(readable(loc), index)]
