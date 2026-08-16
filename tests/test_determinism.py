"""Golden: the same cassettes and the same seed produce a byte-identical trace.

Without this, "it reproduces offline" is a claim rather than a property. It is
also the test that keeps wall-clock values and random ids from creeping into the
trace, because either one breaks it immediately.
"""

from __future__ import annotations

import json

from desk.orchestrator import run
from desk.pipeline import AGENTS, demo_plan


def render(ctx) -> str:
    return ctx.tracer.render()


def test_two_runs_over_the_same_cassettes_are_byte_identical(make_ctx):
    first = make_ctx()
    second = make_ctx()

    run(demo_plan(), AGENTS, first)
    run(demo_plan(), AGENTS, second)

    assert render(first) == render(second)


def test_the_trace_carries_no_wall_clock_value(make_ctx):
    ctx = make_ctx()
    run(demo_plan(), AGENTS, ctx)
    # The frozen clock starts at this instant and steps one second per read.
    assert all(e["ts"].startswith("2026-01-01T00:") for e in ctx.tracer.events)


def test_every_model_span_names_its_prompt_version_and_hash(make_ctx):
    ctx = make_ctx()
    run(demo_plan(), AGENTS, ctx)
    spans = [e for e in ctx.tracer.events if e["kind"] == "model.end"]
    assert spans, "no model spans were recorded"
    for span in spans:
        assert span["prompt_id"].endswith(".v1")
        assert len(span["prompt_sha256"]) == 64
        assert span["usage"]["cost_usd"] > 0


def test_cost_is_attributed_per_step_and_sums_to_the_total(make_ctx):
    ctx = make_ctx()
    run(demo_plan(), AGENTS, ctx)
    per_span = sum(e["usage"]["cost_usd"] for e in ctx.tracer.events if e["kind"] == "model.end")
    assert round(per_span, 8) == round(ctx.tracer.total.cost_usd, 8)


def test_the_trace_file_on_disk_matches_the_in_memory_trace(make_ctx):
    ctx = make_ctx()
    run(demo_plan(), AGENTS, ctx)
    on_disk = ctx.tracer.path.read_text(encoding="utf-8")
    assert on_disk == ctx.tracer.render()
    for line in on_disk.splitlines():
        json.loads(line)  # append-only JSONL, one object per line
