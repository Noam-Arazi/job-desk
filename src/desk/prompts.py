"""Prompts are versioned files on disk, never inline strings.

    prompts/<agent>/<name>.v<N>.md

Every prompt is loaded by id and version, and its sha256 is written into the run
trace. When a result changes, the trace says which prompt version produced it.
That is the whole point: without it, "the output got worse" is unattributable.

The loader lives in the package while the prompt files live at the repo root, so
a prompt can be edited without touching Python.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import PROMPTS_DIR

_FILENAME = re.compile(r"^(?P<name>[a-z0-9_]+)\.v(?P<version>\d+)\.md$")


class PromptNotFound(FileNotFoundError):
    pass


@dataclass(frozen=True)
class Prompt:
    agent: str
    name: str
    version: int
    path: Path
    content: str
    sha256: str

    @property
    def id(self) -> str:
        return f"{self.agent}/{self.name}.v{self.version}"

    def render(self, **fields: object) -> str:
        """Substitute {placeholders}. Missing keys are an error, not a blank."""
        try:
            return self.content.format(**fields)
        except KeyError as exc:
            raise KeyError(f"{self.id} needs a value for {exc}") from exc


@lru_cache(maxsize=64)
def load(agent: str, name: str, version: int, directory: Path | None = None) -> Prompt:
    base = Path(directory or PROMPTS_DIR)
    path = base / agent / f"{name}.v{version}.md"
    if not path.exists():
        raise PromptNotFound(f"no prompt at {path}")
    content = path.read_text(encoding="utf-8")
    return Prompt(
        agent=agent,
        name=name,
        version=version,
        path=path,
        content=content,
        sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
    )


def latest(agent: str, name: str, directory: Path | None = None) -> Prompt:
    base = Path(directory or PROMPTS_DIR)
    versions = [
        int(m.group("version"))
        for f in (base / agent).glob(f"{name}.v*.md")
        if (m := _FILENAME.match(f.name)) and m.group("name") == name
    ]
    if not versions:
        raise PromptNotFound(f"no versions of {agent}/{name} under {base}")
    return load(agent, name, max(versions), directory)


def all_prompts(directory: Path | None = None) -> list[Prompt]:
    base = Path(directory or PROMPTS_DIR)
    found: list[Prompt] = []
    for path in sorted(base.rglob("*.md")):
        match = _FILENAME.match(path.name)
        if not match:
            continue
        found.append(load(path.parent.name, match.group("name"), int(match.group("version")), base))
    return found
