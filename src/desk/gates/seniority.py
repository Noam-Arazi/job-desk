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
# `(?<!\d)`: "over 125 years of in-house recruiting" read as a 25-year bar
# (13.11.2026), because a two-digit pattern happily starts mid-number.
_RANGE = re.compile(rf"(?<!\d)(\d{{1,2}})\s*-\s*(\d{{1,2}})\s*\+?\s*{_UNIT}")
_PLUS = re.compile(rf"(?<!\d)(\d{{1,2}})\s*\+\s*{_UNIT}")
_DIGITS = re.compile(rf"(?<!\d)(\d{{1,2}})\s*{_UNIT}")

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
# 28.10.2026 — "5+ years in a technical role such as software engineering"
# names no experience word within reach, so the gate read it as silence and
# passed JFrog's AI Solutions Builder on to a 0.82. A year count followed
# directly by what the years were spent on is a requirement in its own right.
# Only the English forms: the Hebrew boards say "שנות ניסיון", which the
# proximity test already catches.
_ROLE_TAIL = re.compile(r"\s*(?:of|in|as|at|working|hands-on|building|delivering|leading)\b")

_SCHOOLING = re.compile(r"\s*(?:לימוד|לימודים|schooling|education)")

# 13.11.2026 — the company's own history, read as the candidate's bar. "With
# over 40 years of semiconductor process control", "מתבסס על מעל 30 שנות
# ניסיון בכלל תחומי החלל", "who brings over 20 years": the experience word is
# right there, so the proximity test passes it, and under `use_highest` it then
# outranks the real "2+ years" further down. Over two weeks it blocked dozens of
# postings, several repeatedly. Requirements are phrased as demands ("at least",
# "לפחות", a bare "3+ years"); boasts are phrased as possession, and the
# possession phrasing is narrow enough to list.
_BOAST_LEAD = re.compile(
    r"(?:"
    r"with\s+(?:over|more\s+than|nearly|almost)"
    # "brings", never "bring": "what you bring 5+ years" is the demand itself.
    r"|brings\s+(?:over|more\s+than|nearly)?"
    r"|we\s+bring\s+(?:over|more\s+than)"
    r"|builds?\s+on\s+(?:over\s+|more\s+than\s+)?"
    r"|going\s+back\s+(?:over\s+|more\s+than\s+)?"
    r"|comes\s+with"
    r"|עם\s+למעלה\s+מ-?"
    r"|עם\s+ניסיון\s+של\s+(?:מעל|למעלה\s+מ-?)"
    r"|מתבסס(?:ת)?\s+על\s+(?:מעל\s+|למעלה\s+מ-?)?"
    r"|מלאו"
    r"|גיל"
    r")\s*$"
)
_BOAST_TAIL = re.compile(
    r"\s*(?:of\s+(?:success|being|presence|history|innovation|leadership\s+in)|in\s+the\s+industry)\b"
)


def _is_boast(text: str, start: int, end: int) -> bool:
    """Whether a year figure describes the company (or an age), not a demand."""
    return bool(_BOAST_LEAD.search(text[max(0, start - 40) : start])) or bool(
        _BOAST_TAIL.match(text, end)
    )


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


def title_is_blocked(title: str, spec: Mapping[str, Any]) -> bool:
    """Whether the title alone already decides this gate. Free, and needs no body.

    09.11.2026 — the gate runs in the analyst, the last step before the model,
    so a posting titled senior had already cost a browser page and a place in
    the duplicate comparison: 16% of the postings and 44% of the pairs, for
    roles nothing downstream would ever show. The steps before the analyst ask
    this instead, so the words stay in one list in the spec.
    """
    rules = ((spec.get("gates") or {}).get("seniority")) or {}
    if str(rules.get("mode", "block")) != "block":
        return False
    text = readable(title)
    return any(_title_term(str(term)).search(text) for term in rules.get("title_block_terms") or ())


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

    # 28.10.2026 — the title outranks every number. Noam: a posting titled
    # (`title_is_blocked` asks the same question before a page is read.)
    # senior is not worth his time or the tokens, whatever the body says. Most
    # of them state no years at all, so the year rules below let them through
    # on silence and the model paid to score them — 27 were scored, five
    # cleared the digest floor. The words are the spec's; matching is on word
    # boundaries so "sr" does not fire inside another word.
    title_text = readable(title)
    for term in rules.get("title_block_terms") or ():
        match = _title_term(str(term)).search(title_text)
        if match:
            return GateResult(
                GATE,
                Verdict.BLOCK,
                reason=f"the title says {term!r}, a level the spec blocks",
                evidence=quote(title_text, match.start(), match.end()),
                details={"source": "title"},
            )

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
    # A figure above `max_plausible_years` is the company's age read as a bar:
    # nobody the spec would ever show is asked for 25 years.
    plausible = int(rules.get("max_plausible_years", 20))
    anchored = [
        f
        for f in figures
        if (near(text, f[1], _EXPERIENCE_WORDS) or _ROLE_TAIL.match(text, f[2]))
        and not _is_boast(text, f[1], f[2])
        and f[0] <= plausible
    ]

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


def _title_term(term: str) -> re.Pattern[str]:
    # Latin and Hebrew letters both count as "inside a word": "בכיר" must not
    # fire inside "בכירות" and "sr" must not fire inside "isr".
    return re.compile(rf"(?<![A-Za-zא-ת]){re.escape(term.lower())}(?![A-Za-zא-ת])")


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
