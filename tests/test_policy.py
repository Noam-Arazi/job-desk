from __future__ import annotations

import pytest

from desk.hooks import ToolCall
from desk.policy import Policy, PolicyDenied, Tier
from desk.registry import registry


def call(name: str, tier: Tier) -> ToolCall:
    return ToolCall(name=name, args={}, tier=tier.value)


def test_read_is_always_allowed():
    Policy(approval_token=None).check(call("get_posting", Tier.READ), None)


def test_write_local_needs_an_approval_token():
    with pytest.raises(PolicyDenied, match="approval token"):
        Policy(approval_token=None).check(call("record_decision", Tier.WRITE_LOCAL), None)


def test_write_local_passes_with_a_token():
    Policy(approval_token="local-run").check(call("record_decision", Tier.WRITE_LOCAL), None)


def test_dispatch_denies_a_local_write_without_a_token(ctx):
    ctx.approval_token = None
    ctx.hooks.hooks[0].policy = Policy(approval_token=None)
    result = registry.dispatch(
        "record_decision",
        {"fingerprint": "abc", "stage": "test", "verdict": "pass"},
        ctx,
    )
    assert result.denied is True
    assert ctx.store.decisions_for("abc") == []


def test_a_denied_call_is_a_result_and_not_a_raise(ctx):
    """Tool errors come back as tool_result so the loop can recover."""
    result = registry.dispatch(
        "submit_application", {"fingerprint": "a", "url": "u", "cv_path": "p"}, ctx
    )
    assert result.ok is False
    assert isinstance(result.as_dict(), dict)


def test_an_unknown_tool_is_a_result_and_not_a_raise(ctx):
    result = registry.dispatch("delete_everything", {}, ctx)
    assert result.ok is False
    assert "unknown tool" in result.error


def test_a_handler_exception_is_a_result_and_not_a_raise(ctx):
    """get_posting on a malformed fingerprint must not take the run down."""
    result = registry.dispatch("get_posting", {"fingerprint": None}, ctx)
    assert result.ok is True or result.ok is False  # either way, no exception escaped
