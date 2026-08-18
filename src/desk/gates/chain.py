"""The gate chain — everything that is decided before a token is spent.

This is the cost engine of the whole system. Every posting that dies here is a
posting the analyst never reads, and the analyst is the only stage that calls a
model. Cutting deterministically first is not an optimisation bolted on at the
end; it is the reason the daily run is affordable at all.

Two properties this chain is built to keep:

    it is total. Every gate runs on every posting, even after one has already
    blocked. Short-circuiting would save nothing measurable — none of these
    gates costs anything — and it would cost the human the answer to "what else
    was wrong with it", which is what tells them whether the spec is too tight.

    it decides nothing of its own. Every threshold, list and window is read from
    spec/search.yaml. Changing a criterion is an edit there, never here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from . import applied, degree, freshness, geography, seniority
from .result import GateReport, GateResult


@dataclass(frozen=True)
class Candidate:
    """What the gates need from a posting, from whichever side it arrives.

    Site modules hand over `RawPosting`; a re-run over what is already stored
    hands over a sqlite row. Both become this, so no gate has to know which.
    """

    title: str = ""
    company: str = ""
    location: str = ""
    body: str = ""
    posted_at: str = ""
    stated_experience: str = ""
    fingerprint: str = ""
    site: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> Candidate:
        return cls(
            title=raw.title,
            company=raw.company,
            location=raw.location,
            body=raw.body,
            posted_at=raw.posted_at,
            stated_experience=getattr(raw, "stated_experience", ""),
            fingerprint=getattr(raw, "fingerprint", "") or "",
            site=raw.site,
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Candidate:
        return cls(
            title=row.get("title") or "",
            company=row.get("company") or "",
            location=row.get("location") or "",
            body=row.get("body") or "",
            posted_at=row.get("posted_at") or "",
            stated_experience=row.get("stated_experience") or "",
            fingerprint=row.get("fingerprint") or "",
            site=row.get("site") or "",
        )


class FirstSeen(Protocol):
    """The store's answer to "when did anyone first see this role".

    Deliberately the cluster's earliest first-seen and not the fingerprint's
    own. Once the resolver links a role across sites, the raw per-fingerprint
    value is wrong in one direction only: a job that sat on a board for three
    weeks looks new the day an agency relists it, and it clears the seven-day
    window every time it moves between sites.

    Note which way that correction runs. It can only make an item older, never
    fresher, so it can only ever block. That is the safe direction for a
    freshness rule — but it does mean a genuinely new posting that was wrongly
    merged is dropped silently, which is a second reason the resolver refuses
    to merge on uncertainty.
    """

    def __call__(self, fingerprint: str) -> str: ...


def run_gates(
    candidate: Candidate,
    *,
    spec: Mapping[str, Any],
    now: datetime,
    first_seen: FirstSeen | None = None,
    has_applied: Callable[[str], bool] | None = None,
    first_run: bool = False,
) -> GateReport:
    """Every deterministic verdict about one posting, in cost order."""
    first_seen_at = ""
    if first_seen is not None and candidate.fingerprint:
        first_seen_at = first_seen(candidate.fingerprint) or ""

    results: tuple[GateResult, ...] = (
        applied.check(fingerprint=candidate.fingerprint, has_applied=has_applied),
        freshness.check(
            spec=spec,
            now=now,
            posted_at=candidate.posted_at,
            first_seen_at=first_seen_at,
            first_run=first_run,
        ),
        geography.check(
            spec=spec,
            location=candidate.location,
            title=candidate.title,
            body=candidate.body,
            site=candidate.site,
        ),
        seniority.check(
            spec=spec,
            title=candidate.title,
            body=candidate.body,
            stated_experience=candidate.stated_experience,
        ),
        degree.check(spec=spec, title=candidate.title, body=candidate.body),
    )
    return GateReport(results)


def store_first_seen(store: Any) -> FirstSeen:
    """Bind the chain to a store, preferring the cluster-aware answer.

    Falls back to the fingerprint's own first-seen only when the store predates
    the resolver's linking table, so a clean clone with no merges still gates on
    dates rather than on nothing.
    """

    def lookup(fingerprint: str) -> str:
        cluster_aware = getattr(store, "cluster_first_seen", None)
        if cluster_aware is not None:
            return cluster_aware(fingerprint) or ""
        row = store.conn.execute(
            "SELECT first_seen_at FROM fingerprints WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return (row["first_seen_at"] if row else "") or ""

    return lookup
