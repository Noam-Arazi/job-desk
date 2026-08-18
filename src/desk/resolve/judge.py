"""The escalation — the only part of dedup that costs anything.

It is deliberately small and deliberately last. The arithmetic settles 99% of
the pairs it compares; this exists for the remainder, and the ratio between the
two is one of the numbers the evals table reports.

The stage routes to Haiku at low effort. The question is narrow, the evidence is
already extracted, and a larger model here would buy nothing measurable — which
is itself the point being demonstrated.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import prompts
from ..llm.base import LLMRequest
from .resolver import Judge, PairScore

STAGE = "dedup_tiebreak"

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "same_opening": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["same_opening", "reason"],
    "additionalProperties": False,
}

SYSTEM = (
    "You decide whether two job postings are one opening or two. "
    "You answer 'different' whenever the evidence does not settle it."
)

# Enough to recognise the job, short enough that an escalated pair stays cheap.
BODY_CHARS = 1200


def build_request(
    left: Mapping[str, Any], right: Mapping[str, Any], score: PairScore
) -> LLMRequest:
    """Built in one place so the recorder and the run always agree on the key."""
    prompt = prompts.load("resolver", "judge_pair", 1)
    company = {True: "both stated, and they agree", False: "both stated, and they differ"}.get(
        score.company, "at least one posting does not say"
    )
    return LLMRequest(
        stage=STAGE,
        system=SYSTEM,
        user=prompt.render(
            core=f"{score.core:.2f}",
            body=f"{score.body:.2f}",
            company=company,
            same_site=str(left.get("site") == right.get("site")).lower(),
            left_site=left.get("site", ""),
            left_title=left.get("title", ""),
            left_company=left.get("company", "") or "(not stated)",
            left_location=left.get("location", "") or "(not stated)",
            left_body=str(left.get("body", ""))[:BODY_CHARS],
            right_site=right.get("site", ""),
            right_title=right.get("title", ""),
            right_company=right.get("company", "") or "(not stated)",
            right_location=right.get("location", "") or "(not stated)",
            right_body=str(right.get("body", ""))[:BODY_CHARS],
        ),
        schema=SCHEMA,
        max_tokens=512,
        prompt_id=f"{prompt.agent}/{prompt.name}.v{prompt.version}",
        prompt_sha256=prompt.sha256,
    )


def gateway_judge(gateway: Any, ctx: Any = None) -> Judge:
    """A judge backed by the model gateway.

    A model that errors or returns nothing usable is a "different", not a crash
    and not a merge. The run continues, the pair stays separate, and the digest
    carries one extra line rather than losing a job.
    """

    def judge(left: Mapping[str, Any], right: Mapping[str, Any], score: PairScore) -> bool:
        response = gateway.complete(build_request(left, right, score), ctx=ctx)
        parsed = response.parsed
        return bool(parsed.get("same_opening")) if isinstance(parsed, dict) else False

    return judge
