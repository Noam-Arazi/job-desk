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


# Towns rejected one by one rather than by the region they sit in. They need a
# region label to travel through the same machinery as the rest, and this is it.
REJECTED = "rejected_by_name"


def city_index(spec: Mapping[str, Any]) -> dict[str, str]:
    """Every city the spec names, mapped to its region.

    Empty when the spec states no city data, which is a real state and not an
    error: this gate then reports `unknown` rather than inventing a geography.

    `exclude_cities` joins the same index under a reserved label. A town on that
    list is rejected wherever it sits, including inside an accepted region —
    which is the whole reason the list exists rather than a region edit.
    """
    geography = spec.get("geography") or {}
    index: dict[str, str] = {}
    for region, names in (geography.get("cities") or {}).items():
        for name in names or ():
            index[readable(str(name))] = str(region)
    for name in geography.get("exclude_cities") or ():
        index[readable(str(name))] = REJECTED
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
            if not _inside_a_word(text, start, end) and not any(
                s <= start and end <= e for s, e in consumed
            ):
                consumed.append((start, end))
                found.append((city, index[city], start))
                break
            start = text.find(city, end)
    return sorted(found, key=lambda f: f[2])


# Hebrew glues its prepositions and its definite article onto the front of the
# next word, so "ברעננה" is where the job is and not a different word. One such
# letter is allowed in front of a city name; two Hebrew letters mean the match
# is a fragment of something else.
_PREFIXES = "בלמהוש כ".replace(" ", "")


def _inside_a_word(text: str, start: int, end: int) -> bool:
    """Whether the match is a fragment of a longer Hebrew word.

    Short names make this necessary: "לוד" is three letters and appears inside
    ordinary words, and the gate falls back to reading the body, where a chance
    fragment would place a job in a town nobody mentioned.
    """
    after = text[end] if end < len(text) else " "
    if _is_hebrew_letter(after):
        return True
    before = text[start - 1] if start > 0 else " "
    if not _is_hebrew_letter(before):
        return False
    if before not in _PREFIXES:
        return True
    two_back = text[start - 2] if start > 1 else " "
    return _is_hebrew_letter(two_back)


def _is_hebrew_letter(char: str) -> bool:
    return "\u05d0" <= char <= "\u05ea"


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
    excluded = {str(r) for r in geography.get("exclude_regions") or ()} | {REJECTED}
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
        regions = sorted({h[1] for h in in_excluded})
        if regions == [REJECTED]:
            why = "every place named is one the spec rejects by name"
        else:
            named = ", ".join(r for r in regions if r != REJECTED)
            why = f"every city named is in an excluded region ({named})"
        return GateResult(
            GATE,
            Verdict.BLOCK,
            reason=why,
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

    "עבודה מהבית" is excluded, and the exclusion is the point: it is not a town
    the spec forgot, it is a posting the remote rule already handles, and it was
    the single most frequent entry here until it was taken out. A report of
    missing data that is mostly not missing data does not get read.
    """
    index = city_index(spec)
    return [
        loc
        for loc in locations
        if loc and not _found_cities(readable(loc), index) and not _remote_marker(readable(loc))
    ]
