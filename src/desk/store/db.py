"""The store — the memory pattern, on sqlite.

It holds five things, and every one of them is state that has to survive between
runs rather than context budgeted inside one:

    postings        what has been seen, with its content fingerprint
    fingerprints    the cross-run dedup index; collapses a role seen twice
    applications    the applied-blocklist. Its only job is: never show this again
    decisions       what each stage concluded, so the calibration loop has ground
                    to stand on
    cv_bases        the approved bases from session 2, hash-pinned
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

CREATE TABLE IF NOT EXISTS cv_bases (
    family       TEXT NOT NULL,
    language     TEXT NOT NULL,
    path         TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    approved_at  TEXT NOT NULL,
    PRIMARY KEY (family, language)
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
                                     location, body, posted_at, fetched_at, run_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(site, external_id) DO UPDATE SET
                    fingerprint = excluded.fingerprint,
                    title       = excluded.title,
                    company     = excluded.company,
                    location    = excluded.location,
                    body        = excluded.body,
                    url         = excluded.url,
                    posted_at   = excluded.posted_at,
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

    def unseen_postings(self, limit: int = 50) -> list[dict[str, Any]]:
        """Distinct roles that have not been applied to, newest first."""
        rows = self.conn.execute(
            """
            SELECT p.* FROM postings p
            JOIN (SELECT fingerprint, MIN(id) AS id FROM postings GROUP BY fingerprint) d
              ON d.id = p.id
            WHERE p.fingerprint NOT IN (SELECT fingerprint FROM applications)
            ORDER BY p.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

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
        row = self.conn.execute(
            "SELECT 1 FROM applications WHERE fingerprint = ?", (fingerprint,)
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

    def counts(self) -> dict[str, int]:
        tables = ("runs", "fingerprints", "postings", "applications", "decisions", "cv_bases")
        return {
            t: int(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables
        }
