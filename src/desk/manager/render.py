"""Three renderings of one digest: a terminal, a Telegram message, and JSON.

The digest object is built once and rendered three ways. Nothing here decides
what is in the digest — a renderer that filters is a second, invisible copy of
the ranking rules, and the first time the terminal and the phone disagree about
which five jobs came up, neither can be trusted.

The layout rule that looks cosmetic and is not: **one direction per line**.

Every line of this output is either entirely Hebrew or entirely English, never
both. Terminal emulators reorder bidirectional text per line, and a line that
mixes a Hebrew sentence with a Latin token gets the token flung to the wrong
end of the line — so a path, a score or a state name lands next to the wrong
label and reads as a different fact. Quoting or backticking does not fix it;
the reordering happens below the level of any markup. So the Hebrew here is
prose on its own lines, every technical token sits on an all-English line, and
a posting's own title and its one-line reason each get a line to themselves
rather than a label in front of them, because whichever language they arrive in
is then the only direction on that line.

The one thing this cannot fix is a filesystem path that itself contains both
scripts — the approved CV bases live in a Hebrew-named folder. Such a path gets
its own line with nothing added to it, which is as far as the rule can go.
"""

from __future__ import annotations

import json

from .digest import Digest, Item
from .timers import Nudge

# Telegram rejects a message over this length. It is a protocol limit, not a
# policy knob, which is why it is here and not in spec/search.yaml.
TELEGRAM_LIMIT = 4096

_NEVER_APPLIES_HE = (
    "המערכת אינה מגישה ואינה שולחת דבר בשמך.",
    "אישור הוא רישום בלבד, וההגשה נשארת בידיים שלך.",
)

_EMPTY_HE = "היום אף פריט לא עבר את רף הציון. יום ריק הוא תשובה תקפה."
_EMPTY_EN = "nothing cleared the floor. the digest is never padded to fill it."

_FOLLOW_UPS_HE = "מעקב — מה שהוגש וממתין לתשובה"
_CLOSED_HE = "נסגר מעצמו אחרי חלון הקיפאון שבמפרט"


def as_text(digest: Digest) -> str:
    """The terminal rendering. Wide, unabbreviated, every verdict visible."""
    lines: list[str] = [
        f"job-desk digest   {digest.date}",
        _headline(digest),
    ]
    if digest.suppressed:
        lines.append("suppressed  " + _suppressed(digest))
    lines.append("")

    if digest.empty:
        lines += [_EMPTY_HE, _EMPTY_EN, ""]
    for position, item in enumerate(digest.items, start=1):
        lines += _item_block(position, item)

    lines += _follow_up_block(digest)
    lines += _closed_block(digest)
    lines += list(_NEVER_APPLIES_HE)
    return "\n".join(lines) + "\n"


def as_telegram(digest: Digest) -> str:
    """The phone rendering: the same facts, fewer columns, one hard length cap.

    Sent as plain text with no parse mode. A Hebrew job title is full of
    characters that Markdown treats as syntax, and a message that fails to send
    because a posting had an underscore in it is a worse outcome than a message
    without bold text.
    """
    lines: list[str] = [f"job-desk  {digest.date}", _headline(digest)]
    if digest.empty:
        lines += ["", _EMPTY_HE, _EMPTY_EN]
    for position, item in enumerate(digest.items, start=1):
        lines.append("")
        lines.append(f"{position}. {item.score:.2f}  {item.channel}  {item.region}")
        if item.title:
            lines.append(item.title)
        if item.company:
            lines.append(item.company)
        if item.reason:
            lines.append(item.reason)
        if item.gates:
            lines.append(item.gate_line())
        if item.unknown_gates:
            lines.append("unstated: " + ", ".join(item.unknown_gates))
        if item.also_on:
            lines.append("also on: " + ", ".join(item.also_on))
        if item.url:
            lines.append(item.url)
        if item.has_cv:
            lines.append("cv ready:")
            lines.append(item.cv_path)

    if digest.follow_ups:
        lines += ["", _FOLLOW_UPS_HE]
        lines += [_nudge_line(n) for n in digest.follow_ups]

    lines += [""] + list(_NEVER_APPLIES_HE)
    return _cap("\n".join(lines))


def as_json(digest: Digest) -> str:
    """The machine rendering. Sorted keys so two identical days are identical."""
    return json.dumps(digest.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)


RENDERERS = {"text": as_text, "telegram": as_telegram, "json": as_json}


def render(digest: Digest, fmt: str) -> str:
    if fmt not in RENDERERS:
        raise ValueError(f"unknown format {fmt!r}; have {', '.join(sorted(RENDERERS))}")
    return RENDERERS[fmt](digest)


def _headline(digest: Digest) -> str:
    return (
        f"{len(digest.items)} of {digest.considered} shown"
        f"   floor {digest.min_score:.2f}   ceiling {digest.max_items}"
    )


def _suppressed(digest: Digest) -> str:
    return ", ".join(f"{reason} {count}" for reason, count in sorted(digest.suppressed.items()))


def _item_block(position: int, item: Item) -> list[str]:
    lines = [
        f"  {position}  score {item.score:.2f}   channel {item.channel}"
        f"   region {item.region}   distance {item.distance}"
    ]
    if item.title:
        lines.append(f"     {item.title}")
    if item.company:
        lines.append(f"     {item.company}")
    if item.reason:
        lines.append(f"     {item.reason}")
    if item.gates:
        lines.append(f"     gates    {item.gate_line()}")
    if item.unknown_gates:
        # Spelled out rather than left as a verdict name. `unknown` is the
        # answer that most looks like a pass at a glance, and it is not one.
        lines.append(f"     unstated {', '.join(item.unknown_gates)} — judged on nothing")
    if item.also_on:
        lines.append(f"     also on  {', '.join(item.also_on)}")
    if item.url:
        lines.append(f"     link     {item.url}")
    if item.gaps:
        lines.append("     gaps")
        lines += [f"     {gap}" for gap in item.gaps]
    if item.has_cv:
        lines.append("     cv ready")
        lines.append(f"     {item.cv_path}")
    else:
        lines.append("     no tailored cv on disk yet")
    lines.append("")
    return lines


def _follow_up_block(digest: Digest) -> list[str]:
    if not digest.follow_ups:
        return []
    return [_FOLLOW_UPS_HE] + [f"  {_nudge_line(n)}" for n in digest.follow_ups] + [""]


def _closed_block(digest: Digest) -> list[str]:
    if not digest.closed:
        return []
    return [_CLOSED_HE] + [f"  {fingerprint[:16]}" for fingerprint in digest.closed] + [""]


def _nudge_line(nudge: Nudge) -> str:
    return (
        f"{nudge.state}   due {nudge.due_at[:10]}"
        f"   {nudge.days_late} days late   {nudge.fingerprint[:16]}"
    )


def _cap(text: str) -> str:
    """Truncate to Telegram's limit and say so, rather than being rejected."""
    if len(text) <= TELEGRAM_LIMIT:
        return text
    notice = "\n... truncated to fit one Telegram message"
    return text[: TELEGRAM_LIMIT - len(notice)] + notice
