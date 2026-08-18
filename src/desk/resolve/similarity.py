"""Deterministic similarity — the part of dedup that never calls a model.

Two numbers, measured on different evidence, because either one alone is wrong
on a real pair in the store.

    core        token overlap of the role cores. Survives an agency rewriting
                the employer blurb, fails when two sites word the role itself
                differently ("BI Developer" against "מפתח BI").
    body        character-shingle overlap of the posting text. A board and an
                aggregator usually carry the employer's own paragraphs verbatim,
                so this is near 1.0 on exactly the pairs core misses. It is near
                0 when an agency writes its own prose about the same job.

Characters and not words for the body, because Hebrew inflects with prefixes:
"בחברה" and "לחברה" share no word token and four of five characters.
"""

from __future__ import annotations

from ..store.fingerprint import normalize

SHINGLE = 4


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def shingles(text: str, n: int = SHINGLE) -> frozenset[str]:
    """Character n-grams over the normalized text.

    Returns empty for anything shorter than one shingle, which keeps a missing
    body scoring 0.0 rather than accidentally matching another missing body.
    """
    flat = normalize(text)
    if len(flat) < n:
        return frozenset()
    return frozenset(flat[i : i + n] for i in range(len(flat) - n + 1))


def body_similarity(a: str, b: str) -> float:
    return jaccard(shingles(a), shingles(b))


def company_agrees(a: str, b: str) -> bool | None:
    """Whether two employer fields agree. None means one of them did not say.

    An unknown is not a disagreement, and this is the field where that matters
    most: gotfriends states no employer at all, and 27 of the 191 alljobs rows
    say "חברה חסויה", which is the board's way of also saying nothing. Treating
    either as a name would merge every confidential posting into one job.

    Agreement is never a decision on its own either, because half the named
    employers on a board are staffing agencies: two unrelated roles both placed
    by "דנאל משאבי אנוש" agree here and are not the same job.
    """
    left, right = normalize(placeholder_to_blank(a)), normalize(placeholder_to_blank(b))
    if not left or not right:
        return None
    return left == right


_PLACEHOLDERS = {
    "חברה חסויה",
    "חברת השמה",
    "חסוי",
    "confidential",
    "company confidential",
}


def placeholder_to_blank(company: str) -> str:
    """A stated non-name becomes the blank it actually is."""
    return "" if normalize(company) in _PLACEHOLDERS else company
