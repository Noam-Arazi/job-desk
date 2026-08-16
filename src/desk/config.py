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
