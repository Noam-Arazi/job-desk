"""Hand-labelling duplicate pairs, without being told what the resolver decided.

Dedup precision and recall are the two measurements in this project that no
amount of code can produce, because the question — are these two adverts the
same seat — has no mechanical answer. It needs a person, and the fixture file
the eval suite reads says so in its own header: extended by a human, never
generated.

What this module adds is the sampling, and one property that matters as much
here as it does in the gold set: the pair is shown WITHOUT the verdict. It
would be easier to list what the resolver merged and ask for a nod, and the
number that came back would mean much less — a person shown "these are the
same" and asked to agree is measuring how convincing the pairing looks, not
whether it is right. So merged pairs and uncertain pairs are drawn together and
shuffled, and nothing on screen says which is which.

The rejections matter as much as the confirmations. A pair a person opened,
compared and called different is what gives precision an honest denominator;
without it every unlabelled pair is unjudgeable and the rate is computed over
whatever happened to be confirmed.

Nothing here writes to the store. The output is the fixture file, which is
read by `desk evals --suite dedup` and by nothing else.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SAME = "same"
DIFFERENT = "different"
SKIP = "skip"


@dataclass(frozen=True)
class Pair:
    """Two postings to compare, carrying nothing that says what was decided."""

    left: str
    right: str
    left_row: Mapping[str, Any]
    right_row: Mapping[str, Any]

    def as_lines(self) -> list[str]:
        """One posting per block, one field per line.

        Nothing this function composes puts a Hebrew and an English run on the
        same line — a terminal reorders the English run and the two adverts
        become hard to compare, which is the one thing this screen is for. That
        is why the company and the town are separate lines rather than joined
        by a separator, and why nothing is prefixed with an English label.

        The advert's own text is a different matter: it is quoted as the board
        wrote it, mixed scripts included, because a person judging whether two
        postings are the same seat has to see the words that were actually
        published.
        """
        lines: list[str] = []
        for side, row in (("A", self.left_row), ("B", self.right_row)):
            lines.append(f"  [{side}] {row.get('site', '')}")
            lines.append(f"      {(row.get('url') or '')[:70]}")
            lines.append(f"      {row.get('title', '')}")
            lines.append(f"      {row.get('company') or '—'}")
            lines.append(f"      {row.get('location') or '—'}")
            body = " ".join(str(row.get("body") or "").split())[:200]
            if body:
                lines.append(f"      {body}")
            lines.append("")
        return lines


def sample(
    links: Sequence[Mapping[str, Any]],
    rows: Mapping[str, Mapping[str, Any]],
    *,
    bands: Sequence[str] = ("duplicate", "uncertain"),
    size: int = 20,
    seed: int = 0,
    exclude: frozenset[tuple[str, str]] = frozenset(),
) -> list[Pair]:
    """Pairs to judge, drawn from several bands at once and shuffled.

    Seeded, so a run interrupted halfway resumes the same set instead of
    quietly changing what the measurement was taken on.
    """
    wanted = set(bands)
    pool: list[Pair] = []
    for link in links:
        if str(link.get("band")) not in wanted:
            continue
        left, right = str(link["left_fp"]), str(link["right_fp"])
        if (left, right) in exclude or (right, left) in exclude:
            continue
        if left not in rows or right not in rows:
            continue
        pool.append(Pair(left=left, right=right, left_row=rows[left], right_row=rows[right]))

    random.Random(seed).shuffle(pool)
    return pool[:size]


def load_fixture(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"clusters": [], "distinct_pairs": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("clusters", [])
    data.setdefault("distinct_pairs", [])
    return data


def already_judged(fixture: Mapping[str, Any]) -> frozenset[tuple[str, str]]:
    """Every pair the file already settles, in either direction.

    A cluster of three settles all three of its pairs, which is why this is
    computed from members rather than read off a list of pairs.
    """
    seen: set[tuple[str, str]] = set()
    for cluster in fixture.get("clusters", ()):
        members = [str(m) for m in cluster.get("members", ())]
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                seen.add((left, right))
    for pair in fixture.get("distinct_pairs", ()):
        members = [str(m) for m in pair.get("members", ())]
        if len(members) == 2:
            seen.add((members[0], members[1]))
    return frozenset(seen)


def record(
    fixture: dict[str, Any],
    pair: Pair,
    verdict: str,
    *,
    note: str = "",
    now: str = "",
    by: str = "",
) -> dict[str, Any]:
    """Add one judgement. `skip` writes nothing, deliberately.

    An unjudged pair has to stay unjudged: filing it as "different" because the
    person could not tell would put a guess into the denominator of a
    measurement whose whole value is that a person actually looked.
    """
    if verdict == SAME:
        merged = False
        for cluster in fixture["clusters"]:
            members = {str(m) for m in cluster.get("members", ())}
            if pair.left in members or pair.right in members:
                cluster["members"] = sorted(members | {pair.left, pair.right})
                merged = True
                break
        if not merged:
            fixture["clusters"].append(
                {"members": sorted({pair.left, pair.right}), "note": note}
            )
    elif verdict == DIFFERENT:
        fixture["distinct_pairs"].append(
            {"members": [pair.left, pair.right], "note": note}
        )
    if verdict in (SAME, DIFFERENT):
        if now:
            fixture["labelled_at"] = now
        if by:
            fixture["labelled_by"] = by
    return fixture


def save(fixture: Mapping[str, Any], path: Path) -> None:
    path.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
