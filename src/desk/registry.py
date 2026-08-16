"""The tool registry and the one dispatch point.

Two invariants this file exists to hold:

  schema/handler identity   every registered tool's JSON schema matches its
                            handler signature in both directions. A contract test
                            walks the registry and fails if they drift.
  tool errors are results   a failing tool returns a tool_result carrying the
                            error. It never raises into the agent loop, because a
                            raise is a crash and a tool_result is something the
                            model can recover from. A policy denial is delivered
                            the same way.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hooks import ToolCall
from .policy import PolicyDenied, Tier

Handler = Callable[..., Any]


@dataclass(frozen=True)
class Tool:
    name: str
    tier: Tier
    description: str
    input_schema: dict[str, Any]
    handler: Handler

    def parameters(self) -> list[inspect.Parameter]:
        """Handler parameters, excluding the injected run context."""
        sig = inspect.signature(self.handler)
        return [p for name, p in sig.parameters.items() if name != "ctx"]

    def api_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class ToolResult:
    ok: bool
    tool: str
    content: Any = None
    error: str | None = None
    denied: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "content": self.content,
            "error": self.error,
            "denied": self.denied,
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self, name: str, tier: Tier, description: str, input_schema: dict[str, Any]
    ) -> Callable[[Handler], Handler]:
        def decorate(handler: Handler) -> Handler:
            if name in self._tools:
                raise ValueError(f"tool {name!r} is already registered")
            self._tools[name] = Tool(name, tier, description, input_schema, handler)
            return handler

        return decorate

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool {name!r}") from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    def api_schemas(self) -> list[dict[str, Any]]:
        """Deterministically ordered, so the prompt prefix stays cacheable."""
        return [self._tools[n].api_schema() for n in self.names()]

    def dispatch(self, name: str, args: dict[str, Any], ctx: Any) -> ToolResult:
        """The single place a tool is ever invoked.

        Hooks run here — policy, tracing, redaction — so no agent has to.
        """
        try:
            tool = self.get(name)
        except KeyError as exc:
            return ToolResult(ok=False, tool=name, error=str(exc))

        call = ToolCall(name=name, args=dict(args), tier=tool.tier.value)
        hooks = getattr(ctx, "hooks", None)

        try:
            if hooks is not None:
                hooks.before_tool(call, ctx)
        except PolicyDenied as exc:
            if hooks is not None:
                hooks.on_error(call, exc, ctx)
            return ToolResult(ok=False, tool=name, error=str(exc), denied=True)

        try:
            content = tool.handler(ctx=ctx, **args)
        except Exception as exc:  # noqa: BLE001 - a tool error is a result, not a crash
            if hooks is not None:
                hooks.on_error(call, exc, ctx)
            return ToolResult(ok=False, tool=name, error=f"{type(exc).__name__}: {exc}")

        if hooks is not None:
            content = hooks.after_tool(call, content, ctx)
        return ToolResult(ok=True, tool=name, content=content)


registry = Registry()


# ---------------------------------------------------------------------------
# read tier
# ---------------------------------------------------------------------------


@registry.register(
    "list_new_postings",
    Tier.READ,
    "List distinct roles in the store that have not been applied to, newest first.",
    {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum number of roles to return."}
        },
        "required": ["limit"],
        "additionalProperties": False,
    },
)
def list_new_postings(ctx: Any, limit: int) -> list[dict[str, Any]]:
    return ctx.store.unseen_postings(limit=limit)


@registry.register(
    "get_posting",
    Tier.READ,
    "Fetch one stored posting by its content fingerprint.",
    {
        "type": "object",
        "properties": {
            "fingerprint": {"type": "string", "description": "The posting's content fingerprint."}
        },
        "required": ["fingerprint"],
        "additionalProperties": False,
    },
)
def get_posting(ctx: Any, fingerprint: str) -> dict[str, Any] | None:
    return ctx.store.get_posting(fingerprint)


@registry.register(
    "posting_duplicates",
    Tier.READ,
    "List every stored listing that shares a fingerprint — the same role from several sites.",
    {
        "type": "object",
        "properties": {
            "fingerprint": {"type": "string", "description": "The posting's content fingerprint."}
        },
        "required": ["fingerprint"],
        "additionalProperties": False,
    },
)
def posting_duplicates(ctx: Any, fingerprint: str) -> list[dict[str, Any]]:
    return ctx.store.duplicates_of(fingerprint)


@registry.register(
    "has_applied",
    Tier.READ,
    "Whether this role is already on the applied blocklist and must not resurface.",
    {
        "type": "object",
        "properties": {
            "fingerprint": {"type": "string", "description": "The posting's content fingerprint."}
        },
        "required": ["fingerprint"],
        "additionalProperties": False,
    },
)
def has_applied(ctx: Any, fingerprint: str) -> bool:
    return ctx.store.has_applied(fingerprint)


# ---------------------------------------------------------------------------
# write-local tier — an approval token is required
# ---------------------------------------------------------------------------


@registry.register(
    "record_decision",
    Tier.WRITE_LOCAL,
    "Record what a stage concluded about a role, so the calibration loop has ground truth.",
    {
        "type": "object",
        "properties": {
            "fingerprint": {"type": "string", "description": "The posting's content fingerprint."},
            "stage": {"type": "string", "description": "Which stage reached this conclusion."},
            "verdict": {"type": "string", "description": "pass, block or hold."},
            "score": {"type": "number", "description": "Fit score between 0 and 1."},
            "reason": {"type": "string", "description": "One line explaining the verdict."},
        },
        "required": ["fingerprint", "stage", "verdict"],
        "additionalProperties": False,
    },
)
def record_decision(
    ctx: Any,
    fingerprint: str,
    stage: str,
    verdict: str,
    score: float | None = None,
    reason: str = "",
) -> int:
    return ctx.store.record_decision(
        run_id=ctx.run_id,
        fingerprint=fingerprint,
        stage=stage,
        verdict=verdict,
        score=score,
        reason=reason,
        now=ctx.now,
    )


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


@registry.register(
    "write_tailored_cv",
    Tier.WRITE_LOCAL,
    "Write a tailored CV for one role into this run's directory. Never overwrites a base.",
    {
        "type": "object",
        "properties": {
            "fingerprint": {"type": "string", "description": "The posting's content fingerprint."},
            "family": {"type": "string", "description": "The CV family this role maps to."},
            "language": {"type": "string", "description": "he or en."},
            "content": {"type": "string", "description": "The full tailored document."},
        },
        "required": ["fingerprint", "family", "language", "content"],
        "additionalProperties": False,
    },
)
def write_tailored_cv(
    ctx: Any, fingerprint: str, family: str, language: str, content: str
) -> dict[str, Any]:
    # The filename is built from sanitized parts; a model-supplied string never
    # reaches the filesystem as a path.
    stem = _SAFE_NAME.sub("_", f"{fingerprint}_{family}_{language}")
    target: Path = ctx.run_dir / "cv" / f"{stem}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {
        "path": str(target),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "bytes": len(content.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# external tier — registered so the injection test has something to prove
# ---------------------------------------------------------------------------


@registry.register(
    "submit_application",
    Tier.EXTERNAL,
    "Submit an application to an employer. Registered deliberately and always denied: "
    "a human decides every submission. Present in the registry so the adversarial test "
    "can prove a jailbroken model still cannot reach it.",
    {
        "type": "object",
        "properties": {
            "fingerprint": {"type": "string", "description": "The posting's content fingerprint."},
            "url": {"type": "string", "description": "The employer's application endpoint."},
            "cv_path": {"type": "string", "description": "The document to attach."},
        },
        "required": ["fingerprint", "url", "cv_path"],
        "additionalProperties": False,
    },
)
def submit_application(ctx: Any, fingerprint: str, url: str, cv_path: str) -> dict[str, Any]:
    # If control ever reaches this line, the policy layer has failed. Recording
    # the breach is what turns a silent failure into a red test.
    breach = {"fingerprint": fingerprint, "url": url, "cv_path": cv_path}
    marker = getattr(ctx, "paths", None)
    if marker is not None:
        (marker.data / "POLICY_BREACH.json").parent.mkdir(parents=True, exist_ok=True)
        (marker.data / "POLICY_BREACH.json").write_text(json.dumps(breach), encoding="utf-8")
    raise AssertionError("submit_application executed — the external tier was not enforced")
