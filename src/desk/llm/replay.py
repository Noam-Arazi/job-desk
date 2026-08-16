"""The replay path: recorded cassettes, no key and no network.

This is what makes the repo reproducible by a stranger, and it is what makes the
golden tests possible: the same cassettes and the same seed produce a
byte-identical trace.

A cassette is keyed by a hash over the stage, model, effort, system prompt, user
prompt, schema and prompt-version hash. Editing a prompt therefore misses its
cassette rather than replaying an answer that prompt never produced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import CASSETTES_DIR
from ..trace import Usage
from .base import LLMRequest, LLMResponse
from .routing import Route, cost_usd


class CassetteMiss(KeyError):
    def __init__(self, key: str, stage: str) -> None:
        super().__init__(
            f"no cassette {key} for stage {stage!r}. "
            "Re-record with DESK_ENGINE=claude-code and DESK_RECORD=1."
        )
        self.key = key
        self.stage = stage


@dataclass
class ReplayClient:
    directory: Path = CASSETTES_DIR
    strict: bool = True
    name: str = "replay"

    def _path(self, key: str) -> Path:
        return Path(self.directory) / f"{key}.json"

    def complete(self, req: LLMRequest, route: Route) -> LLMResponse:
        key = req.cassette_key(route)
        path = self._path(key)
        if not path.exists():
            raise CassetteMiss(key, req.stage)

        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_usage = payload.get("usage", {})
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens", 0)),
            output_tokens=int(raw_usage.get("output_tokens", 0)),
            cache_read_tokens=int(raw_usage.get("cache_read_tokens", 0)),
        )
        usage.cost_usd = cost_usd(
            route.model, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens
        )
        text = payload["text"]
        return LLMResponse(
            text=text,
            usage=usage,
            model=route.model,
            stage=req.stage,
            parsed=json.loads(text) if req.schema is not None else None,
            raw={"engine": "replay", "cassette": key},
        )


@dataclass
class RecordingClient:
    """Wraps a live client and writes what it returns to the cassette directory."""

    inner: object
    directory: Path = CASSETTES_DIR
    name: str = "recording"

    def complete(self, req: LLMRequest, route: Route) -> LLMResponse:
        response = self.inner.complete(req, route)  # type: ignore[attr-defined]
        directory = Path(self.directory)
        directory.mkdir(parents=True, exist_ok=True)
        key = req.cassette_key(route)
        (directory / f"{key}.json").write_text(
            json.dumps(
                {
                    "stage": req.stage,
                    "model": route.model,
                    "effort": route.effort,
                    "text": response.text,
                    "usage": response.usage.as_dict(),
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return response
