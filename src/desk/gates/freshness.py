"""How old the posting is — with one board that never says.

GotFriends states no date anywhere: not on the card, not on the posting page,
not in its JSON-LD. All three were checked, and `posted_at` is empty for all 178
of its rows in the store, permanently and by the agency's design. The scraper
does not invent a timestamp it could not read, which leaves this gate with two
jobs it has to keep separate:

    an empty date is not a parse failure, and must never block. A board that
    publishes no dates is not a board publishing stale jobs.

    recency is still measurable there — as the first time the store saw the
    role. It is weaker evidence than a stated date and it is labelled as such,
    but it is evidence, and it is what stops a permanently-listed agency shelf
    from resurfacing forever.

The first-seen the caller passes in is the cluster's earliest, not the
fingerprint's own — see `chain.FirstSeen`. That correction runs in one direction
only: it can make an item older and never fresher, so it can only ever block.

The window is the spec's, including the one-time backfill that fills an empty
store with a month instead of a week.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from .result import GateResult, Verdict

GATE = "freshness"


def window_days(spec: Mapping[str, Any], *, first_run: bool = False) -> int:
    rules = ((spec.get("gates") or {}).get("freshness")) or {}
    if first_run:
        return int(rules.get("first_run_backfill_days", rules.get("max_age_days", 7)))
    return int(rules.get("max_age_days", 7))


def _age_days(stamp: str, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is not None and now.tzinfo is None:
        when = when.replace(tzinfo=None)
    return (now - when).total_seconds() / 86400.0


def check(
    *,
    spec: Mapping[str, Any],
    now: datetime,
    posted_at: str = "",
    first_seen_at: str = "",
    first_run: bool = False,
) -> GateResult:
    limit = window_days(spec, first_run=first_run)
    cutoff = now - timedelta(days=limit)

    stamp, basis = posted_at.strip(), "posted_at"
    if not stamp:
        stamp, basis = first_seen_at.strip(), "first seen in the store"

    if not stamp:
        return GateResult(
            GATE,
            Verdict.UNKNOWN,
            reason="the board states no date and the store has not seen this before",
        )

    age = _age_days(stamp, now)
    if age is None:
        # A stamp that will not parse is a bug in whatever wrote it, not a
        # reason to drop a job. It is surfaced, not acted on.
        return GateResult(
            GATE,
            Verdict.UNKNOWN,
            reason=f"could not read the {basis} timestamp",
            evidence=stamp,
        )

    details = {"basis": basis, "age_days": round(age, 2), "window_days": limit}
    if age > limit:
        return GateResult(
            GATE,
            Verdict.BLOCK,
            reason=f"{age:.0f} days old by {basis}, older than the {limit}-day window",
            evidence=stamp,
            details=details,
        )
    return GateResult(
        GATE,
        Verdict.PASS,
        reason=f"{age:.0f} days old by {basis}, inside the {limit}-day window",
        evidence=stamp,
        details={**details, "cutoff": cutoff.isoformat(timespec="seconds")},
    )
