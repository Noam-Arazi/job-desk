"""The analyst run: gates, family, requirements, reflection, score.

Four stages in strict cost order, and the ordering is the design. The gates
decide everything they can for nothing; the family router decides most of the
rest for nothing and reaches Haiku only where the arithmetic is genuinely mute;
the extractor and the scorer are Sonnet with thinking, and they are the last two
things reached rather than the first.

The rule that keeps the daily run affordable is stated here because this is the
file that enforces it: **a posting the gates blocked never reaches Sonnet.** It
still gets a family, because a family costs nothing when one term in the title
settles it, and knowing that a blocked posting was a data-analyst role is what
tells the human whether the spec is too tight. But the run stops at
`STOPPED_GATES` immediately after, and no judgment-tier call is made. Roughly
half of the live store is blocked; spending the extractor on that half would
multiply the cost of the daily run for answers nobody reads.

The chain is honest about where it stopped, which is the other half of the
design. Four different endings — blocked at the gates, matched no family,
produced no requirement, had every requirement deleted as unanchored — are four
different facts, and a digest that showed them identically would hide the only
signal that says which criterion needs loosening. `Analysis.stopped_at` carries
which one it was, and `Analysis.scored` is true only of a posting that ran all
the way through.

Nothing here applies to anything. The end of a full run is a score, a sentence
and a recommended channel, and the channel is advice.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..gates.chain import Candidate, FirstSeen, run_gates
from ..llm.base import LLMRequest
from . import extract, families, reflect, score
from .types import (
    STOPPED_EXTRACT,
    STOPPED_FAMILY,
    STOPPED_GATES,
    STOPPED_REFLECT,
    Analysis,
    Family,
    Fit,
    Requirement,
)


@dataclass
class Analyst:
    """One configured analyst, reusable across the postings of a single run.

    It holds the counter rather than returning it, because the number that
    matters is per run and not per posting: "how many model calls did tonight's
    analysis cost" is the question, and a per-posting count that the caller has
    to add up invites the caller to forget.
    """

    spec: Mapping[str, Any]
    gateway: Any = None
    ctx: Any = None
    now: datetime = field(default_factory=datetime.now)
    first_seen: FirstSeen | None = None
    has_applied: Callable[[str], bool] | None = None
    first_run: bool = False
    run_id: str = ""
    calls: Counter[str] = field(default_factory=Counter)
    stops: Counter[str] = field(default_factory=Counter)

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def ask(self, request: LLMRequest) -> Any:
        """The one place a model call is made, so the one place it is counted.

        Every stage receives this rather than the gateway. A stage that could
        reach the gateway directly is a stage that could spend without being
        counted, and the count is the measurement this session exists to make.
        """
        if self.gateway is None:
            raise RuntimeError("the analyst was asked for a model call with no gateway attached")
        self.calls[request.stage] += 1
        return self.gateway.complete(request, ctx=self.ctx).parsed

    def analyse(self, candidate: Candidate) -> Analysis:
        report = run_gates(
            candidate,
            spec=self.spec,
            now=self.now,
            first_seen=self.first_seen,
            has_applied=self.has_applied,
            first_run=self.first_run,
        )
        gates = tuple(r.as_dict() for r in report.results)

        # The deterministic family attempt happens for blocked postings too, and
        # deliberately without `ask`: a term in the title is free, and a model
        # call to refine the label of a posting nobody will score is not.
        family = families.route(
            candidate,
            spec=self.spec,
            ask=None if report.blocked else self.ask,
        )

        if report.blocked:
            return self._stop(candidate, gates, family, STOPPED_GATES)
        if not family.matched:
            return self._stop(candidate, gates, family, STOPPED_FAMILY)

        # Asked again when it comes back empty, because it does. The same
        # posting, the same prompt and the same model returned seven
        # requirements, then none, then none, then seven — and an empty answer
        # ends the analysis here, so the posting leaves the morning unscored
        # and looks exactly like one that did not match. The retry costs a call
        # only on the empty answer, and it is finite: no stated requirement is
        # a legal thing for a posting to have.
        attempts = 1 + int(((self.spec.get("analyst") or {}).get("extract") or {}).get(
            "empty_retries", 0))
        for _ in range(attempts):
            found = extract.extract(candidate, ask=self.ask, spec=self.spec)
            if found:
                break
        if not found:
            return self._stop(candidate, gates, family, STOPPED_EXTRACT)

        reflection = reflect.reflect(
            found,
            candidate,
            spec=self.spec,
            ask=self.ask,
            regenerate=extract.regenerator(candidate, ask=self.ask),
        )
        if not reflection.requirements:
            return self._stop(
                candidate,
                gates,
                family,
                STOPPED_REFLECT,
                rounds=reflection.rounds,
                dropped=reflection.dropped,
                extracted=reflection.extracted,
                unanchored=reflection.unanchored,
                unsupported=reflection.unsupported,
            )

        fit = score.score(
            candidate,
            family,
            reflection.requirements,
            spec=self.spec,
            ask=self.ask,
        )
        self.stops[""] += 1
        return self._analysis(
            candidate,
            gates=gates,
            family=family,
            requirements=reflection.requirements,
            fit=fit,
            stopped_at="",
            rounds=reflection.rounds,
            dropped=reflection.dropped,
            extracted=reflection.extracted,
            unanchored=reflection.unanchored,
            unsupported=reflection.unsupported,
        )

    def _stop(
        self,
        candidate: Candidate,
        gates: tuple[dict[str, Any], ...],
        family: Family,
        stopped_at: str,
        *,
        rounds: int = 0,
        dropped: tuple[str, ...] = (),
        extracted: int = 0,
        unanchored: int = 0,
        unsupported: int = 0,
    ) -> Analysis:
        self.stops[stopped_at] += 1
        return self._analysis(
            candidate,
            gates=gates,
            family=family,
            requirements=(),
            fit=Fit(),
            stopped_at=stopped_at,
            rounds=rounds,
            dropped=dropped,
            extracted=extracted,
            unanchored=unanchored,
            unsupported=unsupported,
        )

    def _analysis(
        self,
        candidate: Candidate,
        *,
        gates: tuple[dict[str, Any], ...],
        family: Family,
        requirements: tuple[Requirement, ...],
        fit: Fit,
        stopped_at: str,
        rounds: int,
        dropped: tuple[str, ...],
        extracted: int = 0,
        unanchored: int = 0,
        unsupported: int = 0,
    ) -> Analysis:
        return Analysis(
            fingerprint=candidate.fingerprint,
            site=candidate.site,
            title=candidate.title,
            company=candidate.company,
            url="",
            gates=gates,
            family=family,
            requirements=requirements,
            fit=fit,
            stopped_at=stopped_at,
            reflect_rounds=rounds,
            dropped=dropped,
            extracted=extracted,
            unanchored=unanchored,
            unsupported=unsupported,
            run_id=self.run_id,
        )


def analyse_row(analyst: Analyst, row: Mapping[str, Any]) -> Analysis:
    """Analyse one stored posting, keeping the url the row carries.

    `Candidate` deliberately does not carry a url — no gate reads one — so it is
    put back here rather than being lost. The digest links to the posting, and a
    ranked item with no way to open it is not usable advice.
    """
    analysis = analyst.analyse(Candidate.from_row(row))
    url = row.get("url") or ""
    if not url:
        return analysis
    data = analysis.as_dict()
    data["url"] = url
    return Analysis.from_dict(data)
