"""Budget: the run stops cleanly at the ceiling instead of spending past it."""

from __future__ import annotations

import pytest

from desk.llm.base import BudgetExceeded
from desk.orchestrator import Status, run
from desk.pipeline import AGENTS, demo_plan


def test_the_gateway_refuses_a_call_once_the_ceiling_is_reached(make_ctx):
    ctx = make_ctx(budget_usd=0.001)  # one sample call costs more than this
    with pytest.raises(BudgetExceeded):
        run_until_budget(ctx)


def run_until_budget(ctx) -> None:
    from desk.pipeline import load_samples, normalize_request

    for posting in load_samples():
        ctx.gateway.complete(normalize_request(posting), ctx=ctx)


def test_the_budget_hook_fires_exactly_once(make_ctx):
    ctx = make_ctx(budget_usd=0.001)
    with pytest.raises(BudgetExceeded):
        run_until_budget(ctx)
    with pytest.raises(BudgetExceeded):
        run_until_budget(ctx)

    fired = [e for e in ctx.tracer.events if e["kind"] == "budget.exceeded"]
    assert len(fired) == 1, "the ceiling should announce itself once, not on every blocked call"
    assert fired[0]["ceiling_usd"] == 0.001


def test_a_run_that_hits_the_ceiling_reports_partial_rather_than_crashing(make_ctx):
    ctx = make_ctx(budget_usd=0.001)
    report = run(demo_plan(), AGENTS, ctx)

    assert report.ok is False
    assert report.partial is True
    assert report.by_id("ingest").status is Status.OK
    assert report.by_id("normalize").status is Status.FAILED
    assert "BudgetExceeded" in report.by_id("normalize").error
    # Dependents are skipped, not attempted with missing input.
    assert report.by_id("resolve").status is Status.SKIPPED
    assert report.by_id("report").status is Status.SKIPPED


def test_no_ceiling_means_no_limit(make_ctx):
    ctx = make_ctx(budget_usd=None)
    assert ctx.gateway.remaining() == float("inf")
    assert run(demo_plan(), AGENTS, ctx).ok


def test_remaining_shrinks_as_the_run_spends(make_ctx):
    ctx = make_ctx(budget_usd=1.0)
    before = ctx.gateway.remaining()
    run(demo_plan(), AGENTS, ctx)
    assert ctx.gateway.remaining() < before
