"""Judge agreement — the analyst's score against Noam's three-way label.

Two design decisions carry this file.

The first is that the score-to-label mapping is not invented here. The analyst
emits a number between 0 and 1 and the gold set is three words, so something has
to bridge them, and the tempting move is to pick the two cut points that make
agreement look best. Instead both come out of spec/search.yaml: `digest.min_score`
is the floor below which nothing is recommended at all, and
`analyst.score.channel.person_min` is the bar above which a direct approach is
judged worth the effort. Those are the thresholds the running system already
acts on, so this suite measures the system as it behaves rather than a version
of it tuned to be measured. If either is absent from the spec, this suite
refuses rather than substituting a default — a threshold invented in an eval is
the same bug as a threshold hard-coded in a gate.

The second is that a single accuracy percentage is not reported alone, because
it cannot answer the only question worth asking. A system that scores too high
and a system that scores too low can hit identical accuracy, and their
consequences are opposite: an optimistic analyst floods the digest with items
Noam then has to reject by hand, a pessimistic one silently withholds work he
would have taken. So the confusion matrix is the primary output, and
`optimistic` and `pessimistic` are counted as separate measurements.

Postings Noam labelled that the analyst stopped on before scoring — no family
matched, the gates blocked it, extraction failed — are not folded into the
matrix as a low score. Stopping and scoring-low are different answers, exactly
as `Analysis.stopped_at` records, and a posting he labelled high that the
analyst never scored is a routing failure rather than a judgment failure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..label import HIGH, IRRELEVANT, MEDIUM
from .result import SHARE, Measurement, SuiteResult, Table, missing

SUITE = "agreement"

# Worst to best, so a disagreement has a signed direction.
ORDER = (IRRELEVANT, MEDIUM, HIGH)
RANK = {name: i for i, name in enumerate(ORDER)}

NO_LABELS = "no labels recorded; run `desk label` — the gold set is collected by hand"
NO_ANALYSES = "the analyst has not scored anything yet; run `desk analyze --write`"
NO_OVERLAP = "no labelled posting has been scored yet — the two sets do not overlap"


class MissingThreshold(KeyError):
    """The spec does not state a threshold this suite is required to read."""


def thresholds(spec: Mapping[str, Any]) -> tuple[float, float]:
    """(medium floor, high floor), read from the spec and nowhere else."""
    try:
        medium = float(spec["digest"]["min_score"])
    except (KeyError, TypeError) as exc:
        raise MissingThreshold("spec/search.yaml has no digest.min_score") from exc
    try:
        high = float(spec["analyst"]["score"]["channel"]["person_min"])
    except (KeyError, TypeError) as exc:
        raise MissingThreshold(
            "spec/search.yaml has no analyst.score.channel.person_min"
        ) from exc
    if high < medium:
        raise MissingThreshold(
            f"analyst.score.channel.person_min ({high}) is below digest.min_score "
            f"({medium}); the spec's own cut points do not order"
        )
    return medium, high


def label_for_score(score: float, *, spec: Mapping[str, Any]) -> str:
    """Map a fit score onto the three words Noam labels in."""
    medium, high = thresholds(spec)
    if score >= high:
        return HIGH
    if score >= medium:
        return MEDIUM
    return IRRELEVANT


def run(
    labels: Mapping[str, Mapping[str, Any]],
    analyses: Sequence[Mapping[str, Any]],
    *,
    spec: Mapping[str, Any],
) -> SuiteResult:
    """Compare the analyst's score against the labels, over their intersection."""
    medium, high = thresholds(spec)
    cutpoints = f"spec thresholds: medium >= {medium}, high >= {high}"

    if not labels:
        return _empty(NO_LABELS, cutpoints)
    if not analyses:
        return _empty(NO_ANALYSES, cutpoints)

    matrix = {(h, s): 0 for h in ORDER for s in ORDER}
    stopped = 0
    unscored = 0
    judged = 0

    for row in analyses:
        fingerprint = row.get("fingerprint") or ""
        label = labels.get(fingerprint)
        if not label:
            continue
        human = str(label.get("label") or "")
        if human not in RANK:
            continue
        if row.get("stopped_at"):
            stopped += 1
            continue
        score = row.get("score")
        if score is None:
            unscored += 1
            continue
        matrix[(human, label_for_score(float(score), spec=spec))] += 1
        judged += 1

    if not judged:
        return _empty(NO_OVERLAP, cutpoints, stopped=stopped, unscored=unscored)

    exact = sum(matrix[(name, name)] for name in ORDER)
    optimistic = sum(c for (h, s), c in matrix.items() if RANK[s] > RANK[h])
    pessimistic = sum(c for (h, s), c in matrix.items() if RANK[s] < RANK[h])
    adjacent = sum(c for (h, s), c in matrix.items() if abs(RANK[s] - RANK[h]) == 1)
    two_apart = sum(c for (h, s), c in matrix.items() if abs(RANK[s] - RANK[h]) == 2)

    measurements = (
        Measurement("postings judged", judged, detail="labelled and scored"),
        Measurement("exact agreement", exact / judged, unit=SHARE, detail=cutpoints),
        Measurement(
            "off by one band", adjacent / judged, unit=SHARE, detail="medium <-> high or "
            "medium <-> irrelevant"
        ),
        Measurement(
            "opposite ends",
            two_apart,
            detail="he said high and it said irrelevant, or the reverse — the "
            "disagreements worth reading one by one",
        ),
        Measurement(
            "optimistic",
            optimistic,
            detail="the system scored above his label: these reach the digest and he "
            "rejects them by hand",
        ),
        Measurement(
            "pessimistic",
            pessimistic,
            detail="the system scored below his label: these are withheld silently, "
            "which is the costlier direction",
        ),
        Measurement(
            "labelled but never scored",
            stopped + unscored,
            detail="the analyst stopped before scoring — a routing answer, not a "
            "judgment, so it is kept out of the matrix",
        ),
    )

    table = Table(
        title="fit score against the gold set",
        columns=("he said \\ it said", *ORDER),
        rows=tuple((human, *(str(matrix[(human, s)]) for s in ORDER)) for human in ORDER),
        note=cutpoints + ". The diagonal is agreement; above it the system is "
        "optimistic, below it pessimistic.",
    )

    notes = [
        "Accuracy alone is not reported. Two systems with the same accuracy and "
        "opposite bias have opposite consequences, so the direction is counted.",
    ]
    if optimistic or pessimistic:
        leaning = "optimistic" if optimistic > pessimistic else "pessimistic"
        if optimistic == pessimistic:
            leaning = "balanced"
        notes.append(
            f"Of {optimistic + pessimistic} disagreements the system leans {leaning} "
            f"({optimistic} above his label, {pessimistic} below)."
        )
    if judged < len(labels):
        notes.append(
            f"{len(labels) - judged} labelled postings are not in the matrix: "
            f"{stopped} stopped before scoring, {unscored} carry no score, the rest "
            "have not been analysed."
        )

    return SuiteResult(
        suite=SUITE,
        measurements=measurements,
        notes=tuple(notes),
        tables=(table,),
        extra={
            "thresholds": {"medium_min": medium, "high_min": high},
            "matrix": {f"{h}->{s}": c for (h, s), c in matrix.items()},
        },
    )


def _empty(why: str, cutpoints: str, *, stopped: int = 0, unscored: int = 0) -> SuiteResult:
    """The same rows as a scored run, none of them carrying a number.

    The row set is identical on purpose. A baseline diff compares by (suite,
    name), so a row that only exists once there is data appears as `new` the
    first time the gold set is labelled, when what actually happened is that a
    known-missing measurement became measurable.
    """
    notes = [cutpoints]
    if stopped or unscored:
        notes.append(
            f"{stopped} labelled postings stopped before scoring and {unscored} "
            "carry no score."
        )
    # When nothing overlapped we did count these; when there are no labels or no
    # analyses at all we did not, and 0 would be a claim rather than a count.
    never_scored: Measurement = (
        Measurement(
            "labelled but never scored",
            stopped + unscored,
            detail="the analyst stopped before scoring — a routing answer, not a "
            "judgment, so it is kept out of the matrix",
        )
        if stopped or unscored
        else missing("labelled but never scored", why)
    )
    return SuiteResult(
        suite=SUITE,
        measurements=(
            missing("postings judged", why),
            missing("exact agreement", why, unit=SHARE),
            missing("off by one band", why, unit=SHARE),
            missing("opposite ends", why),
            missing("optimistic", why),
            missing("pessimistic", why),
            never_scored,
        ),
        notes=tuple(notes),
    )
