"""The text a gate reads, prepared in one place.

The store's `normalize` is deliberately not reused here. It strips every
non-word character, and `3+` and `3-5` are precisely the characters a seniority
requirement is written in — it is the right function for fingerprints and the
wrong one for reading a number out of prose. What the gates need is milder:
markup gone, dual-gender spellings collapsed, whitespace flattened, everything
else left where the poster put it.

Dual-gender spellings are collapsed because Israeli boards write the same noun
twice — "דרוש /ה", "מנהל /ת", "איש.ת" — and a keyword compared against the raw
string misses one spelling in every pair. The stem is a matching convenience
and never reaches anything the human reads.
"""

from __future__ import annotations

import re
import unicodedata

from ..resolve.titles import strip_gender

_TAG = re.compile(r"<[^>]+>")
_ENTITY = re.compile(r"&(?:nbsp|amp|lt|gt|quot|#\d+);")

# Boards mix hyphen, en-dash and the Hebrew maqaf inside ranges. One character
# downstream means one range pattern instead of three.
_DASHES = re.compile(r"[‐-―־]")
_WS = re.compile(r"\s+")

# Hebrew spells small numbers as words at least as often as it writes digits,
# and "שנתיים" is a single word that means two years with no digit anywhere.
# Feminine forms only: years are feminine.
YEAR_WORDS: dict[str, int] = {
    "שנה": 1,
    "אחת": 1,
    "שנתיים": 2,
    "שתיים": 2,
    "שתי": 2,
    "שלוש": 3,
    "שלושה": 3,
    "ארבע": 4,
    "חמש": 5,
    "שש": 6,
    "שבע": 7,
    "שמונה": 8,
    "תשע": 9,
    "עשר": 10,
}


def readable(*parts: str) -> str:
    """Join the fields a gate reads into one flat, comparable string.

    Empty parts are dropped rather than joined, so a missing body does not glue
    a title to a location and invent an adjacency that neither of them stated.
    """
    text = " \n ".join(p for p in parts if p)
    text = unicodedata.normalize("NFKC", text)
    text = _TAG.sub(" ", text)
    text = _ENTITY.sub(" ", text)
    text = _DASHES.sub("-", text)
    text = strip_gender(text)
    return _WS.sub(" ", text).strip().casefold()


def quote(text: str, start: int, end: int, *, window: int = 30) -> str:
    """The matched span with a little of what surrounded it.

    A gate that blocks has to be able to show the human the sentence it read,
    not the two words the regex happened to capture.
    """
    left = max(0, start - window)
    right = min(len(text), end + window)
    span = text[left:right].strip()
    return ("…" if left > 0 else "") + span + ("…" if right < len(text) else "")


def near(text: str, index: int, keywords: tuple[str, ...], *, window: int = 60) -> str | None:
    """The keyword that appears within `window` characters of `index`, if any.

    Proximity is how a gate tells a requirement from a coincidence. "3 שנים"
    beside "ניסיון" is a seniority bar; the same two words beside "החברה קיימת"
    is the company's age, and blocking on it would be a lie the human cannot
    see. The same holds for a degree named inside a description of the field
    rather than inside a demand for a diploma.
    """
    left = max(0, index - window)
    right = min(len(text), index + window)
    around = text[left:right]
    for word in keywords:
        if word in around:
            return word
    return None
