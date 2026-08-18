"""The stage routing table.

Two principles, stated once here and repeated in the README.

Cut deterministically before spending a token, and step up a model tier only as
the candidate set narrows. Tens of postings are ingested a day, single digits
pass the gates into analysis, two or three reach tailoring.

A mechanical stage goes to the cheapest model with a tighter prompt rather than
to a stronger model with a loose one. Normalizing, routing to a family, breaking
a dedup tie, checking that nothing was fabricated and emitting a plan over a
fixed list of agents are all copy-and-classify work: the cost of a mistake there
is caught by the next stage, and the instruction can be made explicit enough
that a small model is not guessing. Judgment stages — what a posting actually
requires, how well it fits, what a tailored document should say — keep Sonnet,
because there the instruction cannot be made explicit enough to substitute.

Each stage names its model explicitly. A stage cannot silently escalate: the
resolver refuses any model whose tier rank exceeds the stage's declared ceiling,
and a test walks the whole table asserting the ranks.
"""

from __future__ import annotations

from dataclasses import dataclass

# --- models ---------------------------------------------------------------
# Prices are list USD per million tokens, used for local cost attribution only.

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-5"
OPUS = "claude-opus-5"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    rank: int  # cost/capability order; a stage may not exceed its ceiling rank
    input_per_mtok: float
    output_per_mtok: float
    # Haiku 4.5 does not accept output_config.effort — sending it is a 400.
    supports_effort: bool
    # Sonnet 5 and Opus 5 take adaptive thinking; Haiku 4.5 takes a token budget,
    # and none of the Haiku stages here want thinking at all.
    supports_adaptive_thinking: bool


MODELS: dict[str, ModelSpec] = {
    HAIKU: ModelSpec(
        HAIKU,
        rank=1,
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        supports_effort=False,
        supports_adaptive_thinking=False,
    ),
    SONNET: ModelSpec(
        SONNET,
        rank=2,
        input_per_mtok=3.00,
        output_per_mtok=15.00,
        supports_effort=True,
        supports_adaptive_thinking=True,
    ),
    OPUS: ModelSpec(
        OPUS,
        rank=3,
        input_per_mtok=5.00,
        output_per_mtok=25.00,
        supports_effort=True,
        supports_adaptive_thinking=True,
    ),
}

CACHE_READ_MULTIPLIER = 0.1


@dataclass(frozen=True)
class Route:
    stage: str
    model: str
    effort: str | None
    thinking: bool
    ceiling: str  # the most expensive model this stage is ever allowed to use

    @property
    def spec(self) -> ModelSpec:
        return MODELS[self.model]


def _route(stage: str, model: str, effort: str | None, thinking: bool = False) -> Route:
    return Route(stage=stage, model=model, effort=effort, thinking=thinking, ceiling=model)


# Deterministic stages are absent on purpose: fetch, HTML-to-text, the dedup
# fingerprint and the hard gates spend no tokens at all.
TABLE: dict[str, Route] = {
    "normalize_posting": _route("normalize_posting", HAIKU, "low"),
    "route_family": _route("route_family", HAIKU, "low"),
    "dedup_tiebreak": _route("dedup_tiebreak", HAIKU, "low"),
    "extract_requirements": _route("extract_requirements", SONNET, "medium", thinking=True),
    # The evaluator half of the extraction loop. It is deliberately a tier
    # below the generator: the Python check has already thrown out every span
    # that is not literally in the posting, so what is left for the model is
    # "does this quote actually support this requirement" — a comparison over
    # two short strings, not a reading of the posting.
    "reflect_anchors": _route("reflect_anchors", HAIKU, "low"),
    "fit_score": _route("fit_score", SONNET, "medium", thinking=True),
    # Exists only for the baseline the measurements table compares against.
    # A single agent has no routing table: it holds one conversation and one
    # model does normalising, extraction and scoring together, so the tier
    # here is the strongest tier any of those stages needs. Routing it lower
    # would flatter the orchestrated side of the comparison.
    "single_agent_turn": _route("single_agent_turn", SONNET, "medium", thinking=True),
    "tailor_cv": _route("tailor_cv", SONNET, "high", thinking=True),
    "verify_no_fabrication": _route("verify_no_fabrication", HAIKU, "low"),
    "freelance_proposal": _route("freelance_proposal", SONNET, "medium", thinking=True),
    "outreach_draft": _route("outreach_draft", SONNET, "medium", thinking=True),
    # Moved down from Sonnet: the plan is a small typed structure over a fixed
    # list of registered agents, and the runner validates it before executing.
    "orchestrator_plan": _route("orchestrator_plan", HAIKU, "low"),
    "weekly_calibration": _route("weekly_calibration", OPUS, "high", thinking=True),
    "eval_judge": _route("eval_judge", OPUS, "high", thinking=True),
}


class RoutingError(Exception):
    pass


def resolve(stage: str, override_model: str | None = None) -> Route:
    """Resolve a stage to its route.

    An override may only move *down* the cost ladder. Escalating past the stage's
    declared ceiling raises instead of quietly costing more.
    """
    try:
        route = TABLE[stage]
    except KeyError as exc:
        raise RoutingError(f"unknown stage {stage!r}") from exc

    if override_model is None:
        return route

    if override_model not in MODELS:
        raise RoutingError(f"unknown model {override_model!r}")
    if MODELS[override_model].rank > MODELS[route.ceiling].rank:
        raise RoutingError(
            f"stage {stage!r} may not escalate from {route.ceiling} to {override_model}"
        )
    return Route(stage, override_model, route.effort, route.thinking, route.ceiling)


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
) -> float:
    spec = MODELS[model]
    return round(
        (input_tokens / 1e6) * spec.input_per_mtok
        + (output_tokens / 1e6) * spec.output_per_mtok
        + (cache_read_tokens / 1e6) * spec.input_per_mtok * CACHE_READ_MULTIPLIER,
        8,
    )
