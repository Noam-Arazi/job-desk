"""The approved bases, read off disk and turned into something addressable.

Two decisions are load-bearing here, and both come out of session 2.

The first is that a base is re-read and re-hashed on every single run. Noam
edits the six bases by hand, in Word, between rounds. A cached copy would make
his edit look like a bug the next time the two disagreed, and the safe reading
of a disagreement is always that the file on disk is right. Nothing in this
package holds a base across runs; `load` opens the file every time it is asked.

The second is that a base is parsed into *addressed lines* rather than into a
blob of text. Every rule in spec/change-contract.yaml is stated in terms of a
place — the summary, a skills line, a bullet under one employer, the identity
block — so a tailoring agent that could only see a document would have no way
to obey them and no way to be checked. The addresses below are what the model
proposes changes against, what the contract checks, and what the renderer
applies. They are stable for one run only, because they are derived from the
file that run read.

Addresses look like this, and are deliberately language-free so the same code
reads a Hebrew base and an English one:

    identity.0        the name
    identity.1        the contact line
    summary.0         the locked paragraph
    skills.2          the third skills line
    exp.1.header      the second employer's header
    exp.1.bullet.0    its first bullet — where the anchors live
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# What a line is, for the purposes of the contract. Only SKILL and BULLET are
# ever tailorable; everything else is either locked outright (SUMMARY) or
# structural (the rest), which is what `touch_identity_block` means in practice.
IDENTITY = "identity"
HEADING = "heading"
SUMMARY = "summary"
SKILL = "skill"
EMPLOYER = "employer"
SUBTITLE = "subtitle"
BULLET = "bullet"
STRUCTURAL = "structural"

TAILORABLE = (SKILL, BULLET)

# Word's own list styles. The real bases carry paragraph-level numbering, but a
# document built from python-docx's stock styles carries it on the style
# instead, so both are treated as a bullet.
LIST_STYLES = ("list paragraph", "list bullet", "list number")

# A heading is short, bold, unnumbered, and carries neither a date nor the
# employer separator. Employer headers are also bold, which is why the negative
# half of this test matters more than the positive half.
HEADING_MAX_CHARS = 40

# Section kinds, keyed off the heading's own words in both languages. The first
# section after the identity block is treated as the summary whatever it is
# called, so a renamed heading cannot quietly unlock the one paragraph that is
# supposed to be frozen.
SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "profile", "about", "תקציר", "פרופיל"),
    "skills": ("skills", "technical", "technologies", "כישורים", "מיומנויות", "טכנולוגיות"),
    "experience": ("experience", "employment", "ניסיון", "תעסוקתי", "תעסוקה"),
    "projects": ("projects", "selected", "מיזמים", "פרויקטים"),
    "education": ("education", "השכלה", "לימודים"),
    "languages": ("languages", "שפות"),
    "military": ("military", "service", "צבאי", "שירות"),
}
EMPLOYER_SECTIONS = ("experience", "projects")

# The contract names employers by key; the bases name them in prose, in two
# languages. This is the only place the two vocabularies meet.
EMPLOYER_KEYS: dict[str, tuple[str, ...]] = {
    "growth_directorate": ("growth directorate", "growth", "מנהלת הצמיחה", "הצמיחה"),
    "fischer_technologies": ("fischer", "פישר"),
}

# How a base file announces which family and language it is. Discovered from
# the filenames Noam already uses rather than configured, so adding a seventh
# base is a matter of naming it like its siblings.
FAMILY_MARKERS: dict[str, tuple[str, ...]] = {
    "ai_builder": ("ai_builder", "ai builder"),
    "data_analyst": ("data_analyst", "data analyst", "דאטה"),
    "product_project": ("product_project", "product project", "פרודקט"),
    "strategy_public": ("strategy", "אסטרטגיה"),
}

HEBREW = re.compile(r"[֐-׿]")
WORD = re.compile(r"[\w֐-׿']+")


class BaseNotFound(FileNotFoundError):
    """No approved base exists for this family and language."""


def normalize(text: str) -> str:
    """Casefold, strip accents and collapse whitespace — for matching only."""
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def tokens(text: str) -> set[str]:
    return {t for t in WORD.findall(normalize(text)) if len(t) > 1}


@dataclass(frozen=True)
class Line:
    """One paragraph of a base, with everywhere it sits recorded on it."""

    index: int  # paragraph index in the document — the key the renderer uses
    address: str
    kind: str
    text: str
    section: str = ""  # the section kind, not its heading text
    heading: str = ""
    employer: str = ""  # the contract's employer key, "" when unmatched
    group: str = ""  # the reorderable group this line belongs to
    position: int = 0  # ordinal inside that group

    @property
    def tailorable(self) -> bool:
        return self.kind in TAILORABLE


@dataclass(frozen=True)
class Employer:
    """One employer inside an experience section, and its ordered bullets."""

    key: str  # the contract's key, "" when the header matched none
    header: str
    group: str
    bullets: tuple[Line, ...]


@dataclass
class Base:
    """One approved CV, as read from disk on this run.

    `document` is the live python-docx object. The renderer edits *this* object
    and saves it, so that every piece of formatting the file already carries
    survives untouched. Generating a fresh document from the parsed lines would
    be simpler and would throw away the layout Noam settled on by hand.
    """

    family: str
    language: str
    path: Path
    sha256: str
    document: Any
    lines: tuple[Line, ...]
    employers: tuple[Employer, ...]

    def line(self, address: str) -> Line | None:
        for line in self.lines:
            if line.address == address:
                return line
        return None

    def of_kind(self, kind: str) -> tuple[Line, ...]:
        return tuple(line for line in self.lines if line.kind == kind)

    def group(self, group: str) -> tuple[Line, ...]:
        return tuple(line for line in self.lines if line.group == group)

    @property
    def summary(self) -> tuple[Line, ...]:
        return self.of_kind(SUMMARY)

    @property
    def summary_text(self) -> str:
        return "\n".join(line.text for line in self.summary)

    @property
    def skills(self) -> tuple[Line, ...]:
        return self.of_kind(SKILL)

    def employer_of(self, address: str) -> str:
        line = self.line(address)
        return line.employer if line else ""


# --- selection ------------------------------------------------------------


@dataclass(frozen=True)
class BaseFile:
    family: str
    language: str
    path: Path


def catalog(directory: Path | str) -> tuple[BaseFile, ...]:
    """Every approved base on disk, in a stable order.

    The "six bases, not eight" ruling is not encoded here. It is simply what
    the directory contains: ai_builder exists only in English and
    strategy_public only in Hebrew, and `select` reads that off the shelf
    rather than off a list that could drift from it.
    """
    found: list[BaseFile] = []
    for path in sorted(Path(directory).expanduser().glob("*.docx")):
        if path.name.startswith("~$"):  # a Word lock file, not a base
            continue
        name = normalize(path.stem)
        language = "he" if HEBREW.search(path.stem) else "en"
        for family, markers in FAMILY_MARKERS.items():
            if any(normalize(m) in name for m in markers):
                found.append(BaseFile(family=family, language=language, path=path))
                break
    return tuple(found)


def choose_language(
    available: tuple[str, ...],
    *,
    requested: str | None = None,
    posting_is_english: bool = False,
    international_product_company: bool = False,
    default: str = "he",
) -> str:
    """Apply inputs.base_selection.language from the change contract.

    A family that exists in one language only settles the question before the
    rule is consulted, which is exactly what the six-not-eight ruling means at
    runtime: asking for an English strategy CV is not a failure, it is a
    request that has one answer.
    """
    if len(available) == 1:
        return available[0]
    if requested and requested in available:
        return requested
    wants_english = posting_is_english or international_product_company
    preferred = "en" if wants_english else default
    return preferred if preferred in available else available[0]


def select(
    family: str,
    *,
    directory: Path | str,
    language: str | None = None,
    posting_is_english: bool = False,
    international_product_company: bool = False,
    default_language: str = "he",
) -> BaseFile:
    files = [f for f in catalog(directory) if f.family == family]
    if not files:
        raise BaseNotFound(f"no approved base for family {family!r} under {directory}")
    available = tuple(sorted({f.language for f in files}))
    chosen = choose_language(
        available,
        requested=language,
        posting_is_english=posting_is_english,
        international_product_company=international_product_company,
        default=default_language,
    )
    for candidate in files:
        if candidate.language == chosen:
            return candidate
    raise BaseNotFound(f"no {chosen!r} base for family {family!r}")


# --- parsing --------------------------------------------------------------


def _is_numbered(paragraph: Any) -> bool:
    return paragraph._p.find(f".//{W_NS}numPr") is not None


def _is_bold(paragraph: Any) -> bool:
    return any(run.bold for run in paragraph.runs)


def _style_name(paragraph: Any) -> str:
    try:
        return normalize(paragraph.style.name or "")
    except AttributeError:  # a style the document never defined
        return ""


def _is_list(paragraph: Any) -> bool:
    return _is_numbered(paragraph) or _style_name(paragraph) in LIST_STYLES


def _is_heading(paragraph: Any) -> bool:
    text = paragraph.text.strip()
    if not text or len(text) > HEADING_MAX_CHARS:
        return False
    if _is_list(paragraph) or not _is_bold(paragraph):
        return False
    # Everything else in a CV that is bold and short is bold and short for a
    # reason a heading never has. An employer header carries a date and the
    # " | " separator; a skills line carries a category and a colon. Without
    # the colon test a short bold skills line — "Data: SQL, Python" — reads as
    # the start of a new section and takes the rest of the section with it.
    return "|" not in text and ":" not in text and not re.search(r"\d", text)


def _section_kind(heading: str, *, first: bool) -> str:
    if first:
        return "summary"
    folded = normalize(heading)
    for kind, words in SECTION_KEYWORDS.items():
        if any(normalize(w) in folded for w in words):
            return kind
    return "other"


def _employer_key(header: str) -> str:
    folded = normalize(header)
    for key, markers in EMPLOYER_KEYS.items():
        if any(normalize(m) in folded for m in markers):
            return key
    return ""


def parse(document: Any) -> tuple[tuple[Line, ...], tuple[Employer, ...]]:
    """Walk the document once and address every paragraph.

    Deliberately a single forward pass with no lookahead beyond the current
    section: a CV is a sequence of headings and what follows them, and a parser
    that tried to be cleverer than that would start disagreeing with the file
    the moment Noam moved a section.
    """
    lines: list[Line] = []
    employers: list[Employer] = []
    pending: dict[str, Any] = {}

    section = ""
    heading = ""
    seen_heading = False
    identity_count = 0
    heading_count = 0
    skills_count = 0
    summary_count = 0
    structural_count = 0
    employer_index = -1
    bullet_index = 0
    current: list[Line] = []

    def close_employer() -> None:
        if employer_index < 0 or not pending:
            return
        employers.append(
            Employer(
                key=pending["key"],
                header=pending["header"],
                group=pending["group"],
                bullets=tuple(current),
            )
        )

    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text.strip()
        if not text:
            continue

        if _is_heading(paragraph) and (seen_heading or identity_count):
            close_employer()
            pending.clear()
            current = []
            employer_index = -1
            heading = text
            section = _section_kind(text, first=not seen_heading)
            seen_heading = True
            lines.append(
                Line(
                    index=index,
                    address=f"heading.{heading_count}",
                    kind=HEADING,
                    text=text,
                    section=section,
                    heading=heading,
                )
            )
            heading_count += 1
            continue

        if not seen_heading:
            lines.append(
                Line(
                    index=index,
                    address=f"identity.{identity_count}",
                    kind=IDENTITY,
                    text=text,
                    section="identity",
                )
            )
            identity_count += 1
            continue

        if section == "summary":
            lines.append(
                Line(
                    index=index,
                    address=f"summary.{summary_count}",
                    kind=SUMMARY,
                    text=text,
                    section=section,
                    heading=heading,
                )
            )
            summary_count += 1
            continue

        if section == "skills":
            lines.append(
                Line(
                    index=index,
                    address=f"skills.{skills_count}",
                    kind=SKILL,
                    text=text,
                    section=section,
                    heading=heading,
                    group="skills",
                    position=skills_count,
                )
            )
            skills_count += 1
            continue

        if section in EMPLOYER_SECTIONS:
            if _is_list(paragraph) and employer_index >= 0:
                line = Line(
                    index=index,
                    address=f"exp.{employer_index}.bullet.{bullet_index}",
                    kind=BULLET,
                    text=text,
                    section=section,
                    heading=heading,
                    employer=pending["key"],
                    group=pending["group"],
                    position=bullet_index,
                )
                lines.append(line)
                current.append(line)
                bullet_index += 1
                continue
            if _is_bold(paragraph):
                close_employer()
                employer_index = len(employers)
                bullet_index = 0
                current = []
                pending.clear()
                pending.update(
                    key=_employer_key(text), header=text, group=f"exp.{employer_index}"
                )
                lines.append(
                    Line(
                        index=index,
                        address=f"exp.{employer_index}.header",
                        kind=EMPLOYER,
                        text=text,
                        section=section,
                        heading=heading,
                        employer=pending["key"],
                        group=pending["group"],
                    )
                )
                continue
            if employer_index >= 0:
                lines.append(
                    Line(
                        index=index,
                        address=f"exp.{employer_index}.subtitle",
                        kind=SUBTITLE,
                        text=text,
                        section=section,
                        heading=heading,
                        employer=pending["key"],
                        group=pending["group"],
                    )
                )
                continue

        # Education, languages, military service and anything unrecognised.
        # Never tailored, so they need no finer address than their order.
        lines.append(
            Line(
                index=index,
                address=f"structural.{structural_count}",
                kind=STRUCTURAL,
                text=text,
                section=section or "other",
                heading=heading,
            )
        )
        structural_count += 1

    close_employer()
    return tuple(lines), tuple(employers)


def sha256_of(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load(path: Path | str, *, family: str = "", language: str = "") -> Base:
    """Open a base, hash the bytes, and address every line.

    No caching, by contract: `inputs.reread_before_each_run` in
    spec/change-contract.yaml. The hash travels with the result so a stored
    tailored document can be traced to the exact bytes it was cut from.
    """
    from docx import Document  # imported here: python-docx is an optional extra

    resolved = Path(path).expanduser()
    document = Document(str(resolved))
    lines, employers = parse(document)
    return Base(
        family=family,
        language=language,
        path=resolved,
        sha256=sha256_of(resolved),
        document=document,
        lines=lines,
        employers=employers,
    )


def load_for(
    family: str,
    *,
    directory: Path | str,
    language: str | None = None,
    posting_is_english: bool = False,
    international_product_company: bool = False,
) -> Base:
    chosen = select(
        family,
        directory=directory,
        language=language,
        posting_is_english=posting_is_english,
        international_product_company=international_product_company,
    )
    return load(chosen.path, family=chosen.family, language=chosen.language)
