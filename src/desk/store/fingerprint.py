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
