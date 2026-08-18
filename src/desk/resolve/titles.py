"""Role cores — what is left of a title once the marketing is stripped.

Cross-site dedup runs on this, and the store proved why nothing simpler works.
The two sites in the store phrase the same fact in opposite shapes:

    gotfriends   Senior BI Developer בחברת סטארט-אפ בתחום ה-Analytics
    alljobs      לחברת נת"ע דרוש /ה רכז /ת בקרת כספים - החלפה לחל"ד

The agency puts the role first and the employer blurb after it; the board puts
the employer first and the role after a "wanted" verb. Both bury the role in a
clause that says nothing about which job this is. Comparing raw titles scores
those two as unrelated and scores every "בחברת סטארט-אפ" title as related, which
is the exact opposite of the truth.

Nothing here invents a word. Every function only removes.
"""

from __future__ import annotations

import re

# "דרוש /ה", "איש.ת", "מנהל /ת" — the same noun written for both genders.
# The suffix carries no meaning for matching and varies freely between posters.
_GENDER_SLASH = re.compile(r"\s*/\s*(?:ה|ת|ית|ות|ים|ה\b)")
_GENDER_DOT = re.compile(r"(?<=[א-ת])\.(?:ת|ית|ות|ים)\b")

# The verb that separates an employer lead-in from the role on Israeli boards.
# Everything before it is who is hiring; the role starts after it.
_WANTED = re.compile(r"\b(?:דרוש(?:ים|ות|ה)?|מחפשים|מגייסים|מגייסת|מגייס)\b")

# The clause an agency appends to describe a client it will not name.
_EMPLOYER_CLAUSE = re.compile(
    r"\s+(?:ל|ב)(?:חברת|חברה|ארגון|קבוצת|קבוצה|גוף|משרד|בנק|סטארט|"
    r"סטארטאפ|סטארט-אפ|אחת|מרכז|יצרנית|יוניקורן)\b"
)

# Perks, urgency and conditions bolted onto the end of a board title.
# A dash only separates when it is spaced: "Full-Stack" and "סטארט-אפ" are one
# word, and an unspaced cut there would truncate the role to "Full". A comma
# only separates when a digit does not follow it, or "16,000" becomes "16".
_TAIL = re.compile(r"\s+[-–|]\s*|\s*[-–|]\s+|\s*!\s*|\s*,(?!\d)\s*")

# "משרת X" / "תפקיד X" — a noun that announces a role without naming one.
_LEAD_NOUN = re.compile(r"^\s*(?:משרת|תפקיד|דרושה?)\s+")

_WS = re.compile(r"\s+")


def strip_gender(text: str) -> str:
    """Collapse dual-gender spellings onto the masculine stem.

    This is a matching convenience and never reaches anything the human reads.
    """
    text = _GENDER_SLASH.sub("", text)
    text = _GENDER_DOT.sub("", text)
    return _WS.sub(" ", text).strip()


def role_core(title: str) -> str:
    """The part of a title that names the job.

    Returns the whole stripped title when no structure is recognised, because a
    title we cannot parse is still better evidence than an empty string.
    """
    text = strip_gender(title)

    # An employer lead-in: keep what follows the wanted-verb, not what precedes.
    match = _WANTED.search(text)
    if match and match.start() > 0:
        text = text[match.end() :]
    elif match:
        text = text[match.end() :]

    # An employer blurb appended to the role: keep what precedes it.
    clause = _EMPLOYER_CLAUSE.search(text)
    if clause and clause.start() > 0:
        text = text[: clause.start()]

    # Perks and conditions after a dash, pipe, comma or bang.
    text = _TAIL.split(text)[0]

    text = _LEAD_NOUN.sub("", text)
    return _WS.sub(" ", text).strip()


def core_tokens(title: str) -> frozenset[str]:
    """The comparable token set of a role core.

    Single characters are dropped: they are almost always a stray conjunction
    left behind by a clause cut, and they match everything.
    """
    from ..store.fingerprint import normalize

    return frozenset(t for t in normalize(role_core(title)).split() if len(t) > 1)
