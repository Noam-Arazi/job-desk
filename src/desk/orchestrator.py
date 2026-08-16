"""The planning pattern: a typed plan with dependencies, so it can be asserted on.

A plan that exists only as prose inside a model's head cannot be tested. This one
is a Pydantic object with `depends_on` edges, validated before a single step
runs: unknown agents, unknown dependencies, duplicate ids and cycles are all
rejected up front rather than discovered halfway through a run.

Failure is isolated by design. A step that raises is recorded as failed, its
dependents are skipped, and every independent branch still completes — the run
reports partial success instead of collapsing. That is the property the chaos
test pins down: a site module that falls over must not take the run with it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .trace import Tracer

Agent = Callable[[Any, dict[str, Any], dict[str, Any]], Any]


class Status(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class Step(BaseModel):
    id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _snake(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError(f"step id {value!r} must be alphanumeric with underscores")
        return value


class Plan(BaseModel):
    goal: str
    steps: list[Step]

    @model_validator(mode="after")
    def _wellformed(self) -> Plan:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id {step.id!r}")
            for dep in step.depends_on:
                if dep not in seen:
                    raise ValueError(
                        f"step {step.id!r} depends on {dep!r}, which is not an earlier step"
                    )
            seen.add(step.id)
        return self

    def order(self) -> list[Step]:
        """Steps in dependency order. Validation already forbids cycles."""
        by_id = {s.id: s for s in self.steps}
        done: set[str] = set()
        ordered: list[Step] = []
        remaining = list(self.steps)
        while remaining:
            progressed = False
            for step in list(remaining):
                if all(d in done for d in step.depends_on):
                    ordered.append(by_id[step.id])
                    done.add(step.id)
                    remaining.remove(step)
                    progressed = True
            if not progressed:  # pragma: no cover - the validator rejects cycles first
                raise ValueError(f"cycle among steps: {[s.id for s in remaining]}")
        return ordered


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "agent": {"type": "string"},
                    "inputs": {"type": "object"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "agent"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["goal", "steps"],
    "additionalProperties": False,
}


@dataclass
class StepResult:
    id: str
    agent: str
    status: Status
    value: Any = None
    error: str | None = None


@dataclass
class RunReport:
    results: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.status is Status.OK for r in self.results)

    @property
    def partial(self) -> bool:
        return any(r.status is Status.OK for r in self.results) and not self.ok

    def by_id(self, step_id: str) -> StepResult:
        for result in self.results:
            if result.id == step_id:
                return result
        raise KeyError(step_id)

    def values(self) -> dict[str, Any]:
        return {r.id: r.value for r in self.results if r.status is Status.OK}

    def summary(self) -> dict[str, int]:
        counts = {s.value: 0 for s in Status}
        for result in self.results:
            counts[result.status.value] += 1
        return counts


class UnknownAgent(KeyError):
    pass


def validate(plan: Plan, agents: dict[str, Agent]) -> None:
    unknown = sorted({s.agent for s in plan.steps} - set(agents))
    if unknown:
        raise UnknownAgent(f"plan names agents that are not registered: {', '.join(unknown)}")


def run(plan: Plan, agents: dict[str, Agent], ctx: Any, tracer: Tracer | None = None) -> RunReport:
    """Execute a validated plan. A failing step never aborts the run."""
    validate(plan, agents)
    tracer = tracer or getattr(ctx, "tracer", None)
    report = RunReport()
    values: dict[str, Any] = {}
    failed: set[str] = set()

    for step in plan.order():
        blocked = [d for d in step.depends_on if d in failed]
        if blocked:
            report.results.append(
                StepResult(
                    step.id,
                    step.agent,
                    Status.SKIPPED,
                    error=f"depends on failed step(s): {', '.join(blocked)}",
                )
            )
            failed.add(step.id)
            if tracer is not None:
                tracer.emit("step.skipped", step=step.id, agent=step.agent, blocked_by=blocked)
            continue

        upstream = {d: values.get(d) for d in step.depends_on}
        span = tracer.span("step", step.id, agent=step.agent) if tracer else _NullSpan()
        with span:
            try:
                value = agents[step.agent](ctx, step.inputs, upstream)
            except Exception as exc:  # noqa: BLE001 - isolate the blast radius
                # The exception is swallowed here so one module cannot take the
                # run down, so the span has to be told, or it closes as a success.
                span.fail(exc)
                failed.add(step.id)
                report.results.append(
                    StepResult(
                        step.id, step.agent, Status.FAILED, error=f"{type(exc).__name__}: {exc}"
                    )
                )
                if hasattr(ctx, "hooks"):
                    ctx.hooks.on_error(None, exc, ctx)
            else:
                values[step.id] = value
                report.results.append(StepResult(step.id, step.agent, Status.OK, value=value))

    return report


class _NullSpan:
    def __enter__(self) -> _NullSpan:
        return self

    def fail(self, error: object) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return False
