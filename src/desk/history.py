"""Noam's own application history, imported from the file he keeps by hand.

The manager has eight states, a legal-transition table and an append-only event
log, and until now it held nothing. Thirty-seven real applications — the ones
that produced the rejections, the one that produced an interview — lived in a
CSV on the Desktop, and the system that exists to track exactly that had never
seen one of them.

This closes that, and it needs no model and no network, which is the whole
reason it is worth doing today: the daily pipeline is blocked on an expired CLI
session, and none of this is.

Three decisions, each of which could reasonably have gone the other way.

    identity is the same content fingerprint everything else uses. Not a
    synthetic id for imported rows: `fingerprint(title, company, location)` is
    the store's answer to "which job is this", and an imported row that happens
    to name the same job as a scraped one should land on the same identity for
    free rather than by a matching rule written here.

    an unmatched row still becomes a posting, on a `manual` site. The
    alternative — application rows pointing at fingerprints with no posting
    behind them — leaves every reader downstream (the digest, the blocklist,
    the resolver) holding an id it cannot resolve to anything. A thin posting
    is a worse record than a scraped one and a far better one than a dangling
    reference.

    linking imported rows to scraped ones is the resolver's job, not this
    file's. Most of these were applied to through LinkedIn and email, so their
    titles and companies are written the way Noam types them and not the way a
    board prints them. Doing fuzzy matching here would be a second, invisible
    copy of `resolve/` with none of its bands or its record. So this writes the
    rows and says how many landed on an identity the store already had; `desk
    resolve --write` is what finds the rest, and it can be re-run and inspected.

What the import deliberately does not do: invent a date. A row with no applied
date keeps none, and the follow-up clock skips it rather than starting a timer
from today and reporting a three-week-old silence as fresh.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .manager.states import ACK, APPLIED, CLOSED, INTERVIEW, OFFER, IllegalTransition, move
from .store import Posting, Store
from .store.fingerprint import fingerprint as make_fingerprint

SITE = "manual"

# Where the job descriptions live, beside the tracker. One file per application,
# named for the employer — or `<employer> - <role>` when the same employer was
# applied to more than once.
DESCRIPTIONS = "תיאורי-משרה"

# The tracker's vocabulary on the left, the manager's on the right. `screening`
# maps to `ack` because they are the same fact under two names: the employer
# answered and nothing has been decided.
STATE_OF: dict[str, str] = {
    "applied": APPLIED,
    "screening": ACK,
    "interview": INTERVIEW,
    "offer": OFFER,
    "closed": CLOSED,
}

COLUMNS = ("date", "company", "role", "location", "source", "via", "status", "note")


class UnknownStatus(ValueError):
    """A status the manager has no state for. Refused rather than guessed at."""


@dataclass(frozen=True)
class Entry:
    """One row of the tracker."""

    date: str
    company: str
    role: str
    location: str = ""
    source: str = ""
    via: str = ""
    status: str = ""
    note: str = ""

    @property
    def fingerprint(self) -> str:
        return make_fingerprint(self.role, self.company, self.location)

    @property
    def state(self) -> str:
        try:
            return STATE_OF[self.status.strip().lower()]
        except KeyError as exc:
            raise UnknownStatus(
                f"{self.company} / {self.role}: status {self.status!r} is not one of "
                + ", ".join(sorted(STATE_OF))
            ) from exc

    @property
    def channel(self) -> str:
        """Where it was applied through, as the tracker records it."""
        return self.via or self.source


@dataclass(frozen=True)
class Row:
    """One entry, resolved against the store, before anything is written."""

    entry: Entry
    fingerprint: str
    state: str
    known: bool  # the store already had a posting on this fingerprint
    current: str | None  # the state it is already in, if any
    body: str = ""  # the saved job description, when one exists
    description_file: str = ""


@dataclass
class Result:
    written: list[Row]
    skipped: list[Row]
    refused: list[tuple[Row, str]]
    reclocked: list[Row] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "written": len(self.written),
            "skipped": len(self.skipped),
            "refused": len(self.refused),
            "reclocked": len(self.reclocked),
            "matched_existing": sum(1 for r in self.written if r.known),
            "created_manual": sum(1 for r in self.written if not r.known),
        }


def descriptions(folder: Path | str) -> dict[str, str]:
    """The saved job descriptions, keyed by filename stem.

    This is the fuel the duplicate resolver was built to burn. Its founding
    measurement was that title-and-company identity finds nothing across sites,
    because an agency states no employer and writes a paragraph where a title
    belongs — so it scores on body text instead. An imported row with no body
    is therefore invisible to the one comparison that could tell whether the
    anonymous `ארגון ממשלתי (חסוי)` and the anonymous `חברה טכנולוגית גדולה` are
    the same seat. Paste the posting in and the arithmetic answers it.
    """
    directory = Path(folder).expanduser()
    if not directory.is_dir():
        return {}
    found = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() in (".md", ".txt") and path.is_file():
            found[path.stem.strip()] = path.read_text(encoding="utf-8").strip()
    return found


def body_for(entry: Entry, saved: Mapping[str, str]) -> tuple[str, str]:
    """The description for one entry, and the filename it came from.

    `<employer> - <role>` wins over `<employer>` so that three applications to
    the same company do not all read the same posting — abra is in the tracker
    three times, for three different roles.
    """
    for key in (f"{entry.company} - {entry.role}", entry.company):
        if key in saved:
            return saved[key], key
    return "", ""


def read(path: Path | str) -> list[Entry]:
    """Parse the tracker. A row missing both company and role is not a row."""
    with Path(path).expanduser().open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or ())]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        entries = []
        for raw in reader:
            entry = Entry(**{c: (raw.get(c) or "").strip() for c in COLUMNS})
            if not entry.company and not entry.role:
                continue
            entries.append(entry)
    return entries


def plan(
    entries: Iterable[Entry], store: Store, saved: Mapping[str, str] | None = None
) -> list[Row]:
    """What the import would do, resolved against the store and written nowhere."""
    from .manager.states import current as current_state

    saved = saved or {}
    rows = []
    for entry in entries:
        fp = entry.fingerprint
        body, filename = body_for(entry, saved)
        rows.append(
            Row(
                entry=entry,
                fingerprint=fp,
                state=entry.state,
                known=store.get_posting(fp) is not None,
                current=current_state(store, fp),
                body=body,
                description_file=filename,
            )
        )
    return rows


def _posting(entry: Entry, fingerprint: str, body: str = "") -> Posting:
    """A thin posting for a job that was never scraped.

    `external_id` is the fingerprint so re-running the import updates the same
    row instead of adding another. The body is the saved job description when
    there is one and the tracker's own note when there is not — the resolver
    reads bodies, and a note is a poor posting but a better one than nothing.
    """
    return Posting(
        site=SITE,
        external_id=fingerprint,
        title=entry.role,
        company=entry.company,
        location=entry.location,
        body=body or entry.note,
        posted_at=entry.date,
        fingerprint=fingerprint,
    )


def apply(
    rows: Sequence[Row],
    store: Store,
    *,
    spec: Mapping[str, Any],
    now: datetime,
    run_id: str | None = None,
) -> Result:
    """Write the plan. Every refusal is reported, never swallowed."""
    written: list[Row] = []
    skipped: list[Row] = []
    refused: list[tuple[Row, str]] = []
    reclocked: list[Row] = []
    stamp = now.isoformat(timespec="seconds")

    for row in rows:
        # Written whenever the posting is ours to write, not only when it is new:
        # a description pasted in today has to reach a row imported last week,
        # and that row is exactly the one the resolver cannot use without it. A
        # posting from a real board is never touched — the site is checked, not
        # assumed, because the fingerprint is shared identity and a scraped body
        # is worth more than anything this file can mint.
        existing = store.get_posting(row.fingerprint)
        if existing is None or existing["site"] == SITE:
            store.upsert_posting(
                _posting(row.entry, row.fingerprint, row.body), now=stamp, run_id=run_id
            )

        if row.current == row.state:
            # Already where the tracker says it is, so no move — but the
            # follow-up date is derived from the application, not from the move,
            # and a re-run is how a missing one gets corrected.
            if store.set_due_at(row.fingerprint, _due_at(row, spec=spec)):
                reclocked.append(row)
            skipped.append(row)
            continue
        # Before the move, not after. Every row in this file is an application —
        # that is what the file is — so the blocklist is written for all of them
        # and not only for the ones still sitting in `applied`. It has to happen
        # first because `move` writes the same row with `INSERT OR IGNORE` and an
        # empty channel, and the second writer of an ignored insert loses.
        # An undated row records no date. Today would be a lie that every later
        # reading of "how long has this been silent" would inherit.
        store.mark_applied(
            row.fingerprint,
            now=row.entry.date,
            channel=row.entry.channel,
        )
        try:
            move(
                store,
                row.fingerprint,
                row.state,
                spec=dict(spec),
                now=now,
                due_at=_due_at(row, spec=spec),
                note=_note(row.entry),
                source="import",
            )
        except IllegalTransition as exc:
            refused.append((row, str(exc)))
            continue
        written.append(row)

    return Result(written=written, skipped=skipped, refused=refused, reclocked=reclocked)


def orphans(entries: Iterable[Entry], store: Store) -> list[dict[str, Any]]:
    """Manual postings no line of the tracker names any more.

    The import is idempotent as long as the file does not change, and the file
    changes — a title corrected from a screenshot, a location filled in. Each
    such edit moves the row's content fingerprint, so the next run writes a new
    identity and abandons the old one in whatever state it had reached. Nothing
    downstream can tell that row from a real application: it sits in `applied`,
    it holds a blocklist entry, and it inflates every count taken off the store.

    So the import reports them, and removes them only when told to. They are
    the one kind of posting safe to delete — every one of them was written by a
    previous run of this file, and the tracker is the record they came from.
    """
    wanted = {entry.fingerprint for entry in entries}
    return [row for row in store.postings_from(SITE) if row["fingerprint"] not in wanted]


def prune(rows: Iterable[Mapping[str, Any]], store: Store) -> int:
    """Erase abandoned manual postings. Returns how many were actually removed."""
    return sum(1 for row in rows if store.forget(str(row["fingerprint"]), only_site=SITE))


def _due_at(row: Row, *, spec: Mapping[str, Any]) -> str | None:
    """The follow-up date, counted from when he applied and not from today.

    This is the difference between the import being a record and being useful.
    An application sent three weeks ago and never answered is already overdue,
    and dating its clock from the import would report that silence as fresh —
    the whole set would come due seven days from now, together, which is the
    one arrangement guaranteed to tell him nothing.

    An undated row gets no clock at all. There is nothing to count from, and
    counting from today is the invented date this file refuses to write.
    """
    from .manager.timers import due_at_for

    if not row.entry.date:
        return None
    try:
        applied = datetime.fromisoformat(row.entry.date)
    except ValueError:
        return None
    return due_at_for(row.state, now=applied, spec=dict(spec))


def _note(entry: Entry) -> str:
    """The tracker's own note, kept verbatim, prefixed with where it came from."""
    head = f"imported from the tracker ({entry.date or 'no date recorded'})"
    return f"{head} · {entry.note}" if entry.note else head
