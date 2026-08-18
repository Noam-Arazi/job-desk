"""The gateway: structured output is validated, never regexed, and retried once."""

from __future__ import annotations

import json

import pytest

from desk.hooks import HookBus
from desk.llm.base import LLMRequest, LLMResponse, StructuredOutputError
from desk.llm.gateway import Gateway
from desk.llm.routing import Route
from desk.trace import Tracer, Usage

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "score": {"type": "number"}},
    "required": ["verdict"],
    "additionalProperties": False,
}


class ScriptedClient:
    """Returns a canned answer per call, so schema handling can be pinned down."""

    name = "scripted"

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[LLMRequest] = []

    def complete(self, req: LLMRequest, route: Route) -> LLMResponse:
        self.calls.append(req)
        text = self.texts.pop(0)
        return LLMResponse(
            text=text,
            usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.0001),
            model=route.model,
            stage=req.stage,
        )


def gateway(client, **kwargs) -> Gateway:
    return Gateway(client=client, tracer=Tracer(run_id="t"), hooks=HookBus(), **kwargs)


def request() -> LLMRequest:
    return LLMRequest(stage="fit_score", system="s", user="u", schema=SCHEMA)


def test_a_valid_answer_is_parsed_into_an_object():
    client = ScriptedClient(json.dumps({"verdict": "pass", "score": 0.7}))
    response = gateway(client).complete(request())
    assert response.parsed == {"verdict": "pass", "score": 0.7}
    assert len(client.calls) == 1


def test_a_malformed_answer_is_retried_once_with_the_mismatch_named():
    client = ScriptedClient("not json at all", json.dumps({"verdict": "pass"}))
    response = gateway(client).complete(request())
    assert response.parsed == {"verdict": "pass"}
    assert len(client.calls) == 2
    assert "did not match the required schema" in client.calls[1].user


def test_a_missing_required_key_is_a_mismatch():
    client = ScriptedClient(json.dumps({"score": 1}), json.dumps({"verdict": "pass"}))
    assert gateway(client).complete(request()).parsed == {"verdict": "pass"}


def test_an_invented_key_is_a_mismatch():
    """additionalProperties is False, so the model cannot smuggle a field through."""
    client = ScriptedClient(
        json.dumps({"verdict": "pass", "shell_command": "rm -rf /"}),
        json.dumps({"verdict": "pass"}),
    )
    assert gateway(client).complete(request()).parsed == {"verdict": "pass"}


def test_it_gives_up_rather_than_looping_forever():
    client = ScriptedClient("nope", "still nope")
    with pytest.raises(StructuredOutputError, match="after 2 attempts"):
        gateway(client).complete(request())


def test_a_stage_without_a_schema_returns_raw_text():
    client = ScriptedClient("just prose")
    response = gateway(client).complete(LLMRequest(stage="outreach_draft", system="s", user="u"))
    assert response.text == "just prose"
    assert response.parsed is None


def test_every_call_lands_in_the_trace_with_its_own_cost():
    client = ScriptedClient(json.dumps({"verdict": "pass"}))
    gw = gateway(client)
    gw.complete(request())
    spans = [e for e in gw.tracer.events if e["kind"] == "model.end"]
    assert len(spans) == 1
    assert spans[0]["model"] == "claude-sonnet-5"
    assert spans[0]["usage"]["cost_usd"] == 0.0001


def test_a_failing_cli_reports_its_own_reason_and_not_an_empty_string() -> None:
    """The CLI writes why it failed to stdout as JSON and leaves stderr empty.

    Reading only stderr produced "claude exited 1: ", which is the least useful
    possible message at the moment the reader most needs to know whether the run
    died on credentials, on quota, or on a bug.
    """
    from desk.llm.claude_code import _reason

    payload = '{"is_error":true,"result":"Failed to authenticate: OAuth session expired"}'
    assert _reason(payload) == "Failed to authenticate: OAuth session expired"
    assert _reason("plain text failure") == "plain text failure"
    assert _reason("{}") == ""
