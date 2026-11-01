"""Content fingerprints — the memory pattern's primitive.

The same job reaches the store from several sites and from several agencies,
with different whitespace, different casing and a wrapper of HTML. A fingerprint
has to be stable under all three, or cross-run dedup and the applied-blocklist
both leak duplicates.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#\d+);")
_NON_WORD = re.compile(r"[^\w֐-׿]+", re.UNICODE)

# Agency and boilerplate noise that varies between listings of the same role.
_NOISE = re.compile(
    r"\b(m/f|f/m|מ/נ|job\s*id|ref(?:erence)?\s*(?:no\.?|number)?\s*[:#]?\s*\w+)\b",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    """Collapse a posting field to its comparable form."""
    text = unicodedata.normalize("NFKC", text)
    text = _TAG.sub(" ", text)
    text = _ENTITY.sub(" ", text)
    text = _NOISE.sub(" ", text)
    text = text.casefold()
    text = _NON_WORD.sub(" ", text)
    return " ".join(text.split())


def fingerprint(title: str, company: str, location: str = "") -> str:
    """A stable 16-hex-char identity for a posting.

    Location participates because the same title at the same company in two
    cities is two jobs; it is normalized so "Tel Aviv" and "tel-aviv" agree.
    """
    parts = [normalize(title), normalize(company), normalize(location)]
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


# Words that change between two listings of the same employer and say nothing
# about which employer it is: "BDO" in the tracker, "BDO Israel" on LinkedIn.
_COMPANY_NOISE = frozenset("israel ישראל ltd inc בע מ בעמ technologies technology group".split())
# An employer an agency hides is not one employer. Two `חברה חסויה` rows with the
# same title are as likely two seats as one, so they never match on name.
_ANONYMOUS = ("חסוי", "confidential", "חברה גדולה", "ארגון ")
# Hebrew gender endings — `מפתח/ת`, `אנליסט.ית` — are not separators.
_GENDER = re.compile(r"\s?[/.]\s?(?:ת|ית|ה|ות|יות)(?=[\s,|(/-]|$)")
# Where a title stops naming the role and starts describing it.
_TITLE_TAIL = re.compile(r"\s[-–—|/]\s|[(/|,–—]")


def role_key(title: str, company: str) -> tuple[str, str] | None:
    """A looser identity than `fingerprint`, for the applied-blocklist only.

    The fingerprint includes location and the raw company name, so the role
    Noam typed into his tracker as `AI Specialist / BDO / (no location)` and the
    one LinkedIn prints as `AI Specialist / BDO Israel / Tel Aviv District` are
    two jobs to it, and the second came back in the digest after he had applied.
    This keys on the employer without its suffixes and on the title up to its
    first separator. None when the employer is anonymous or either part is empty.
    """
    raw = company.casefold()
    if any(mark in raw for mark in _ANONYMOUS):
        return None
    employer = " ".join(w for w in normalize(company).split() if w not in _COMPANY_NOISE)
    head = normalize(_TITLE_TAIL.split(_GENDER.sub("", title), maxsplit=1)[0])
    if not employer or not head:
        return None
    return employer, head
