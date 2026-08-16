"""Three permission tiers, enforced at the dispatch point.

    read          fetch, search, score, resolve        always allowed
    write-local   draft a CV, write a tracking entry    approval token required
    external      submit_application                    REGISTERED BUT ALWAYS DENIED

`submit_application` is registered on purpose and then denied on purpose. It
exists so the injection test can prove that a model which has been talked into
calling it still does not reach the handler. A tool that was simply absent would
prove nothing — the model would fail with "unknown tool", which is a different
and much weaker claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .hooks import BaseHook, ToolCall


class Tier(StrEnum):
    READ = "read"
    WRITE_LOCAL = "write-local"
    EXTERNAL = "external"


class PolicyDenied(Exception):
    """Raised at the dispatch point. Never caught inside a handler."""

    def __init__(self, tool: str, tier: str, reason: str) -> None:
        super().__init__(f"{tool} ({tier}) denied: {reason}")
        self.tool = tool
        self.tier = tier
        self.reason = reason


@dataclass(frozen=True)
class Policy:
    """The decision function. Pure, so it is trivially testable on its own."""

    approval_token: str | None = None
    allow_external: bool = False  # never set true; kept so the denial is explicit, not implicit

    def check(self, call: ToolCall, ctx: Any = None) -> None:
        tier = Tier(call.tier)

        if tier is Tier.READ:
            return

        if tier is Tier.WRITE_LOCAL:
            token = getattr(ctx, "approval_token", None) or self.approval_token
            if not token:
                raise PolicyDenied(call.name, call.tier, "no approval token for a local write")
            return

        if tier is Tier.EXTERNAL:
            # There is no branch that lets this through. The flag above exists so
            # a reader can see the denial is unconditional rather than an oversight.
            raise PolicyDenied(
                call.name,
                call.tier,
                "external-tier tools are registered and always denied; a human applies",
            )

        raise PolicyDenied(call.name, call.tier, "unknown tier")


@dataclass
class PolicyHook(BaseHook):
    """Policy as a hook, so no agent has to remember to consult it."""

    policy: Policy
    name: str = "policy"

    def before_tool(self, call: ToolCall, ctx: Any) -> None:
        self.policy.check(call, ctx)
