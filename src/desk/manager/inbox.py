"""The one thing that comes back — a button press on the daily digest.

Everything else in this system pushes outward. This module is the only inbound
path, and it is deliberately the narrowest one that could work: no free text,
no commands, no conversation. Three buttons exist and a press is the entire
vocabulary. A message typed at the bot is not read by anything here.

    a press names one posting the same message just showed. It cannot name a
    posting Noam has not seen, because the fingerprint travels in the button
    rather than being typed, and it cannot name a job that is not in the store,
    because an unknown fingerprint is refused rather than created.

    only the configured chat is heard. A bot token is a URL anybody who has it
    can talk to, and `getUpdates` returns whatever arrived — including messages
    from a stranger who found the bot. Both the chat the button sat in and the
    account that pressed it are checked against `DESK_TELEGRAM_CHAT_ID`, and an
    update from anywhere else is dropped without being acted on, acknowledged
    or logged in full.

    a press is a decision, not an instruction. `y` records `approved`, which is
    the state that already meant "Noam decided this one is worth it", and `n`
    records `closed`. Neither sends anything to an employer, and there is no
    button that could: `check_auto_apply` still runs and this module has no
    code path to an application.

The cursor is stored, not held. Telegram keeps an unacknowledged update for 24
hours and hands it back until an offset moves past it, so a run that dies
mid-batch loses nothing and a run that never happens loses nothing either — the
presses are still waiting the next time the poll runs. The offset is only ever
advanced past updates this process actually finished handling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CHANNEL = "telegram"

APPROVE = "approve"
DISMISS = "dismiss"
MORE = "more"
IGNORE = "ignore"

# The three prefixes render.keyboard writes into the buttons.
ACTIONS = {"y": APPROVE, "n": DISMISS, "m": MORE}


@dataclass(frozen=True)
class Press:
    """One button press, already checked, or one update being refused."""

    update_id: int
    action: str
    callback_id: str = ""
    fingerprint: str = ""
    offset: int = 0
    why: str = ""
    message_id: int = 0
    rows: tuple = ()

    @property
    def actionable(self) -> bool:
        return self.action != IGNORE


def read(update: Mapping[str, Any], *, chat_id: str) -> Press:
    """Turn one raw update into a press, or into a refusal that says why.

    Refusing is a return value rather than an exception because a batch of
    updates has to survive a bad one: the poll is unattended, and an unknown
    update from a stranger must not stop the presses behind it from being
    handled or the cursor from moving past it.
    """
    update_id = int(update.get("update_id") or 0)
    query = update.get("callback_query")
    if not isinstance(query, dict):
        return Press(update_id, IGNORE, why="not a button press")

    callback_id = str(query.get("id") or "")
    author = query.get("from") if isinstance(query.get("from"), dict) else {}
    sender = str(author.get("id", ""))
    message = query.get("message") if isinstance(query.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    origin = str(chat.get("id", ""))
    message_id = int(message.get("message_id") or 0)
    markup = message.get("reply_markup") if isinstance(message.get("reply_markup"), dict) else {}
    rows = tuple(
        tuple((str(b.get("text", "")), str(b.get("callback_data", ""))) for b in row)
        for row in markup.get("inline_keyboard", [])
    )

    # Both halves, and both equal to the configured chat. In a private chat
    # with a bot the chat id *is* the account id, so this is one fact checked
    # twice — and that is the point: a group chat, where the two differ and
    # anybody in the room could press a button, is refused rather than
    # supported. If this bot is ever wanted in a group, the decision about who
    # is allowed to press has to be made deliberately, here.
    wanted = str(chat_id)
    if origin != wanted:
        return Press(update_id, IGNORE, callback_id, why="another chat")
    if sender and sender != wanted:
        return Press(update_id, IGNORE, callback_id, why="another account")

    data = str(query.get("data") or "")
    prefix, _, rest = data.partition(":")
    action = ACTIONS.get(prefix)
    if action is None:
        return Press(update_id, IGNORE, callback_id, why=f"unknown button {data[:32]!r}")

    if action is MORE:
        return Press(update_id, MORE, callback_id, offset=_offset(rest),
                     message_id=message_id, rows=rows)
    if not rest:
        return Press(update_id, IGNORE, callback_id, why="a button with no posting")
    return Press(update_id, action, callback_id, fingerprint=rest,
                 message_id=message_id, rows=rows)


def _offset(raw: str) -> int:
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def next_cursor(presses: list[Press], current: str) -> str:
    """One past the highest update handled, which is what `getUpdates` wants.

    Never moves backwards. A batch that arrives out of order, or a cursor that
    a restored backup put ahead of the channel, must not cause every press in
    between to be replayed — a replayed `y` would re-cut a document Noam has
    since been editing.
    """
    highest = max((p.update_id for p in presses), default=0)
    known = int(current) if current.isdigit() else 0
    return str(max(known, highest + 1 if highest else known))


def _stamped(log, now):
    """Every line the poll prints carries the minute it ran.

    Added 24.08.2026 after an hour was spent asking of a log full of identical
    lines: did the job see that press, or did it run before it? A record with
    no clock cannot answer the only question anybody asks of it.
    """
    prefix = now.strftime("%d.%m %H:%M:%S")
    return lambda line: log(f"{prefix}  {line}")


def run(store, sink, *, spec: dict[str, Any], now, cut, log=print) -> int:
    """Poll once, act on every press, and move the cursor past what was handled.

    `cut` is passed in rather than imported, which is what keeps this file free
    of the tailoring stack and testable without one. It is called with a
    fingerprint and returns the path of the document it wrote, or an empty
    string; raising is allowed and is treated as "this one failed", never as
    "stop reading Noam's answers".

    Returns the number of presses that could not be carried out. Zero updates
    is the ordinary case and is not a failure — most polls find nothing.
    """
    from . import digest as digest_module
    from . import render, states, timers

    log = _stamped(log, now)
    cursor = store.cursor(CHANNEL)
    updates = sink.updates(offset=int(cursor) if cursor.isdigit() else 0)
    presses = [read(update, chat_id=sink.chat_id) for update in updates]
    if not presses:
        log(f"inbox    nothing pressed   (reading from {cursor or 0})")
        return 0

    failed = 0
    handled: list[Press] = []
    for press in presses:
        handled.append(press)
        if not press.actionable:
            # Acknowledged so the phone stops spinning, and named in the log so
            # a stranger pressing at a bot is visible rather than silent.
            log(f"ignored  update {press.update_id}: {press.why}")
            continue
        try:
            log(_act(press, store=store, sink=sink, spec=spec, now=now, cut=cut,
                     digest_module=digest_module, render=render, states=states, timers=timers))
        except Exception as error:  # noqa: BLE001 - one press must not stop the rest
            failed += 1
            log(f"failed   {press.action} {press.fingerprint[:12]}: {type(error).__name__}")
            _answer(sink, press, "לא הצלחתי. נסה שוב מאוחר יותר")

    store.set_cursor(CHANNEL, next_cursor(handled, cursor), now=now.isoformat(timespec="seconds"))
    return failed


def _act(press, *, store, sink, spec, now, cut, digest_module, render, states, timers) -> str:
    if press.action == MORE:
        page = digest_module.build(
            store,
            now=now,
            spec=spec,
            offset=press.offset,
            closed=timers.pending(store, now=now, spec=spec),
        )
        sink.send(render.render(page, "telegram"), render.keyboard(page))
        _answer(sink, press, "")
        return f"more     from {press.offset}, {len(page.items)} sent"

    if store.get_posting(press.fingerprint) is None:
        _answer(sink, press, "המודעה לא נמצאה")
        return f"unknown  {press.fingerprint[:12]} is not in the store"

    if press.action == DISMISS:
        states.move(store, press.fingerprint, states.CLOSED, spec=spec, now=now,
                    note="not relevant, by button")
        _answer(sink, press, "ירד מהרשימה")
        _mark(sink, press, render, "n")
        return f"closed   {press.fingerprint[:12]}"

    # APPROVE. The state is recorded first and the document second, because the
    # decision is the fact worth keeping: a cut that fails is retried by the
    # morning pass, while an approval that was never written is a decision Noam
    # made and the system forgot.
    if states.current(store, press.fingerprint) != states.APPROVED:
        states.move(store, press.fingerprint, states.APPROVED, spec=spec, now=now,
                    note="relevant, by button")
    _answer(sink, press, "מכין קורות חיים")
    _mark(sink, press, render, "y")
    path = cut(press.fingerprint)
    if not path:
        return f"approved {press.fingerprint[:12]}, no document yet"

    from .delivery import Document

    sink.send_documents([Document(path=path, caption=_caption(store, press.fingerprint))])
    return f"approved {press.fingerprint[:12]}, document sent"


def _caption(store, fingerprint: str) -> str:
    posting = store.get_posting(fingerprint) or {}
    title = str(posting.get("title") or "")
    company = str(posting.get("company") or "")
    return "\n".join(line for line in (title, company) if line)


def _mark(sink, press: Press, render, action: str) -> None:
    """Show the decision on the message itself, not only in a toast.

    A toast lives two seconds and is missed on a phone in a pocket. The message
    is what gets scrolled back to, so the row Noam pressed becomes the record of
    what he decided. Failing to redraw it is cosmetic and must never undo the
    decision that was already written, which is why this cannot raise.
    """
    if not press.message_id or not press.rows:
        return
    try:
        sink.edit_keyboard(press.message_id, render.decided(press.rows, press.fingerprint, action))
    except Exception as error:  # noqa: BLE001 - the state is written; drawing is not the fact
        # Swallowed once, and it cost an evening: the decision was recorded and
        # the screen did not move, which reads exactly like a button that does
        # nothing. It still must not raise — but it says so now.
        print(f"unmarked {press.fingerprint[:12]}: {type(error).__name__}: {error}")


def _answer(sink, press: Press, text: str) -> None:
    """Clear the button's spinner. A phone shows it until this lands."""
    if not press.callback_id:
        return
    try:
        sink.answer_callback(press.callback_id, text)
    except Exception:  # noqa: BLE001 - an unacknowledged press is cosmetic
        pass
