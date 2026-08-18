"""Follow-ups and staleness — as arithmetic on stored dates, not as a daemon.

Two windows, both from spec/search.yaml and neither of them written here:

    manager.follow_up_days   applied, no answer, this long -> it is due a nudge
    manager.stale_days       untouched this long -> it closes itself

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

    def as_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "state": self.state,
            "due_at": self.due_at,
            "days_late": self.days_late,
            "note": self.note,
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
        )
        for row in rows
    ]
    return sorted(nudges, key=lambda n: (-n.days_late, n.fingerprint))


def stale(store: TimerStore, *, now: datetime, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows untouched for longer than the spec's window, closed ones excluded.

    Walks the states rather than asking for every row, because the store exposes
    a per-state query and no all-rows one, and inventing a table access here
    would put a second definition of "the pipeline" in the codebase.
    """
    cutoff = now - timedelta(days=stale_days(spec))
    found: list[dict[str, Any]] = []
    for state in states.states(spec):
        if state == CLOSED:
            continue
        for row in store.in_state(state):
            touched = _parse(str(row["updated_at"]))
            if touched is not None and touched <= cutoff:
                found.append(dict(row))
    return sorted(found, key=lambda r: str(r["updated_at"]))


def sweep(store: TimerStore, *, now: datetime, spec: dict[str, Any]) -> tuple[str, ...]:
    """Close everything the spec calls stale. Returns what was closed.

    Recorded as a `system` event, so the log distinguishes an item Noam closed
    from one the calendar closed for him. That distinction is the whole reason
    the event log carries a source column.
    """
    closed: list[str] = []
    for row in stale(store, now=now, spec=spec):
        fingerprint = str(row["fingerprint"])
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
