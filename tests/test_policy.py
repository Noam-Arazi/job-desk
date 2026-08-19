from __future__ import annotations

from types import SimpleNamespace

import pytest

from desk.hooks import HookBus, ToolCall
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


# ---------------------------------------------------------------------------
# The guarantee is not "the hooks deny it". It is "dispatch denies it".
#
# A reviewer broke the original in three ways, none of which needed a cleverer
# jailbreak: a context with no `hooks` attribute, a context with `hooks=None`,
# and a HookBus somebody assembled without a PolicyHook. In all three the
# external handler ran. The check was real but it lived in the caller's hook
# stack, and a caller can be built without one.
# ---------------------------------------------------------------------------

EXTERNAL_ARGS = {"fingerprint": "abc", "url": "https://employer.example", "cv_path": "/tmp/cv.docx"}


@pytest.mark.parametrize(
    "ctx",
    [
        SimpleNamespace(),
        SimpleNamespace(hooks=None),
        SimpleNamespace(hooks=HookBus()),
    ],
    ids=["no hooks attribute", "hooks is None", "a hook bus with no policy hook"],
)
def test_the_external_tier_is_denied_whatever_the_caller_installed(ctx) -> None:
    result = registry.dispatch("submit_application", dict(EXTERNAL_ARGS), ctx)
    assert result.denied is True
    assert result.ok is False


def test_a_breach_is_raised_and_not_filed_as_an_ordinary_tool_error() -> None:
    """The one event that must never be quiet must not look like a timeout.

    `dispatch` converts a handler exception into a failed ToolResult, which is
    right for a network error and wrong for this. If the boundary ever fails,
    the breach has to leave the dispatch point as an exception.
    """
    from desk.policy import PolicyBreach
    from desk.registry import Registry, Tool

    escaped = Registry()
    escaped.policy = SimpleNamespace(check=lambda call, ctx: None)  # a policy that allows anything
    escaped._tools = dict(registry._tools)

    with pytest.raises(PolicyBreach):
        escaped.dispatch("submit_application", dict(EXTERNAL_ARGS), SimpleNamespace())

    assert issubclass(PolicyBreach, AssertionError)
    assert Tool is not None
