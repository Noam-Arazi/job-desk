"""Paths and the search specification.

`spec/search.yaml` is the single source of truth for what counts as a relevant
posting. Nothing in this package hard-codes a filtering criterion.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent

SPEC_PATH = REPO_ROOT / "spec" / "search.yaml"
PROMPTS_DIR = REPO_ROOT / "prompts"
CASSETTES_DIR = REPO_ROOT / "cassettes"
SAMPLES_DIR = REPO_ROOT / "samples"


@dataclass(frozen=True)
class Paths:
    """Where a run reads and writes. `data` and `runs` are gitignored."""

    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def runs(self) -> Path:
        return self.root / "runs"

    @property
    def db(self) -> Path:
        return self.data / "desk.sqlite"

    def ensure(self) -> Paths:
        self.data.mkdir(parents=True, exist_ok=True)
        self.runs.mkdir(parents=True, exist_ok=True)
        return self


def paths(root: Path | str | None = None) -> Paths:
    if root is None:
        root = os.environ.get("DESK_HOME", REPO_ROOT)
    return Paths(Path(root))


@lru_cache(maxsize=4)
def load_spec(path: Path | None = None) -> dict[str, Any]:
    """Load and cache the search specification."""
    p = path or SPEC_PATH
    with p.open(encoding="utf-8") as fh:
        spec = yaml.safe_load(fh)
    if not isinstance(spec, dict) or "version" not in spec:
        raise ValueError(f"{p} is not a valid search specification")
    return spec


def families(spec: dict[str, Any] | None = None) -> list[str]:
    return sorted((spec or load_spec()).get("families", {}))


def enabled_sites(spec: dict[str, Any] | None = None) -> list[str]:
    sites = (spec or load_spec()).get("sites", [])
    return [s["id"] for s in sorted(sites, key=lambda s: s["order"]) if s.get("enabled")]


ENV_FILE = REPO_ROOT / ".env"


def load_env(path: Path | None = None) -> list[str]:
    """Read `.env` into the process environment. Returns the names it set.

    The credentials this system needs — the engine choice and the Telegram bot
    token — are read from `os.environ` and nowhere else, which is the right
    rule and left a gap: a token written into `.env` reached no process at all,
    because nothing here ever read the file. An interactive shell can export
    them; the launchd job at 08:00 cannot, and a scheduled run that silently
    lost its channel is exactly the failure `delivery.py` is built to refuse.

    Two rules, both deliberate. A variable already present in the environment
    **wins** — the file is the floor, never an override, so `DESK_ENGINE=replay
    uv run desk ...` still does what it says. And a malformed line is skipped
    rather than raising: this file holds secrets, and an exception carrying the
    offending line is a token in a traceback.
    """
    target = path or ENV_FILE
    if not target.exists():
        return []
    loaded: list[str] = []
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name or name in os.environ:
            continue
        os.environ[name] = value.strip().strip('"').strip("'")
        loaded.append(name)
    return loaded
