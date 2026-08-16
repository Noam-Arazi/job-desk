"""Prompts are artifacts: versioned files, hash-pinned, never inline strings."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from desk import prompts
from desk.config import PROMPTS_DIR
from desk.prompts import PromptNotFound

SRC = Path(__file__).resolve().parent.parent / "src" / "desk"


def test_every_prompt_file_loads_and_is_hash_pinned():
    found = prompts.all_prompts()
    assert found, "no prompts on disk"
    for prompt in found:
        assert len(prompt.sha256) == 64
        assert prompt.content.strip()
        assert prompt.id == f"{prompt.agent}/{prompt.name}.v{prompt.version}"


def test_hashes_are_stable_across_loads():
    a = prompts.load("normalizer", "normalize_posting", 1)
    b = prompts.load("normalizer", "normalize_posting", 1)
    assert a.sha256 == b.sha256


def test_a_missing_version_is_an_error_not_a_silent_fallback():
    with pytest.raises(PromptNotFound):
        prompts.load("normalizer", "normalize_posting", 99)


def test_latest_picks_the_highest_version():
    assert prompts.latest("normalizer", "normalize_posting").version == 1


def test_a_missing_placeholder_is_an_error_not_a_blank():
    prompt = prompts.load("normalizer", "normalize_posting", 1)
    with pytest.raises(KeyError, match="needs a value"):
        prompt.render(site="alljobs")


def test_filenames_follow_the_versioning_convention():
    pattern = re.compile(r"^[a-z0-9_]+\.v\d+\.md$")
    for path in PROMPTS_DIR.rglob("*.md"):
        assert pattern.match(path.name), f"{path} does not match <name>.v<N>.md"


def test_no_prompt_text_is_inlined_in_the_package():
    """The registry only means something if nothing bypasses it.

    A heuristic, not a proof: flag any long triple-quoted string in the package
    that reads like a prompt rather than a docstring.
    """
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for block in re.findall(r'"""(.*?)"""', text, flags=re.S):
            lowered = block.lower()
            if len(block) > 200 and ("you are" in lowered or "return only json" in lowered):
                offenders.append(path.name)
    assert not offenders, f"prompt-shaped strings inlined in: {sorted(set(offenders))}"
