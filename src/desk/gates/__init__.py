"""The deterministic gates — the stage that runs before any model call.

Five findings per posting, none of them costing a token:

    already_applied   the store's blocklist. Finished is not the same as
                      irrelevant, and neither is shown twice
    freshness         inside the spec's window, with a documented fallback for
                      the one board that publishes no dates at all
    geography         any named city in an accepted region, out of a field that
                      is usually a list and sometimes not a place
    seniority         the lowest stated experience bar, from the board's own
                      field where it has one
    degree            a closed technical list, unless an open clause cancels it

Every threshold they read is in `spec/search.yaml`. Nothing in this package
hard-codes a criterion.
"""

from __future__ import annotations

from . import applied, degree, freshness, geography, seniority
from .chain import Candidate, run_gates, store_first_seen
from .result import GateReport, GateResult, Verdict

__all__ = [
    "Candidate",
    "GateReport",
    "GateResult",
    "Verdict",
    "applied",
    "degree",
    "freshness",
    "geography",
    "run_gates",
    "seniority",
    "store_first_seen",
]
