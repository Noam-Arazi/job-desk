"""Requirement extraction — the one quality measure that needs no human at all.

Every requirement the analyst emits carries `evidence`: the posting's own
wording, verbatim, for the thing being claimed. That field turns a question that
normally needs a judge into a string comparison. Either the quoted span is in
the posting or it is not, and if it is not the requirement was invented. No
label, no model, no second opinion — so this suite is measurable today, while
the gold set is still waiting on Noam.

That is worth stating plainly because it is the cheap half of a design that is
usually done expensively. The common approach to "did the extractor hallucinate"
is to ask a second model, which costs tokens, is itself unvalidated, and
produces a number nobody can reproduce. Anchoring converts most of that question
into arithmetic. What it cannot answer — whether the quoted span actually
supports the requirement written next to it — is what the reflection loop's
model half is for, and it is a much smaller question because every span that is
not literally present has already been thrown out.

The anchoring check has to normalise whitespace before comparing, and does so
under the spec's own `analyst.reflect.normalize_whitespace` flag rather than
because it seemed reasonable here. A posting is HTML flattened to text and a
model re-emits a quote with its spacing regularised; treating that as a
fabrication would make the number measure typography.

The second measurement is how many requirements the reflection loop dropped per
posting. A drop rate of zero over many postings does not mean the extractor is
perfect — it much more likely means the loop is not running, or that the
generator learned to emit only what it could quote and stopped emitting the
harder requirements at all. The rate is reported next to the anchored share for
that reason: they move together when the loop works and apart when it does not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..analyst.types import Analysis
from .result import RATIO, SHARE, Measurement, SuiteResult, Table, missing

SUITE = "extraction"

NO_ANALYSES = "the analyst has not written any analyses yet; run `desk analyze --write`"
NO_REQUIREMENTS = "no analysis carries a requirement; nothing to anchor"

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse whitespace so a re-typed quote is not read as an invention."""
    return _WHITESPACE.sub(" ", text or "").strip()


def anchored(evidence: str, haystack: str, *, normalize_whitespace: bool = True) -> bool:
    """Is this span literally in the posting?

    An empty span is not anchored. That is deliberate and it is the strict
    reading: a requirement that came back with no quote is exactly the case the
    contract in analyst/types.py calls fabricated.
    """
    span = normalise(evidence) if normalize_whitespace else (evidence or "")
    if not span:
        return False
    text = normalise(haystack) if normalize_whitespace else (haystack or "")
    return span in text


def posting_text(row: Mapping[str, Any]) -> str:
    """Everything of a posting a requirement may legitimately be quoted from."""
    return "\n".join(
        str(row.get(field) or "") for field in ("title", "company", "location", "body")
    )


def run(
    analyses: Sequence[Analysis],
    postings: Mapping[str, Mapping[str, Any]],
    *,
    spec: Mapping[str, Any] | None = None,
) -> SuiteResult:
    """Score span anchoring and reflection drops over stored analyses."""
    reflect = ((spec or {}).get("analyst") or {}).get("reflect") or {}
    normalize_whitespace = bool(reflect.get("normalize_whitespace", True))

    if not analyses:
        return SuiteResult(
            suite=SUITE,
            measurements=(
                missing("requirements extracted", NO_ANALYSES),
                missing("anchored to a real span", NO_ANALYSES, unit=SHARE),
                missing("unanchored", NO_ANALYSES),
                missing("dropped by reflection, per posting", NO_ANALYSES, unit=RATIO),
            ),
            notes=(
                "This suite needs no labels — only analyses. It is the measurement "
                "that unblocks first.",
            ),
        )

    total = 0
    hits = 0
    misses: list[tuple[str, str, str]] = []
    no_text = 0
    dropped_total = 0
    rounds_total = 0
    with_requirements = 0

    for analysis in analyses:
        row = postings.get(analysis.fingerprint)
        text = posting_text(row) if row else ""
        dropped_total += len(analysis.dropped)
        rounds_total += analysis.reflect_rounds
        if analysis.requirements:
            with_requirements += 1
        for requirement in analysis.requirements:
            total += 1
            if not text:
                # The posting is gone from the store. Not anchored and not
                # unanchored — unjudgeable, and counted as its own thing.
                no_text += 1
                continue
            if anchored(
                requirement.evidence, text, normalize_whitespace=normalize_whitespace
            ):
                hits += 1
            else:
                misses.append(
                    (analysis.fingerprint, requirement.text, requirement.evidence)
                )

    judgeable = total - no_text
    measurements: list[Measurement] = [
        Measurement(
            "analyses read",
            len(analyses),
            detail=f"{with_requirements} carry at least one requirement",
        ),
        Measurement("requirements extracted", total),
    ]

    if judgeable:
        measurements += [
            Measurement(
                "anchored to a real span",
                hits / judgeable,
                unit=SHARE,
                detail=f"{hits} of {judgeable} quotes found verbatim in the posting",
            ),
            Measurement(
                "unanchored",
                len(misses),
                detail="a quote the posting does not contain — a fabricated "
                "requirement, not a weak one",
            ),
        ]
    else:
        why = NO_REQUIREMENTS if not total else "no posting text is available to quote against"
        measurements += [
            missing("anchored to a real span", why, unit=SHARE),
            missing("unanchored", why),
        ]

    # The generator's own rate, which is the number this suite is actually
    # about. The re-check above reads the stored requirements — and those are
    # exactly the ones the reflection loop already kept, using the same
    # containment test, so it can only ever come out at 100 percent however
    # badly the extractor behaved. It stays, because it catches a stored
    # analysis drifting out of agreement with the posting it was read from, but
    # it is not evidence about extraction. These three counts are.
    produced = sum(a.extracted for a in analyses)
    invented = sum(a.unanchored for a in analyses)
    refused = sum(a.unsupported for a in analyses)
    if produced:
        measurements += [
            Measurement(
                "requirements the generator produced",
                produced,
                detail="before either half of the reflection loop deleted anything",
            ),
            Measurement(
                "anchored when first produced",
                (produced - invented) / produced,
                unit=SHARE,
                detail=f"{produced - invented} of {produced}; the rest quoted a span "
                "the posting does not contain",
            ),
            Measurement(
                "deleted by the evaluator",
                refused,
                detail="anchored, but the quote did not support the line",
            ),
        ]
    else:
        why = (
            "these analyses predate the counts; re-run `desk analyze --write`"
            if analyses
            else NO_ANALYSES
        )
        measurements += [
            missing("requirements the generator produced", why),
            missing("anchored when first produced", why, unit=SHARE),
            missing("deleted by the evaluator", why),
        ]

    measurements.append(
        Measurement(
            "dropped by reflection, per posting",
            dropped_total / len(analyses),
            unit=RATIO,
            detail=f"{dropped_total} dropped across {len(analyses)} analyses",
        )
    )
    measurements.append(
        Measurement(
            "reflection rounds, per posting",
            rounds_total / len(analyses),
            unit=RATIO,
            detail=f"spec ceiling {reflect.get('max_rounds', '?')}",
        )
    )
    if no_text:
        measurements.append(
            Measurement(
                "unjudgeable",
                no_text,
                detail="the posting is no longer in the store, so the quote cannot "
                "be checked either way",
            )
        )

    notes = [
        "No labels are involved. Anchoring is a string comparison against the "
        "posting, which is why this number exists before the gold set does.",
    ]
    if total and dropped_total == 0:
        notes.append(
            "Nothing was dropped by reflection. Read that next to the anchored "
            "share rather than as a success: a loop that never drops anything is "
            "equally consistent with a loop that is not running."
        )

    tables = ()
    if misses:
        tables = (
            Table(
                title="unanchored requirements",
                columns=("fingerprint", "requirement", "quoted span"),
                rows=tuple(
                    (fp[:12], _clip(text), _clip(evidence)) for fp, text, evidence in misses[:20]
                ),
                note=f"{len(misses)} in total; the first 20 are listed.",
            ),
        )

    return SuiteResult(
        suite=SUITE,
        measurements=tuple(measurements),
        notes=tuple(notes),
        tables=tables,
        extra={"unanchored": [{"fingerprint": f, "text": t, "evidence": e} for f, t, e in misses]},
    )


def from_rows(rows: Iterable[Mapping[str, Any]]) -> list[Analysis]:
    """Turn stored `analyses` rows into the frozen contract type.

    A row whose payload will not parse is skipped and not counted as an analysis
    with no requirements, which would have quietly improved the anchored share.
    """
    out: list[Analysis] = []
    for row in rows:
        payload = row.get("payload")
        if not payload:
            continue
        try:
            out.append(Analysis.from_json(str(payload)))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


def _clip(text: str, width: int = 60) -> str:
    flat = normalise(text)
    return flat if len(flat) <= width else flat[: width - 1] + "…"
