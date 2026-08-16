"""Append-only JSONL trace: one span per step, with tokens and cost attributed.

Two properties this file is responsible for:

  observability   every agent step, tool call and model call is a span carrying
                  its own token and cost figures, so cost is attributable rather
                  than aggregate.
  determinism     the same cassettes and the same seed produce a byte-identical
                  trace. That is only possible if nothing in here reads the wall
                  clock or a random source directly — both arrive through the
                  Clock passed in.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol


class Clock(Protocol):
    def now(self) -> str:
        """An ISO-8601 timestamp."""


class WallClock:
    def now(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class FrozenClock:
    """A clock that steps by a fixed amount per read. Used by deterministic runs."""

    start: str = "2026-01-01T00:00:00+00:00"
    step_seconds: int = 1
    _ticks: int = 0

    def now(self) -> str:
        base = datetime.fromisoformat(self.start).timestamp()
        stamp = base + self._ticks * self.step_seconds
        self._ticks += 1
        return datetime.fromtimestamp(stamp, UTC).isoformat(timespec="milliseconds")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            round(self.cost_usd + other.cost_usd, 8),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cost_usd": round(self.cost_usd, 8),
        }


@dataclass
class Tracer:
    """Writes spans to a JSONL file and keeps a running usage total.

    Span ids are a monotonic counter, not a uuid, so two runs over the same
    cassettes produce the same ids.
    """

    run_id: str
    path: Path | None = None
    clock: Clock = field(default_factory=WallClock)
    events: list[dict[str, Any]] = field(default_factory=list)
    total: Usage = field(default_factory=Usage)
    _seq: int = 0

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def emit(self, kind: str, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": self.clock.now(),
            "run_id": self.run_id,
            "kind": kind,
            **fields,
        }
        self.events.append(event)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event

    def record_usage(self, usage: Usage) -> None:
        self.total = self.total + usage

    def span(self, kind: str, name: str, **fields: Any):
        return _Span(self, kind, name, fields)

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == kind]

    def render(self) -> str:
        return "".join(
            json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in self.events
        )


class _Span:
    """Context manager emitting a start and an end event, with cost attribution."""

    def __init__(self, tracer: Tracer, kind: str, name: str, fields: dict[str, Any]) -> None:
        self.tracer = tracer
        self.kind = kind
        self.name = name
        self.fields = fields
        self.usage = Usage()
        self.error: str | None = None
        self._start_id: int | None = None
        self._t0 = 0.0

    def __enter__(self) -> _Span:
        self._t0 = time.perf_counter()
        event = self.tracer.emit(f"{self.kind}.start", name=self.name, **self.fields)
        self._start_id = event["seq"]
        return self

    def attribute(self, usage: Usage) -> None:
        self.usage = self.usage + usage
        self.tracer.record_usage(usage)

    def fail(self, error: BaseException | str) -> None:
        """Mark the span failed when the caller handles the exception itself.

        The orchestrator catches a step's exception in order to isolate it, so
        the span would otherwise close as ok — a failed step recorded as a
        success is worse than no trace at all.
        """
        self.error = error if isinstance(error, str) else f"{type(error).__name__}: {error}"

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Elapsed time is measured but deliberately not written to the trace:
        # it is the one field that would break byte-identical replay.
        _ = time.perf_counter() - self._t0
        if exc is not None:
            self.error = f"{type(exc).__name__}: {exc}"
        self.tracer.emit(
            f"{self.kind}.end",
            name=self.name,
            start_seq=self._start_id,
            ok=self.error is None,
            error=self.error,
            usage=self.usage.as_dict(),
            **self.fields,
        )
        return False
