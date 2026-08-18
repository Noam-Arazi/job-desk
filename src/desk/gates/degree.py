"""Which diploma the posting demands, and whether it closes the door.

The held degree is a B.A. in Government & Strategy. A posting that demands a
degree from a closed technical list is not a near miss to be argued with — it is
a filter the application will not survive, and the spec blocks on it.

Two things keep this from over-blocking.

An open clause cancels the list. "תואר במדעי המחשב או תואר רלוונטי אחר" is not a
closed list, it is a preference, and the spec says so explicitly. The markers are
the spec's, not this module's.

A field named is not a diploma demanded. "מדעי המחשב" inside a sentence about the
team's domain is not a requirement, so an alias only counts when a degree word
sits near it — the same proximity rule the seniority gate uses, for the same
reason.

Aliases matter more than they look. The spec's lists were written in English and
every posting in the store is Hebrew, so a list without its Hebrew alias matches
nothing and the gate silently passes everything.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .result import GateResult, Verdict
from .text import near, quote, readable

GATE = "degree"

_DEGREE_WORDS = (
    "תואר",
    "בוגר",
    "בוגרת",
    "השכלה",
    "לימודי",
    "degree",
    "b.sc",
    "bsc",
    "b.a",
    "ba ",
    "m.sc",
    "msc",
    "bachelor",
    "master",
)


def closed_aliases(spec: Mapping[str, Any]) -> dict[str, str]:
    """Every spelling of every closed list, mapped to the list it belongs to.

    Two shapes are accepted so the spec can be tightened without a code change:
    a bare string, which is its own only spelling, or a mapping with `name` and
    `aliases`. The bare form is what session 1 wrote; the mapping is what the
    Hebrew boards require.
    """
    lists = ((spec.get("gates") or {}).get("degree") or {}).get("closed_lists") or ()
    aliases: dict[str, str] = {}
    for entry in lists:
        if isinstance(entry, Mapping):
            name = str(entry.get("name", ""))
            spellings = [name, *(entry.get("aliases") or ())]
        else:
            name = str(entry)
            spellings = [name]
        for spelling in spellings:
            flat = readable(str(spelling))
            if flat:
                aliases[flat] = name
    return aliases


def open_clause(spec: Mapping[str, Any], text: str) -> str:
    markers = ((spec.get("gates") or {}).get("degree") or {}).get("open_clause_markers") or ()
    for marker in markers:
        flat = readable(str(marker))
        if flat and flat in text:
            return flat
    return ""


def check(*, spec: Mapping[str, Any], title: str = "", body: str = "") -> GateResult:
    rules = ((spec.get("gates") or {}).get("degree")) or {}
    if str(rules.get("mode", "block")) != "block":
        return GateResult(GATE, Verdict.PASS, reason="the spec does not gate on the degree")

    aliases = closed_aliases(spec)
    if not aliases:
        return GateResult(GATE, Verdict.UNKNOWN, reason="the spec names no closed degree lists")

    text = readable(title, body)
    hits: list[tuple[str, str, int]] = []
    for spelling, name in aliases.items():
        at = text.find(spelling)
        if at != -1 and near(text, at, _DEGREE_WORDS):
            hits.append((spelling, name, at))

    if not hits:
        mentioned = any(word in text for word in _DEGREE_WORDS)
        if mentioned:
            return GateResult(
                GATE,
                Verdict.PASS,
                reason="a degree is discussed, but not one on the spec's closed lists",
            )
        return GateResult(GATE, Verdict.UNKNOWN, reason="the posting states no degree requirement")

    hits.sort(key=lambda h: h[2])
    spelling, name, at = hits[0]
    named = sorted({h[1] for h in hits})

    marker = open_clause(spec, text) if rules.get("open_clause_overrides", True) else ""
    if marker:
        return GateResult(
            GATE,
            Verdict.PASS,
            reason=f"names {', '.join(named)}, but an open clause cancels the list",
            evidence=marker,
            details={"closed_lists": named, "open_clause": marker},
        )

    return GateResult(
        GATE,
        Verdict.BLOCK,
        reason=f"demands a closed degree list: {', '.join(named)}",
        evidence=quote(text, at, at + len(spelling)),
        details={"closed_lists": named, "held": rules.get("held_degree", "")},
    )
