"""Contract: every tool's schema matches its handler signature, in both directions.

This is the test that keeps the tool-use pattern honest. A schema that has
drifted from its handler produces a model call that looks valid and fails at
dispatch — the kind of bug that only shows up in production, against a real
posting, at eight in the morning.
"""

from __future__ import annotations

import inspect

import pytest

from desk.policy import Tier
from desk.registry import registry

TOOLS = list(registry)
IDS = [t.name for t in TOOLS]


def test_registry_is_not_empty():
    assert TOOLS, "no tools registered"


@pytest.mark.parametrize("tool", TOOLS, ids=IDS)
def test_schema_is_wellformed(tool):
    schema = tool.input_schema
    assert schema["type"] == "object"
    assert schema.get("additionalProperties") is False, (
        f"{tool.name}: additionalProperties must be False or the model can invent arguments"
    )
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    assert set(required) <= set(properties), (
        f"{tool.name}: required names a property that does not exist"
    )
    for name, prop in properties.items():
        assert prop.get("description"), f"{tool.name}.{name} has no description"


@pytest.mark.parametrize("tool", TOOLS, ids=IDS)
def test_every_schema_property_is_a_handler_parameter(tool):
    """Forward direction: the model cannot be told about an argument that does not exist."""
    handler_params = {p.name for p in tool.parameters()}
    schema_props = set(tool.input_schema.get("properties", {}))
    extra = schema_props - handler_params
    assert not extra, f"{tool.name}: schema declares {sorted(extra)}, handler does not accept them"


@pytest.mark.parametrize("tool", TOOLS, ids=IDS)
def test_every_handler_parameter_is_in_the_schema(tool):
    """Reverse direction: an argument the handler needs must be reachable by the model."""
    schema = tool.input_schema
    schema_props = set(schema.get("properties", {}))
    required = set(schema.get("required", []))

    for param in tool.parameters():
        assert param.name in schema_props, (
            f"{tool.name}: handler takes {param.name!r}, schema never mentions it"
        )
        if param.default is inspect.Parameter.empty:
            assert param.name in required, (
                f"{tool.name}: {param.name!r} has no default, so the schema must require it"
            )


@pytest.mark.parametrize("tool", TOOLS, ids=IDS)
def test_handler_takes_the_run_context(tool):
    assert "ctx" in inspect.signature(tool.handler).parameters, (
        f"{tool.name}: handlers receive the run context as ctx"
    )


def test_api_schemas_are_deterministically_ordered():
    """Tool order feeds the prompt prefix; a reshuffle would silently break caching."""
    assert [s["name"] for s in registry.api_schemas()] == sorted(registry.names())


def test_the_external_tier_exists_and_holds_exactly_one_tool():
    external = [t.name for t in TOOLS if t.tier is Tier.EXTERNAL]
    assert external == ["submit_application"], (
        "the external tier is a single deliberately-denied tool; adding to it needs a decision"
    )
