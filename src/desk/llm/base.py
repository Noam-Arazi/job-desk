"""One interface, three implementations.

    ClaudeCodeClient   subprocess: claude -p        subscription, ~0 marginal cost
    AnthropicClient    messages API                 needs a key, billed per token
    ReplayClient       recorded cassettes           no key, no network, free forever

The daily run goes through the subscription. The API path exists because a repo
that only runs through Claude Code reads as a script. The replay path is what
lets a stranger clone this and run it with nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..trace import Usage
from .routing import Route


@dataclass(frozen=True)
class LLMRequest:
    stage: str
    system: str
    user: str
    schema: dict[str, Any] | None = None
    max_tokens: int = 4096
    prompt_id: str = ""
    prompt_sha256: str = ""

    def cassette_key(self, route: Route) -> str:
        """A content hash over everything that could change the answer.

        The prompt hash is part of it, so a prompt edit invalidates its cassette
        instead of silently replaying an answer the new prompt never produced.
        """
        payload = json.dumps(
            {
                "stage": self.stage,
                "model": route.model,
                "effort": route.effort,
                "system": self.system,
                "user": self.user,
                "schema": self.schema,
                "prompt_id": self.prompt_id,
                "prompt_sha256": self.prompt_sha256,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class LLMResponse:
    text: str
    usage: Usage
    model: str
    stage: str
    stop_reason: str = "end_turn"
    parsed: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    name: str

    def complete(self, req: LLMRequest, route: Route) -> LLMResponse: ...


class BudgetExceeded(Exception):
    """The run's token ceiling was reached. Raised before the call is made."""


class StructuredOutputError(Exception):
    """The model's answer did not validate against the stage's schema."""
