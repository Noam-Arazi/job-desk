"""Writing the document — by editing the base, never by generating a new one.

The naive renderer builds a fresh .docx out of the parsed lines. It is easier
to write and it is wrong, because a CV is not its text. The six bases carry
margins, a font, spacing, a numbering scheme and a right-to-left layout that
Noam arrived at by hand over several rounds, and none of that survives a
regeneration. So the renderer opens the base, edits the paragraphs the approved
changeset names, and saves the same document object under a new name. A
paragraph nobody changed is not rewritten — it is not touched at all.

Inside a paragraph the same principle holds one level down. A skills line is
often a bold category followed by plain items, and replacing the paragraph's
text would flatten that into whichever run happened to be first. So a swap is
localised: the unchanged prefix and suffix are found, and the edit is applied
to the run that actually contains the difference.

Two things about the output path come straight from `review.output` in the
contract, and both are Noam's rulings rather than conveniences. The document
goes into a folder named for the posting, and the *filename* never names the
role — a recruiter who receives a file called "CV for your Data Analyst
opening" learns that the CV was cut for them. And writing happens only under
`--write`; the default run prints the diff and its evidence and touches
nothing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bases import Base
from .changeset import ChangeSet

# Characters a filesystem, a Finder window or a mail client would rather not
# see in a folder name. Hebrew and spaces are deliberately kept: these folders
# sit next to the ones Noam already made by hand.
UNSAFE = re.compile(r"[\\/:*?\"<>|\n\r\t]+")
FOLDER_PLACEHOLDER = "<posting folder>"


class UnsafeOutputPath(ValueError):
    """The document would land outside the folder the contract names."""


class OutputExists(FileExistsError):
    """A document is already there. Noam edits these in Word — see `write`."""


class BaseMismatch(ValueError):
    """The paragraph does not say what the change says it says."""


@dataclass(frozen=True)
class Rendered:
    path: Path
    changed: int
    removed: int
    reordered: int


def folder_name(*, company: str = "", title: str = "", fingerprint: str = "") -> str:
    """Name the folder the way Noam's existing posting folders are named.

    `company` and `title` are scraped from a posting, which makes them
    attacker-controlled text on its way to a filesystem path. Stripping the
    separators is not enough: "." and ".." are legal filenames that name a
    directory rather than a new one, and a posting called "." would have put
    the CV straight into ~/קורות חיים/ next to the bases and the
    experience inventory. A name that is nothing but dots, or that starts with
    one, is therefore not a folder name at all and the fingerprint is used.
    """
    def clean(raw: str) -> str:
        return UNSAFE.sub(" ", raw).strip()[:96].strip()

    parts = [p.strip() for p in (company, title) if p and p.strip()]
    name = clean(" - ".join(parts))
    if not name or name.startswith("."):
        name = clean(fingerprint[:12])
    return name if name and not name.startswith(".") else "posting"


def output_root(contract: Mapping[str, Any], *, root: Path | str | None = None) -> Path:
    """The directory every tailored document must stay inside."""
    if root is not None:
        return Path(root).expanduser()
    template = str(contract.get("review", {}).get("output", {}).get("dir", "~/קורות חיים/"))
    head = template.split(FOLDER_PLACEHOLDER)[0] if FOLDER_PLACEHOLDER in template else template
    return Path(head).expanduser()


def output_path(
    contract: Mapping[str, Any],
    *,
    company: str = "",
    title: str = "",
    fingerprint: str = "",
    root: Path | str | None = None,
) -> Path:
    """Where the document goes, per `review.output` in the contract.

    The result is asserted to sit under the configured root before it is
    returned, so a folder name that escaped `folder_name` would still fail
    here rather than silently write somewhere else.
    """
    output = contract.get("review", {}).get("output", {})
    template = str(output.get("dir", "~/קורות חיים/"))
    filename = str(output.get("filename", "cv.docx"))
    folder = folder_name(company=company, title=title, fingerprint=fingerprint)

    base_dir = output_root(contract, root=root)
    if root is None and FOLDER_PLACEHOLDER not in template:
        directory = base_dir
    else:
        directory = base_dir / folder
    path = directory / filename

    if not path.resolve().is_relative_to(base_dir.resolve()):
        raise UnsafeOutputPath(f"{path} is outside {base_dir}")
    return path


def _set_text(paragraph: Any, before: str, after: str) -> None:
    """Replace a paragraph's text, keeping as much run formatting as possible.

    The two escape hatches this used to have both ended in "collapse the whole
    paragraph into run 0", which is precisely the flattening the localised swap
    exists to avoid: a skills line whose bold category and plain items are two
    runs came back entirely bold. So neither case is written through any more.
    A change that says nothing (`before == after`) is not applied at all, and a
    paragraph that does not say what `before` says it says means the base moved
    under the changeset — the run stops there rather than overwriting a line
    nobody checked.
    """
    if before == after:
        return
    runs = list(paragraph.runs)
    if not runs:
        paragraph.text = after
        return
    if paragraph.text != before:
        raise BaseMismatch(
            f"the paragraph says {paragraph.text!r}, the change says it says {before!r}"
        )

    head = 0
    limit = min(len(before), len(after))
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while (
        tail < limit - head and before[len(before) - 1 - tail] == after[len(after) - 1 - tail]
    ):
        tail += 1
    start, stop = head, len(before) - tail

    offset = 0
    for run in runs:
        length = len(run.text)
        if offset <= start and stop <= offset + length:
            run.text = run.text[: start - offset] + after[start : len(after) - tail] + (
                run.text[stop - offset :]
            )
            return
        offset += length

    runs[0].text = after
    for run in runs[1:]:
        run.text = ""


def apply(base: Base, changeset: ChangeSet) -> Rendered:
    """Apply an already-approved changeset to the base's own document object.

    Every element is resolved up front. Removals and reorders change what
    `document.paragraphs` indexes, so a loop that looked lines up as it went
    would start editing the wrong paragraph halfway through.
    """
    paragraphs = list(base.document.paragraphs)
    element = {line.address: paragraphs[line.index]._p for line in base.lines}
    by_address = {line.address: paragraphs[line.index] for line in base.lines}

    changed = removed = reordered = 0

    for change in changeset.changes:
        # `edits_text` is the projection's own dispatch, imported rather than
        # re-derived. When the two disagreed, a document-level check that read
        # the projection was blind to what this loop wrote.
        if not change.edits_text or change.before == change.after:
            continue
        paragraph = by_address.get(change.section)
        if paragraph is None:
            continue
        _set_text(paragraph, change.before, change.after)
        changed += 1

    for change in changeset.changes:
        if not change.removes_line:
            continue
        el = element.pop(change.section, None)
        if el is not None and el.getparent() is not None:
            el.getparent().remove(el)
            removed += 1

    for change in changeset.changes:
        if not change.is_reorder:
            continue
        order = [a for a in change.order_after() if a in element]
        old = [a for a in change.order_before() if a in element]
        if not order or sorted(order) != sorted(old):
            continue
        elements = [element[a] for a in old]
        parent = elements[0].getparent()
        slots = sorted(parent.index(el) for el in elements)
        for el in elements:
            parent.remove(el)
        for slot, address in zip(slots, order, strict=False):
            parent.insert(slot, element[address])
        reordered += 1

    return Rendered(path=base.path, changed=changed, removed=removed, reordered=reordered)


def write(base: Base, changeset: ChangeSet, path: Path | str, *, force: bool = False) -> Rendered:
    """Apply and save. Called only on `--write`; the default run never gets here.

    Two rules about the destination, both of them consequences of `format:
    docx` in the contract — the whole reason the output is Word is that Noam
    edits it by hand afterwards and then sends it.

    An existing file is never overwritten without `--force`. A second `desk
    tailor --write` on the same posting used to silently destroy an evening of
    his edits, with no prompt and no backup, which is a worse outcome than the
    run failing.

    And the save is atomic: the document is written to a temporary file beside
    the destination and moved onto it with `os.replace`. A save that fails
    halfway then leaves the previous document intact instead of a truncated
    .docx that Word will not open.
    """
    destination = Path(path).expanduser()
    if destination.exists() and not force:
        raise OutputExists(
            f"{destination} already exists; you edit these in Word, so it is not "
            "overwritten. Re-run with --force to replace it."
        )
    result = apply(base, changeset)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        base.document.save(str(staging))
        os.replace(staging, destination)
    finally:
        staging.unlink(missing_ok=True)
    return Rendered(
        path=destination,
        changed=result.changed,
        removed=result.removed,
        reordered=result.reordered,
    )


def diff(base: Base, changeset: ChangeSet, *, width: int = 96) -> list[str]:
    """The dry run's output: every change, before and after, with its evidence.

    This is `review.digest_shows` — changed_lines_diff, evidence_per_change and
    missing_requirements — rendered for a terminal.
    """
    out: list[str] = []
    for change in changeset.changes:
        line = base.line(change.section)
        where = f"{change.section}"
        if line is not None and line.employer:
            where = f"{change.section} ({line.employer})"
        out.append(f"  {change.op}  {where}")
        if change.is_reorder:
            out.append(f"    from  {', '.join(change.order_before())}")
            out.append(f"    to    {', '.join(change.order_after())}")
        else:
            out.append(f"    -  {change.before[:width]}")
            out.append(f"    +  {(change.after or '(removed)')[:width]}")
        evidence = change.source_line[:width] or "(the line itself)"
        out.append(f"    src   {change.source}: {evidence}")
    for gap in changeset.missing_requirements:
        out.append(f"  gap  {gap[:width]}")
    return out
