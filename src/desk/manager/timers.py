"""Follow-ups and staleness — as arithmetic on stored dates, not as a daemon.

Two windows, both from spec/search.yaml and neither of them written here:

    manager.follow_up_days   applied, no answer, this long -> it is due a nudge
    manager.stale_days       untouched this long -> it closes itself, but only
                             up to and including `applied`; see `stale`

The design decision worth stating is that nothing in this module runs in the
background. There is no thread, no timer, no scheduled callback inside the
process. A due date is computed once, at the moment of the transition, and
written to the row; "what is due today" is then a query against a column. The
alternative — a resident process that wakes up and acts — would have to be
running for the system to be correct, and on a laptop that sleeps it would not
be. The daily launchd run is the only heartbeat, and if it is missed for three
days the arithmetic simply returns three days of overdue items when it next
runs, which is the behaviour anyone would want.

The second decision is that `now` is always a parameter. Nothing here reads the
wall clock. Every window in this file is testable at any date, in a fraction of
a second, without freezing time globally or waiting for one — and that is the
same reason the trace, the gates and the resolver all take a clock rather than
calling one.

The nudge is a reminder to a human. It sends nothing and it applies to nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from . import states
from .states import APPLIED, CLOSED, SYSTEM, stamp

# Why an untouched item closes itself, rather than being left open: an open item
# is a claim that an answer might still come, and a list where most of the
# claims are false is a list nobody reads. This is the note it closes with.
STALE_NOTE = "closed automatically: untouched past the spec's stale window"


class TimerStore(states.StateStore, Protocol):
    """The store slice this module reads. It writes only through states.move."""

    def in_state(self, state: str) -> list[dict[str, Any]]: ...

    def due_before(self, when: str) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Nudge:
    """One item whose follow-up date has arrived."""

    fingerprint: str
    state: str
    due_at: str
    days_late: int
    note: str = ""
    title: str = ""
    company: str = ""

    def label(self) -> str:
        """Who this reminder is about, or an honest admission that it is unknown.

        A follow-up used to be rendered as sixteen hex characters. On a terminal
        that is at least a key you can paste into `desk state`; on a phone, the
        only place this digest is actually read, it is unactionable — Noam
        cannot tell which of twenty-four employers has gone quiet, and a nudge
        nobody can act on is a line that trains him to skip the section.
        """
        named = self.company or self.title
        return named or f"unnamed  {self.fingerprint[:16]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "state": self.state,
            "due_at": self.due_at,
            "days_late": self.days_late,
            "note": self.note,
            "title": self.title,
            "company": self.company,
        }


def follow_up_days(spec: dict[str, Any]) -> int:
    return int((spec.get("manager") or {})["follow_up_days"])


def stale_days(spec: dict[str, Any]) -> int:
    return int((spec.get("manager") or {})["stale_days"])


def due_at_for(state: str, *, now: datetime, spec: dict[str, Any]) -> str | None:
    """When this state should next be looked at, or None if it should not.

    Only `applied` carries a follow-up: it is the one state where silence is the
    thing being waited on. Every other state returns None, and returning None
    matters as much as returning a date — the store overwrites the column on
    every transition, so an item that moves on from `applied` has its nudge
    cleared by the same write that moves it, and cannot go on reminding Noam to
    chase an employer who already answered.
    """
    if state != APPLIED:
        return None
    return stamp(now + timedelta(days=follow_up_days(spec)))


def due(store: TimerStore, *, now: datetime) -> list[Nudge]:
    """Everything whose follow-up date has arrived. A query, and only a query."""
    rows = store.due_before(stamp(now))
    nudges = [
        Nudge(
            fingerprint=str(row["fingerprint"]),
            state=str(row["state"]),
            due_at=str(row["due_at"]),
            days_late=_days_between(str(row["due_at"]), now),
            note=str(row["note"] or ""),
            title=str(row.get("title") or ""),
            company=str(row.get("company") or ""),
        )
        for row in rows
    ]
    return sorted(nudges, key=lambda n: (-n.days_late, n.fingerprint))


def stale(store: TimerStore, *, now: datetime, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows untouched past the spec's window, up to and including `applied`.

    Walks the states rather than asking for every row, because the store exposes
    a per-state query and no all-rows one, and inventing a table access here
    would put a second definition of "the pipeline" in the codebase.

    **Nothing past `applied` is swept, and that exclusion is the fix to a bug
    that destroyed live conversations.** The sweep used to exclude only
    `closed`, so an interview arranged on day 0 and not touched again was
    terminally closed on day 21 — and `closed` has an empty transition set, so
    the offer that arrived on day 22 could never be recorded at all. There was
    also no way to prevent it: `interview -> interview` is an illegal move, so
    the row's `updated_at` could not be refreshed by saying "still
    interviewing".

    Of the two available repairs, this is the one that does not touch the state
    machine. Allowing a same-state touch would have to make `interview ->
    interview` legal, which contradicts the rule that an illegal move raises
    rather than being coerced, and it would write a no-op event into a log whose
    entire purpose is to record the things that actually happened.

    Excluding the later states is also the more honest reading of what stale
    means. Before `applied`, silence is Noam's own — a shortlisted posting
    nobody returned to for three weeks is a decision made by not deciding, and
    closing it is accurate. `applied` stays in the sweep for the same reason:
    an employer's three weeks of silence is an answer. After `applied` there is
    a live thread with a person on the other end, an item count small enough to
    read, and no clock this module owns that can tell an interview scheduled
    for next month from an abandoned one. Those close by hand.
    """
    cutoff = now - timedelta(days=stale_days(spec))
    limit = states.index(spec, APPLIED)
    found: list[dict[str, Any]] = []
    for state in states.states(spec):
        if state == CLOSED or states.index(spec, state) > limit:
            continue
        for row in store.in_state(state):
            touched = _parse(str(row["updated_at"]))
            if touched is not None and touched <= cutoff:
                found.append(dict(row))
    return sorted(found, key=lambda r: str(r["updated_at"]))


def pending(store: TimerStore, *, now: datetime, spec: dict[str, Any]) -> tuple[str, ...]:
    """What a sweep would close right now, without closing any of it.

    Split out from `sweep` so the daily command can name the closes in the
    digest it is about to deliver and only commit them once that delivery has
    succeeded. See `close` for why the order matters.
    """
    return tuple(str(row["fingerprint"]) for row in stale(store, now=now, spec=spec))


def close(
    store: TimerStore,
    fingerprints: tuple[str, ...],
    *,
    now: datetime,
    spec: dict[str, Any],
) -> tuple[str, ...]:
    """Close these, as the calendar rather than as Noam. Returns what moved.

    Recorded as a `system` event, so the log distinguishes an item Noam closed
    from one the calendar closed for him. That distinction is the whole reason
    the event log carries a source column.

    Called after the digest has been delivered, never before. An auto-close is
    only ever reported in one digest — the sweep that performs it — so a close
    committed before a send that then fails is a close no human is ever told
    about: tomorrow's sweep finds the row already `closed` and returns nothing.
    Committing last makes the failure repeat instead of vanish.
    """
    closed: list[str] = []
    for fingerprint in fingerprints:
        states.move(
            store,
            fingerprint,
            CLOSED,
            spec=spec,
            now=now,
            note=STALE_NOTE,
            source=SYSTEM,
        )
        closed.append(fingerprint)
    return tuple(closed)


def sweep(store: TimerStore, *, now: datetime, spec: dict[str, Any]) -> tuple[str, ...]:
    """Find what is stale and close it, in one step. Returns what was closed."""
    return close(store, pending(store, now=now, spec=spec), now=now, spec=spec)


def _days_between(when: str, now: datetime) -> int:
    moment = _parse(when)
    if moment is None:
        return 0
    return max(0, (_naive(now) - moment).days)


def _parse(value: str) -> datetime | None:
    """Read a stored timestamp, or give up rather than guess.

    Timezone offsets are dropped instead of converted. Every row this package
    writes comes from one machine and one clock, the windows are measured in
    days, and a naive-versus-aware comparison error would be a crash in the
    daily run — which is a worse failure than being an hour out on a 7-day
    window that has never been near an hour of precision.
    """
    try:
        return _naive(datetime.fromisoformat(value))
    except ValueError:
        return None


def _naive(moment: datetime) -> datetime:
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment
