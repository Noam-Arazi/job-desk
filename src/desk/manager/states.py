"""Where a posting stands, and which moves are allowed to take it there.

The store already keeps both halves of this: a current state and an append-only
event log, written in one transaction so they cannot disagree. Nothing here
stores anything of its own. What this module owns is the one thing the store
deliberately does not — the rules.

One write beyond the state row, and it is deliberate: a move to `applied` also
records the store's applied blocklist, because those are two records of one
fact and only one of them was ever being written. See `move`.

Three properties, each of which prevents a specific mistake:

    the transitions are data, derived from the order the spec lists the states
    in. Adding a stage to the pipeline is an edit to spec/search.yaml, and the
    table rebuilds itself. A hand-written table drifts from the spec the first
    time somebody edits one and not the other.

    an illegal move raises. The tempting alternative is to coerce — to treat
    `applied -> shortlisted` as a no-op, or to quietly re-run the transition
    forward. Both write a history that never happened, and the history is the
    only record of what Noam actually did. A wrong `--set` should be a visible
    error, not a silently swallowed one.

    moving forward is legal, moving back is not. Skipping is allowed on
    purpose: employers answer with an interview and never acknowledge, so
    `applied -> interview` is an ordinary Tuesday. Reversing is not allowed,
    because none of these events can un-happen. `closed` is last in the spec's
    order and additionally reachable from every state, so an item can be
    dropped at any point — and it is terminal, since the honest record of an
    employer who answers after a close is a new event, not a rewind.

And the rule that stands above the rules: `approved` is a recorded human act
and nothing more. Nothing in this package submits anything. See the spec's
`manager.delivery.auto_apply: never`, which is enforced in delivery.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

DISCOVERED = "discovered"
SHORTLISTED = "shortlisted"
APPROVED = "approved"
APPLIED = "applied"
ACK = "ack"
INTERVIEW = "interview"
OFFER = "offer"
CLOSED = "closed"

# The states this module reasons about by name. Every one of them must appear in
# spec/search.yaml under `manager.states`; the spec owns the list and its order,
# and these names are how the code refers to the three points it treats
# specially — the entry point, the point past which an item stops being offered,
# and the terminal one.
REQUIRED = (DISCOVERED, APPLIED, CLOSED)

HUMAN = "human"
SYSTEM = "system"


class StateStore(Protocol):
    """The slice of the store this module uses. Nothing else is touched."""

    def set_state(
        self,
        fingerprint: str,
        state: str,
        *,
        now: str,
        due_at: str | None = ...,
        note: str = ...,
        source: str = ...,
    ) -> str | None: ...

    def state(self, fingerprint: str) -> dict[str, Any] | None: ...

    def mark_applied(
        self,
        fingerprint: str,
        *,
        now: str,
        channel: str = ...,
        cv_path: str = ...,
    ) -> None: ...


class IllegalTransition(ValueError):
    """A move the pipeline does not allow. Raised rather than coerced."""

    def __init__(self, fingerprint: str, from_state: str | None, to_state: str) -> None:
        self.fingerprint = fingerprint
        self.from_state = from_state
        self.to_state = to_state
        origin = from_state or "nothing"
        super().__init__(f"{fingerprint[:12]}: {origin} -> {to_state} is not a legal move")


class UnknownState(ValueError):
    """A state name the spec does not list."""


@dataclass(frozen=True)
class Move:
    """One recorded transition. Returned so a caller can report what happened."""

    fingerprint: str
    from_state: str | None
    to_state: str
    at: str
    due_at: str | None = None
    note: str = ""

    @property
    def first(self) -> bool:
        return self.from_state is None


def stamp(now: datetime) -> str:
    """The one timestamp format this package writes.

    Every window here is measured in days, and every comparison the store does
    on these columns is a string comparison, so one format for every row is the
    difference between a due list that works and one that silently misses.
    """
    return now.isoformat(timespec="seconds")


def states(spec: dict[str, Any]) -> tuple[str, ...]:
    """The pipeline, in the order the spec lists it."""
    listed = tuple(str(s) for s in (spec.get("manager") or {}).get("states") or ())
    if not listed:
        raise UnknownState("spec/search.yaml states no manager.states")
    missing = [s for s in REQUIRED if s not in listed]
    if missing:
        raise UnknownState(f"manager.states is missing {', '.join(missing)}")
    return listed


def index(spec: dict[str, Any], state: str) -> int:
    """How far along the pipeline a state sits. Raises on a name the spec lacks."""
    order = states(spec)
    if state not in order:
        raise UnknownState(f"{state!r} is not one of: {', '.join(order)}")
    return order.index(state)


def transitions(spec: dict[str, Any]) -> dict[str, frozenset[str]]:
    """The legal-move table, built from the spec's ordering.

    Forward to anything later, plus `closed` from anywhere. The second clause is
    written out rather than left to fall out of `closed` being last, so that
    re-ordering the spec cannot quietly remove the ability to drop an item.
    """
    order = states(spec)
    table = {state: set(order[position + 1 :]) for position, state in enumerate(order)}
    for state in order:
        if state != CLOSED:
            table[state].add(CLOSED)
    return {state: frozenset(nexts) for state, nexts in table.items()}


def allows(spec: dict[str, Any], from_state: str | None, to_state: str) -> bool:
    """Whether the move is legal. A posting with no state yet can enter anywhere.

    Entering anywhere is deliberate: Noam applies to things by hand, and the
    first the system hears of a posting may be that it is already applied to.
    Forcing it through `discovered` first would write two events for one fact.
    """
    if to_state not in states(spec):
        raise UnknownState(f"{to_state!r} is not one of: {', '.join(states(spec))}")
    if from_state is None:
        return True
    if from_state not in states(spec):
        raise UnknownState(f"{from_state!r} is not one of: {', '.join(states(spec))}")
    return to_state in transitions(spec)[from_state]


def at_or_after(spec: dict[str, Any], state: str | None, mark: str) -> bool:
    """Whether a posting has reached a point in the pipeline. Used by the digest.

    A posting with no state at all has reached nothing, which is why an unknown
    state is False here rather than an error: most postings in the store have
    never been moved anywhere.
    """
    if state is None or state not in states(spec):
        return False
    return index(spec, state) >= index(spec, mark)


def current(store: StateStore, fingerprint: str) -> str | None:
    row = store.state(fingerprint)
    return str(row["state"]) if row else None


def move(
    store: StateStore,
    fingerprint: str,
    to_state: str,
    *,
    spec: dict[str, Any],
    now: datetime,
    due_at: str | None = None,
    note: str = "",
    source: str = HUMAN,
) -> Move:
    """Record a transition, or refuse it.

    `due_at` is passed in rather than computed here. The follow-up clock is the
    business of timers.py, and keeping it out of this function is what lets the
    rules be tested without a calendar. `command.py` composes the two.

    Moving to `applied` also writes the store's applied blocklist, and that one
    line is the difference between two doors and one. The digest suppresses a
    posting either because the blocklist holds it or because the pipeline moved
    past `applied`, and the blocklist is what the gates and `unseen_postings`
    read too — but nothing in `src/desk` ever wrote to it. `desk state --set
    applied` wrote only the pipeline row, so `has_applied` was False for every
    posting in production and the only test that covered it passed by calling
    `mark_applied` itself. The insert is `INSERT OR IGNORE`, so a re-recorded
    application is a no-op rather than a second row.

    This adds no way for anything to *become* applied on its own. The only
    caller that reaches `applied` is `desk state --set applied`, typed by Noam;
    the sweep only ever moves things to `closed`.
    """
    from_state = current(store, fingerprint)
    if not allows(spec, from_state, to_state):
        raise IllegalTransition(fingerprint, from_state, to_state)
    at = stamp(now)
    store.set_state(fingerprint, to_state, now=at, due_at=due_at, note=note, source=source)
    if to_state == APPLIED:
        store.mark_applied(fingerprint, now=at)
    return Move(fingerprint, from_state, to_state, at, due_at=due_at, note=note)
