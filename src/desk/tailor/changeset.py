"""The unit of work is a change, never a rewritten document.

This is the single design decision the whole tailoring agent rests on, and it
is worth stating plainly because the obvious alternative is so much easier to
build: hand the model the base and the posting and ask for a tailored CV back.

That version cannot be checked. Every guarantee in spec/change-contract.yaml is
a statement about the *difference* between the base and the result — the
summary is untouched, no bullet was added, no figure appeared, this anchor
still says what it said — and a returned document does not carry a difference.
It carries a result, and reconstructing the difference from it is guesswork
about which sentence used to be which. So the model returns changes, each one
naming the line it edits, the text before, the text after, and where the new
wording came from. The contract then checks the changes, and only afterwards
does anything touch a file.

The fields on a change are not chosen here. They are `evidence.record_per_change`
in the contract, in that order, and the reason they are exactly those is the
rule directly under them: `unsourced_change: reject`. A change that cannot say
where its wording came from fails the run. It is never quietly discarded, which
would leave a document that looks tailored and a log that says nothing happened.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .bases import Base

# The allowed operations, by their ids in spec/change-contract.yaml.
SWAP_TERMINOLOGY = "swap_terminology"
ADD_TERM_TO_EXISTING_LINE = "add_term_to_existing_line"
REORDER_SKILL_CATEGORIES = "reorder_skill_categories"
REORDER_LINES_WITHIN_SECTION = "reorder_lines_within_section"
DROP_SECONDARY_BULLET = "drop_secondary_bullet"
DROP_SKILL_ITEM = "drop_skill_item"

REORDER_OPS = (REORDER_SKILL_CATEGORIES, REORDER_LINES_WITHIN_SECTION)
DROP_OPS = (DROP_SECONDARY_BULLET, DROP_SKILL_ITEM)
TEXT_OPS = (SWAP_TERMINOLOGY, ADD_TERM_TO_EXISTING_LINE, DROP_SKILL_ITEM)

BASE = "base"
INVENTORY = "inventory"


@dataclass(frozen=True)
class Change:
    """One edit, and everything needed to judge it before it is applied.

    `section` is a line address from bases.py for a text or drop change, and a
    group address ("skills", "exp.1") for a reorder. Reorders carry the old and
    the new ordering of addresses in `before` and `after` as comma-separated
    lists, so one shape covers every operation and the contract has one thing
    to read.
    """

    op: str
    section: str
    before: str
    after: str
    source: str = ""
    source_line: str = ""
    note: str = ""

    @property
    def sourced(self) -> bool:
        return self.source in (BASE, INVENTORY)

    @property
    def is_reorder(self) -> bool:
        return self.op in REORDER_OPS

    @property
    def removes_line(self) -> bool:
        return self.op in DROP_OPS and not self.after.strip()

    def order_before(self) -> tuple[str, ...]:
        return _addresses(self.before)

    def order_after(self) -> tuple[str, ...]:
        return _addresses(self.after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "section": self.section,
            "before": self.before,
            "after": self.after,
            "source": self.source,
            "source_line": self.source_line,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Change:
        return cls(
            op=str(data.get("op", "")),
            section=str(data.get("section", "")),
            before=str(data.get("before", "")),
            after=str(data.get("after", "")),
            source=str(data.get("source", "")),
            source_line=str(data.get("source_line", "")),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class ChangeSet:
    """Everything one posting asks of one base.

    `missing_requirements` is not a leftover. It is the other half of the
    contract's `missing_requirement` rule: a term the posting demands that the
    inventory does not cover never enters the document, so the only place it
    can go is out to the human. A changeset that silently held no record of it
    would turn a reportable gap into nothing at all.
    """

    changes: tuple[Change, ...] = ()
    missing_requirements: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.changes)

    def __iter__(self) -> Any:
        return iter(self.changes)

    def touching(self, address: str) -> tuple[Change, ...]:
        return tuple(c for c in self.changes if c.section == address)

    def as_dict(self) -> dict[str, Any]:
        return {
            "changes": [c.as_dict() for c in self.changes],
            "missing_requirements": list(self.missing_requirements),
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChangeSet:
        return cls(
            changes=tuple(Change.from_dict(c) for c in data.get("changes", ())),
            missing_requirements=tuple(str(m) for m in data.get("missing_requirements", ())),
        )

    @classmethod
    def from_json(cls, text: str) -> ChangeSet:
        return cls.from_dict(json.loads(text))


@dataclass(frozen=True)
class Projected:
    """What the document would say if the changeset were applied.

    Computed in strings, never in Word. The contract has to run *before*
    anything is written, and a check that needed a saved file to run against
    would be a check that runs after the damage.
    """

    addresses: tuple[str, ...] = ()
    text: dict[str, str] = field(default_factory=dict)
    dropped: tuple[str, ...] = ()

    def of(self, address: str) -> str | None:
        return self.text.get(address)

    def texts(self) -> tuple[str, ...]:
        return tuple(self.text[a] for a in self.addresses)

    def joined(self) -> str:
        return "\n".join(self.texts())


def _addresses(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def project(base: Base, changeset: ChangeSet | Sequence[Change]) -> Projected:
    """Apply a changeset to the base's text, in memory and in order.

    Text edits first, then removals, then reorders. The order matters only for
    predictability: a reorder names addresses, and an address that a removal
    already took away should simply not appear, rather than depending on which
    change the model happened to list first.
    """
    changes = tuple(changeset.changes if isinstance(changeset, ChangeSet) else changeset)
    text = {line.address: line.text for line in base.lines}
    order = [line.address for line in base.lines]
    dropped: list[str] = []

    for change in changes:
        if change.op in TEXT_OPS and not change.removes_line:
            if change.section in text:
                text[change.section] = change.after

    for change in changes:
        if change.removes_line and change.section in text:
            dropped.append(change.section)

    survivors = [a for a in order if a not in dropped]
    for change in changes:
        if not change.is_reorder:
            continue
        new = [a for a in change.order_after() if a in text and a not in dropped]
        old = [a for a in change.order_before() if a in text and a not in dropped]
        if sorted(new) != sorted(old) or not new:
            continue  # not a permutation; the contract rejects it and says why
        slots = sorted(survivors.index(a) for a in old if a in survivors)
        for slot, address in zip(slots, new, strict=False):
            survivors[slot] = address

    return Projected(
        addresses=tuple(survivors),
        text={a: text[a] for a in survivors},
        dropped=tuple(dropped),
    )
