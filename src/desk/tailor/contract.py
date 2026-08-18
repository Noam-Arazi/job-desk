"""Every rule in spec/change-contract.yaml, as arithmetic over a changeset.

This is the file the session exists for, and the shape of it is the argument.

The tempting way to enforce a change contract is to put it in the prompt. It
reads well, it takes an afternoon, and it produces a system whose safety
property is "the model was asked nicely". The rules here are not preferences —
a figure that appears in a CV, an adoption claim on work that was never
adopted, a summary that drifts per posting — they are things Noam has decided
must not happen, and a guarantee that depends on a sampled token is not a
guarantee. So the contract is checked in Python, before a byte is written, and
a violation fails the run and names the rule id that caught it.

Two consequences shape the code.

The rules are read out of the YAML, not restated here. Blocked phrases, blocked
escalations, anchors, `chars_per_line` and the forbidden ids themselves are all
loaded from the file, so tightening a rule is an edit to Noam's ruling and not
a code change. Where the YAML is prose rather than data, the check names the
rule it implements in a comment and says what part of it is deterministic.

Every id under `forbidden:` must have a checker. A rule in the file with no
function behind it produces a violation of its own, so adding a rule and
forgetting to implement it fails loudly instead of passing silently — which is
the exact failure mode a contract-driven design is supposed to make impossible.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Any

import yaml

from ..config import REPO_ROOT
from .bases import BULLET, SKILL, SUMMARY, Base, normalize, tokens
from .changeset import Change, ChangeSet, Projected, project

CONTRACT_PATH = REPO_ROOT / "spec" / "change-contract.yaml"

# How much of an anchor's own vocabulary a line must carry to be that anchor.
# Measured across all six approved bases: restricted to bullets, the correct
# line scores between 0.6 and 1.0 against the anchor's `he`/`en` text and the
# runner-up scores 0.4 or less. The summary scores high too, which is why the
# locator only ever looks at bullets.
ANCHOR_MATCH = 0.5

# Escalations are written "built -> deployed" in the YAML.
ARROW = re.compile(r"\s*->\s*")

AI_ASSISTED = re.compile(r"\bai[\s\-_‐-―]*assisted\b")

# Make and Vertex, in both scripts. Both are matched only when a change
# *introduces* them into a Fischer line, so the ordinary English verb "make"
# already sitting in the base is not a violation of anything.
MAKE = re.compile(r"\bmake\b|מייק")
VERTEX = re.compile(r"\bvertex\b|ורטקס")


class ContractError(Exception):
    """The changeset broke the contract. Carries every violation, not the first."""

    def __init__(self, violations: tuple[Violation, ...]) -> None:
        super().__init__("; ".join(f"{v.rule}: {v.detail}" for v in violations))
        self.violations = violations


@dataclass(frozen=True)
class Violation:
    rule: str  # the id under `forbidden:` (or `allowed:`) that caught it
    where: str  # the change address, or the line, this is about
    detail: str

    def __str__(self) -> str:
        return f"{self.rule} @ {self.where}: {self.detail}"


@lru_cache(maxsize=4)
def load_contract(path: Path | str | None = None) -> dict[str, Any]:
    p = Path(path or CONTRACT_PATH)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "forbidden" not in data:
        raise ValueError(f"{p} is not a change contract")
    return data


def allowed_ops(contract: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(rule["id"] for rule in contract.get("allowed", ()))


def forbidden_ids(contract: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(rule["id"] for rule in contract.get("forbidden", ()))


def rule(contract: Mapping[str, Any], rule_id: str) -> dict[str, Any]:
    for entry in contract.get("forbidden", ()):
        if entry.get("id") == rule_id:
            return dict(entry)
    return {}


# --- shared helpers -------------------------------------------------------


def _digits(text: str) -> Counter[str]:
    return Counter(re.findall(r"\d", text))


def _word(needle: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(normalize(needle))}(?!\w)")


def estimate_lines(texts: tuple[str, ...], chars_per_line: int) -> int:
    """The one-page estimator from `length:` in the contract.

    There is no Word and no LibreOffice on this machine, so the contract names
    a character count per line and hands final authority to Noam, in Word. The
    check below is therefore comparative and not absolute: the base is an
    approved one-page CV, so the tailored version staying at or under the
    base's own estimate is the strongest claim this machine can make.
    """
    return sum(max(1, ceil(len(t) / chars_per_line)) for t in texts if t)


@dataclass(frozen=True)
class Anchor:
    """One contract anchor, and the line in *this* base that carries it."""

    id: str
    employer: str
    position: str
    claim: str
    address: str  # "" when the base does not carry it
    score: float = 0.0

    @property
    def located(self) -> bool:
        return bool(self.address)


def locate_anchors(base: Base, contract: Mapping[str, Any]) -> tuple[Anchor, ...]:
    """Find each anchor's line in the base, by vocabulary overlap.

    An anchor that cannot be located is reported as unlocated and not as a
    violation. Noam edits the bases by hand, and a base that no longer phrases
    an anchor the way the contract does is his edit, not this run's mistake —
    failing the run over it would be this code overruling the file it is
    supposed to serve. What is *not* forgiving is an anchor that was located
    and then dropped, moved or weakened; that is a violation below.
    """
    found: list[Anchor] = []
    for entry in contract.get("anchors", ()):
        claim_texts = [entry.get(base.language) or "", entry.get("claim") or ""]
        best_address, best_score = "", 0.0
        for line in base.lines:
            if line.kind != BULLET:
                continue
            for claim in claim_texts:
                wanted = tokens(claim)
                if not wanted:
                    continue
                score = len(wanted & tokens(line.text)) / len(wanted)
                if score > best_score:
                    best_address, best_score = line.address, score
        if best_score < ANCHOR_MATCH:
            best_address = ""
        found.append(
            Anchor(
                id=str(entry.get("id", "")),
                employer=str(entry.get("employer", "")),
                position=str(entry.get("position", "any_bullet")),
                claim=claim_texts[0] or claim_texts[1],
                address=best_address,
                score=round(best_score, 3),
            )
        )
    return tuple(found)


# --- the checks, one per `forbidden` id -----------------------------------
#
# Each takes the same arguments and yields violations. They are registered by
# the rule id they implement, and `check` refuses to run a contract whose ids
# it cannot all account for.


@dataclass(frozen=True)
class Subject:
    base: Base
    changeset: ChangeSet
    projected: Projected
    contract: Mapping[str, Any]
    anchors: tuple[Anchor, ...]


Checker = Callable[[Subject], Iterator[Violation]]
CHECKS: dict[str, Checker] = {}


def check_for(rule_id: str) -> Callable[[Checker], Checker]:
    def register(fn: Checker) -> Checker:
        CHECKS[rule_id] = fn
        return fn

    return register


@check_for("edit_summary")
def _edit_summary(s: Subject) -> Iterator[Violation]:
    """`edit_summary`: the summary is LOCKED and byte-identical everywhere.

    Checked twice on purpose. Once on intent — no change may name a summary
    address — and once on outcome, so a summary that moved for any reason at
    all, including one this file did not anticipate, still fails.
    """
    summary_addresses = {line.address for line in s.base.summary}
    for change in s.changeset.changes:
        if change.section in summary_addresses:
            yield Violation("edit_summary", change.section, "the summary is locked")
        if change.is_reorder and summary_addresses & set(change.order_after()):
            yield Violation("edit_summary", change.section, "a reorder moved the summary")
    for line in s.base.summary:
        after = s.projected.of(line.address)
        if after is None:
            yield Violation("edit_summary", line.address, "the summary was removed")
        elif after != line.text:
            yield Violation("edit_summary", line.address, "the summary is not byte-identical")


@check_for("add_new_bullet")
def _add_new_bullet(s: Subject) -> Iterator[Violation]:
    """`add_new_bullet`: no new bullet, ever. Noam deletes lines, never adds.

    A new line can only enter through an address the base does not have, so
    the check is that every change names a line that already exists. The
    second half enforces `add_term_to_existing_line`'s own `never:
    creates_a_new_line` clause: adding a term may not drop what was there.
    """
    known = {line.address for line in s.base.lines}
    groups = {line.group for line in s.base.lines if line.group}
    for change in s.changeset.changes:
        target_known = change.section in known or (change.is_reorder and change.section in groups)
        if not target_known:
            yield Violation(
                "add_new_bullet", change.section, "names a line that is not in the base"
            )
            continue
        if change.is_reorder or change.removes_line:
            continue
        if not change.before.strip():
            yield Violation("add_new_bullet", change.section, "an empty `before` is a new line")
        if change.op == "add_term_to_existing_line":
            lost = tokens(change.before) - tokens(change.after)
            if lost:
                yield Violation(
                    "add_new_bullet",
                    change.section,
                    f"adding a term dropped {sorted(lost)} from the line",
                )

    base_bullets = Counter(line.group for line in s.base.lines if line.kind == BULLET)
    kept = Counter(
        s.base.line(a).group
        for a in s.projected.addresses
        if s.base.line(a) and s.base.line(a).kind == BULLET
    )
    for group, count in kept.items():
        if count > base_bullets.get(group, 0):
            yield Violation("add_new_bullet", group, "this employer gained a bullet")


@check_for("introduce_number")
def _introduce_number(s: Subject) -> Iterator[Violation]:
    """`introduce_number`: no figures — not counts, not percentages, not sizes.

    Counted as characters rather than as numbers, so "40%" cannot arrive by
    being spelled differently, and a digit that was already in the base (a year
    in an employer header, a version) is not mistaken for a new claim.
    """
    for change in s.changeset.changes:
        if change.is_reorder:
            continue
        gained = _digits(change.after) - _digits(change.before)
        if gained:
            yield Violation(
                "introduce_number",
                change.section,
                f"introduces the digits {''.join(sorted(gained.elements()))}",
            )
    gained_doc = _digits(s.projected.joined()) - _digits(
        "\n".join(line.text for line in s.base.lines)
    )
    if gained_doc:
        yield Violation(
            "introduce_number",
            "document",
            f"the document gained the digits {''.join(sorted(gained_doc.elements()))}",
        )


@check_for("exceed_one_page")
def _exceed_one_page(s: Subject) -> Iterator[Violation]:
    """`exceed_one_page`: the output stays one page.

    Comparative against the base, for the reason given on `estimate_lines`:
    this machine cannot paginate, and the base is the only document known to
    fit. `length.authority: human` still holds — Noam checks it in Word.
    """
    per_line = int(s.contract.get("length", {}).get("chars_per_line", 108))
    before = estimate_lines(tuple(line.text for line in s.base.lines), per_line)
    after = estimate_lines(s.projected.texts(), per_line)
    if after > before:
        yield Violation(
            "exceed_one_page",
            "document",
            f"estimated {after} lines against the base's {before} at {per_line} chars per line",
        )


@check_for("drop_or_weaken_anchor")
def _drop_or_weaken_anchor(s: Subject) -> Iterator[Violation]:
    """`drop_or_weaken_anchor`: every anchor survives, with its claim intact.

    Three of those four words are decidable here. The line must survive, it
    must keep the position the contract gives it, and it must still carry the
    anchor's own vocabulary above the threshold that identified it — a swap
    that leaves the sentence unrecognisable as the claim is a dropped anchor
    whatever else it says. Whether the *meaning* survived is the judgement the
    `verify_no_fabrication` stage makes; nothing in Python decides that.
    """
    for anchor in s.anchors:
        if not anchor.located:
            continue
        after = s.projected.of(anchor.address)
        if after is None:
            yield Violation("drop_or_weaken_anchor", anchor.id, "the anchor line was removed")
            continue
        wanted = tokens(anchor.claim)
        if wanted and len(wanted & tokens(after)) / len(wanted) < ANCHOR_MATCH:
            yield Violation(
                "drop_or_weaken_anchor", anchor.id, "the rewritten line no longer states the claim"
            )
        if anchor.position != "first_bullet":
            continue
        line = s.base.line(anchor.address)
        surviving = [
            a
            for a in s.projected.addresses
            if (candidate := s.base.line(a))
            and candidate.kind == BULLET
            and candidate.group == (line.group if line else "")
        ]
        if surviving and surviving[0] != anchor.address:
            yield Violation(
                "drop_or_weaken_anchor", anchor.id, "the anchor is no longer the first bullet"
            )


@check_for("claim_without_evidence")
def _claim_without_evidence(s: Subject) -> Iterator[Violation]:
    """`claim_without_evidence` with `evidence.unsourced_change: reject`.

    Every change carries a source, not only the two operations the YAML lists
    under `required_for` — a removal that cannot say which base line it removed
    is as unreviewable as an addition that cannot say where its words came
    from. The quoted `source_line` is demanded only where the YAML demands it.
    """
    evidence = s.contract.get("evidence", {})
    sources = tuple(evidence.get("sources", ("base", "inventory")))
    needs_quote = tuple(evidence.get("required_for", ()))
    for change in s.changeset.changes:
        if change.source not in sources:
            yield Violation(
                "claim_without_evidence",
                change.section,
                f"source {change.source!r} is not one of {list(sources)}",
            )
        if change.op in needs_quote and not change.source_line.strip():
            yield Violation(
                "claim_without_evidence", change.section, f"{change.op} quotes no source line"
            )


@check_for("fischer_adoption_claim")
def _fischer_adoption_claim(s: Subject) -> Iterator[Violation]:
    """`fischer_adoption_claim`: no adoption, deployment or production claim.

    The blocked phrases are read from the rule. `allowed_for:
    growth_directorate` is why the scope is Fischer's lines only — there the
    systems really are in daily use.
    """
    phrases = tuple(rule(s.contract, "fischer_adoption_claim").get("blocked_phrases", ()))
    for change in _fischer_changes(s):
        after = normalize(change.after)
        for phrase in phrases:
            if normalize(phrase) in after and normalize(phrase) not in normalize(change.before):
                yield Violation(
                    "fischer_adoption_claim", change.section, f"claims {phrase!r} for Fischer"
                )


@check_for("attribute_make_to_fischer")
def _attribute_make_to_fischer(s: Subject) -> Iterator[Violation]:
    """`attribute_make_to_fischer`: Make belongs to the Growth Directorate.

    Only an *introduction* into a Fischer line counts, which is also how the
    rule's own carve-out is honoured: Make in an unattributed skills line is
    fine, and the ordinary English verb already in the base is not a claim.
    """
    for change in _fischer_changes(s):
        if MAKE.search(normalize(change.after)) and not MAKE.search(normalize(change.before)):
            yield Violation("attribute_make_to_fischer", change.section, "puts Make on Fischer")


@check_for("vertex_on_fischer")
def _vertex_on_fischer(s: Subject) -> Iterator[Violation]:
    """`vertex_on_fischer`: the Fischer stack is Claude Code and Firebase."""
    for change in _fischer_changes(s):
        if VERTEX.search(normalize(change.after)) and not VERTEX.search(normalize(change.before)):
            yield Violation("vertex_on_fischer", change.section, "puts Vertex AI on Fischer")


@check_for("write_ai_assisted")
def _write_ai_assisted(s: Subject) -> Iterator[Violation]:
    """`write_ai_assisted`: never write "AI-assisted".

    The literal and its spacing variants are decidable. "or any equivalent" is
    prose, and catching a paraphrase is the fabrication verifier's job.
    """
    if AI_ASSISTED.search(normalize(s.projected.joined())):
        yield Violation("write_ai_assisted", "document", 'the document says "AI-assisted"')
    for change in s.changeset.changes:
        if AI_ASSISTED.search(normalize(change.after)):
            yield Violation("write_ai_assisted", change.section, 'this change says "AI-assisted"')


@check_for("touch_identity_block")
def _touch_identity_block(s: Subject) -> Iterator[Violation]:
    """`touch_identity_block`: contact, employers, titles, dates, education
    and military service are structural and are never tailored.

    Stated as an allowlist rather than a blocklist: the only line kinds a
    change may name are a skills line and a bullet. Anything the parser did not
    recognise therefore lands on the safe side by default.
    """
    for change in s.changeset.changes:
        if change.is_reorder:
            continue
        line = s.base.line(change.section)
        if line is None:
            continue  # `add_new_bullet` already reported it
        if line.kind not in (SKILL, BULLET):
            kind = "the summary" if line.kind == SUMMARY else f"a {line.kind} line"
            yield Violation("touch_identity_block", change.section, f"{kind} is structural")
    for line in s.base.lines:
        if line.kind in (SKILL, BULLET, SUMMARY):
            continue
        if s.projected.of(line.address) != line.text:
            yield Violation("touch_identity_block", line.address, "a structural line changed")


# --- the two checks that are not `forbidden` ids --------------------------


def _unknown_operations(s: Subject) -> Iterator[Violation]:
    """Enforces the `allowed:` block — an op outside it is not a change at all."""
    permitted = allowed_ops(s.contract)
    for change in s.changeset.changes:
        if change.op not in permitted:
            yield Violation(
                "allowed", change.section, f"{change.op!r} is not one of {list(permitted)}"
            )
        if change.op == "drop_secondary_bullet":
            line = s.base.line(change.section)
            if line is not None and line.kind != BULLET:
                yield Violation("allowed", change.section, "drop_secondary_bullet needs a bullet")
            anchored = {a.address for a in s.anchors if a.located}
            if change.section in anchored:
                yield Violation("allowed", change.section, "an anchor is never a secondary bullet")
        if change.is_reorder:
            before, after = change.order_before(), change.order_after()
            if not after or sorted(before) != sorted(after):
                yield Violation(
                    "allowed", change.section, "a reorder must be a permutation of the same lines"
                )


def _scope_escalation(s: Subject) -> Iterator[Violation]:
    """Enforces `evidence.scope_unchanged`: a swap may not enlarge the claim.

    The pairs come from `blocked_escalations`. This is also the rule that keeps
    the 2026-08-18 anchor ruling coherent — wording inside an anchor may be
    swapped for the posting's phrasing, and a swap that strengthens the claim
    is a claim change and is caught here rather than there.
    """
    raw = s.contract.get("evidence", {}).get("scope_unchanged", {})
    for pair in raw.get("blocked_escalations", ()):
        parts = ARROW.split(str(pair), maxsplit=1)
        if len(parts) != 2:
            continue
        weaker, stronger = parts
        for change in s.changeset.changes:
            if change.is_reorder:
                continue
            before, after = normalize(change.before), normalize(change.after)
            if not (_word(weaker).search(before) and _word(stronger).search(after)):
                continue
            # The weaker term is cut out of `before` before asking whether the
            # stronger one was already there. Several of the pairs nest —
            # "developed" sits inside "co-developed" — and a plain substring
            # test would read the escalation as pre-existing and allow it.
            residue = _word(weaker).sub(" ", before)
            if not _word(stronger).search(residue):
                yield Violation(
                    "scope_unchanged", change.section, f"escalates {weaker!r} to {stronger!r}"
                )


def _fischer_changes(s: Subject) -> tuple[Change, ...]:
    return tuple(
        c
        for c in s.changeset.changes
        if not c.is_reorder and s.base.employer_of(c.section) == "fischer_technologies"
    )


# --- the entry points -----------------------------------------------------


def check(
    base: Base,
    changeset: ChangeSet,
    *,
    contract: Mapping[str, Any] | None = None,
) -> tuple[Violation, ...]:
    """Every violation in the changeset, evaluated before anything is written."""
    rules = contract if contract is not None else load_contract()
    subject = Subject(
        base=base,
        changeset=changeset,
        projected=project(base, changeset),
        contract=rules,
        anchors=locate_anchors(base, rules),
    )

    violations: list[Violation] = []
    for rule_id in forbidden_ids(rules):
        checker = CHECKS.get(rule_id)
        if checker is None:
            # A rule in the file with nothing behind it fails loudly. Silence
            # here would be a contract that looks enforced and is not.
            violations.append(
                Violation(rule_id, "contract", "no deterministic check implements this rule")
            )
            continue
        violations.extend(checker(subject))
    violations.extend(_unknown_operations(subject))
    violations.extend(_scope_escalation(subject))
    return tuple(violations)


def enforce(
    base: Base,
    changeset: ChangeSet,
    *,
    contract: Mapping[str, Any] | None = None,
) -> Projected:
    """Check, and raise if anything failed. Returns what the document will say."""
    violations = check(base, changeset, contract=contract)
    if violations:
        raise ContractError(violations)
    return project(base, changeset)
