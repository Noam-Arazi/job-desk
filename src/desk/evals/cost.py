"""Cost and latency, read out of the run traces.

READ THIS BEFORE QUOTING A DOLLAR FIGURE FROM THIS SUITE.

The daily run goes through the Claude Code subscription, which shells out to
`claude -p`. Its marginal cost is effectively zero. The `cost_usd` this suite
totals is what the same tokens would have cost at API list price, and it is a
comparison unit, not money that was spent. `src/desk/llm/claude_code.py` says so
in its own docstring and the trace records the figure under that meaning. A
number from here belongs in a README as "list-price equivalent"; written as
"we spent" it is simply false. This repo has published a wrong measurement
before — the gate run measured on 369 rows that the current fetcher would never
have produced — and the correction is in STATUS.md. The cheap way to avoid the
next one is to make the unit part of the number.

What is genuinely measured here is tokens per stage. The trace emits one
`model.end` span per model call carrying the stage name, the model, the prompt
id and sha, and the usage. That is the orchestrator-and-workers claim made
checkable: narrow context per worker is either visible in the per-stage input
counts or it is marketing.

One trap this suite has to avoid is reading its own output. The guardrail suite
dispatches tools at a real run context, and opening a context opens a tracer, so
running `desk evals` writes traces of its own into runs/. Counted, they would
make this suite measure the cost of measuring — a total that grows every time
somebody looks at it, with the harness's spans attributed to the daily run's
stages. Two things stop it: the guardrail contexts are built under a temporary
root (see command.py), and any trace whose run id says the harness wrote it is
excluded here and named in a note (see `is_self_trace`). An exclusion nobody can
see is its own kind of dishonesty, so the count of skipped traces is reported.

Latency is NOT measured and cannot be, and the reason is a deliberate trade
made elsewhere. `src/desk/trace.py` measures elapsed time per span and then
declines to write it, because it is the one field that would break byte-
identical replay, which is what the determinism test asserts on. So this suite
reports latency as missing with that reason rather than inventing a timing.
Getting it back means a second, wall-clock trace — a real decision, not an
oversight to paper over.

The single-agent baseline has two forms and they are not the same thing:

    measured    a recorded single-agent run, at runs/single-agent/trace.jsonl.
                Nothing produces one yet, so it reports as missing.
    projected   arithmetic over the orchestrated trace under one stated
                assumption: a single agent holds one conversation, so call k
                re-reads every earlier input and output. It is a projection and
                it is labelled as one everywhere it appears.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .result import RATIO, SECONDS, TOKENS, USD, Measurement, SuiteResult, Table, missing

SUITE = "cost"

BASELINE_RUN = "single-agent"
MODEL_END = "model.end"

# The guardrail suite builds run contexts in order to dispatch tools at them,
# and building a context writes a trace. Left in, those traces would make this
# suite measure the cost of running the evals rather than the cost of the daily
# run, and the number would grow every time somebody looked at it.
SELF_RUN_PREFIX = "evals"

NO_TRACES = "no run trace found; run `desk demo` or `desk analyze` first"
ONLY_SELF_TRACES = (
    "the only traces under runs/ were written by the eval harness itself and are "
    "excluded; run `desk demo` or `desk analyze` first"
)
NO_MODEL_CALLS = "the traces contain no model call — every stage so far is deterministic"
NO_BASELINE = (
    f"no single-agent baseline trace at runs/{BASELINE_RUN}/trace.jsonl; the "
    "comparison below is a projection, not a second run"
)
FAILED_BASELINE = (
    f"the trace at runs/{BASELINE_RUN}/ has no successful model call — the baseline "
    "run started and died, most likely on the engine. A failed run is not a "
    "measurement of zero"
)
NO_LATENCY = (
    "the trace omits elapsed time on purpose — it is the one field that would "
    "break byte-identical replay (see src/desk/trace.py); measuring it needs a "
    "separate wall-clock run"
)

PRICE_NOTE = (
    "Dollar figures are API list price for the same tokens. The daily run goes "
    "through the Claude Code subscription at effectively zero marginal cost, so "
    "this is a comparison unit and never a bill."
)


def read_trace(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a run killed mid-write leaves a partial last line
    return events


def find_traces(runs_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Every trace under runs/, keyed by run id, sorted by id.

    This returns everything it finds, including the harness's own traces. The
    exclusion happens in `run()` so that the number of traces it declined to
    read is a knowable quantity rather than a silent filter.
    """
    if not runs_dir.exists():
        return {}
    found: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(runs_dir.glob("*/trace.jsonl")):
        events = read_trace(path)
        if events:
            found[path.parent.name] = events
    return found


def is_self_trace(run_id: str) -> bool:
    """Was this trace written by the eval harness rather than by a real run?

    The guardrail suite has to build run contexts in order to dispatch tools at
    them, and building a context opens a tracer, so `desk evals` leaves
    `runs/evals-0000/` and `runs/evals-noauth-0000/` behind. Reading those back
    here would make this suite measure the cost of measuring: the totals would
    grow every time somebody ran the command, and the eval's own spans would be
    attributed to the daily run's stages.

    Two things stop that, and this is the second of them. `command.py` now
    builds those contexts under a temporary root so nothing is written into
    `runs/` at all — but traces written before that change are still on disk,
    and a caller can still hand the suite an explicit `runs_dir`. Excluding by
    run id is the check that holds in every one of those cases.

    Run ids are `f"{mode}-{seed:04d}"` (src/desk/runner.py), so the eval modes
    `evals` and `evals-noauth` land under this prefix and a real run named e.g.
    `evaluation-0000` would not.
    """
    return run_id == SELF_RUN_PREFIX or run_id.startswith(SELF_RUN_PREFIX + "-")


def model_calls(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(e) for e in events if e.get("kind") == MODEL_END]


def per_stage(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Tokens and list-price cost, attributed to the stage that spent them."""
    stages: dict[str, dict[str, Any]] = {}
    for call in model_calls(events):
        stage = str(call.get("name") or "?")
        usage = dict(call.get("usage") or {})
        row = stages.setdefault(
            stage,
            {"stage": stage, "model": call.get("model", ""), "calls": 0, "in": 0, "out": 0,
             "cached": 0, "usd": 0.0},
        )
        row["calls"] += 1
        row["in"] += int(usage.get("input_tokens", 0))
        row["out"] += int(usage.get("output_tokens", 0))
        row["cached"] += int(usage.get("cache_read_tokens", 0))
        row["usd"] += float(usage.get("cost_usd", 0.0))
    return stages


def single_agent_projection(events: Sequence[Mapping[str, Any]]) -> int:
    """Input tokens one agent in one conversation would have read.

    The assumption, stated once and applied literally: a single agent keeps one
    conversation, so the k-th model call carries every earlier input and output
    in its context in addition to its own. The orchestrated run instead hands
    each worker only what that step needs, which is why its per-call input stays
    flat as the run grows.

    This is arithmetic over the orchestrated trace. It is not a second run, and
    every measurement derived from it is named `projected`.
    """
    total = 0
    carried = 0
    for call in model_calls(events):
        usage = dict(call.get("usage") or {})
        own_input = int(usage.get("input_tokens", 0))
        total += carried + own_input
        carried += own_input + int(usage.get("output_tokens", 0))
    return total


def run(
    *,
    runs_dir: Path,
    traces: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> SuiteResult:
    """Aggregate tokens and list-price cost per stage across the run traces."""
    all_traces = dict(traces) if traces is not None else find_traces(Path(runs_dir))
    baseline_events = all_traces.pop(BASELINE_RUN, None)

    # Drop the harness's own traces before anything is counted. See
    # `is_self_trace` — a suite that reads its own output measures itself.
    skipped = sorted(run_id for run_id in all_traces if is_self_trace(run_id))
    found = {k: v for k, v in all_traces.items() if not is_self_trace(k)}
    self_note = (
        f"Excluded {len(skipped)} trace(s) this harness wrote itself: "
        f"{', '.join(skipped)}. The guardrail suite dispatches tools at a real run "
        "context, which opens a tracer; counting those would make this suite "
        "measure the cost of measuring."
        if skipped
        else ""
    )

    if not found:
        why = ONLY_SELF_TRACES if skipped else NO_TRACES
        notes = [PRICE_NOTE, NO_BASELINE]
        if self_note:
            notes.append(self_note)
        return SuiteResult(
            suite=SUITE,
            measurements=(
                missing("model calls", why),
                missing("input tokens", why, unit=TOKENS),
                missing("output tokens", why, unit=TOKENS),
                missing("list-price equivalent", why, unit=USD),
                missing("latency", NO_LATENCY, unit=SECONDS),
            ),
            notes=tuple(notes),
            extra={"self_traces_excluded": skipped},
        )

    events = [e for run_events in found.values() for e in run_events]
    stages = per_stage(events)
    calls = sum(row["calls"] for row in stages.values())

    if not calls:
        notes = [PRICE_NOTE, NO_BASELINE]
        if self_note:
            notes.append(self_note)
        return SuiteResult(
            suite=SUITE,
            measurements=(
                Measurement("run traces read", len(found), detail=", ".join(sorted(found))),
                missing("model calls", NO_MODEL_CALLS),
                missing("input tokens", NO_MODEL_CALLS, unit=TOKENS),
                missing("output tokens", NO_MODEL_CALLS, unit=TOKENS),
                missing("list-price equivalent", NO_MODEL_CALLS, unit=USD),
                missing("latency", NO_LATENCY, unit=SECONDS),
            ),
            notes=tuple(notes),
            extra={"self_traces_excluded": skipped},
        )

    total_in = sum(row["in"] for row in stages.values())
    total_out = sum(row["out"] for row in stages.values())
    total_usd = sum(row["usd"] for row in stages.values())
    projected_in = sum(single_agent_projection(ev) for ev in found.values())

    measurements: list[Measurement] = [
        Measurement("run traces read", len(found), detail=", ".join(sorted(found))),
        Measurement("model calls", calls, detail=f"across {len(stages)} stages"),
        Measurement("input tokens", total_in, unit=TOKENS),
        Measurement("output tokens", total_out, unit=TOKENS),
        Measurement(
            "list-price equivalent",
            round(total_usd, 6),
            unit=USD,
            detail="API list price for the same tokens — NOT a bill; the daily run "
            "is on the subscription",
        ),
        Measurement(
            "projected single-agent input tokens",
            projected_in,
            unit=TOKENS,
            detail="projection: one conversation, every call re-reading all earlier "
            "turns. Arithmetic over this trace, not a second run.",
        ),
        (
            Measurement(
                "projected context saving",
                projected_in / total_in,
                unit=RATIO,
                detail="orchestrated input tokens against the projection above",
            )
            if total_in
            else missing("projected context saving", "no input tokens recorded", unit=RATIO)
        ),
        missing("latency", NO_LATENCY, unit=SECONDS),
    ]

    notes = [PRICE_NOTE]
    if self_note:
        notes.append(self_note)
    # A baseline that started and died leaves a trace full of failed calls with
    # zero tokens on them. Read as data it says "measured: 0 tokens, saving
    # 0.0x", which is the most flattering possible lie about the orchestrated
    # side. An unsuccessful run is missing data, exactly like no run at all.
    if baseline_events is not None and not any(
        call.get("ok") is not False and int((call.get("usage") or {}).get("input_tokens", 0))
        for call in model_calls(baseline_events)
    ):
        measurements.append(missing("measured single-agent baseline", FAILED_BASELINE, unit=TOKENS))
        notes.append(FAILED_BASELINE)
    elif baseline_events is None:
        measurements.append(missing("measured single-agent baseline", NO_BASELINE, unit=TOKENS))
        notes.append(NO_BASELINE)
    else:
        baseline_stages = per_stage(baseline_events)
        baseline_in = sum(row["in"] for row in baseline_stages.values())
        measurements.append(
            Measurement(
                "measured single-agent baseline",
                baseline_in,
                unit=TOKENS,
                detail=f"from runs/{BASELINE_RUN}/trace.jsonl",
            )
        )
        if total_in:
            measurements.append(
                Measurement(
                    "measured context saving",
                    baseline_in / total_in,
                    unit=RATIO,
                    detail="a real second run, not a projection",
                )
            )

    table = Table(
        title="tokens per stage",
        columns=("stage", "model", "calls", "in", "out", "cached", "list-price usd"),
        rows=tuple(
            (
                row["stage"],
                str(row["model"]),
                str(row["calls"]),
                f"{row['in']:,}",
                f"{row['out']:,}",
                f"{row['cached']:,}",
                f"${row['usd']:.6f}",
            )
            for row in sorted(stages.values(), key=lambda r: -r["in"])
        ),
        note="attribution per stage is what makes the narrow-context claim "
        "checkable rather than asserted.",
    )

    return SuiteResult(
        suite=SUITE,
        measurements=tuple(measurements),
        notes=tuple(notes),
        tables=(table,),
        extra={
            "stages": {k: v for k, v in sorted(stages.items())},
            "self_traces_excluded": skipped,
        },
    )
