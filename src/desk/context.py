"""What a single run carries around."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Paths
from .config import paths as default_paths
from .hooks import HookBus
from .store import Store
from .trace import Tracer


@dataclass
class RunContext:
    run_id: str
    store: Store
    tracer: Tracer
    hooks: HookBus
    gateway: Any = None
    paths: Paths = field(default_factory=default_paths)
    approval_token: str | None = None
    spec: dict[str, Any] = field(default_factory=dict)
    # The change contract, when the command already loaded it. Configuration, not
    # policy: `write_tailored_cv` reads the bases directory and the output
    # template from here rather than parsing the file a second time, and a test
    # can point both at a tmp_path. The tier check is not caller-supplied and
    # never reads this.
    contract: dict[str, Any] | None = None
    mode: str = "demo"

    @property
    def now(self) -> str:
        return self.tracer.clock.now()

    @property
    def run_dir(self) -> Path:
        d = self.paths.runs / self.run_id
        d.mkdir(parents=True, exist_ok=True)
        return d
