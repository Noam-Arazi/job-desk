"""The tailoring agent — the reflection pattern, with the critic written in code.

Session 6. One posting, one approved base, and a list of changes that has to
survive a deterministic contract before a document exists at all.

    bases.py      the six approved CVs, re-read and re-hashed on every run
    changeset.py  the unit of work: a change, with the source behind it
    contract.py   spec/change-contract.yaml, as arithmetic — the file that matters
    tailor.py     the run: propose, enforce, verify, and only then write
    render.py     edits the base's own document so its formatting survives
    command.py    `desk tailor`, a dry run unless --write

The generator is a model and the evaluator is Python. That asymmetry is the
point: what may change in a CV is a decision Noam already made, and a decision
already made does not need to be re-derived by a model every morning.
"""

from __future__ import annotations

from .bases import Base, BaseNotFound, Line, load, load_for, select
from .changeset import Change, ChangeSet, project
from .contract import ContractError, Violation, check, enforce, load_contract
from .tailor import Fabrication, NoFamily, TailorResult

# `tailor` the function is deliberately not re-exported here. It would shadow
# `desk.tailor.tailor`, the module it lives in, and a caller reaching for one
# and getting the other is a confusing failure for no gain — the same reason
# the resolver's entry point sits in `resolve/resolver.py`.

__all__ = [
    "Base",
    "BaseNotFound",
    "Change",
    "ChangeSet",
    "ContractError",
    "Fabrication",
    "Line",
    "NoFamily",
    "TailorResult",
    "Violation",
    "check",
    "enforce",
    "load",
    "load_contract",
    "load_for",
    "project",
    "select",
    "tailor",
]
