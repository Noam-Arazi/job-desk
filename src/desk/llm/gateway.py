"""The single point every model call goes through.

Everything that must be true of *every* call lives here rather than in the
agents: the budget ceiling, the trace span with its own cost attribution, and
schema validation with a bounded retry. An agent calls `gateway.complete(...)`
and gets a validated object back or an exception; it cannot opt out of the
accounting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..hooks import HookBus
from ..trace import Tracer, Usage
from .base import BudgetExceeded, LLMRequest, LLMResponse, StructuredOutputError
from .routing import Route, resolve


@dataclass
class Gateway:
    client: Any
    tracer: Tracer
    hooks: HookBus = field(default_factory=HookBus)
    budget_usd: float | None = None
    max_schema_retries: int = 1
    _budget_fired: bool = False

    def spent(self) -> Usage:
        return self.tracer.total

    def remaining(self) -> float:
        if self.budget_usd is None:
            return float("inf")
        return max(0.0, self.budget_usd - self.tracer.total.cost_usd)

    def _check_budget(self, ctx: Any) -> None:
        if self.budget_usd is None:
            return
        if self.tracer.total.cost_usd < self.budget_usd:
            return
        if not self._budget_fired:
            self._budget_fired = True
            self.hooks.on_budget_exceeded(self.tracer.total, self.budget_usd, ctx)
        raise BudgetExceeded(
            f"spent ${self.tracer.total.cost_usd:.4f} of ${self.budget_usd:.4f} ceiling"
        )

    def complete(
        self,
        req: LLMRequest,
        *,
        override_model: str | None = None,
        ctx: Any = None,
    ) -> LLMResponse:
        route: Route = resolve(req.stage, override_model)

        attempt = 0
        last_error: Exception | None = None
        with self.tracer.span(
            "model",
            req.stage,
            model=route.model,
            effort=route.effort,
            prompt_id=req.prompt_id,
            prompt_sha256=req.prompt_sha256,
        ) as span:
            while attempt <= self.max_schema_retries:
                # Checked every time round, not once before the loop. The retry
                # is a second billable call, and the ceiling that was checked
                # against an empty tally cannot speak for what the first call
                # already spent: a $6 answer under a $1 ceiling used to become
                # $12 inside a single complete().
                self._check_budget(ctx)
                attempt += 1
                response = self.client.complete(req, route)
                span.attribute(response.usage)

                if req.schema is None:
                    return response

                try:
                    response.parsed = _validate(response, req.schema)
                    return response
                except StructuredOutputError as exc:
                    last_error = exc
                    self.hooks.on_error(None, exc, ctx)
                    # Retry on mismatch — never regex the response into shape.
                    req = LLMRequest(
                        stage=req.stage,
                        system=req.system,
                        user=(
                            f"{req.user}\n\n"
                            f"Your previous answer did not match the required schema "
                            f"({exc}). Return only valid JSON matching the schema."
                        ),
                        schema=req.schema,
                        max_tokens=req.max_tokens,
                        prompt_id=req.prompt_id,
                        prompt_sha256=req.prompt_sha256,
                    )

            raise StructuredOutputError(
                f"stage {route.stage!r} failed schema validation after {attempt} attempts: "
                f"{last_error}"
            )


def _validate(response: LLMResponse, schema: dict[str, Any]) -> Any:
    payload = response.parsed
    if payload is None:
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(f"not JSON: {exc}") from exc

    required = schema.get("required", [])
    properties = schema.get("properties", {})

    if schema.get("type") == "object":
        if not isinstance(payload, dict):
            raise StructuredOutputError(f"expected an object, got {type(payload).__name__}")
        missing = [k for k in required if k not in payload]
        if missing:
            raise StructuredOutputError(f"missing required keys: {', '.join(sorted(missing))}")
        if schema.get("additionalProperties") is False:
            extra = [k for k in payload if k not in properties]
            if extra:
                raise StructuredOutputError(f"unexpected keys: {', '.join(sorted(extra))}")

    return payload
