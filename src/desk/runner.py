"""Assembling a run: which engine, which hooks, which clock.

`build_context` is the only place the three model clients are chosen between, and
the only place the hook stack is assembled. Everything downstream sees one
gateway and one hook bus and cannot tell which engine is underneath.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import load_spec
from .config import paths as default_paths
from .context import RunContext
from .hooks import HookBus, RedactionHook, TraceHook
from .llm.gateway import Gateway
from .llm.replay import RecordingClient, ReplayClient
from .policy import Policy, PolicyHook
from .store import Store
from .trace import FrozenClock, Tracer, WallClock

ENGINES = ("replay", "claude-code", "api")


@dataclass
class RunSettings:
    engine: str = "replay"
    mode: str = "demo"
    deterministic: bool = True
    budget_usd: float | None = 1.00
    approval_token: str | None = "local-run"
    record: bool = False
    root: Path | None = None
    seed: int = 0
    # Normally the run id is derived from the mode, which keeps runs of the same
    # kind from overwriting one another. The single-agent baseline is the one
    # exception: the eval harness looks for it at exactly runs/single-agent/,
    # because a baseline is a fixed point being compared against and not one
    # more run in a series.
    run_id: str | None = None


def _client(settings: RunSettings) -> Any:
    if settings.engine == "replay":
        return ReplayClient()
    if settings.engine == "claude-code":
        from .llm.claude_code import ClaudeCodeClient

        client: Any = ClaudeCodeClient()
    elif settings.engine == "api":
        from .llm.anthropic_api import AnthropicClient

        client = AnthropicClient()
    else:
        raise ValueError(f"unknown engine {settings.engine!r}; expected one of {ENGINES}")

    return RecordingClient(inner=client) if settings.record else client


def build_context(settings: RunSettings | None = None) -> RunContext:
    settings = settings or RunSettings()
    paths = default_paths(settings.root).ensure()

    clock = FrozenClock() if settings.deterministic else WallClock()
    run_id = settings.run_id or (
        f"{settings.mode}-{settings.seed:04d}"
        if settings.deterministic
        else f"{settings.mode}-{WallClock().now().replace(':', '').replace('-', '')}"
    )

    tracer = Tracer(run_id=run_id, path=paths.runs / run_id / "trace.jsonl", clock=clock)

    hooks = HookBus()
    hooks.add(PolicyHook(Policy(approval_token=settings.approval_token)))
    hooks.add(TraceHook(tracer))
    hooks.add(RedactionHook())

    spec = load_spec()
    # A deterministic run gets a fresh in-memory store so replays do not inherit
    # yesterday's rows; a real run persists.
    store = Store(":memory:" if settings.deterministic else paths.db)
    store.start_run(run_id, clock.now(), settings.mode, int(spec["version"]))

    ctx = RunContext(
        run_id=run_id,
        store=store,
        tracer=tracer,
        hooks=hooks,
        paths=paths,
        approval_token=settings.approval_token,
        spec=spec,
        mode=settings.mode,
    )
    ctx.gateway = Gateway(
        client=_client(settings),
        tracer=tracer,
        hooks=hooks,
        budget_usd=settings.budget_usd,
    )
    return ctx


def settings_from_env(**overrides: Any) -> RunSettings:
    base = RunSettings(
        engine=os.environ.get("DESK_ENGINE", "replay"),
        record=os.environ.get("DESK_RECORD", "") == "1",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base
