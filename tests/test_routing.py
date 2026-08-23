"""Routing: no stage silently escalates to a more expensive model.

The cost story of this project is "cut deterministically, then step up a tier
only as the candidate set narrows". That story is only true if it is enforced.
An escalation that costs five times as much and produces a slightly nicer answer
is exactly the change nobody notices in review.
"""

from __future__ import annotations

import pytest

from desk.llm.routing import HAIKU, MODELS, OPUS, SONNET, TABLE, RoutingError, cost_usd, resolve

# The intended table, written out independently of the implementation. If a
# stage moves tier, this test fails and the move has to be deliberate.
EXPECTED = {
    "normalize_posting": HAIKU,
    "route_family": HAIKU,
    "dedup_tiebreak": HAIKU,
    "verify_no_fabrication": HAIKU,
    "orchestrator_plan": HAIKU,
    "reflect_anchors": HAIKU,
    "extract_requirements": SONNET,
    "fit_score": SONNET,
    "single_agent_turn": SONNET,
    "tailor_cv": SONNET,
    "freelance_proposal": SONNET,
    "outreach_draft": SONNET,
    "weekly_calibration": OPUS,
    "eval_judge": OPUS,
}


def test_every_stage_is_routed_where_the_plan_says():
    assert {stage: route.model for stage, route in TABLE.items()} == EXPECTED


def test_no_stage_can_escalate_past_its_ceiling():
    for stage, route in TABLE.items():
        for model, spec in MODELS.items():
            if spec.rank > MODELS[route.ceiling].rank:
                with pytest.raises(RoutingError, match="may not escalate"):
                    resolve(stage, model)


def test_a_stage_may_be_moved_down_the_ladder():
    assert resolve("tailor_cv", HAIKU).model == HAIKU


def test_unknown_stage_and_model_are_errors():
    with pytest.raises(RoutingError, match="unknown stage"):
        resolve("make_coffee")
    with pytest.raises(RoutingError, match="unknown model"):
        resolve("fit_score", "gpt-9")


def test_haiku_never_receives_the_effort_parameter():
    """Haiku 4.5 rejects output_config.effort; the route carries it, the client drops it."""
    assert MODELS[HAIKU].supports_effort is False
    for route in TABLE.values():
        if route.model == HAIKU:
            assert route.thinking is False


def test_only_the_offline_judge_and_the_weekly_loop_use_opus():
    opus_stages = sorted(s for s, r in TABLE.items() if r.model == OPUS)
    assert opus_stages == ["eval_judge", "weekly_calibration"]


def test_cost_is_monotonic_in_model_tier():
    for cheap, dear in ((HAIKU, SONNET), (SONNET, OPUS)):
        assert cost_usd(cheap, 1000, 1000) < cost_usd(dear, 1000, 1000)


def test_cached_input_is_cheaper_than_fresh_input():
    assert cost_usd(SONNET, 0, 0, cache_read_tokens=10_000) < cost_usd(SONNET, 10_000, 0)


# --- the subprocess engine is an endpoint, not an agent ----------------------


def _command(monkeypatch, system: str = "") -> list[str]:
    """Capture the argv the client would run, without running anything."""
    import subprocess

    from desk.llm.base import LLMRequest
    from desk.llm.claude_code import ClaudeCodeClient

    captured: dict[str, object] = {}

    class Done:
        returncode = 0
        stdout = '{"result": "{}", "usage": {}}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCodeClient().complete(
        LLMRequest(stage="extract_requirements", system=system, user="a posting"),
        resolve("extract_requirements"),
    )
    return captured  # type: ignore[return-value]


def test_the_agent_system_prompt_is_replaced_and_not_appended(monkeypatch) -> None:
    """`--append-system-prompt` leaves the agent in place. The agent is the bug."""
    cmd = _command(monkeypatch)["cmd"]
    assert "--system-prompt" in cmd
    assert "--append-system-prompt" not in cmd


def test_a_stage_system_prompt_still_reaches_the_model(monkeypatch) -> None:
    cmd = _command(monkeypatch, system="You extract requirements.")["cmd"]
    prompt = cmd[cmd.index("--system-prompt") + 1]
    assert "You extract requirements." in prompt
    assert "not an agent" in prompt


def test_no_settings_file_and_no_claude_md_is_loaded(monkeypatch) -> None:
    """Run bare, it loads the operator's own CLAUDE.md and answers in Hebrew."""
    cmd = _command(monkeypatch)["cmd"]
    assert cmd[cmd.index("--setting-sources") + 1] == ""


def test_no_mcp_server_on_the_machine_is_reachable(monkeypatch) -> None:
    assert "--strict-mcp-config" in _command(monkeypatch)["cmd"]


def test_every_built_in_tool_is_named_as_disallowed(monkeypatch) -> None:
    """A posting is hostile text. This subprocess must hold no tool at all."""
    from desk.llm.claude_code import DISALLOWED_TOOLS

    cmd = _command(monkeypatch)["cmd"]
    flag = cmd.index("--disallowedTools")
    passed = cmd[flag + 1 :]
    for tool in ("Bash", "Read", "Write", "Edit", "WebFetch", "Task"):
        assert tool in passed, f"{tool} was left available to an injected posting"
    assert list(DISALLOWED_TOOLS) == passed


def test_the_subprocess_does_not_run_inside_the_repository(monkeypatch) -> None:
    """Even a tool that got through finds no project file by a relative path."""
    from pathlib import Path

    from desk.config import REPO_ROOT

    cwd = _command(monkeypatch)["cwd"]
    assert cwd is not None
    assert REPO_ROOT != Path(cwd)
    assert not str(cwd).startswith(str(REPO_ROOT))


# --- one transport wrapper is unwrapped, and only one ------------------------


def test_a_response_that_is_entirely_one_fenced_block_is_unwrapped() -> None:
    from desk.llm.claude_code import _unfence

    assert _unfence('```json\n{"verdicts": []}\n```') == '{"verdicts": []}'
    assert _unfence('```\n{"verdicts": []}\n```') == '{"verdicts": []}'


def test_prose_around_a_code_block_is_left_to_fail(monkeypatch) -> None:
    """The no-regex rule exists for exactly this shape. Retry, never salvage."""
    from desk.llm.claude_code import _unfence

    commented = 'Here is the answer:\n```json\n{"verdicts": []}\n```'
    assert _unfence(commented) == commented

    two_blocks = '```\n{"a": 1}\n```\nand\n```\n{"b": 2}\n```'
    assert _unfence(two_blocks) == two_blocks


def test_plain_json_is_returned_byte_for_byte() -> None:
    from desk.llm.claude_code import _unfence

    payload = '{"requirements":[{"text":"SQL"}]}'
    assert _unfence(payload) == payload


def test_a_first_line_carrying_content_is_never_dropped() -> None:
    """`isalpha` on the fence tag, so a real first line survives."""
    from desk.llm.claude_code import _unfence

    assert _unfence('```\n{"a": 1}\n{"b": 2}\n```') == '{"a": 1}\n{"b": 2}'


def test_the_schema_reaches_the_model_verbatim(monkeypatch) -> None:
    """It was validated against a schema it had never been shown, and it guessed."""
    from desk.llm.claude_code import _with_schema

    schema = {"type": "object", "required": ["requirements"]}
    rendered = _with_schema("extract this", schema)
    assert "extract this" in rendered
    assert '"requirements"' in rendered
    assert _with_schema("no schema here", None) == "no schema here"


def test_the_declared_effort_is_actually_sent(monkeypatch) -> None:
    """`desk routes` printed a ceiling the subscription path did not honour."""
    cmd = _command(monkeypatch)["cmd"]
    assert cmd[cmd.index("--effort") + 1] == resolve("extract_requirements").effort


def test_a_model_that_rejects_effort_is_not_sent_one(monkeypatch) -> None:
    """Haiku 4.5 returns 400 for it. One rule, honoured on both paths."""
    import subprocess

    from desk.llm.base import LLMRequest
    from desk.llm.claude_code import ClaudeCodeClient

    captured: dict[str, list[str]] = {}

    class Done:
        returncode = 0
        stdout = '{"result": "{}", "usage": {}}'
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (captured.update(cmd=cmd), Done())[1])
    route = resolve("reflect_anchors")
    assert MODELS[route.ceiling].supports_effort is False
    ClaudeCodeClient().complete(LLMRequest(stage="reflect_anchors", system="", user="x"), route)
    assert "--effort" not in captured["cmd"]
