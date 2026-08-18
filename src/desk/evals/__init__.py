"""The measurement half of the project — one suite per claim the README makes.

    gates        two error counts, never averaged: what the gates wrongly
                 dropped, and what they wrongly kept
    agreement    the analyst's score against Noam's three-way label, as a
                 confusion matrix with the direction of the error named
    extraction   what share of extracted requirements quote a real span of the
                 posting. Needs no labels, so it is measurable first
    dedup        duplicate precision and recall against hand-labelled clusters,
                 and the plain statement that unlabelled counts are unvalidated
    guardrails   ten hostile postings, each naming the mechanism that stops it
    prompts      every prompt version scored on its own fixture set, keyed by id
                 and sha256 so an edit cannot inherit an old score
    cost         tokens per stage from the run traces, in list-price units that
                 are labelled as list price and not as a bill

Every suite reports a measurement it could not make as missing, with the reason.
None of them substitutes a default, and none of them invents a threshold: all
thresholds come from spec/search.yaml. `result.py` explains why that rule is
enforced by the type rather than by discipline.
"""

from __future__ import annotations

from .result import (
    COUNT,
    RATIO,
    SECONDS,
    SHARE,
    TOKENS,
    USD,
    EvalRun,
    Measurement,
    SuiteResult,
    Table,
    failed,
    missing,
)

__all__ = [
    "COUNT",
    "RATIO",
    "SECONDS",
    "SHARE",
    "TOKENS",
    "USD",
    "EvalRun",
    "Measurement",
    "SuiteResult",
    "Table",
    "failed",
    "missing",
]
