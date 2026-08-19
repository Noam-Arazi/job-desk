"""The gold set — Noam's own verdicts, collected before he is told ours.

This exists because of one methodological point, and the point is the whole
value of the file.

The obvious way to build a gold set is to run the analyst, show him what it
decided, and let him agree or disagree. It is faster, it is easier on him, and
it produces a number that means nothing. A label formed while looking at the
system's answer is anchored to that answer: agreement is then partly a measure
of how persuasive the system's reasoning looked, and there is no way afterwards
to separate the part that was judgment from the part that was assent. The gold
set has to be independent of the thing it measures or it is not a gold set.

So the sampler here shows the posting and nothing else. No score, no gate
verdict, no reason. The verdicts are revealed only in `review`, after the labels
are recorded and can no longer move.

Two consequences worth stating:

    it does not need the analyst. The labels can be collected today, in
    parallel with building it, and they stay valid afterwards.

    it measures the gates too. The sample deliberately includes postings the
    gates blocked, so a label that disagrees with a block is visible. A sample
    drawn only from survivors can show what the gates wrongly let through and is
    structurally blind to what they wrongly dropped.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .gates import Candidate, run_gates

HIGH = "high"
MEDIUM = "medium"
IRRELEVANT = "irrelevant"
LABELS = (HIGH, MEDIUM, IRRELEVANT)

SURVIVED = "survived"
BLOCKED = "blocked"

# Twenty that reached the analyst against ten the gates dropped. Weighted toward
# survivors because that is the population the analyst actually scores, but not
# purely, because a sample with no blocked postings in it cannot tell him the
# gates are too tight.
DEFAULT_SIZE = 30
DEFAULT_BLOCKED_SHARE = 10


@dataclass(frozen=True)
class Item:
    """One posting to be judged, carrying nothing that could anchor the judge."""

    fingerprint: str
    site: str
    title: str
    company: str
    location: str
    body: str
    url: str
    stratum: str  # recorded, never displayed

    def render(self, *, width: int = 1200) -> str:
        company = self.company.strip() or "—"
        body = " ".join(self.body.split())[:width]
        return "\n".join(
            [
                f"{self.title}",
                f"{company} · {self.location or '—'} · {self.site}",
                f"{self.url}" if self.url else "",
                "",
                body,
            ]
        ).strip()


def sample(
    rows: Sequence[Mapping[str, Any]],
    *,
    spec: Mapping[str, Any],
    now: datetime,
    size: int = DEFAULT_SIZE,
    blocked_share: int = DEFAULT_BLOCKED_SHARE,
    seed: int = 0,
    exclude: frozenset[str] = frozenset(),
    first_seen: Any = None,
    has_applied: Any = None,
) -> list[Item]:
    """A stratified, reproducible sample of postings to label.

    Seeded so that two people running this see the same thirty postings, and so
    that a rerun after a crash resumes the same set rather than drawing a fresh
    one and quietly changing what the number was measured on.
    """
    survived: list[Mapping[str, Any]] = []
    blocked: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        fingerprint = row.get("fingerprint") or ""
        if not fingerprint or fingerprint in seen or fingerprint in exclude:
            continue
        seen.add(fingerprint)
        report = run_gates(
            Candidate.from_row(row),
            spec=spec,
            now=now,
            first_seen=first_seen,
            has_applied=has_applied,
        )
        (blocked if report.blocked else survived).append(row)

    rng = random.Random(seed)
    rng.shuffle(survived)
    rng.shuffle(blocked)

    # Clamped to the sample size before anything else. Without it a size smaller
    # than the blocked share makes the survivor count negative, and a negative
    # count is a slice from the end rather than an error — `--count 3` quietly
    # offered 976 postings.
    want_blocked = max(0, min(blocked_share, len(blocked), size))
    want_survived = max(0, min(size - want_blocked, len(survived)))
    # If one stratum is short, the other makes up the difference rather than
    # returning fewer than asked for.
    want_blocked = max(0, min(len(blocked), size - want_survived))

    picked = [(r, SURVIVED) for r in survived[:want_survived]]
    picked += [(r, BLOCKED) for r in blocked[:want_blocked]]
    rng.shuffle(picked)  # so the order itself does not leak the stratum
    return [_item(row, stratum) for row, stratum in picked]


def _item(row: Mapping[str, Any], stratum: str) -> Item:
    return Item(
        fingerprint=row.get("fingerprint") or "",
        site=row.get("site") or "",
        title=row.get("title") or "",
        company=row.get("company") or "",
        location=row.get("location") or "",
        body=row.get("body") or "",
        url=row.get("url") or "",
        stratum=stratum,
    )


@dataclass(frozen=True)
class Agreement:
    """How often the gates and the human reached the same conclusion.

    Only the gates are scored here. The analyst is not built yet, and when it is,
    it is measured against these same labels without them being collected again.
    """

    labelled: int
    gate_blocked_human_irrelevant: int
    gate_blocked_human_wanted: int
    gate_passed_human_irrelevant: int
    gate_passed_human_wanted: int

    @property
    def agreed(self) -> int:
        return self.gate_blocked_human_irrelevant + self.gate_passed_human_wanted

    @property
    def rate(self) -> float:
        return self.agreed / self.labelled if self.labelled else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "labelled": self.labelled,
            "agreed": self.agreed,
            "rate": round(self.rate, 3),
            "gates_dropped_something_he_wanted": self.gate_blocked_human_wanted,
            "gates_passed_something_he_did_not": self.gate_passed_human_irrelevant,
        }


def agreement(
    rows: Sequence[Mapping[str, Any]],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    spec: Mapping[str, Any],
    now: datetime,
    first_seen: Any = None,
    has_applied: Any = None,
) -> Agreement:
    """Compare the gates against the labels, once the labels are already fixed.

    The asymmetry is the interesting half. A posting the gates dropped and he
    would have wanted is a false block — the expensive kind, because he never
    sees it and cannot report it. A posting they passed and he calls irrelevant
    is what the analyst is for, and costs only a model call.
    """
    counters = dict.fromkeys(
        ("bb", "bw", "pb", "pw"), 0
    )  # gate blocked/passed × human irrelevant/wanted
    n = 0
    # One label is one judgement, however many boards carry the posting. The
    # sampler dedupes by fingerprint and this did not, so a role sitting on two
    # boards counted its single label twice — inflating the denominator of the
    # only number in this file, on a corpus where forty clusters span sites.
    counted: set[str] = set()
    for row in rows:
        fingerprint = row.get("fingerprint") or ""
        label = labels.get(fingerprint)
        if not label or fingerprint in counted:
            continue
        counted.add(fingerprint)
        n += 1
        # The same chain the daily run binds. Left unbound, freshness has no
        # store to ask how old a role really is and the applied gate can never
        # fire, so the gold set would be measuring a differently-configured
        # chain than the one it is meant to be scoring.
        blocked = run_gates(
            Candidate.from_row(row),
            spec=spec,
            now=now,
            first_seen=first_seen,
            has_applied=has_applied,
        ).blocked
        wanted = label["label"] in (HIGH, MEDIUM)
        key = ("b" if blocked else "p") + ("w" if wanted else "b")
        counters[key] += 1
    return Agreement(
        labelled=n,
        gate_blocked_human_irrelevant=counters["bb"],
        gate_blocked_human_wanted=counters["bw"],
        gate_passed_human_irrelevant=counters["pb"],
        gate_passed_human_wanted=counters["pw"],
    )
