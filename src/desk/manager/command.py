"""The two commands the CLI hands off to: `desk digest` and `desk state`.

Both return an exit code and print nothing that the digest object does not
already contain, so that what Noam reads on the terminal and what a scheduled
run writes to a log are the same text.

`desk digest` sweeps the stale items closed. That is a write inside what
otherwise reads like a read-only command, and it is deliberate: the daily run is
the only heartbeat this system has, so it is the only moment at which "untouched
for three weeks" can become "closed". The alternative is a resident process,
which on a laptop that sleeps is a process that is not running. Every close is
recorded as a `system` event and appears in the digest it was swept by, so it is
never silent.

The order of those steps is the whole guarantee, and it used to be the wrong way
round. The sweep committed its closes first, then the digest was rendered and
sent; if the send raised, the command returned 1 with the closes already
written, and tomorrow's sweep found those rows already `closed` and returned
nothing — so an auto-close that failed to deliver was never reported to a human
in any digest, ever. It now finds what is stale, names it in the digest,
delivers, and writes only after the delivery succeeded. A failed send therefore
loses nothing: the same rows are still stale tomorrow and get reported again.
The alternative — keeping the sweep first and persisting an "unreported closes"
list — needs a second place where pipeline truth lives, and the store's schema
is not this session's to change.

Ordering the write after the send is also why `build` is told what is closing:
an item that the digest reports as closed must not also appear in the same
digest as a candidate or as a follow-up to chase.

`desk state --set` records a human decision and does nothing else. Approving is
a fact about what Noam decided, not an instruction to a machine — the command
says so on approval, because the whole system is built around that sentence and
the one place a person might expect otherwise is the moment they type it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from ..config import load_spec, paths
from ..store import Store
from . import delivery, render, states, timers
from . import digest as digest_module


def cmd_digest(args: argparse.Namespace) -> int:
    """Build today's digest, render it, and deliver it to the chosen sink."""
    spec = load_spec()
    try:
        delivery.check_auto_apply(spec)
    except delivery.NeverApplies as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        sink = delivery.sink_for(spec, send=bool(args.send))
    except delivery.DeliveryError as error:
        print(str(error), file=sys.stderr)
        return 1

    now = datetime.now()
    store = Store(paths().ensure().db)
    try:
        # Found, reported, delivered, and only then written. See the note above.
        closing = timers.pending(store, now=now, spec=spec)
        today = digest_module.build(
            store,
            now=now,
            spec=spec,
            limit=args.limit,
            min_score=args.min_score,
            closed=closing,
        )
        sink.send(render.render(today, args.format))
        timers.close(store, closing, now=now, spec=spec)
    except delivery.DeliveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """Show or move where a posting stands. Four modes, one store."""
    spec = load_spec()
    now = datetime.now()
    store = Store(paths().ensure().db)
    try:
        if args.due:
            return _show_due(store, now=now)
        if args.list_state:
            return _show_state(store, spec=spec, state=args.list_state)
        if args.fingerprint and args.new_state:
            return _move(store, spec=spec, now=now, args=args)
        if args.fingerprint:
            return _show_one(store, fingerprint=args.fingerprint)
        return _show_all(store, spec=spec)
    finally:
        store.close()


def _move(store: Store, *, spec: dict, now: datetime, args: argparse.Namespace) -> int:
    target = str(args.new_state)
    try:
        due_at = timers.due_at_for(target, now=now, spec=spec)
        moved = states.move(
            store,
            args.fingerprint,
            target,
            spec=spec,
            now=now,
            due_at=due_at,
            note=args.note,
            source=states.HUMAN,
        )
    except (states.IllegalTransition, states.UnknownState) as error:
        print(str(error), file=sys.stderr)
        return 1

    origin = moved.from_state or "(new)"
    print(f"{moved.fingerprint[:16]}   {origin} -> {moved.to_state}")
    if moved.due_at:
        print(f"follow-up due {moved.due_at[:10]}")
    if target == states.APPROVED:
        # Said out loud at the one moment somebody might expect otherwise.
        print("recorded. nothing was submitted and nothing was sent.")
    return 0


def _show_one(store: Store, *, fingerprint: str) -> int:
    row = store.state(fingerprint)
    if row is None:
        print(f"{fingerprint[:16]} has no recorded state")
        return 1
    print(f"{fingerprint[:16]}   {row['state']}   since {str(row['updated_at'])[:16]}")
    if row["due_at"]:
        print(f"follow-up due {str(row['due_at'])[:10]}")
    if row["note"]:
        print(str(row["note"]))
    print("")
    for event in store.state_history(fingerprint):
        origin = event["from_state"] or "(new)"
        print(f"  {str(event['at'])[:16]}  {origin} -> {event['to_state']}  [{event['source']}]")
    return 0


def _show_state(store: Store, *, spec: dict, state: str) -> int:
    try:
        states.index(spec, state)
    except states.UnknownState as error:
        print(str(error), file=sys.stderr)
        return 1
    rows = store.in_state(state)
    for row in rows:
        print(f"  {str(row['fingerprint'])[:16]}   {str(row['updated_at'])[:16]}")
    print(f"{len(rows)} in {state}")
    return 0


def _show_all(store: Store, *, spec: dict) -> int:
    for state in states.states(spec):
        print(f"  {state:<12} {len(store.in_state(state))}")
    return 0


def _show_due(store: Store, *, now: datetime) -> int:
    nudges = timers.due(store, now=now)
    for nudge in nudges:
        print(
            f"  {nudge.fingerprint[:16]}   {nudge.state:<12}"
            f" due {nudge.due_at[:10]}   {nudge.days_late} days late"
        )
    print(f"{len(nudges)} awaiting a follow-up")
    return 0
