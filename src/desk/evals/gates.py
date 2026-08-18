"""Gate correctness — two errors that must never be added together.

The gates make two kinds of mistake and they are not comparable, so this suite
reports them as two numbers and never as one accuracy figure.

    a false block   the gates dropped a posting Noam labelled high or medium.
                    This is the expensive one. He never sees it, so he can
                    never report it, and the system's own logs look perfectly
                    healthy while it happens. Nothing else in the repo can
                    detect it.

    a false pass    the gates passed a posting he labelled irrelevant. This is
                    the cheap one. It costs one analyst call, and the analyst
                    exists precisely to catch it.

Averaging them produces a number that improves when the gates are tightened
past the point of usefulness, because tightening trades many cheap errors for
a few expensive ones. So there is no combined score in this file.

The false-block number is only measurable because the gold-set sampler
deliberately drew ten of its thirty postings from the ones the gates blocked
(`src/desk/label.py`, DEFAULT_BLOCKED_SHARE). A sample drawn only from
survivors is structurally blind to false blocks: every posting in it passed, so
the count is zero by construction and the zero means nothing. That stratum is
the entire reason the first number above exists, and it is why this suite
reports the false-block rate over the labelled blocked stratum rather than over
all thirty labels.

One thing this suite watches that the sampler could not: the gates have been
edited since the sample was drawn. Each label carries the stratum it was drawn
from, and the gates are re-run live here, so a label whose recorded stratum
disagrees with today's verdict is reported as drift rather than silently
re-classified.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .. import label as gold
from ..gates import Candidate, run_gates
from .result import SHARE, Measurement, SuiteResult, Table, missing

SUITE = "gates"

NO_LABELS = "no labels recorded; run `desk label` — the gold set is collected by hand"
NO_BLOCKED = (
    "no labelled posting is blocked by today's gates; the false-block rate has "
    "no denominator"
)
NO_PASSED = "no labelled posting passes today's gates; the false-pass rate has no denominator"


def run(
    rows: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    spec: Mapping[str, Any],
    now: datetime,
) -> SuiteResult:
    """Score the gates against the labels, if there are any labels."""
    report = gold.agreement(rows, labels, spec=spec, now=now)

    if not report.labelled:
        return SuiteResult(
            suite=SUITE,
            measurements=(
                missing("postings labelled", NO_LABELS),
                missing("false blocks (wanted, dropped)", NO_LABELS),
                missing("false block rate", NO_LABELS, unit=SHARE),
                missing("false passes (irrelevant, kept)", NO_LABELS),
                missing("false pass rate", NO_LABELS, unit=SHARE),
            ),
            notes=(
                "The gold set is blind by design and cannot be generated: it is "
                "Noam judging thirty real postings without being shown what the "
                "system concluded. See src/desk/label.py.",
            ),
        )

    blocked_stratum = report.gate_blocked_human_irrelevant + report.gate_blocked_human_wanted
    passed_stratum = report.gate_passed_human_irrelevant + report.gate_passed_human_wanted

    measurements: list[Measurement] = [
        Measurement(
            "postings labelled",
            report.labelled,
            detail=_coverage(report.labelled),
        ),
        Measurement(
            "false blocks (wanted, dropped)",
            report.gate_blocked_human_wanted,
            detail=f"of {blocked_stratum} labelled postings the gates block",
        ),
        (
            Measurement(
                "false block rate",
                report.gate_blocked_human_wanted / blocked_stratum,
                unit=SHARE,
                detail="the expensive error: he never sees these and cannot report them",
            )
            if blocked_stratum
            else missing("false block rate", NO_BLOCKED, unit=SHARE)
        ),
        Measurement(
            "false passes (irrelevant, kept)",
            report.gate_passed_human_irrelevant,
            detail=f"of {passed_stratum} labelled postings the gates pass",
        ),
        (
            Measurement(
                "false pass rate",
                report.gate_passed_human_irrelevant / passed_stratum,
                unit=SHARE,
                detail="the cheap error: it costs one analyst call, which is what the "
                "analyst is for",
            )
            if passed_stratum
            else missing("false pass rate", NO_PASSED, unit=SHARE)
        ),
    ]

    drift = _stratum_drift(rows, labels, spec=spec, now=now)
    measurements.append(
        Measurement(
            "stratum drift",
            drift,
            detail="labels whose recorded stratum disagrees with today's gates — the "
            "gates were edited after the sample was drawn",
        )
    )

    table = Table(
        title="gates against the gold set",
        columns=("", "he wanted it", "he called it irrelevant"),
        rows=(
            (
                "gates blocked",
                str(report.gate_blocked_human_wanted),
                str(report.gate_blocked_human_irrelevant),
            ),
            (
                "gates passed",
                str(report.gate_passed_human_wanted),
                str(report.gate_passed_human_irrelevant),
            ),
        ),
        note="top-left is the expensive error. Bottom-right is the cheap one. "
        "They are not averaged.",
    )

    notes = [
        "The two error counts are reported separately on purpose: tightening the "
        "gates trades many cheap errors for a few expensive ones, so any combined "
        "figure improves as the system gets worse.",
    ]
    if report.labelled < gold.DEFAULT_SIZE:
        notes.append(
            f"Partial gold set: {report.labelled} of {gold.DEFAULT_SIZE} labelled. "
            "Every rate above is over that partial sample."
        )
    if not blocked_stratum:
        notes.append(
            "No labelled posting is currently blocked, so the false-block number is "
            "not zero — it is unmeasured. The blocked stratum exists to prevent "
            "exactly this reading."
        )

    return SuiteResult(
        suite=SUITE,
        measurements=tuple(measurements),
        notes=tuple(notes),
        tables=(table,),
        extra={"agreement": report.as_dict()},
    )


def _coverage(labelled: int) -> str:
    target = gold.DEFAULT_SIZE
    if labelled >= target:
        return f"the gold set, target {target}"
    return f"of a target {target} — partial"


def _stratum_drift(
    rows: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    spec: Mapping[str, Any],
    now: datetime,
) -> int:
    """Labels whose recorded stratum no longer matches the live gate verdict.

    Not an error in either direction — the gates were meant to be corrected by
    what the labels revealed. It is reported so that a rate computed over "the
    blocked stratum" is known to mean today's blocked set and not the one the
    sampler drew.
    """
    drifted = 0
    for row in rows:
        fingerprint = row.get("fingerprint") or ""
        label = labels.get(fingerprint)
        if not label:
            continue
        recorded = label.get("stratum") or ""
        if recorded not in (gold.SURVIVED, gold.BLOCKED):
            continue
        blocked = run_gates(Candidate.from_row(row), spec=spec, now=now).blocked
        live = gold.BLOCKED if blocked else gold.SURVIVED
        if live != recorded:
            drifted += 1
    return drifted


__all__ = ["SUITE", "run"]
