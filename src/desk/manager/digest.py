"""The daily digest — a ranked shortlist, built from what is already stored.

This is the last stage, and it is the one Noam actually reads, so its whole job
is to be honest about a small number of things rather than impressive about a
large number of them.

    an empty day is a valid answer, and it is stated in the spec so that it is
    a decision and not an oversight. `max_items: 5` is a ceiling and never a
    quota. A digest that reaches for a fifth item by relaxing the floor teaches
    Noam that the floor means nothing, and after a week of that he stops
    reading the score. Nothing here pads.

    nothing already applied to comes back, and neither does anything that has
    moved past `applied` in the pipeline. The store's blocklist answers the
    first; the state machine answers the second. Both are asked, because an
    interview that was arranged by phone was never marked applied.

    an `unknown` gate verdict is shown, not folded into a pass. That verdict
    exists precisely so a board that states no dates cannot look identical to a
    board that states fresh ones — one of the boards in the store publishes no
    date on any listing at all — and hiding it here would waste the distinction
    the gates went to the trouble of making.

    one cluster is one item. The resolver already decided which postings are
    one job seen twice; the digest asks it rather than re-deciding, and names
    the other boards the job also sits on, since that is useful and free.

Everything is read from the store. No model is called here and no posting is
fetched: by the time a run reaches this file, every judgment it shows has
already been made, recorded, and paid for once.

Distance is not computed anywhere in this repo. The spec asks for region and
distance per item, and the region is what the geography gate concluded — the
digest reports the gate's finding rather than re-deriving a location from the
text, so the digest and the gate can never disagree. Distance is reported as
unmeasured rather than estimated, because a made-up kilometre figure would be
the one number in this output that nobody could check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from ..analyst.types import Analysis
from . import states, timers
from .states import APPLIED
from .timers import Nudge

GEOGRAPHY = "geography"
UNKNOWN = "unknown"

# There is no distance source in this repo, and the honest string is better than
# a plausible one. If a site ever starts stating a commute, this is what changes.
DISTANCE_UNMEASURED = "not measured"


class DigestStore(timers.TimerStore, Protocol):
    """Everything the digest reads. It writes nothing."""

    def analyses(self, *, min_score: float | None = ...) -> list[dict[str, Any]]: ...

    def has_applied(self, fingerprint: str) -> bool: ...

    def merged_with(self, fingerprint: str) -> list[str]: ...

    def get_posting(self, fingerprint: str) -> dict[str, Any] | None: ...

    def tailored(self, fingerprint: str) -> dict[str, Any] | None: ...


@dataclass(frozen=True)
class Item:
    """One posting, carrying exactly what `digest.include_per_item` lists."""

    fingerprint: str
    title: str = ""
    company: str = ""
    site: str = ""
    url: str = ""
    score: float = 0.0
    reason: str = ""
    gates: tuple[dict[str, Any], ...] = ()
    region: str = UNKNOWN
    distance: str = DISTANCE_UNMEASURED
    channel: str = ""
    cv_path: str = ""
    also_on: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()

    @property
    def unknown_gates(self) -> tuple[str, ...]:
        """The gates that found nothing to judge on. Shown, never hidden."""
        return tuple(str(g.get("gate", "")) for g in self.gates if g.get("verdict") == UNKNOWN)

    @property
    def has_cv(self) -> bool:
        return bool(self.cv_path)

    def gate_line(self) -> str:
        return "  ".join(f"{g.get('gate')}={g.get('verdict')}" for g in self.gates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "title": self.title,
            "company": self.company,
            "site": self.site,
            "url": self.url,
            "score": self.score,
            "reason": self.reason,
            "gates": [dict(g) for g in self.gates],
            "unknown_gates": list(self.unknown_gates),
            "region": self.region,
            "distance": self.distance,
            "channel": self.channel,
            "tailored_cv_path": self.cv_path,
            "also_on": list(self.also_on),
            "gaps": list(self.gaps),
        }


@dataclass(frozen=True)
class Digest:
    """One day's answer, which is allowed to be that there is nothing."""

    date: str
    items: tuple[Item, ...] = ()
    follow_ups: tuple[Nudge, ...] = ()
    closed: tuple[str, ...] = ()
    min_score: float = 0.0
    max_items: int = 0
    considered: int = 0
    suppressed: dict[str, int] = field(default_factory=dict)
    offset: int = 0
    remaining: int = 0

    @property
    def empty(self) -> bool:
        return not self.items

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "items": [i.as_dict() for i in self.items],
            "follow_ups": [n.as_dict() for n in self.follow_ups],
            "closed": list(self.closed),
            "min_score": self.min_score,
            "max_items": self.max_items,
            "considered": self.considered,
            "offset": self.offset,
            "remaining": self.remaining,
            "suppressed": dict(sorted(self.suppressed.items())),
            # Stated in the payload and not only in the prose, so a consumer of
            # the JSON reads the same guarantee a reader of the text does.
            "auto_apply": "never",
        }


def _by_score_desc(items: list[Item]) -> list[Item]:
    """Highest score first, fingerprint breaking the tie.

    The tiebreak is not cosmetic. Two items on the same score would otherwise
    order by whatever sqlite returned, and the digest is one of the outputs the
    determinism test covers.
    """
    return sorted(items, key=lambda i: (-i.score, i.fingerprint))


ORDERINGS = {"score_desc": _by_score_desc}


def build(
    store: DigestStore,
    *,
    now: datetime,
    spec: dict[str, Any],
    limit: int | None = None,
    min_score: float | None = None,
    closed: tuple[str, ...] = (),
    offset: int = 0,
) -> Digest:
    """Assemble today's digest from the store.

    `limit` and `min_score` override the spec for one run — that is what the
    CLI flags are for, and an override is a deliberate act at a keyboard. When
    they are None the spec decides, which is the case the scheduled run takes.

    `offset` is how far down the same ranking to start, and it is what the
    "more" button sends back. A page is a window over one ordering rather than
    a second query: the fifth-best job is the fifth-best job whether it is read
    at 06:00 or three taps later, and re-ranking between pages would show the
    same posting twice or skip one silently. `remaining` says how many ranked
    candidates sit below the window, which is how the caller knows whether to
    offer another page at all.

    `closed` is what today's sweep is closing. It is passed in rather than read,
    because the command commits those closes only after this digest has been
    delivered — see `timers.close`. They are named in the digest and suppressed
    from it in the same breath: an item reported as closed must not also be
    offered as a candidate, nor chased in the follow-up list.
    """
    settings = spec["digest"]
    ceiling = settings["max_items"] if limit is None else limit
    floor = settings["min_score"] if min_score is None else min_score
    order_by = str(settings["order_by"])
    if order_by not in ORDERINGS:
        raise ValueError(f"digest.order_by is {order_by!r}; have {', '.join(ORDERINGS)}")

    candidates: list[Item] = []
    seen: set[str] = set()
    closing = set(closed)
    suppressed = {"applied": 0, "later_state": 0, "blocked": 0, "duplicate": 0, "closed": 0}

    for row in store.analyses(min_score=float(floor)):
        analysis = Analysis.from_json(str(row["payload"]))
        fingerprint = analysis.fingerprint or str(row["fingerprint"])
        if analysis.blocked:
            suppressed["blocked"] += 1
            continue

        # The cluster is computed and remembered before any suppression, and
        # every suppression question is asked of the whole cluster. Both halves
        # matter, and each one on its own is a bug that was there:
        #
        #   seeding `seen` after the `continue`s meant an applied posting never
        #   entered the dedupe set, so its merged twin sailed through the next
        #   morning as a fresh job — the same job Noam had already applied to,
        #   under a different fingerprint, on a different board.
        #
        #   asking the state machine about one fingerprint meant an interview
        #   arranged through the board that happened to be the *other* member
        #   of the cluster suppressed nothing at all. One cluster is one job,
        #   so one member having moved on is the job having moved on.
        #
        # `has_applied` is asked once and not per member: the store already
        # answers it for the whole cluster. The state row has no cluster-aware
        # reader, so that half is asked here.
        cluster = _cluster(store, fingerprint)
        already = cluster & seen
        seen |= cluster
        if already:
            suppressed["duplicate"] += 1
            continue
        if cluster & closing:
            suppressed["closed"] += 1
            continue
        if store.has_applied(fingerprint):
            suppressed["applied"] += 1
            continue
        if any(
            states.at_or_after(spec, states.current(store, member), APPLIED)
            for member in sorted(cluster)
        ):
            suppressed["later_state"] += 1
            continue

        candidates.append(_item(store, analysis, cluster))

    ranked = ORDERINGS[order_by](candidates)
    start = max(0, int(offset))
    window = max(0, int(ceiling))
    return Digest(
        date=now.date().isoformat(),
        items=tuple(ranked[start : start + window]),
        follow_ups=tuple(n for n in timers.due(store, now=now) if n.fingerprint not in closing),
        closed=tuple(closed),
        min_score=float(floor),
        max_items=int(ceiling),
        considered=len(candidates),
        suppressed=suppressed,
        offset=start,
        remaining=max(0, len(ranked) - start - window),
    )


def _cluster(store: DigestStore, fingerprint: str) -> set[str]:
    """Everything the resolver merged with this role, including itself."""
    try:
        return set(store.merged_with(fingerprint)) | {fingerprint}
    except Exception:
        # A store with no linking table is a store with no merges, not a store
        # that should fail to produce a digest.
        return {fingerprint}


def _item(store: DigestStore, analysis: Analysis, cluster: set[str]) -> Item:
    fingerprint = analysis.fingerprint
    posting = store.get_posting(fingerprint) or {}
    tailored = store.tailored(fingerprint) or {}
    return Item(
        fingerprint=fingerprint,
        title=analysis.title or str(posting.get("title") or ""),
        company=analysis.company or str(posting.get("company") or ""),
        site=analysis.site or str(posting.get("site") or ""),
        url=analysis.url or str(posting.get("url") or ""),
        score=float(analysis.fit.score),
        reason=analysis.fit.rationale,
        gates=tuple(dict(g) for g in analysis.gates),
        region=_region(analysis.gates),
        channel=analysis.fit.channel,
        cv_path=str(tailored.get("path") or ""),
        also_on=_also_on(store, cluster, fingerprint),
        gaps=tuple(analysis.fit.gaps),
    )


def _region(gates: tuple[dict[str, Any], ...]) -> str:
    """What the geography gate placed the posting in, in its own words.

    Read from the gate's stored details rather than recomputed. The gate is the
    only thing in the system allowed to decide where a posting is, and a second
    implementation here would eventually disagree with it about one posting and
    nobody would know which one was right.
    """
    for gate in gates:
        if gate.get("gate") != GEOGRAPHY:
            continue
        details = gate.get("details") or {}
        accepted = [str(r) for r in details.get("accepted_regions") or ()]
        if accepted:
            return accepted[0]
        regions = [str(r) for r in details.get("regions") or ()]
        if regions:
            return regions[0]
        if details.get("remote"):
            return "remote"
        return UNKNOWN
    return UNKNOWN


def _also_on(store: DigestStore, cluster: set[str], fingerprint: str) -> tuple[str, ...]:
    """The other boards carrying this same job, if the resolver found any."""
    sites: list[str] = []
    own = store.get_posting(fingerprint) or {}
    for member in sorted(cluster):
        if member == fingerprint:
            continue
        row = store.get_posting(member) or {}
        site = str(row.get("site") or "")
        if site and site != str(own.get("site") or "") and site not in sites:
            sites.append(site)
    return tuple(sites)
