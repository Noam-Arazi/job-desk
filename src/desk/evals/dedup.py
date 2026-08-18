"""Duplicate precision and recall — against hand labels, or not at all.

The resolver produces two numbers on its own: how many pairs it merged and how
many it left uncertain. Neither is a quality measure. A resolver that merges
everything reports a large, healthy-looking merge count while quietly losing
jobs, and a resolver that merges nothing reports a clean run. The counts say
what happened, not whether it was right, and this suite refuses to present them
as though they were the same thing.

So precision and recall are computed only against clusters a human wrote down,
in `fixtures/duplicate_clusters.json`. That file ships empty on purpose. It is
meant to be extended by hand from a live run — open two postings, decide whether
they are one seat, and record it — and the format is a list of clusters plus a
list of explicitly-distinct pairs, because "we looked and they are different" is
evidence too and it is what makes a precision denominator honest.

Two consequences of scoring against a partial fixture, both handled here rather
than hidden:

    the universe is the fixture. A merge between two postings nobody has
    labelled cannot be scored either way, so it is counted separately as
    unjudgeable instead of being charged as a false positive. Precision over a
    denominator that includes unlabelled pairs would fall as the store grows.

    recall is over labelled clusters only. It answers "of the duplicates a
    human found, how many did the resolver find", which is a real question, and
    not "how many duplicates exist in the store", which nobody knows.

With an empty fixture the suite reports the resolver's own counts and says in as
many words that they are unvalidated.

And when the resolver has not run at all, it reports no counts. This is the
distinction the whole package is built on and it is easy to lose exactly here.
"merged: 0" is a sentence with two completely different meanings — the resolver
looked at every pair and merged none of them, or the resolver has never been
run — and printed as a zero they are indistinguishable. The first is a finding
about a cautious resolver. The second is an empty database. So with no verdicts
on record every count below is `missing` with that reason, and a number appears
only once there is something to count.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from itertools import combinations
from pathlib import Path
from typing import Any

from .result import SHARE, Measurement, SuiteResult, Table, missing

SUITE = "dedup"

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CLUSTERS_PATH = FIXTURES_DIR / "duplicate_clusters.json"

DUPLICATE = "duplicate"
UNCERTAIN = "uncertain"
DISTINCT = "distinct"

NO_LINKS = "the resolver has not recorded any verdicts; run `desk resolve --write`"

# The counts the resolver's own output supports. Named once so that the
# no-verdicts branch reports exactly the same rows as the measured one, and a
# baseline diff sees a row become measured rather than a row appear.
COUNT_NAMES = (
    "pairs the resolver ruled on",
    "merged",
    "left uncertain",
    "called distinct",
    "escalated to a model",
)


def _unvalidated(path: Path) -> str:
    return f"no hand-labelled clusters in {path.name}; a human extends that file"


def load_clusters(path: Path | None = None) -> dict[str, Any]:
    """Read the hand-labelled clusters. A missing file is an empty fixture."""
    target = Path(path or CLUSTERS_PATH)
    if not target.exists():
        return {"clusters": [], "distinct_pairs": []}
    data = json.loads(target.read_text(encoding="utf-8"))
    return {
        "clusters": list(data.get("clusters", [])),
        "distinct_pairs": [tuple(p) for p in data.get("distinct_pairs", [])],
    }


def _pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def truth_pairs(clusters: Iterable[Mapping[str, Any]]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for cluster in clusters:
        members = sorted({str(m) for m in cluster.get("members", [])})
        for left, right in combinations(members, 2):
            pairs.add(_pair(left, right))
    return pairs


def run(
    links: Sequence[Mapping[str, Any]],
    *,
    fixture: Mapping[str, Any] | None = None,
    fixture_path: Path | None = None,
) -> SuiteResult:
    """Score the resolver's merges against hand-labelled clusters, if any exist."""
    path = Path(fixture_path or CLUSTERS_PATH)
    hand = dict(fixture) if fixture is not None else load_clusters(path)
    clusters = list(hand.get("clusters", []))
    distinct = {_pair(str(a), str(b)) for a, b in hand.get("distinct_pairs", [])}

    merged = {_pair(str(r["left_fp"]), str(r["right_fp"])) for r in links if r["band"] == DUPLICATE}

    if not links:
        # Nothing to count, so nothing is counted. A zero here would read as
        # "the resolver merged nothing", which is a finding; the truth is that
        # it has not run, which is the absence of one.
        counts: list[Measurement] = [
            missing(name, NO_LINKS)
            for name in COUNT_NAMES
        ]
    else:
        counts = [
            Measurement(
                "pairs the resolver ruled on",
                len(links),
                detail="every verdict is kept, not only the merges",
            ),
            Measurement("merged", len(merged)),
            Measurement(
                "left uncertain",
                sum(1 for r in links if r["band"] == UNCERTAIN),
                detail="these do not merge without a judge",
            ),
            Measurement("called distinct", sum(1 for r in links if r["band"] == DISTINCT)),
            Measurement(
                "escalated to a model",
                sum(1 for r in links if str(r.get("method", "")).startswith("judge")),
                detail="the only part of dedup that costs anything",
            ),
        ]

    truth = truth_pairs(clusters)
    universe = {m for cluster in clusters for m in map(str, cluster.get("members", []))}
    universe |= {fp for pair in distinct for fp in pair}

    if not truth and not distinct:
        why = _unvalidated(path)
        notes = [
            f"To validate, add clusters to {path}. The format takes both "
            "clusters of the same opening and pairs a human checked and found "
            "different — the second kind is what gives precision an honest "
            "denominator.",
        ]
        if links:
            notes.insert(
                0,
                "The counts above are what the resolver produced. They are "
                "UNVALIDATED: nothing here says whether a merge was correct.",
            )
        else:
            notes.insert(
                0,
                "The resolver has recorded no verdicts, so there is nothing above "
                "to validate. Every count is unmeasured rather than zero: "
                "'merged: 0' would say the resolver looked and merged nothing, "
                "which is not what happened.",
            )
        return SuiteResult(
            suite=SUITE,
            measurements=tuple(
                counts
                + [
                    missing("precision", why, unit=SHARE),
                    missing("recall", why, unit=SHARE),
                ]
            ),
            notes=tuple(notes),
            extra={"fixture": str(path), "hand_labelled_clusters": 0, "links": len(links)},
        )

    judgeable = {p for p in merged if p[0] in universe and p[1] in universe}
    unjudgeable = len(merged) - len(judgeable)
    true_positive = len(judgeable & truth)
    false_positive = len(judgeable - truth)
    false_negative = len(truth - merged)

    precision_den = true_positive + false_positive
    recall_den = len(truth)

    measurements = counts + [
        Measurement(
            "hand-labelled clusters",
            len(clusters),
            detail=f"{recall_den} duplicate pairs, {len(distinct)} pairs checked and "
            "found different",
        ),
        (
            missing("precision", NO_LINKS, unit=SHARE)
            if not links
            else Measurement(
                "precision",
                true_positive / precision_den,
                unit=SHARE,
                detail=f"{true_positive} of {precision_den} judgeable merges are in a "
                "hand-labelled cluster",
            )
            if precision_den
            else missing(
                "precision",
                "the resolver merged nothing inside the labelled universe",
                unit=SHARE,
            )
        ),
        # Recall over an empty verdict set would come out 0% and read as "the
        # resolver missed every labelled duplicate". It missed nothing; it has
        # not run. Same zero, opposite meaning.
        (
            missing("recall", NO_LINKS, unit=SHARE)
            if not links
            else Measurement(
                "recall",
                true_positive / recall_den,
                unit=SHARE,
                detail=f"{true_positive} of {recall_den} hand-labelled duplicate pairs "
                "were merged",
            )
            if recall_den
            else missing("recall", "no hand-labelled duplicate pairs", unit=SHARE)
        ),
        (
            missing("unjudgeable merges", NO_LINKS)
            if not links
            else Measurement(
                "unjudgeable merges",
                unjudgeable,
                detail="merges outside the labelled universe — not charged as errors, "
                "because nobody has said whether they are right",
            )
        ),
    ]

    tables = ()
    wrong = sorted(judgeable - truth)
    lost = sorted(truth - merged)
    if wrong or lost:
        tables = (
            Table(
                title="dedup disagreements",
                columns=("kind", "left", "right"),
                rows=tuple(
                    [("merged, labelled different", a[:12], b[:12]) for a, b in wrong[:20]]
                    + [("labelled same, not merged", a[:12], b[:12]) for a, b in lost[:20]]
                ),
                note="a wrong merge loses a job silently; a missed merge costs one "
                "duplicate line in the digest. They are not equally bad.",
            ),
        )

    scored_notes = [
        f"Scored against {len(clusters)} hand-labelled clusters in {path.name}. "
        "Recall is over labelled duplicates, not over every duplicate in the "
        "store — nobody knows that number.",
    ]
    if not links:
        scored_notes.append(
            "The fixture is labelled but the resolver has recorded no verdicts, so "
            "precision and recall have nothing to score. They are unmeasured, not "
            "zero."
        )

    return SuiteResult(
        suite=SUITE,
        measurements=tuple(measurements),
        notes=tuple(scored_notes),
        tables=tables,
        extra={
            "fixture": str(path),
            "hand_labelled_clusters": len(clusters),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
        },
    )
