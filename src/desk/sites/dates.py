"""Hebrew posting dates, as the boards write them.

Shared rather than per-site: AllJobs and Drushim state ages in the same
wording, and a second copy of this would drift.

The rule that matters is at the bottom. A date that does not parse is not
guessed at and not dropped — it comes back empty, the board's own wording
travels alongside it, and the freshness gate decides. A scraper that invents
a timestamp it could not read silently passes or fails that gate, and nothing
downstream can tell.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_RELATIVE = re.compile(r"לפני\s+(\d+)?\s*(דקה|דקות|שעה|שעות|יום|ימים|שבוע|שבועות|חודש|חודשים)")
_BARE_DAYS = re.compile(r"^(\d+)\s+ימים")
_ABSOLUTE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})")

_UNITS = {
    "דקה": "minutes",
    "דקות": "minutes",
    "שעה": "hours",
    "שעות": "hours",
    "יום": "days",
    "ימים": "days",
    "שבוע": "weeks",
    "שבועות": "weeks",
}

_MONTHS = {"חודש": 30, "חודשים": 30}


def parse_date(raw: str, *, now: datetime) -> tuple[str, bool]:
    """Turn a board's wording into ISO 8601, or admit that it did not parse."""
    text = (raw or "").strip()
    if not text:
        return "", False

    if "אתמול" in text:
        return (now - timedelta(days=1)).isoformat(timespec="seconds"), True
    if "היום" in text:
        return now.isoformat(timespec="seconds"), True

    match = _RELATIVE.search(text)
    if match:
        amount = int(match.group(1)) if match.group(1) else 1
        unit = match.group(2)
        if unit in _MONTHS:
            delta = timedelta(days=amount * _MONTHS[unit])
        else:
            delta = timedelta(**{_UNITS[unit]: amount})
        return (now - delta).isoformat(timespec="seconds"), True

    bare = _BARE_DAYS.match(text)
    if bare:
        return (now - timedelta(days=int(bare.group(1)))).isoformat(timespec="seconds"), True

    absolute = _ABSOLUTE.search(text)
    if absolute:
        day, month, year = (int(group) for group in absolute.groups())
        year += 2000 if year < 100 else 0
        try:
            return datetime(year, month, day).isoformat(timespec="seconds"), True
        except ValueError:
            return "", False

    return "", False
