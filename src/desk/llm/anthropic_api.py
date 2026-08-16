"""The API path: the Messages API through the official SDK.

Per-model request shape is decided by the routing table, not guessed here:

    Haiku 4.5    no `effort` (sending it is a 400), no adaptive thinking
    Sonnet 5     adaptive thinking, effort low..max, no sampling parameters
    Opus 5       adaptive thinking, effort low..max, no sampling parameters

Sampling parameters are absent everywhere on purpose — `temperature`, `top_p`
and `top_k` are rejected by Sonnet 5 and Opus 5. Determinism for the offline
judge comes from the replay path and a fixed effort level, not from temperature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..trace import Usage
from .base import LLMRequest, LLMResponse
from .routing import Route, cost_usd


@dataclass
class AnthropicClient:
    client: Any = None  # anthropic.Anthropic; injected in tests
    name: str = "anthropic-api"
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ImportError(
                    "the API path needs the optional dependency: uv sync --extra api"
                ) from exc
            # Credentials resolve from the environment or an `ant auth login`
            # profile; nothing is read from a file in this repo.
            self.client = anthropic.Anthropic()

    def _kwargs(self, req: LLMRequest, route: Route) -> dict[str, Any]:
        spec = route.spec
        kwargs: dict[str, Any] = {
            "model": route.model,
            "max_tokens": req.max_tokens,
            "messages": [{"role": "user", "content": req.user}],
        }
        if req.system:
            kwargs["system"] = req.system

        output_config: dict[str, Any] = {}
        if route.effort and spec.supports_effort:
            output_config["effort"] = route.effort
        if req.schema is not None:
            # Structured output is schema-validated at the API. We never regex a
            # response into shape.
            output_config["format"] = {"type": "json_schema", "schema": req.schema}
        if output_config:
            kwargs["output_config"] = output_config

        if spec.supports_adaptive_thinking and route.thinking:
            kwargs["thinking"] = {"type": "adaptive"}

        kwargs.update(self.extra)
        return kwargs

    def complete(self, req: LLMRequest, route: Route) -> LLMResponse:
        message = self.client.messages.create(**self._kwargs(req, route))

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        raw_usage = message.usage
        usage = Usage(
            input_tokens=int(getattr(raw_usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(raw_usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(raw_usage, "cache_read_input_tokens", 0) or 0),
        )
        usage.cost_usd = cost_usd(
            route.model, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens
        )

        parsed = None
        if req.schema is not None and text:
            parsed = json.loads(text)

        return LLMResponse(
            text=text,
            usage=usage,
            model=route.model,
            stage=req.stage,
            stop_reason=str(getattr(message, "stop_reason", "end_turn")),
            parsed=parsed,
            raw={"engine": "anthropic-api"},
        )
