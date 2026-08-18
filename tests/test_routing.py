"""Routing: no stage silently escalates to a more expensive model.

The cost story of this project is "cut deterministically, then step up a tier
only as the candidate set narrows". That story is only true if it is enforced.
An escalation that costs five times as much and produces a slightly nicer answer
is exactly the change nobody notices in review.
"""

from __future__ import annotations

import pytest

from desk.llm.routing import HAIKU, MODELS, OPUS, SONNET, TABLE, RoutingError, cost_usd, resolve

# The intended table, written out independently of the implementation. If a
# stage moves tier, this test fails and the move has to be deliberate.
EXPECTED = {
    "normalize_posting": HAIKU,
    "route_family": HAIKU,
    "dedup_tiebreak": HAIKU,
    "verify_no_fabrication": HAIKU,
    "orchestrator_plan": HAIKU,
    "reflect_anchors": HAIKU,
    "extract_requirements": SONNET,
    "fit_score": SONNET,
    "tailor_cv": SONNET,
    "freelance_proposal": SONNET,
    "outreach_draft": SONNET,
    "weekly_calibration": OPUS,
    "eval_judge": OPUS,
}


def test_every_stage_is_routed_where_the_plan_says():
    assert {stage: route.model for stage, route in TABLE.items()} == EXPECTED


def test_no_stage_can_escalate_past_its_ceiling():
    for stage, route in TABLE.items():
        for model, spec in MODELS.items():
            if spec.rank > MODELS[route.ceiling].rank:
                with pytest.raises(RoutingError, match="may not escalate"):
                    resolve(stage, model)


def test_a_stage_may_be_moved_down_the_ladder():
    assert resolve("tailor_cv", HAIKU).model == HAIKU


def test_unknown_stage_and_model_are_errors():
    with pytest.raises(RoutingError, match="unknown stage"):
        resolve("make_coffee")
    with pytest.raises(RoutingError, match="unknown model"):
        resolve("fit_score", "gpt-9")


def test_haiku_never_receives_the_effort_parameter():
    """Haiku 4.5 rejects output_config.effort; the route carries it, the client drops it."""
    assert MODELS[HAIKU].supports_effort is False
    for route in TABLE.values():
        if route.model == HAIKU:
            assert route.thinking is False


def test_only_the_offline_judge_and_the_weekly_loop_use_opus():
    opus_stages = sorted(s for s, r in TABLE.items() if r.model == OPUS)
    assert opus_stages == ["eval_judge", "weekly_calibration"]


def test_cost_is_monotonic_in_model_tier():
    for cheap, dear in ((HAIKU, SONNET), (SONNET, OPUS)):
        assert cost_usd(cheap, 1000, 1000) < cost_usd(dear, 1000, 1000)


def test_cached_input_is_cheaper_than_fresh_input():
    assert cost_usd(SONNET, 0, 0, cache_read_tokens=10_000) < cost_usd(SONNET, 10_000, 0)
