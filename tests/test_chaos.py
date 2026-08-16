"""Chaos: one module falls over, the run completes and reports partial.

From session 4 onward every site is its own module. AllJobs changing its markup
overnight must not mean no digest that morning — it must mean a digest without
AllJobs, and a line in the report saying so.
"""

from __future__ import annotations

from desk.orchestrator import Plan, Status, run


def exploding(name: str):
    def _agent(ctx, inputs, upstream):
        raise RuntimeError(f"{name} markup changed")

    return _agent


def working(value: str):
    def _agent(ctx, inputs, upstream):
        return {"value": value, "upstream": upstream}

    return _agent


def test_an_independent_branch_survives_a_failing_one(ctx):
    plan = Plan(
        goal="fetch three sites and merge",
        steps=[
            {"id": "alljobs", "agent": "boom"},
            {"id": "drushim", "agent": "ok_a"},
            {"id": "gotfriends", "agent": "ok_b"},
        ],
    )
    report = run(
        plan, {"boom": exploding("alljobs"), "ok_a": working("a"), "ok_b": working("b")}, ctx
    )

    assert report.ok is False
    assert report.partial is True
    assert report.summary() == {"ok": 2, "failed": 1, "skipped": 0}
    assert "markup changed" in report.by_id("alljobs").error
    assert report.by_id("drushim").value["value"] == "a"


def test_dependents_of_a_failed_step_are_skipped_not_attempted(ctx):
    plan = Plan(
        goal="fetch then merge",
        steps=[
            {"id": "fetch", "agent": "boom"},
            {"id": "merge", "agent": "ok_a", "depends_on": ["fetch"]},
        ],
    )
    report = run(plan, {"boom": exploding("fetch"), "ok_a": working("merged")}, ctx)
    assert report.by_id("merge").status is Status.SKIPPED
    assert "depends on failed step" in report.by_id("merge").error


def test_the_failure_reaches_the_trace_and_the_error_hook(ctx):
    plan = Plan(goal="g", steps=[{"id": "s", "agent": "boom"}])
    run(plan, {"boom": exploding("s")}, ctx)

    kinds = [e["kind"] for e in ctx.tracer.events]
    assert "step.start" in kinds and "step.end" in kinds
    assert any(e["kind"] == "error" for e in ctx.tracer.events)
    end = next(e for e in ctx.tracer.events if e["kind"] == "step.end")
    assert end["ok"] is False


def test_upstream_values_reach_the_dependent_step(ctx):
    plan = Plan(
        goal="g",
        steps=[
            {"id": "first", "agent": "ok_a"},
            {"id": "second", "agent": "ok_b", "depends_on": ["first"]},
        ],
    )
    report = run(plan, {"ok_a": working("a"), "ok_b": working("b")}, ctx)
    assert report.by_id("second").value["upstream"]["first"]["value"] == "a"
