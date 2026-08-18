"""Duplicate resolution — content-based, because identity-based does not work here."""

from .resolver import (
    DISTINCT,
    DUPLICATE,
    UNCERTAIN,
    PairScore,
    Resolution,
    candidate_pairs,
    cluster,
    resolve,
    score_pair,
)
from .titles import core_tokens, role_core, strip_gender

__all__ = [
    "DISTINCT",
    "DUPLICATE",
    "UNCERTAIN",
    "PairScore",
    "Resolution",
    "candidate_pairs",
    "cluster",
    "core_tokens",
    "resolve",
    "role_core",
    "score_pair",
    "strip_gender",
]
