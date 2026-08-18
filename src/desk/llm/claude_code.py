"""The subscription path: shell out to `claude -p`.

This is what the daily run uses. Marginal cost is effectively zero, so the cost
figure the trace records here is the list-price equivalent — what the same work
would have cost on the API — rather than money actually spent. The trace labels
it, so the eval table is never accidentally read as a bill.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from ..trace import Usage
from .base import LLMRequest, LLMResponse
from .routing import Route, cost_usd


class ClaudeCodeError(RuntimeError):
    pass


def _reason(stdout: str) -> str:
    """The CLI's own explanation, when it wrote one as JSON on a failing exit."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return stdout[:400].strip()
    if not isinstance(payload, dict):
        return stdout[:400].strip()
    for key in ("result", "error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value[:400]
    return ""


@dataclass
class ClaudeCodeClient:
    binary: str = "claude"
    timeout_seconds: int = 300  # never unbounded — an untimed job hangs forever
    name: str = "claude-code"

    def complete(self, req: LLMRequest, route: Route) -> LLMResponse:
        cmd = [
            self.binary,
            "-p",
            req.user,
            "--output-format",
            "json",
            "--model",
            route.model,
        ]
        if req.system:
            cmd += ["--append-system-prompt", req.system]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ClaudeCodeError(f"{self.binary} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError(f"{self.binary} timed out after {self.timeout_seconds}s") from exc

        if proc.returncode != 0:
            # The CLI reports why it failed on stdout, as JSON, and leaves stderr
            # empty. Reading only stderr produced "claude exited 1: " — an error
            # that tells the reader nothing at the moment they most need to know
            # whether the run died on their credentials, their quota or a bug.
            raise ClaudeCodeError(
                f"{self.binary} exited {proc.returncode}: "
                f"{_reason(proc.stdout) or proc.stderr[:400] or 'no output'}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeCodeError(f"could not parse CLI output: {proc.stdout[:400]}") from exc

        if payload.get("is_error"):
            raise ClaudeCodeError(str(payload.get("result", "unknown CLI error")))

        raw_usage = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens", 0)),
            output_tokens=int(raw_usage.get("output_tokens", 0)),
            cache_read_tokens=int(raw_usage.get("cache_read_input_tokens", 0)),
        )
        usage.cost_usd = cost_usd(
            route.model, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens
        )

        return LLMResponse(
            text=str(payload.get("result", "")),
            usage=usage,
            model=route.model,
            stage=req.stage,
            raw={"engine": "claude-code", "cost_is_list_price_equivalent": True},
        )
