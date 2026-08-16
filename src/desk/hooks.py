"""The lifecycle hook bus.

This is the load-bearing claim of the whole design: policy, tracing, redaction
and budget caps are hooks. None of them is a condition scattered through agent
code. An agent cannot forget to check the policy, because the agent never checks
it — the dispatch point does, by running the before_tool hooks.

Four hook points:

    before_tool           may raise to veto the call
    after_tool            observes the result
    on_error              observes a failure
    on_budget_exceeded    fired once when the token ceiling is crossed
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .trace import Tracer, Usage


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    tier: str


class Hook(Protocol):
    name: str

    def before_tool(self, call: ToolCall, ctx: Any) -> None: ...
    def after_tool(self, call: ToolCall, result: Any, ctx: Any) -> Any: ...
    def on_error(self, call: ToolCall | None, error: BaseException, ctx: Any) -> None: ...
    def on_budget_exceeded(self, spent: Usage, ceiling: float, ctx: Any) -> None: ...


class BaseHook:
    """No-op defaults so a hook implements only what it cares about."""

    name = "base"

    def before_tool(self, call: ToolCall, ctx: Any) -> None:
        return None

    def after_tool(self, call: ToolCall, result: Any, ctx: Any) -> Any:
        return result

    def on_error(self, call: ToolCall | None, error: BaseException, ctx: Any) -> None:
        return None

    def on_budget_exceeded(self, spent: Usage, ceiling: float, ctx: Any) -> None:
        return None


@dataclass
class HookBus:
    hooks: list[Hook] = field(default_factory=list)

    def add(self, hook: Hook) -> HookBus:
        self.hooks.append(hook)
        return self

    def before_tool(self, call: ToolCall, ctx: Any) -> None:
        for hook in self.hooks:
            hook.before_tool(call, ctx)

    def after_tool(self, call: ToolCall, result: Any, ctx: Any) -> Any:
        for hook in self.hooks:
            result = hook.after_tool(call, result, ctx)
        return result

    def on_error(self, call: ToolCall | None, error: BaseException, ctx: Any) -> None:
        for hook in self.hooks:
            hook.on_error(call, error, ctx)

    def on_budget_exceeded(self, spent: Usage, ceiling: float, ctx: Any) -> None:
        for hook in self.hooks:
            hook.on_budget_exceeded(spent, ceiling, ctx)


# --------------------------------------------------------------------------
# concrete hooks
# --------------------------------------------------------------------------


@dataclass
class TraceHook(BaseHook):
    """Writes a span for every tool call."""

    tracer: Tracer
    name: str = "trace"

    def before_tool(self, call: ToolCall, ctx: Any) -> None:
        self.tracer.emit("tool.start", tool=call.name, tier=call.tier, args=_redact(call.args))

    def after_tool(self, call: ToolCall, result: Any, ctx: Any) -> Any:
        self.tracer.emit("tool.end", tool=call.name, tier=call.tier, ok=True)
        return result

    def on_error(self, call: ToolCall | None, error: BaseException, ctx: Any) -> None:
        self.tracer.emit(
            "error",
            tool=None if call is None else call.name,
            error=f"{type(error).__name__}: {error}",
        )

    def on_budget_exceeded(self, spent: Usage, ceiling: float, ctx: Any) -> None:
        self.tracer.emit("budget.exceeded", spent=spent.as_dict(), ceiling_usd=ceiling)


_SECRET_KEYS = re.compile(r"(token|key|secret|password|cookie|authorization)", re.I)
_SECRET_VALUE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if _SECRET_KEYS.search(k) else _redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("<redacted>", value)
    return value


class RedactionHook(BaseHook):
    """Strips anything that looks like a credential out of tool results."""

    name = "redaction"

    def after_tool(self, call: ToolCall, result: Any, ctx: Any) -> Any:
        return _redact(result)


CallableHook = Callable[[ToolCall, Any], None]
