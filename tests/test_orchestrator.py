"""The plan is typed, so it can be asserted on before anything runs."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from desk.orchestrator import Plan, UnknownAgent, validate


def test_a_dependency_on_a_later_step_is_rejected():
    with pytest.raises(ValidationError, match="not an earlier step"):
        Plan(
            goal="g",
            steps=[
                {"id": "a", "agent": "x", "depends_on": ["b"]},
                {"id": "b", "agent": "x"},
            ],
        )


def test_a_dependency_on_a_step_that_does_not_exist_is_rejected():
    with pytest.raises(ValidationError, match="not an earlier step"):
        Plan(goal="g", steps=[{"id": "a", "agent": "x", "depends_on": ["ghost"]}])


def test_duplicate_step_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate step id"):
        Plan(goal="g", steps=[{"id": "a", "agent": "x"}, {"id": "a", "agent": "y"}])


def test_a_cycle_cannot_be_expressed():
    """Forbidding backward references makes a cycle unrepresentable, not just caught."""
    with pytest.raises(ValidationError):
        Plan(
            goal="g",
            steps=[
                {"id": "a", "agent": "x", "depends_on": ["b"]},
                {"id": "b", "agent": "x", "depends_on": ["a"]},
            ],
        )


def test_an_unregistered_agent_is_rejected_before_the_run_starts():
    plan = Plan(goal="g", steps=[{"id": "a", "agent": "imaginary"}])
    with pytest.raises(UnknownAgent, match="imaginary"):
        validate(plan, {"real": lambda ctx, i, u: None})


def test_order_is_a_valid_topological_order():
    plan = Plan(
        goal="g",
        steps=[
            {"id": "a", "agent": "x"},
            {"id": "b", "agent": "x", "depends_on": ["a"]},
            {"id": "c", "agent": "x", "depends_on": ["a"]},
            {"id": "d", "agent": "x", "depends_on": ["b", "c"]},
        ],
    )
    order = [s.id for s in plan.order()]
    assert order.index("a") < order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_the_demo_plan_validates_against_the_real_agents():
    from desk.pipeline import AGENTS, demo_plan

    validate(demo_plan(), AGENTS)
