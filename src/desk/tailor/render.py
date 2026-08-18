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


@dataclass(frozen=True)
class Rendered:
    path: Path
    changed: int
    removed: int
    reordered: int


def folder_name(*, company: str = "", title: str = "", fingerprint: str = "") -> str:
    """Name the folder the way Noam's existing posting folders are named."""
    parts = [p.strip() for p in (company, title) if p and p.strip()]
    name = " - ".join(parts) or (fingerprint[:12] if fingerprint else "posting")
    return UNSAFE.sub(" ", name).strip()[:96]


def output_path(
    contract: Mapping[str, Any],
    *,
    company: str = "",
    title: str = "",
    fingerprint: str = "",
    root: Path | str | None = None,
) -> Path:
    """Where the document goes, per `review.output` in the contract."""
    output = contract.get("review", {}).get("output", {})
    template = str(output.get("dir", "~/Desktop/"))
    filename = str(output.get("filename", "cv.docx"))
    folder = folder_name(company=company, title=title, fingerprint=fingerprint)

    if root is not None:
        directory = Path(root).expanduser() / folder
    else:
        directory = Path(template.replace(FOLDER_PLACEHOLDER, folder)).expanduser()
    return directory / filename


def _set_text(paragraph: Any, before: str, after: str) -> None:
    """Replace a paragraph's text, keeping as much run formatting as possible."""
    runs = list(paragraph.runs)
    if not runs:
        paragraph.text = after
        return
    if paragraph.text != before or before == after:
        # The base moved under the changeset, or there is nothing to localise.
        runs[0].text = after
        for run in runs[1:]:
            run.text = ""
        return

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
        if change.is_reorder or change.removes_line:
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


def write(base: Base, changeset: ChangeSet, path: Path | str) -> Rendered:
    """Apply and save. Called only on `--write`; the default run never gets here."""
    result = apply(base, changeset)
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    base.document.save(str(destination))
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
