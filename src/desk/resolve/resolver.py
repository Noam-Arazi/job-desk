"""The duplicate resolver.

The measurement that forced this module: across the 369 postings in the store,
sixteen shared a fingerprint and every one of those sixteen was gotfriends
against gotfriends. Zero cross-site matches. The fingerprint is title + company
+ location, and an agency states no company and writes a paragraph where a title
belongs, so the identity it computes is real and the identity we need is not.

So this works on content. Three stages, cheapest first, and the model is only
reached by pairs the arithmetic could not settle:

    block     an inverted index on role-core tokens, minus the tokens too common
              to carry information. Turns an N-squared comparison into the pairs
              that could plausibly be related at all.
    score     two independent similarities, banded by explicit rules.
    judge     the uncertain band, and only it, goes to a model.

The bands exist so the escalation is bounded and visible. A run reports how many
pairs it settled by arithmetic and how many it paid a model for, and that ratio
is one of the numbers session 9 puts in the table.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .similarity import body_similarity, company_agrees, jaccard
from .titles import core_tokens

DUPLICATE = "duplicate"
UNCERTAIN = "uncertain"
DISTINCT = "distinct"

# A token in more than this share of postings says nothing about which job it is.
# "מנהל", "engineer" and "senior" are in hundreds of rows; blocking on them
# rebuilds the N-squared comparison the index exists to avoid.
MAX_DOC_FREQUENCY = 0.10

# Verbatim employer text. Two different sites carrying the same paragraphs is
# the strongest evidence available, and the only evidence allowed to overrule
# two different stated employers.
VERBATIM = 0.65

STRONG_CORE = 0.80
SUPPORTING_BODY = 0.30
WEAK_BODY = 0.35

# Below this, matching titles are not an open question — they are two different
# jobs that happen to share a name. Measured over the store: of the 185 pairs
# with near-identical role cores, 183 sat at a body similarity of 0.0 to 0.2,
# which is the floor two unrelated Hebrew postings in one industry reach anyway.
# Sending those to a model is paying for an answer the arithmetic already has.
UNCERTAIN_FLOOR = 0.25

# Words that name a rank rather than a job. Measured, not guessed: the first run
# over the store merged "AI Platform Engineer" with "AI Platform Team Lead" at
# the same client, because the agency reuses one blurb across a whole team it is
# staffing. Two postings whose rank words differ are two openings, and no amount
# of shared prose changes that.
_RANK = {
    "senior", "junior", "lead", "leader", "head", "principal", "staff", "chief",
    "director", "manager", "team", "expert", "בכיר", "זוטר", "ראש", "מוביל",
    "מנהל", "מומחה", "צוות",
}


@dataclass(frozen=True)
class PairScore:
    left: str
    right: str
    core: float
    body: float
    company: bool | None
    score: float
    band: str
    method: str = "deterministic"

    def as_row(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "core": round(self.core, 4),
            "body": round(self.body, 4),
            "company": self.company,
            "score": round(self.score, 4),
            "band": self.band,
            "method": self.method,
        }


def _key(posting: Mapping[str, Any]) -> str:
    return str(posting["fingerprint"])


def candidate_pairs(
    postings: list[Mapping[str, Any]],
    *,
    max_doc_frequency: float = MAX_DOC_FREQUENCY,
) -> set[tuple[int, int]]:
    """Index positions of pairs worth scoring, by shared informative token.

    Two postings that share no informative token are not compared at all. That
    is the whole cost saving, and it is also why the token cutoff is a spec-level
    number rather than a constant buried in a loop.
    """
    tokens = [core_tokens(str(p.get("title", ""))) for p in postings]

    frequency: dict[str, int] = {}
    for bag in tokens:
        for token in bag:
            frequency[token] = frequency.get(token, 0) + 1

    # At least two, or the share cuts below the count that makes a pair at all
    # and a small batch blocks on nothing. A daily run compares a handful of new
    # postings against a handful of recent ones, so this is the normal case.
    ceiling = max(2, int(len(postings) * max_doc_frequency))
    index: dict[str, list[int]] = {}
    for position, bag in enumerate(tokens):
        for token in bag:
            if frequency[token] <= ceiling:
                index.setdefault(token, []).append(position)

    pairs: set[tuple[int, int]] = set()
    for positions in index.values():
        for i, left in enumerate(positions):
            for right in positions[i + 1 :]:
                pairs.add((left, right) if left < right else (right, left))
    return pairs


def score_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> PairScore:
    """Two similarities and a band. No model, no network, no clock."""
    core = jaccard(
        core_tokens(str(left.get("title", ""))),
        core_tokens(str(right.get("title", ""))),
    )
    body = body_similarity(str(left.get("body", "")), str(right.get("body", "")))
    company = company_agrees(str(left.get("company", "")), str(right.get("company", "")))

    score = max(body, 0.6 * core + 0.4 * body)
    band = _band(
        core,
        body,
        company,
        same_site=left.get("site") == right.get("site"),
        same_rank=_ranks(left) == _ranks(right),
    )
    return PairScore(
        left=_key(left), right=_key(right), core=core, body=body,
        company=company, score=score, band=band,
    )


def _ranks(posting: Mapping[str, Any]) -> frozenset[str]:
    """The rank words in a role core, if any."""
    return frozenset(core_tokens(str(posting.get("title", ""))) & _RANK)


def _band(
    core: float,
    body: float,
    company: bool | None,
    *,
    same_site: bool,
    same_rank: bool,
) -> str:
    """The rules, written out rather than folded into one threshold.

    Each branch answers a pair shape that actually occurs in the store, and each
    is pinned by a test, so tightening one is a visible edit and not a nudge to
    a magic number.
    """
    # Different ranks are different openings, however alike the prose. This
    # never merges on its own evidence; it only refuses to.
    if not same_rank:
        return UNCERTAIN if (core >= STRONG_CORE or body >= VERBATIM) else DISTINCT

    # Two different named employers. Only their own words, carried verbatim, can
    # say this is one job posted twice rather than two jobs.
    if company is False:
        return DUPLICATE if body >= VERBATIM else DISTINCT

    if body >= VERBATIM:
        # Within one site, shared prose proves one author, not one job: an agency
        # recycles a client blurb across every seat it is filling there. The role
        # itself has to agree too before this collapses.
        if same_site and core < STRONG_CORE:
            return UNCERTAIN
        return DUPLICATE

    if core >= STRONG_CORE and body >= SUPPORTING_BODY:
        return DUPLICATE

    # A named employer that agrees supports a matching role; it cannot carry a
    # pair on its own. Measured: it merged a Cellcom distribution-channel rep
    # with a Cellcom warehouse hand, on nothing but the employer's boilerplate
    # paragraphs and one shared word. A large employer posts many different jobs.
    if company is True and core >= STRONG_CORE:
        return DUPLICATE

    if core >= STRONG_CORE and body >= UNCERTAIN_FLOOR:
        return UNCERTAIN
    if body >= WEAK_BODY:
        return UNCERTAIN

    return DISTINCT


Judge = Callable[[Mapping[str, Any], Mapping[str, Any], PairScore], bool]


@dataclass
class Resolution:
    pairs: list[PairScore]
    clusters: list[list[str]]
    compared: int
    judged: int

    @property
    def duplicates(self) -> list[PairScore]:
        return [p for p in self.pairs if p.band == DUPLICATE]

    def summary(self) -> dict[str, Any]:
        collapsed = sum(len(c) - 1 for c in self.clusters)
        return {
            "compared": self.compared,
            "duplicate": len(self.duplicates),
            "uncertain": sum(1 for p in self.pairs if p.band == UNCERTAIN),
            "judged": self.judged,
            "clusters": len(self.clusters),
            "collapsed": collapsed,
        }


def resolve(
    postings: Iterable[Mapping[str, Any]],
    *,
    judge: Judge | None = None,
    max_doc_frequency: float = MAX_DOC_FREQUENCY,
) -> Resolution:
    """Score the plausible pairs, escalate only the uncertain ones, then cluster.

    With no judge the uncertain band stays uncertain and does not merge. That is
    the safe direction: showing one job twice costs a line in the digest, and
    collapsing two different jobs loses one of them silently.
    """
    rows = list(postings)
    pairs: list[PairScore] = []
    judged = 0

    for left, right in sorted(candidate_pairs(rows, max_doc_frequency=max_doc_frequency)):
        scored = score_pair(rows[left], rows[right])
        if scored.band == UNCERTAIN and judge is not None:
            verdict = judge(rows[left], rows[right], scored)
            judged += 1
            scored = PairScore(
                left=scored.left, right=scored.right, core=scored.core,
                body=scored.body, company=scored.company, score=scored.score,
                band=DUPLICATE if verdict else DISTINCT, method="judged",
            )
        pairs.append(scored)

    merged = [(p.left, p.right) for p in pairs if p.band == DUPLICATE]
    return Resolution(
        pairs=pairs,
        clusters=cluster([_key(r) for r in rows], merged),
        compared=len(pairs),
        judged=judged,
    )


def cluster(keys: Iterable[str], links: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Union-find over the merged pairs.

    Transitivity is deliberate: if a board and an aggregator are the same job,
    and the aggregator and an agency are the same job, all three are one item in
    the digest even though the board and the agency were never compared directly.
    """
    parent: dict[str, str] = {k: k for k in keys}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for left, right in links:
        a, b = find(left), find(right)
        if a != b:
            parent[b] = a

    groups: dict[str, list[str]] = {}
    for key in parent:
        groups.setdefault(find(key), []).append(key)
    return [sorted(members) for members in groups.values() if len(members) > 1]
