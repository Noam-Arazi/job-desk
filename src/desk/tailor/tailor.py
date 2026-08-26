"""The run: analysis in, an approved changeset out, and a gap list either way.

The order of the four steps is the whole design, and it is the reverse of the
order that comes naturally.

    read the analysis      what this posting asks for, and where it fell short
    select and load a base an approved CV, re-read and re-hashed from disk
    propose                a model returns changes; it never returns a document
    enforce                the contract, in Python, before anything is written
    verify                 a cheaper model judges what code cannot: does each
                           changed line still trace to the base or the inventory
    write                  only under --write, and only after all of the above

The natural order puts the model last, generating the finished thing, with
checks bolted on afterwards as best-effort. That arrangement can only report
damage. Here the expensive stage produces a proposal, the free stage decides
whether the proposal is allowed, and the document is the last thing that
exists rather than the first.

The two model stages are deliberately different sizes. Proposing is judgement —
which of the posting's terms mean the same as which of Noam's, and which of his
lines is the one to put them in — and it runs on Sonnet with thinking. Checking
that a line traces to a source is a comparison between two short passages, and
it runs on Haiku. The routing table says so; nothing here chooses a model.

One rule from the contract governs everything the model is tempted to do:
`missing_requirement`. A term the posting demands and the inventory covers may
enter an existing line with its source recorded. A term the inventory does not
cover never enters the CV at all — it becomes a gap, travels to the digest, and
Noam decides whether to apply anyway. Inventing across that gap is the single
most likely failure of a CV writer, human or otherwise, and it is the one
failure this file is built to make structurally impossible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import prompts
from ..analyst.types import NONE, Analysis
from ..llm.base import LLMRequest
from .bases import Base, load_for, normalize, tokens
from .changeset import Change, ChangeSet, Projected
from .contract import (
    ContractError,
    Violation,
    allowed_ops,
    forbidden_ids,
    load_contract,
    locate_anchors,
    rule,
)
from .contract import enforce as enforce_contract

TAILOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string"},
                    "section": {"type": "string"},
                    "before": {"type": "string"},
                    "after": {"type": "string"},
                    "source": {"type": "string"},
                    "source_line": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["op", "section", "before", "after", "source", "source_line"],
            },
        },
        "missing_requirements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["changes", "missing_requirements"],
    "additionalProperties": False,
}

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "unsupported": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ok", "unsupported"],
    "additionalProperties": False,
}

# How much of a requirement's vocabulary the document must carry before the
# requirement counts as answered. Only used to decide what to *report*: a gap
# that is wrongly reported costs Noam a glance, and one that is wrongly hidden
# costs him the reason he did not get called.
REQUIREMENT_MET = 0.6


class NoFamily(ValueError):
    """The analyst matched this posting to no CV family. There is nothing to cut."""


class Fabrication(Exception):
    """The verifier found a changed line that traces to neither source."""

    def __init__(self, unsupported: tuple[str, ...]) -> None:
        super().__init__("unsupported claims: " + "; ".join(unsupported))
        self.unsupported = unsupported


@dataclass(frozen=True)
class TailorResult:
    fingerprint: str
    base: Base
    changeset: ChangeSet
    projected: Projected
    gaps: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    verified: bool = False
    path: Path | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.verified and not self.unsupported


def load_analysis(store: Any, fingerprint: str) -> Analysis | None:
    """The stored analysis for one posting, as the frozen contract type.

    The store keeps the analyst's whole verdict as JSON in `payload` rather
    than as columns, so this reads that one field and lets `Analysis` decide
    what it means. Nothing here parses the analyst's shape by hand.
    """
    row = store.get_analysis(fingerprint)
    if row is None:
        return None
    return Analysis.from_json(str(row["payload"]))


def read_inventory(path: Path | str | None) -> str:
    """The experience inventory, or an empty string when it is not on this disk.

    It lives outside the repo on purpose — it carries family and client detail
    and this repo goes public. Its absence is not an error: a clone with no
    inventory can still tailor, it simply has one source instead of two and
    reports more gaps.
    """
    if path is None:
        return ""
    resolved = Path(path).expanduser()
    if not resolved.exists():
        return ""
    return resolved.read_text(encoding="utf-8")


def render_lines(base: Base) -> str:
    """The base as the model sees it: one addressed line per row."""
    rows = []
    for line in base.lines:
        mark = "edit" if line.tailorable else "lock"
        rows.append(f"[{mark}] {line.address} ({line.kind}): {line.text}")
    return "\n".join(rows)


def render_requirements(analysis: Analysis) -> str:
    rows = []
    for req in analysis.requirements:
        must = "must" if req.mandatory else "nice"
        rows.append(f"- ({must}, {req.kind}) {req.text} — posting says: {req.evidence}")
    return "\n".join(rows) or "(none extracted)"


def render_rules(contract: Mapping[str, Any]) -> str:
    """The forbidden rules, in the contract's own words.

    Restating them for the model is not what enforces them — contract.py is.
    It is worth doing anyway: a proposal that never breaks a rule costs one
    model call, and a proposal that does costs a failed run and a retry.
    """
    rows = []
    for rule_id in forbidden_ids(contract):
        entry = rule(contract, rule_id)
        text = " ".join(str(entry.get("rule", "")).split())
        rows.append(f"- {rule_id}: {text}")
        for phrase in entry.get("blocked_phrases", ()):
            rows.append(f"    never write: {phrase}")
    return "\n".join(rows)


def unmet_requirements(analysis: Analysis, projected: Projected, inventory: str) -> tuple[str, ...]:
    """What the posting demanded that the tailored document does not claim.

    Computed here and not taken from the model's word for it, for the same
    reason the gates run before the analyst: a gap the system is supposed to
    surface is exactly the thing a generative stage has an incentive to forget.
    A requirement the inventory covers but the document does not state is still
    reported — it means a change was possible and was not made.
    """
    document = tokens(projected.joined())
    stock = tokens(inventory)
    gaps: list[str] = []
    for req in analysis.requirements:
        wanted = tokens(req.text)
        if not wanted:
            continue
        if len(wanted & document) / len(wanted) >= REQUIREMENT_MET:
            continue
        covered = bool(stock) and len(wanted & stock) / len(wanted) >= REQUIREMENT_MET
        suffix = " (in the inventory, not in the CV)" if covered else " (not in the inventory)"
        gaps.append(req.text + suffix)
    return tuple(gaps)


def propose(
    analysis: Analysis,
    base: Base,
    *,
    ctx: Any,
    contract: Mapping[str, Any],
    inventory: str,
) -> ChangeSet:
    """Stage `tailor_cv`. The model returns changes; it never returns a document."""
    prompt = prompts.load("tailor", "tailor_cv", 1)
    request = LLMRequest(
        stage="tailor_cv",
        system=(
            "You propose changes to an approved CV. You never write a CV, never add a line, "
            "and never state anything the base or the inventory does not already contain."
        ),
        user=prompt.render(
            ops="\n".join(f"  {op}" for op in allowed_ops(contract)),
            rules=render_rules(contract),
            family=base.family,
            language=base.language,
            requirements=render_requirements(analysis),
            gaps="\n".join(f"- {g}" for g in analysis.fit.gaps) or "(none)",
            lines=render_lines(base),
            inventory=inventory or "(not available on this machine)",
        ),
        schema=TAILOR_SCHEMA,
        prompt_id=prompt.id,
        prompt_sha256=prompt.sha256,
    )
    response = ctx.gateway.complete(request, ctx=ctx)
    return ChangeSet.from_dict(response.parsed or {})


# Which rejections may be answered by asking again, and which may not.
#
# The split is the whole safety of this retry. A mechanical violation says the
# model mangled an edit — it dropped a word while adding a term, wrote a
# control character, addressed a line that is not there, ran past a page. The
# checker's message names the defect precisely, so a second ask is a correction
# with something to correct against.
#
# Everything else is left out, and the set is deliberately this small.
#
# The integrity rules — claim_without_evidence, the four Fischer and AI-assist
# rules, drop_or_weaken_anchor — did not catch a slip. They caught a claim the
# sources do not support. Telling a model its claim was refused and inviting
# another attempt is not a correction loop, it is an optimisation loop against
# the guard, and the thing being optimised is whether a false statement reaches
# Noam's CV.
#
# edit_summary, touch_identity_block and introduce_number are left out for a
# quieter reason: none of them is a slip either. The prompt states plainly that
# the summary and the identity block are never touched and that no digit is
# ever introduced, so a changeset that does those things disregarded an
# instruction rather than fumbling one. There is nothing precise to hand back.
#
# A mixed batch counts as integrity: one rule outside this set on one violation
# ends the run.
MECHANICAL = frozenset({"add_new_bullet", "exceed_one_page"})


def mechanical_only(violations: Sequence[Violation]) -> bool:
    """True when every violation is a slip rather than an unsupported claim."""
    return bool(violations) and all(v.rule in MECHANICAL for v in violations)


def render_violations(violations: Sequence[Violation]) -> str:
    return "\n".join(f"- {v}" for v in violations)


def render_changes(changeset: ChangeSet) -> str:
    return "\n".join(
        f"- {c.op} @ {c.section}\n  before: {c.before}\n  after:  {c.after}"
        for c in changeset.changes
    )


def repropose(
    analysis: Analysis,
    base: Base,
    *,
    ctx: Any,
    contract: Mapping[str, Any],
    rejected: ChangeSet,
    violations: Sequence[Violation],
) -> ChangeSet:
    """Stage `repropose_after_contract`. The second and last ask.

    A separate prompt file rather than a sentence appended to the first one, for
    the reason `analyst.extract` already had to learn: the cassette key is a
    hash over the prompt, and an addendum built by string concatenation is a
    prompt with no version and no sha in the trace.

    The posting requirements and the inventory are deliberately not sent again.
    This ask is not a second attempt at the tailoring question — it is a repair
    of a changeset that was already judged to be the right edits. Handing back
    the inventory invites a rewrite, and a rewrite is how the first attempt lost
    two words off a skills line.
    """
    prompt = prompts.load("tailor", "repropose_after_contract", 1)
    request = LLMRequest(
        stage="repropose_after_contract",
        system=(
            "You correct a rejected changeset. You never write a CV, never add a line, "
            "and never answer a violation by inventing wording."
        ),
        user=prompt.render(
            rejected=render_changes(rejected),
            violations=render_violations(violations),
            rules=render_rules(contract),
            lines=render_lines(base),
        ),
        schema=TAILOR_SCHEMA,
        prompt_id=prompt.id,
        prompt_sha256=prompt.sha256,
    )
    response = ctx.gateway.complete(request, ctx=ctx)
    return ChangeSet.from_dict(response.parsed or {})


def verify(
    base: Base,
    changeset: ChangeSet,
    *,
    ctx: Any,
    inventory: str,
) -> tuple[bool, tuple[str, ...]]:
    """Stage `verify_no_fabrication`. The judgement Python cannot make.

    The contract has already decided what may change. This decides whether what
    changed is true — whether each new line still traces to the base or the
    inventory. A changeset with no text edits skips the call: there is nothing
    to fabricate in a reorder, and an unnecessary call is an unnecessary cost.
    """
    edits = tuple(c for c in changeset.changes if not c.is_reorder and c.after.strip())
    if not edits:
        return True, ()

    prompt = prompts.load("tailor", "verify_no_fabrication", 1)
    request = LLMRequest(
        stage="verify_no_fabrication",
        system=(
            "You judge whether a claim traces to its source. You answer about support, "
            "never about style, and you never suggest better wording."
        ),
        user=prompt.render(
            changes="\n".join(f"{c.section}\n  -  {c.before}\n  +  {c.after}" for c in edits),
            base="\n".join(line.text for line in base.lines),
            inventory=inventory or "(not available on this machine)",
        ),
        schema=VERIFY_SCHEMA,
        prompt_id=prompt.id,
        prompt_sha256=prompt.sha256,
    )
    response = ctx.gateway.complete(request, ctx=ctx)
    payload = response.parsed or {}
    unsupported = tuple(str(u) for u in payload.get("unsupported", ()))
    return bool(payload.get("ok")) and not unsupported, unsupported


def tailor(
    analysis: Analysis,
    *,
    ctx: Any,
    bases_dir: Path | str | None = None,
    inventory_path: Path | str | None = None,
    contract: Mapping[str, Any] | None = None,
    language: str | None = None,
    posting_is_english: bool = False,
) -> TailorResult:
    """Cut one CV for one posting. Writes nothing; the caller decides that."""
    rules = contract if contract is not None else load_contract()
    inputs = rules.get("inputs", {})
    directory = bases_dir if bases_dir is not None else inputs.get("bases_dir", "")
    inventory_file = (
        inventory_path if inventory_path is not None else inputs.get("inventory")
    )

    if not analysis.family.matched or analysis.family.family == NONE:
        raise NoFamily(f"{analysis.fingerprint}: the analyst matched no CV family")

    base = load_for(
        analysis.family.family,
        directory=directory,
        language=language,
        posting_is_english=posting_is_english,
    )
    inventory = read_inventory(inventory_file)

    changeset = propose(analysis, base, ctx=ctx, contract=rules, inventory=inventory)
    # One repair, and only for a mechanical rejection. Before this, a changeset
    # that dropped a word while adding a term was thrown away whole: the run
    # printed the violation into a log and Noam, who had pressed the button and
    # was waiting for a document, got nothing and no reason. The defect was a
    # rewrite slip the checker could describe exactly, which is the one kind of
    # failure worth handing back.
    #
    # The retry is not a softer gate. What comes back goes through the same
    # `enforce_contract` with nothing relaxed, and a second rejection raises
    # exactly as the first one used to.
    try:
        projected = enforce_contract(base, changeset, contract=rules)
    except ContractError as rejection:
        if not mechanical_only(rejection.violations):
            raise
        changeset = repropose(
            analysis,
            base,
            ctx=ctx,
            contract=rules,
            rejected=changeset,
            violations=rejection.violations,
        )
        projected = enforce_contract(base, changeset, contract=rules)
    verified, unsupported = verify(base, changeset, ctx=ctx, inventory=inventory)

    gaps = _merge_gaps(
        changeset.missing_requirements,
        analysis.fit.gaps,
        unmet_requirements(analysis, projected, inventory),
    )
    notes: list[str] = []
    if not inventory:
        notes.append("no experience inventory on this machine; the base was the only source")

    # An anchor the locator could not find in the base is not a violation — Noam
    # rewords his own bases by hand, and failing his run over his own edit would
    # be this code overruling the file it serves. But an anchor nobody located
    # is also an anchor nobody is protecting, and that fact was being computed
    # and then dropped. It travels to the human here, because the whole reason
    # anchors exist is that a CV which quietly loses one is worse than a CV that
    # was never tailored.
    unlocated = [a.id for a in locate_anchors(base, rules) if not a.located]
    if unlocated:
        notes.append(
            "anchors not found in this base and therefore not protected on this run: "
            + ", ".join(unlocated)
        )

    result = TailorResult(
        fingerprint=analysis.fingerprint,
        base=base,
        changeset=changeset,
        projected=projected,
        gaps=gaps,
        unsupported=unsupported,
        verified=verified,
        notes=tuple(notes),
    )
    if not verified:
        raise Fabrication(unsupported or ("the verifier refused the changeset",))
    return result


def _merge_gaps(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Dedupe gaps across the three sources without losing the wording of any."""
    seen: dict[str, str] = {}
    for group in groups:
        for gap in group:
            key = normalize(gap).split(" (")[0]
            if key and key not in seen:
                seen[key] = gap
    return tuple(seen.values())


__all__ = [
    "Change",
    "ChangeSet",
    "ContractError",
    "Fabrication",
    "NoFamily",
    "TailorResult",
    "read_inventory",
    "tailor",
    "unmet_requirements",
]
