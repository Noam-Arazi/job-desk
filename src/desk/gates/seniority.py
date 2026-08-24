"""How much experience the posting demands, and whether that is a blocker.

Two sources, in this order.

Drushim states the requirement in its own field — "ללא נסיון", "2 שנים",
"4 שנים" — and the scraper already carries it as `stated_experience`. Reading a
field the board itself filled is always better than inferring from prose, so
where it exists nothing else is consulted.

Everywhere else the number has to be read out of the text, and the trap there is
that a year count is not a seniority bar just because it is a year count. "3
שנים" beside "ניסיון" is a requirement; the same words beside "החברה פועלת" are
the company's age. So a figure only counts when an experience word sits near it.

The thresholds and the range rule are the spec's. This module holds no number.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .result import GateResult, Verdict
from .text import YEAR_WORDS, near, quote, readable

GATE = "seniority"

_EXPERIENCE_WORDS = ("ניסיון", "נסיון", "experience", "exp.")

# "ללא ניסיון", "no experience required" — a stated floor of zero, which is a
# pass and not an absence. Worth its own pattern: the boards phrase it without
# a digit, so every numeric pattern below misses it.
_NONE_REQUIRED = re.compile(
    r"(ללא\s+(?:כל\s+)?נ(?:י)?סיון|לא\s+נדרש\s+נ(?:י)?סיון|no\s+experience|"
    r"experience\s+not\s+required|entry[- ]level|ג'וניור|junior)"
)

_UNIT = r"(?:שנ(?:ים|ות|ה)|years?|yrs?)"

# Ordered: the range must be tried before the bare number, or "3-5 שנים" reads
# as a flat 3 and the range rule never runs.
_RANGE = re.compile(rf"(\d{{1,2}})\s*-\s*(\d{{1,2}})\s*\+?\s*{_UNIT}")
_PLUS = re.compile(rf"(\d{{1,2}})\s*\+\s*{_UNIT}")
_DIGITS = re.compile(rf"(\d{{1,2}})\s*{_UNIT}")

# Spelled-out years, and the two rules the store forced on them.
#
# A word-number only counts with the unit behind it — "שלוש שנים" and not a bare
# "שלוש", which is far more often three of something else. The exception is
# "שנתיים", which is already two-years in one word.
#
# The boundaries are not decoration. Hebrew has no casing to fall back on, and
# "שש" sits inside "חשש": the store had a warehouse job blocked for demanding
# six years by a regex reading the word for "worry".
#
# Bare "שנה" is deliberately absent. Digits cover "1 שנה", and a stray one-year
# match near the word for experience is worse than no match at all — the lowest
# figure decides, so a false low would hide a real seven-year bar behind it.
_WORD_ALONE = "שנתיים"
_WORD_WITH_UNIT = "|".join(w for w in YEAR_WORDS if w not in {_WORD_ALONE, "שנה", "אחת"})
_WORDS = re.compile(rf"(?<![א-ת])(?:{_WORD_ALONE}|(?:{_WORD_WITH_UNIT})\s+{_UNIT})(?![א-ת])")

# "12 שנות לימוד, בגרות מלאה" is the Israeli way of asking for a high-school
# diploma. It is a year count, it sits a comma away from the word for
# experience, and it is not a seniority bar — it was the single loudest false
# block on the first run over the store.
_SCHOOLING = re.compile(r"\s*(?:לימוד|לימודים|schooling|education)")


def years_required(text: str, *, range_rule: str = "use_lower_bound") -> list[tuple[int, int, int]]:
    """Every experience figure in the text, as (years, start, end).

    Ranges collapse per the spec's rule. Nothing is deduplicated: a posting that
    states the same bar twice states it twice, and the caller decides.
    """
    found: list[tuple[int, int, int]] = []
    claimed: list[tuple[int, int]] = []

    def take(match: re.Match[str], value: int) -> None:
        span = match.span()
        if any(s <= span[0] < e for s, e in claimed):
            return
        claimed.append(span)
        if _SCHOOLING.match(text, span[1]):
            return
        found.append((value, span[0], span[1]))

    for match in _NONE_REQUIRED.finditer(text):
        take(match, 0)
    for match in _RANGE.finditer(text):
        low, high = int(match.group(1)), int(match.group(2))
        take(match, high if range_rule == "use_upper_bound" else low)
    for match in _PLUS.finditer(text):
        take(match, int(match.group(1)))
    for match in _DIGITS.finditer(text):
        take(match, int(match.group(1)))
    for match in _WORDS.finditer(text):
        word = next((w for w in YEAR_WORDS if match.group(0).startswith(w)), "")
        if word:
            take(match, YEAR_WORDS[word])

    return sorted(found, key=lambda f: f[1])


def check(
    *,
    spec: Mapping[str, Any],
    title: str = "",
    body: str = "",
    stated_experience: str = "",
) -> GateResult:
    rules = ((spec.get("gates") or {}).get("seniority")) or {}
    if str(rules.get("mode", "block")) != "block":
        return GateResult(GATE, Verdict.PASS, reason="the spec does not gate on seniority")

    ceiling = int(rules.get("max_required_years", 3))
    range_rule = str(rules.get("range_rule", "use_lower_bound"))
    multiple_rule = str(rules.get("multiple_rule", "use_highest"))
    unstated = str(rules.get("unstated", "pass"))

    # The board's own field, when it filled one. No proximity test here: the
    # field is the experience requirement, so the number in it is never a
    # coincidence.
    if stated_experience.strip():
        text = readable(stated_experience)
        figures = years_required(text, range_rule=range_rule)
        if figures:
            return _verdict(
                figures[0][0], ceiling, text, figures[0][1], figures[0][2], source="stated field"
            )

    text = readable(title, body)
    figures = years_required(text, range_rule=range_rule)
    anchored = [f for f in figures if near(text, f[1], _EXPERIENCE_WORDS)]

    if not anchored:
        if unstated == "block":
            return GateResult(
                GATE, Verdict.BLOCK, reason="no experience requirement stated, and the spec blocks"
            )
        return GateResult(
            GATE,
            Verdict.UNKNOWN,
            reason="no experience requirement stated; the spec does not treat that as a blocker",
        )

    # Which of several stated figures decides is the spec's, and it changed on
    # 24.08.2026. It used to be the lowest, on the reasoning that a high figure
    # usually belongs to the nice-to-have half of a list. Then a posting opened
    # with "5+ years of experience as a software engineer" and added "2+ years
    # with large language models" further down, and the gate passed it on the
    # two: the lower figure hid the bar instead of softening it.
    #
    # A range is one demand expressed loosely and its rule stays its own. Two
    # separate demands are two things to satisfy, and what has to be cleared is
    # the larger of them.
    pick = max if multiple_rule != "use_lowest" else min
    years, start, end = pick(anchored, key=lambda f: f[0])
    return _verdict(years, ceiling, text, start, end, source="posting text")


def _verdict(
    years: int, ceiling: int, text: str, start: int, end: int, *, source: str
) -> GateResult:
    details = {"years": years, "ceiling": ceiling, "source": source}
    if years > ceiling:
        return GateResult(
            GATE,
            Verdict.BLOCK,
            reason=f"demands {years} years, the spec's ceiling is {ceiling}",
            evidence=quote(text, start, end),
            details=details,
        )
    return GateResult(
        GATE,
        Verdict.PASS,
        reason=f"demands {years} years, within the spec's ceiling of {ceiling}",
        evidence=quote(text, start, end),
        details=details,
    )
