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
import fcntl
import io
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
# answered and nothing has been decided. `rejected` is `closed` for the same
# reason — the manager does not record who ended it, only that it ended.
STATE_OF: dict[str, str] = {
    "applied": APPLIED,
    "screening": ACK,
    "interview": INTERVIEW,
    "offer": OFFER,
    "closed": CLOSED,
    "rejected": CLOSED,
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


def unknown(entries: Iterable[Entry]) -> list[str]:
    """Why each row with a status the manager has no state for was left out.

    One such row used to refuse the whole file, and a single `rejected` typed
    into the tracker on 29.10 kept every application after it out of the
    blocklist — so the digest offered jobs back that had already been sent.
    The row is still refused, not guessed at; it just no longer takes the
    others down with it.
    """
    reasons = []
    for entry in entries:
        try:
            _ = entry.state
        except UnknownStatus as exc:
            reasons.append(str(exc))
    return reasons


def plan(
    entries: Iterable[Entry], store: Store, saved: Mapping[str, str] | None = None
) -> list[Row]:
    """What the import would do, resolved against the store and written nowhere.

    A row `unknown` names is left out; the caller reports it.
    """
    from .manager.states import current as current_state

    saved = saved or {}
    rows = []
    for entry in entries:
        try:
            _ = entry.state
        except UnknownStatus:
            continue
        fp = _board_fingerprint(entry, store) or entry.fingerprint
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


_LINK = re.compile(r"[\w.-]+\.[a-z]{2,}/[^\s,()]+", re.IGNORECASE)


def _board_fingerprint(entry: Entry, store: Store) -> str | None:
    """The scraped posting the tracker row links to, when it names one.

    Agency rows are written under the agency's own words — "לין ביכלר (חברת
    השמה; מעסיק חסוי)" against the board's "לין ביכלר ראשי" — so the name never
    meets the board's copy, and the job came back the morning after it was
    applied to. The link the row carries in `source` or `via` is exact.
    """
    for link in _LINK.findall(f"{entry.source} {entry.via}"):
        row = store.conn.execute(
            "SELECT fingerprint FROM postings WHERE site != ? AND url LIKE ? LIMIT 1",
            (SITE, f"%{link.rstrip('/')}%"),
        ).fetchone()
        if row is not None:
            return str(row["fingerprint"])
    return None


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


# --------------------------------------------------------------------------
# the tracker read for the digest, and written to by the digest's own button
# --------------------------------------------------------------------------

# The tracker's location. The import has always defaulted to this path; it lives
# here now so the digest and the inbox read and write the same file the import
# does, and `DESK_TRACKER` points all three at a copy when that is wanted.
TRACKER_ENV = "DESK_TRACKER"
TRACKER = Path.home() / "קורות חיים" / "הגשות.csv"

# The tracker's own word for a row that was sent and has not been answered, and
# what it writes in `via` for an application made from the digest — three rows
# Noam typed by hand on 02.11 already say `job-desk` there.
SUBMITTED = "applied"
VIA_DIGEST = "job-desk"

# How a board's id is written in `source`, the way the hand-typed rows spell it.
SITE_NAMES = {
    "linkedin": "LinkedIn",
    "alljobs": "AllJobs",
    "jobify": "Jobify",
    "drushim": "Drushim",
    "gotfriends": "GotFriends",
    "xplace": "Xplace",
}


def tracker_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = str(environ.get(TRACKER_ENV) or "").strip()
    return Path(override).expanduser() if override else TRACKER


@dataclass(frozen=True)
class Tally:
    """The two numbers the digest opens with, or why they could not be counted.

    `error` is not swallowed into zeros. A tracker that cannot be read and a
    tracker with nothing open print the same "0", and only one of them is true.
    """

    open: int = 0
    yesterday: int = 0
    error: str = ""  # the kind of failure, in English; empty when it was read
    path: str = ""  # the file asked for, on a line of its own when shown

    @property
    def ok(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "open": self.open,
            "yesterday": self.yesterday,
            "error": self.error,
            "path": self.path,
        }


def tally(path: Path | str, *, today: date) -> Tally:
    """Open applications, and how many were sent the day before `today`.

    Open is the tracker's own semantics, through `STATE_OF`: every status that
    does not map to `closed`. A row whose status the manager does not know is
    neither open nor closed and is not counted as either — `unknown` is how the
    import reports it. Yesterday counts by the date column alone, whatever has
    happened to the application since.
    """
    try:
        entries = read(path)
    except (OSError, ValueError, csv.Error) as error:
        # The type and not the message: the message quotes the path, and the
        # path is in a Hebrew-named folder, so the two travel on separate lines.
        reason = type(error).__name__
        if isinstance(error, ValueError) and "missing columns" in str(error):
            reason = "missing columns " + str(error).rsplit("missing columns", 1)[-1].strip()
        return Tally(error=reason, path=str(path))
    before = (today - timedelta(days=1)).isoformat()
    still_open = 0
    for entry in entries:
        try:
            state = entry.state
        except UnknownStatus:
            continue
        if state != CLOSED:
            still_open += 1
    sent = sum(1 for entry in entries if entry.date[:10] == before)
    return Tally(open=still_open, yesterday=sent)


@dataclass(frozen=True)
class Recorded:
    """What pressing "applied" did to the tracker."""

    written: bool
    entry: Entry
    why: str = ""  # "already there" when it was a repeat press


def entry_for(posting: Mapping[str, Any], fingerprint: str, *, today: date) -> Entry:
    """The tracker row for a job applied to from the digest.

    `source` carries the link, the way Noam's own rows carry `LinkedIn
    (jobs/view/…)`, because the link is what `_board_fingerprint` uses to land a
    re-import on the scraped posting rather than minting a manual twin of it.
    The note carries the fingerprint, which is what makes a second press find
    the first one even when the posting has no link.
    """
    site = str(posting.get("site") or "")
    name = SITE_NAMES.get(site, site)
    url = str(posting.get("url") or "")
    source = f"{name} ({url})" if name and url else (url or name)
    return Entry(
        date=today.isoformat(),
        company=str(posting.get("company") or ""),
        role=str(posting.get("title") or ""),
        location=str(posting.get("location") or ""),
        source=source,
        via=VIA_DIGEST,
        status=SUBMITTED,
        note=f"נרשם מכפתור ההגשה בדייג׳סט · {fingerprint[:16]}",
    )


def record(
    path: Path | str, posting: Mapping[str, Any], fingerprint: str, *, today: date
) -> Recorded:
    """Append one application to the tracker, unless it is already there.

    Append-only, and that is the whole safety argument. Noam edits this file by
    hand, and a read-modify-rewrite would erase whatever he saved between the
    read and the write. Opening for append and writing the row in one call never
    touches a byte he wrote. The lock keeps two presses from both deciding the
    row is missing; an editor does not take it, and does not need to, since
    nothing here rewrites what an editor might be holding.

    The row is written in the file's own shape: its column order, and the line
    ending its last line uses. A file that does not end in a newline gets one
    before the row, or the row would be glued onto Noam's last line.

    A missing tracker is an error, not a file to create — the path being wrong
    is far likelier than the tracker being gone, and a fresh file in the wrong
    place is an application recorded where nobody will look.
    """
    target = Path(path).expanduser()
    entry = entry_for(posting, fingerprint, today=today)
    if not target.is_file():
        raise FileNotFoundError(f"no tracker at {target}")

    with target.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            text = handle.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text, newline=""))
            header = list(reader.fieldnames or ())
            missing = [c for c in COLUMNS if c not in header]
            if missing:
                raise ValueError(f"{target}: missing columns {missing}")
            if any(_names(row, entry, fingerprint, posting) for row in reader):
                return Recorded(written=False, entry=entry, why="already there")

            ending = _ending(text)
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=header, lineterminator=ending)
            writer.writerow({c: getattr(entry, c) for c in COLUMNS})
            row = buffer.getvalue()
            if text and not text.endswith(("\n", "\r")):
                row = ending + row
            # One write, on a handle opened for append: the kernel puts it at
            # the end of the file as it is now, not as it was when it was read.
            handle.write(row.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return Recorded(written=True, entry=entry)


def _ending(text: str) -> str:
    """The newline the file's last line uses, or its header's if it has none."""
    if text.endswith("\r\n"):
        return "\r\n"
    if text.endswith("\n"):
        return "\n"
    first = text.split("\n", 1)
    return "\r\n" if len(first) > 1 and first[0].endswith("\r") else "\n"


def _link_key(url: str) -> str:
    key = re.sub(r"^[a-z]+://", "", url.strip().lower())
    return key.removeprefix("www.").rstrip("/")


def _names(
    row: Mapping[str, Any], entry: Entry, fingerprint: str, posting: Mapping[str, Any]
) -> bool:
    """Whether a tracker row already records this application.

    By the fingerprint a digest press writes into its note, by the posting's
    link anywhere in `source` or `via`, or by the content fingerprint of the
    row itself — the last is how a row Noam typed by hand before pressing is
    recognised.
    """
    note = str(row.get("note") or "")
    if fingerprint and fingerprint[:16] in note:
        return True
    link = _link_key(str(posting.get("url") or ""))
    if link:
        where = f"{row.get('source') or ''} {row.get('via') or ''}".lower()
        if link in where:
            return True
    company = str(row.get("company") or "").strip()
    role = str(row.get("role") or "").strip()
    if not (company or role):
        return False
    theirs = make_fingerprint(role, company, str(row.get("location") or "").strip())
    return theirs in (fingerprint, entry.fingerprint)
