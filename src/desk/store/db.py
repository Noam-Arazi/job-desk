"""The store — the memory pattern, on sqlite.

It holds seven things, and every one of them is state that has to survive between
runs rather than context budgeted inside one:

    postings        what has been seen, with its content fingerprint
    fingerprints    the cross-run dedup index; collapses a role seen twice
    duplicate_links what the resolver concluded about a pair, and whether
                    arithmetic or a model concluded it
    applications    the applied-blocklist. Its only job is: never show this again
    decisions       what each stage concluded, so the calibration loop has ground
                    to stand on
    cv_bases        the approved bases from session 2, hash-pinned
    labels          Noam's own verdicts on real postings — the gold set the
                    analyst is measured against. It lives here and not in a file
                    because it is real posting data, which never enters git
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .fingerprint import fingerprint as make_fingerprint

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    mode         TEXT NOT NULL,
    spec_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fingerprints (
    fingerprint    TEXT PRIMARY KEY,
    first_seen_at  TEXT NOT NULL,
    first_seen_run TEXT,
    times_seen     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS postings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL REFERENCES fingerprints(fingerprint),
    site         TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    url          TEXT,
    title        TEXT NOT NULL,
    company      TEXT NOT NULL,
    location     TEXT,
    body         TEXT,
    posted_at    TEXT,
    -- What the board itself said about required experience, in its own words.
    -- Drushim prints it as a separate field on every card, which is the one
    -- place in this system where a gate gets a stated answer instead of having
    -- to read it out of prose. It was parsed and then dropped here, so the
    -- seniority gate fell back to the body for all 1,108 of those rows.
    stated_experience TEXT,
    fetched_at   TEXT NOT NULL,
    run_id       TEXT,
    UNIQUE (site, external_id)
);
CREATE INDEX IF NOT EXISTS postings_fp ON postings(fingerprint);

CREATE TABLE IF NOT EXISTS applications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL,
    applied_at   TEXT NOT NULL,
    channel      TEXT,
    cv_path      TEXT,
    outcome      TEXT,
    UNIQUE (fingerprint)
);

CREATE TABLE IF NOT EXISTS decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    stage        TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    score        REAL,
    reason       TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_fp ON decisions(fingerprint);

CREATE TABLE IF NOT EXISTS duplicate_links (
    left_fp     TEXT NOT NULL,
    right_fp    TEXT NOT NULL,
    score       REAL NOT NULL,
    band        TEXT NOT NULL,
    method      TEXT NOT NULL,
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (left_fp, right_fp)
);
CREATE INDEX IF NOT EXISTS links_left ON duplicate_links(left_fp);

CREATE TABLE IF NOT EXISTS labels (
    fingerprint  TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    stratum      TEXT NOT NULL,
    labelled_at  TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS cv_bases (
    family       TEXT NOT NULL,
    language     TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    approved_at  TEXT NOT NULL,
    PRIMARY KEY (family, language)
);

CREATE TABLE IF NOT EXISTS analyses (
    fingerprint  TEXT PRIMARY KEY,
    run_id       TEXT,
    family       TEXT NOT NULL,
    score        REAL,
    channel      TEXT,
    rationale    TEXT,
    stopped_at   TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS analyses_score ON analyses(score);

CREATE TABLE IF NOT EXISTS pipeline_state (
    fingerprint  TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    due_at       TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS state_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL,
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    at           TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'system',
    note         TEXT
);
CREATE INDEX IF NOT EXISTS state_events_fp ON state_events(fingerprint);

CREATE TABLE IF NOT EXISTS channel_cursor (
    channel     TEXT PRIMARY KEY,
    position    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tailored (
    fingerprint  TEXT PRIMARY KEY,
    family       TEXT NOT NULL,
    language     TEXT NOT NULL,
    base_sha256  TEXT NOT NULL,
    path         TEXT NOT NULL,
    changes      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


@dataclass
class Posting:
    site: str
    external_id: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    body: str = ""
    posted_at: str = ""
    stated_experience: str = ""
    fingerprint: str = field(default="")

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = make_fingerprint(self.title, self.company, self.location)


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns a store written by an older version does not have.

        `CREATE TABLE IF NOT EXISTS` is silent about a table that exists with
        the wrong shape, so a column added to SCHEMA never reaches a database
        that already exists — and this project's database is the corpus every
        measurement is taken on, which nobody wants to rebuild by re-scraping
        three boards.
        """
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(postings)")}
        if "stated_experience" not in columns:
            with self.tx() as c:
                c.execute("ALTER TABLE postings ADD COLUMN stated_experience TEXT")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # -- runs ------------------------------------------------------------

    def start_run(self, run_id: str, started_at: str, mode: str, spec_version: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_at, mode, spec_version)"
                " VALUES (?,?,?,?)",
                (run_id, started_at, mode, spec_version),
            )

    # -- postings and dedup ----------------------------------------------

    def upsert_posting(self, posting: Posting, *, now: str, run_id: str | None = None) -> bool:
        """Store a posting. Returns True if its fingerprint is new to the store.

        Re-running a day is idempotent: the same (site, external_id) updates in
        place and the fingerprint's counter moves, but no row is duplicated and
        no work is redone downstream.
        """
        with self.tx() as c:
            row = c.execute(
                "SELECT fingerprint FROM fingerprints WHERE fingerprint = ?",
                (posting.fingerprint,),
            ).fetchone()
            is_new = row is None
            if is_new:
                c.execute(
                    "INSERT INTO fingerprints(fingerprint, first_seen_at, first_seen_run,"
                    " times_seen) VALUES (?,?,?,1)",
                    (posting.fingerprint, now, run_id),
                )
            else:
                c.execute(
                    "UPDATE fingerprints SET times_seen = times_seen + 1 WHERE fingerprint = ?",
                    (posting.fingerprint,),
                )
            c.execute(
                """
                INSERT INTO postings(fingerprint, site, external_id, url, title, company,
                                     location, body, posted_at, stated_experience,
                                     fetched_at, run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(site, external_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    title       = excluded.title,
                    company     = excluded.company,
                    location    = excluded.location,
                    body        = excluded.body,
                    url         = excluded.url,
                    posted_at   = excluded.posted_at,
                    stated_experience = excluded.stated_experience,
                    fetched_at  = excluded.fetched_at
                """,
                (
                    posting.fingerprint,
                    posting.site,
                    posting.external_id,
                    posting.url,
                    posting.title,
                    posting.company,
                    posting.location,
                    posting.body,
                    posting.posted_at,
                    posting.stated_experience,
                    now,
                    run_id,
                ),
            )
        return is_new

    def get_posting(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM postings WHERE fingerprint = ? ORDER BY id LIMIT 1",
            (fingerprint,),
        ).fetchone()
        return dict(row) if row else None

    def duplicates_of(self, fingerprint: str) -> list[dict[str, Any]]:
        """Every row sharing a fingerprint — the same role from several sites."""
        rows = self.conn.execute(
            "SELECT * FROM postings WHERE fingerprint = ? ORDER BY id", (fingerprint,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all_postings(self) -> list[dict[str, Any]]:
        """Every stored row, one per (site, external_id).

        The resolver needs the rows and not the distinct fingerprints: the whole
        reason it exists is that the fingerprint collapses the wrong things.
        """
        rows = self.conn.execute("SELECT * FROM postings ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def unseen_postings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Distinct roles that have not been applied to, newest first.

        Two corrections that used to be missing, both of which showed the human
        the same job twice.

        "Newest first" was ordered by rowid, which is insertion order, which is
        crawl order — so a limit returned whatever the scraper happened to store
        last and silently dropped fresher postings. It now orders by the date
        the board printed, falling back to when it was fetched.

        And "applied to" is asked of the whole cluster. The resolver merges the
        same role across boards; applying through one board and then being
        offered the other one tomorrow is precisely the failure the resolver
        exists to prevent, and keying the blocklist on the raw fingerprint
        walked straight into it.
        """
        rows = self.conn.execute(
            """
            SELECT p.* FROM postings p
            JOIN (SELECT fingerprint, MIN(id) AS id FROM postings GROUP BY fingerprint) d
              ON d.id = p.id
            ORDER BY COALESCE(p.posted_at, p.fetched_at) DESC
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if len(out) >= limit:
                break
            if not self.has_applied(str(row["fingerprint"])):
                out.append(dict(row))
        return out

    # -- the applied blocklist -------------------------------------------

    def mark_applied(
        self, fingerprint: str, *, now: str, channel: str = "", cv_path: str = ""
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO applications(fingerprint, applied_at, channel, cv_path)"
                " VALUES (?,?,?,?)",
                (fingerprint, now, channel, cv_path),
            )

    def has_applied(self, fingerprint: str) -> bool:
        """Whether this role was applied to, under any of its fingerprints.

        Asked of the cluster and not of the one row, for the same reason
        `cluster_first_seen` reads the cluster: after the resolver links the
        alljobs and the gotfriends copy of a role, they are one job, and the
        blocklist that only knows the fingerprint the human happened to apply
        through offers the other copy back the next morning.
        """
        group = self.merged_with(fingerprint)
        placeholders = ",".join("?" for _ in group)
        row = self.conn.execute(
            f"SELECT 1 FROM applications WHERE fingerprint IN ({placeholders})",
            tuple(group),
        ).fetchone()
        return row is not None

    # -- decisions --------------------------------------------------------

    def record_decision(
        self,
        *,
        run_id: str,
        fingerprint: str,
        stage: str,
        verdict: str,
        now: str,
        score: float | None = None,
        reason: str = "",
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO decisions(run_id, fingerprint, stage, verdict, score, reason,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (run_id, fingerprint, stage, verdict, score, reason, now),
            )
        return int(cur.lastrowid)

    def decisions_for(self, fingerprint: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM decisions WHERE fingerprint = ? ORDER BY id", (fingerprint,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- approved CV bases (session 2 output) ------------------------------

    def put_cv_base(
        self, family: str, language: str, path: str, sha256: str, approved_at: str
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO cv_bases(family, language, path, sha256, approved_at)"
                " VALUES (?,?,?,?,?)",
                (family, language, path, sha256, approved_at),
            )

    def cv_base(self, family: str, language: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM cv_bases WHERE family = ? AND language = ?", (family, language)
        ).fetchone()
        return dict(row) if row else None

    # -- duplicate links (the resolver's output) ---------------------------

    def record_link(
        self,
        left: str,
        right: str,
        *,
        score: float,
        band: str,
        method: str,
        now: str,
    ) -> None:
        """Store one pair verdict. The pair is ordered so it is written once.

        Every verdict is kept, not only the merges. A pair the arithmetic called
        distinct is the evidence that it was looked at, and a pair a model was
        paid for is the line item that makes the escalation rate measurable.
        """
        a, b = (left, right) if left <= right else (right, left)
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO duplicate_links(left_fp, right_fp, score, band,"
                " method, decided_at) VALUES (?,?,?,?,?,?)",
                (a, b, score, band, method, now),
            )

    def links(self, band: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM duplicate_links"
        params: tuple[Any, ...] = ()
        if band is not None:
            sql += " WHERE band = ?"
            params = (band,)
        rows = self.conn.execute(sql + " ORDER BY score DESC", params).fetchall()
        return [dict(r) for r in rows]

    def merged_with(self, fingerprint: str) -> list[str]:
        """Every fingerprint the resolver merged with this one, transitively.

        Includes the fingerprint itself, so a role that matched nothing returns
        a list of one and a caller needs no special case for it.
        """
        from ..resolve.resolver import cluster

        pairs = [(r["left_fp"], r["right_fp"]) for r in self.links("duplicate")]
        keys = {k for pair in pairs for k in pair} | {fingerprint}
        for group in cluster(keys, pairs):
            if fingerprint in group:
                return group
        return [fingerprint]

    def first_seen(self, fingerprint: str) -> str | None:
        row = self.conn.execute(
            "SELECT first_seen_at FROM fingerprints WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return str(row[0]) if row else None

    def cluster_first_seen(self, fingerprint: str) -> str | None:
        """The earliest first-seen across everything merged with this role.

        A role is as old as the first time anyone showed it to us. Without this,
        a job that has sat on one board for three weeks looks new the day an
        agency relists it, and the freshness gate passes it every time it moves
        between sites. Freshness reads this, never the raw per-fingerprint date.
        """
        stamps = [s for s in (self.first_seen(f) for f in self.merged_with(fingerprint)) if s]
        return min(stamps) if stamps else None

    # -- the gold set ------------------------------------------------------

    def put_label(
        self, fingerprint: str, label: str, *, stratum: str, now: str, note: str = ""
    ) -> None:
        """Record one human verdict. Re-labelling the same posting replaces it."""
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO labels(fingerprint, label, stratum, labelled_at, note)"
                " VALUES (?,?,?,?,?)",
                (fingerprint, label, stratum, now, note),
            )

    def labels(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM labels").fetchall()
        return {r["fingerprint"]: dict(r) for r in rows}

    def is_labelled(self, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM labels WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    # -- what the analyst concluded ---------------------------------------
    #
    # One row per posting and not one per run. A re-analysis replaces the old
    # verdict rather than accumulating a history: the history that matters is
    # the trace, which records which prompt version produced which answer, and
    # a second copy here would only be a second thing to keep in agreement.

    def put_analysis(
        self,
        fingerprint: str,
        payload: str,
        *,
        family: str,
        score: float | None,
        channel: str,
        rationale: str,
        stopped_at: str,
        now: str,
        run_id: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO analyses(fingerprint, run_id, family, score, channel,"
                " rationale, stopped_at, payload, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    fingerprint,
                    run_id,
                    family,
                    score,
                    channel,
                    rationale,
                    stopped_at,
                    payload,
                    now,
                ),
            )

    def get_analysis(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM analyses WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def analyses(self, *, min_score: float | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM analyses"
        args: tuple[Any, ...] = ()
        if min_score is not None:
            sql += " WHERE score >= ?"
            args = (min_score,)
        sql += " ORDER BY score DESC"
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def unanalysed_postings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Postings with no analysis row yet, newest first.

        Duplicates are excluded here rather than in the analyst: a cluster the
        resolver already merged is one role, and paying to score it twice is
        the exact cost the resolver exists to avoid.
        """
        rows = self.conn.execute(
            "SELECT p.* FROM postings p"
            " LEFT JOIN analyses a ON a.fingerprint = p.fingerprint"
            " WHERE a.fingerprint IS NULL"
            " ORDER BY COALESCE(p.posted_at, p.fetched_at) DESC",
        ).fetchall()
        # One row per fingerprint, and the ordering above decides which one
        # survives: a board that prints a date wins over one that does not.
        # Returning both copies meant `desk analyze --all` paid for a full
        # extract-and-score chain twice on the same role and stored the second
        # answer over the first — 72 of them in the live store.
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for row in rows:
            if len(out) >= limit:
                break
            fingerprint = str(row["fingerprint"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            out.append(dict(row))
        return out

    # -- where each posting stands ----------------------------------------

    def set_state(
        self,
        fingerprint: str,
        state: str,
        *,
        now: str,
        due_at: str | None = None,
        note: str = "",
        source: str = "system",
    ) -> str | None:
        """Move a posting to a state and record the move. Returns the old state.

        The event log is append-only and the current state is a cache of its
        last row. Both are written in one transaction so they cannot disagree.
        """
        with self.tx() as c:
            row = c.execute(
                "SELECT state FROM pipeline_state WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            previous = row["state"] if row else None
            c.execute(
                "INSERT OR REPLACE INTO pipeline_state(fingerprint, state, updated_at, due_at,"
                " note) VALUES (?,?,?,?,?)",
                (fingerprint, state, now, due_at, note),
            )
            c.execute(
                "INSERT INTO state_events(fingerprint, from_state, to_state, at, source, note)"
                " VALUES (?,?,?,?,?,?)",
                (fingerprint, previous, state, now, source, note),
            )
        return previous

    def forget(self, fingerprint: str, *, only_site: str) -> bool:
        """Erase a posting and everything hanging off it. Refuses any other site.

        The one destructive method in this class, and it exists for one reason:
        a posting minted from a line of a hand-kept file is only as stable as
        that line. Correct a job title in the tracker and the content
        fingerprint changes, so the next import writes a second identity and
        the first one stays behind in `applied` forever — a duplicate the
        digest suppresses and the counts still carry.

        `only_site` is required and checked against the row rather than trusted,
        so a caller that passes the wrong fingerprint deletes nothing instead of
        deleting a scraped posting and the history attached to it.
        """
        row = self.conn.execute(
            "SELECT site FROM postings WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None or row["site"] != only_site:
            return False
        with self.tx() as c:
            for table in ("pipeline_state", "state_events", "applications", "decisions"):
                c.execute(f"DELETE FROM {table} WHERE fingerprint = ?", (fingerprint,))
            c.execute(
                "DELETE FROM duplicate_links WHERE left_fp = ? OR right_fp = ?",
                (fingerprint, fingerprint),
            )
            c.execute("DELETE FROM postings WHERE fingerprint = ?", (fingerprint,))
            c.execute("DELETE FROM fingerprints WHERE fingerprint = ?", (fingerprint,))
        return True

    def postings_from(self, site: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM postings WHERE site = ?", (site,)).fetchall()
        return [dict(r) for r in rows]

    def set_due_at(self, fingerprint: str, due_at: str | None) -> bool:
        """Correct the follow-up date without inventing a transition.

        The clock is derived from when the application went out, not from a
        move, so a row whose state is already right but whose date is missing
        needs the date fixed and nothing else. Writing it through `set_state`
        would append a state event saying the item moved from `applied` to
        `applied` — a thing that never happened, in the log that exists to say
        what did. Returns whether anything changed.
        """
        with self.tx() as c:
            row = c.execute(
                "SELECT due_at FROM pipeline_state WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row is None or row["due_at"] == due_at:
                return False
            c.execute(
                "UPDATE pipeline_state SET due_at = ? WHERE fingerprint = ?", (due_at, fingerprint)
            )
        return True

    def cursor(self, channel: str) -> str:
        """How far this channel has been read. Empty string when never read.

        One row per channel rather than a file next to the database, because
        losing it and losing the store should be the same event: a cursor that
        survives a restored backup would skip every button pressed in between,
        and a press that is skipped is a decision of Noam's that vanished.
        """
        row = self.conn.execute(
            "SELECT position FROM channel_cursor WHERE channel = ?", (channel,)
        ).fetchone()
        return str(row["position"]) if row else ""

    def set_cursor(self, channel: str, position: str, *, now: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO channel_cursor(channel, position, updated_at)"
                " VALUES (?,?,?)",
                (channel, str(position), now),
            )

    def state(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pipeline_state WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def in_state(self, state: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM pipeline_state WHERE state = ? ORDER BY updated_at DESC", (state,)
        ).fetchall()
        return [dict(r) for r in rows]

    def due_before(self, when: str) -> list[dict[str, Any]]:
        """Everything past its follow-up date, carrying who and what it is.

        The join is left, and that is the whole point of writing it out: a
        follow-up row is keyed by fingerprint and the posting it refers to may
        have been imported by hand or scraped from a board that has since
        dropped it. A missing posting must not remove the reminder — it removes
        only the name on it, which the renderer then says out loud.
        """
        rows = self.conn.execute(
            "SELECT s.*, p.title AS title, p.company AS company"
            " FROM pipeline_state s LEFT JOIN postings p ON p.fingerprint = s.fingerprint"
            " WHERE s.due_at IS NOT NULL AND s.due_at <= ?"
            " ORDER BY s.due_at",
            (when,),
        ).fetchall()
        return [dict(r) for r in rows]

    def state_history(self, fingerprint: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM state_events WHERE fingerprint = ? ORDER BY id", (fingerprint,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- the tailored document --------------------------------------------

    def put_tailored(
        self,
        fingerprint: str,
        *,
        family: str,
        language: str,
        base_sha256: str,
        path: str,
        changes: str,
        now: str,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO tailored(fingerprint, family, language, base_sha256,"
                " path, changes, created_at) VALUES (?,?,?,?,?,?,?)",
                (fingerprint, family, language, base_sha256, path, changes, now),
            )

    def tailored(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM tailored WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        tables = (
            "runs",
            "fingerprints",
            "postings",
            "applications",
            "decisions",
            "duplicate_links",
            "cv_bases",
            "labels",
            "analyses",
            "pipeline_state",
            "tailored",
        )
        return {
            t: int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables
        }
