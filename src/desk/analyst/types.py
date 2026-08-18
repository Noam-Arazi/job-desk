"""The shape the analyst speaks in — written before the analyst, on purpose.

Three sessions read this file. The analyst fills it, the tailoring agent reads a
family and a requirement list out of it, and the daily digest reads a score, a
reason and a channel. Freezing the shape first is what let all three be built at
once without one of them guessing at another's output.

Two properties are structural rather than stylistic:

    every requirement carries the span it was read from. A requirement with no
    quotable span is not a weak requirement, it is a fabricated one, and the
    reflection loop deletes it. The span is checked against the posting text in
    Python before any model is asked whether it agrees.

    the analysis records where it stopped. A posting the gates blocked, one that
    matched no family, and one that was scored and came out low are three
    different answers, and a digest that showed them identically would hide the
    only signal that says whether the spec is too tight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# Families come from spec/search.yaml and map 1:1 to an approved CV base.
# NONE is a real answer and the cheap one: a microbiologist matches no family,
# and there is no reason to spend a judgment-tier call scoring a fit nobody
# will read.
NONE = "none"

SKILL = "skill"
EXPERIENCE = "experience"
DEGREE = "degree"
LANGUAGE = "language"
OTHER = "other"
KINDS = (SKILL, EXPERIENCE, DEGREE, LANGUAGE, OTHER)

# Where a run ended. "" means it ran the whole way.
STOPPED_GATES = "gates"
STOPPED_FAMILY = "family"
STOPPED_EXTRACT = "extract"
STOPPED_REFLECT = "reflect"

BUTTON = "button"
PERSON = "person"
SKIP = "skip"
CHANNELS = (BUTTON, PERSON, SKIP)


@dataclass(frozen=True)
class Requirement:
    """One thing the posting asks for, and the words it asked in.

    `text` is the requirement as a phrase. `evidence` is the posting's own
    wording, verbatim, and it is the field the anchoring check reads.
    """

    text: str
    kind: str = OTHER
    mandatory: bool = True
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "mandatory": self.mandatory,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Requirement:
        return cls(
            text=str(data.get("text", "")),
            kind=str(data.get("kind", OTHER)),
            mandatory=bool(data.get("mandatory", True)),
            evidence=str(data.get("evidence", "")),
        )


@dataclass(frozen=True)
class Family:
    """Which CV base this posting belongs to, or that it belongs to none."""

    family: str = NONE
    confidence: float = 0.0
    reason: str = ""

    @property
    def matched(self) -> bool:
        return self.family != NONE

    def as_dict(self) -> dict[str, Any]:
        return {"family": self.family, "confidence": self.confidence, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Family:
        return cls(
            family=str(data.get("family", NONE)),
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "")),
        )


@dataclass(frozen=True)
class Fit:
    """The score, the one line that justifies it, and how to apply.

    `gaps` is what the posting demanded and the experience inventory does not
    cover. The change contract forbids the tailoring agent from inventing its
    way across a gap, so the gap travels to the human instead of disappearing.
    """

    score: float = 0.0
    rationale: str = ""
    channel: str = SKIP
    gaps: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "rationale": self.rationale,
            "channel": self.channel,
            "gaps": list(self.gaps),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fit:
        return cls(
            score=float(data.get("score", 0.0)),
            rationale=str(data.get("rationale", "")),
            channel=str(data.get("channel", SKIP)),
            gaps=tuple(str(g) for g in data.get("gaps", ())),
        )


@dataclass(frozen=True)
class Analysis:
    """Everything the analyst concluded about one posting.

    `gates` is stored as the list of dicts `GateReport` already emits rather
    than as the report object, so this survives a round trip through sqlite
    without the gates package having to be importable to read a stored row.
    """

    fingerprint: str
    site: str = ""
    title: str = ""
    company: str = ""
    url: str = ""
    gates: tuple[dict[str, Any], ...] = ()
    family: Family = field(default_factory=Family)
    requirements: tuple[Requirement, ...] = ()
    fit: Fit = field(default_factory=Fit)
    stopped_at: str = ""
    reflect_rounds: int = 0
    dropped: tuple[str, ...] = ()
    run_id: str = ""

    @property
    def blocked(self) -> bool:
        return any(g.get("verdict") == "block" for g in self.gates)

    @property
    def scored(self) -> bool:
        return not self.stopped_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "site": self.site,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "gates": [dict(g) for g in self.gates],
            "family": self.family.as_dict(),
            "requirements": [r.as_dict() for r in self.requirements],
            "fit": self.fit.as_dict(),
            "stopped_at": self.stopped_at,
            "reflect_rounds": self.reflect_rounds,
            "dropped": list(self.dropped),
            "run_id": self.run_id,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Analysis:
        return cls(
            fingerprint=str(data.get("fingerprint", "")),
            site=str(data.get("site", "")),
            title=str(data.get("title", "")),
            company=str(data.get("company", "")),
            url=str(data.get("url", "")),
            gates=tuple(dict(g) for g in data.get("gates", ())),
            family=Family.from_dict(data.get("family", {}) or {}),
            requirements=tuple(Requirement.from_dict(r) for r in data.get("requirements", ())),
            fit=Fit.from_dict(data.get("fit", {}) or {}),
            stopped_at=str(data.get("stopped_at", "")),
            reflect_rounds=int(data.get("reflect_rounds", 0)),
            dropped=tuple(str(d) for d in data.get("dropped", ())),
            run_id=str(data.get("run_id", "")),
        )

    @classmethod
    def from_json(cls, text: str) -> Analysis:
        return cls.from_dict(json.loads(text))
