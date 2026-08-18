"""The analyst — the only stage of the system that spends judgment-tier tokens.

Four stages, cheapest first, and the order is the cost design rather than a
narrative convenience:

    families    which CV base this posting belongs to, or none. Settled by the
                spec's own terms wherever they settle it; a model is reached
                only for genuinely ambiguous postings
    extract     what the posting requires, each requirement carrying the span
                of the posting it was read from
    reflect     the evaluator half of the loop. Every span is checked against
                the posting in Python first, and only what survives is worth
                asking a model about
    score       a fit score and one line justifying it. The recommended channel
                is derived from that score in Python and is never the model's

`types.py` was frozen before any of them was written, so the tailoring agent and
the digest could be built against the same shape at the same time.
"""

from __future__ import annotations

from .analyst import Analyst, analyse_row
from .types import (
    BUTTON,
    CHANNELS,
    KINDS,
    NONE,
    PERSON,
    SKIP,
    STOPPED_EXTRACT,
    STOPPED_FAMILY,
    STOPPED_GATES,
    STOPPED_REFLECT,
    Analysis,
    Family,
    Fit,
    Requirement,
)

__all__ = [
    "BUTTON",
    "CHANNELS",
    "KINDS",
    "NONE",
    "PERSON",
    "SKIP",
    "STOPPED_EXTRACT",
    "STOPPED_FAMILY",
    "STOPPED_GATES",
    "STOPPED_REFLECT",
    "Analysis",
    "Analyst",
    "Family",
    "Fit",
    "Requirement",
    "analyse_row",
]
