from __future__ import annotations

import pytest

from desk.runner import RunSettings, build_context


@pytest.fixture
def ctx(tmp_path):
    context = build_context(RunSettings(root=tmp_path, deterministic=True, budget_usd=None))
    yield context
    context.store.close()


@pytest.fixture
def make_ctx(tmp_path):
    created = []

    def _make(**overrides):
        settings = RunSettings(root=tmp_path, deterministic=True, budget_usd=None)
        for key, value in overrides.items():
            setattr(settings, key, value)
        context = build_context(settings)
        created.append(context)
        return context

    yield _make
    for context in created:
        context.store.close()
