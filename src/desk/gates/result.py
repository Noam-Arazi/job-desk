"""What a gate says, and how the chain adds it up.

Three verdicts and not two. A gate that found nothing to judge on is not the
same as a gate that judged and let the posting through: `unknown` never blocks,
but it travels into the digest so the human can see which items were passed on
silence rather than on evidence. Folding it into `pass` would make a board that
states no dates look identical to a board that states fresh ones — and one of
the two boards in the store states no dates at all.

Nothing here decides anything. The gates decide; this is the shape they say it
in, and the shape the decisions table and the digest both read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Verdict(StrEnum):
    PASS = "pass"
    BLOCK = "block"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GateResult:
    """One gate's finding about one posting.

    `evidence` is the span the verdict was read from, quoted as the posting
    wrote it. A gate that blocks without being able to quote what it blocked on
    is a gate the human cannot argue with, so every blocking path here fills it.
    """

    gate: str
    verdict: Verdict
    reason: str = ""
    evidence: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "verdict": str(self.verdict),
            "reason": self.reason,
            "evidence": self.evidence,
            **({"details": dict(self.details)} if self.details else {}),
        }


@dataclass(frozen=True)
class GateReport:
    """Every gate's finding about one posting, in the order they ran."""

    results: tuple[GateResult, ...] = ()

    @property
    def blocked(self) -> bool:
        return any(r.blocks for r in self.results)

    @property
    def passed(self) -> bool:
        return not self.blocked

    @property
    def blocking(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if r.blocks)

    @property
    def unknowns(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if r.verdict is Verdict.UNKNOWN)

    def get(self, gate: str) -> GateResult | None:
        return next((r for r in self.results if r.gate == gate), None)

    def verdict_of(self, gate: str) -> Verdict | None:
        found = self.get(gate)
        return found.verdict if found else None

    @property
    def reason(self) -> str:
        """One line, for the digest and the decisions row.

        Every blocking gate is named, not just the first. A posting dropped for
        two reasons that only reports one sends the human to fix the wrong
        criterion in the spec.
        """
        if self.blocked:
            return " · ".join(f"{r.gate}: {r.reason}" for r in self.blocking)
        unknown = self.unknowns
        if unknown:
            return "passed, unstated: " + ", ".join(r.gate for r in unknown)
        return "passed all gates"

    @property
    def verdict(self) -> Verdict:
        """The chain's own verdict, in three values and not two.

        This file opens by arguing that folding `unknown` into `pass` makes a
        board that states no dates look identical to a board that states fresh
        ones — and then the serialization did exactly that, emitting a binary
        field into the decisions row and the digest. The per-gate verdicts
        survived underneath, so nothing was destroyed, but every reader of the
        top-level field was reading a two-valued answer to a three-valued
        question.

        A block anywhere is a block. Otherwise, a chain carrying any unstated
        gate passed on silence rather than on evidence, and says so.
        """
        if self.blocked:
            return Verdict.BLOCK
        return Verdict.UNKNOWN if self.unknowns else Verdict.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": str(self.verdict),
            # Kept alongside the three-valued field, because the only question
            # most callers have is whether the posting survived, and making
            # them re-derive it from a string is how a caller ends up writing
            # `verdict == "pass"` and dropping every unstated posting.
            "blocked": self.blocked,
            "reason": self.reason,
            "gates": [r.as_dict() for r in self.results],
        }
