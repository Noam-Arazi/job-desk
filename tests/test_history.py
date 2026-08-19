"""Importing the hand-kept application tracker into the manager.

The file being read is the only record of what Noam actually did — which jobs
he applied to, which came back a no, which one produced an interview. So the
tests here are mostly about what the import refuses to do to it: no invented
dates, no guessed statuses, no second row on a re-run, and no silent overwrite
of a state the manager already holds.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

import pytest

from desk import history
from desk.config import load_spec
from desk.manager import states
from desk.store import Posting, Store

SPEC = load_spec()
NOW = datetime(2026, 8, 19, 12, 0, 0)

HEADER = ("date", "company", "role", "location", "source", "via", "status", "note")

ROWS = [
    ("2026-08-14", "Classiq", "AI Developer Builder", "תל אביב", "LinkedIn", "", "closed", "נדחה"),
    ("2026-08-16", "BDO", "AI Specialist", "", "LinkedIn", "", "applied", ""),
    ("", "הטכניון", "רכז/ת איסוף ועיבוד נתונים", "חיפה", "", "", "interview", "ראיון 09/09"),
]


def write_csv(path: Path, rows=ROWS) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER)
        writer.writerows(rows)
    return path


@pytest.fixture
def tracker(tmp_path) -> Path:
    return write_csv(tmp_path / "הגשות.csv")


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "desk.sqlite")
    s.start_run("import-test", NOW.isoformat(timespec="seconds"), "import", int(SPEC["version"]))
    yield s
    s.close()


def load(store: Store, tracker: Path) -> history.Result:
    rows = history.plan(history.read(tracker), store)
    return history.apply(rows, store, spec=SPEC, now=NOW)


# --- reading --------------------------------------------------------------


def test_reading_keeps_every_row_that_names_a_job(tracker):
    entries = history.read(tracker)
    assert len(entries) == 3
    assert entries[0].company == "Classiq"
    assert entries[2].date == ""


def test_a_row_naming_no_job_at_all_is_not_a_row(tmp_path):
    path = write_csv(tmp_path / "t.csv", ROWS + [("", "", "", "", "", "", "applied", "junk")])
    assert len(history.read(path)) == 3


def test_a_missing_column_is_an_error_not_a_default(tmp_path):
    path = tmp_path / "short.csv"
    path.write_text("company,role\nAcme,Analyst\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing columns"):
        history.read(path)


def test_a_status_the_manager_has_no_state_for_is_refused(tmp_path):
    path = write_csv(tmp_path / "t.csv", [("", "Acme", "Analyst", "", "", "", "ghosted", "")])
    entry = history.read(path)[0]
    with pytest.raises(history.UnknownStatus, match="ghosted"):
        assert entry.state


# --- writing --------------------------------------------------------------


def test_every_row_lands_in_the_state_the_tracker_records(store, tracker):
    result = load(store, tracker)

    assert result.summary()["written"] == 3
    fps = {e.company: e.fingerprint for e in history.read(tracker)}
    assert states.current(store, fps["Classiq"]) == "closed"
    assert states.current(store, fps["BDO"]) == "applied"
    assert states.current(store, fps["הטכניון"]) == "interview"


def test_every_imported_row_counts_as_an_application(store, tracker):
    """Including the closed ones. A rejection is the answer to an application."""
    load(store, tracker)
    for entry in history.read(tracker):
        assert store.has_applied(entry.fingerprint), entry.company


def test_an_undated_row_records_no_date(store, tracker):
    """Today would be a lie, and every later reading of the silence inherits it."""
    load(store, tracker)
    technion = next(e for e in history.read(tracker) if e.company == "הטכניון")
    row = store.conn.execute(
        "SELECT applied_at FROM applications WHERE fingerprint = ?", (technion.fingerprint,)
    ).fetchone()
    assert row["applied_at"] == ""


def test_the_channel_the_tracker_names_is_kept(store, tracker):
    load(store, tracker)
    bdo = next(e for e in history.read(tracker) if e.company == "BDO")
    row = store.conn.execute(
        "SELECT channel FROM applications WHERE fingerprint = ?", (bdo.fingerprint,)
    ).fetchone()
    assert row["channel"] == "LinkedIn"


def test_an_unmatched_row_becomes_a_posting_rather_than_a_dangling_id(store, tracker):
    load(store, tracker)
    for entry in history.read(tracker):
        posting = store.get_posting(entry.fingerprint)
        assert posting is not None, entry.company
        assert posting["site"] == history.SITE


def test_a_row_naming_a_job_already_scraped_attaches_to_it(store, tracker):
    """The identity is the store's own fingerprint, so this needs no matching rule."""
    scraped = Posting(
        site="alljobs",
        external_id="8788888",
        title="AI Specialist",
        company="BDO",
        location="",
        body="the board's own text",
    )
    store.upsert_posting(scraped, now="2026-08-16T09:00:00", run_id="fetch")

    rows = history.plan(history.read(tracker), store)
    bdo = next(r for r in rows if r.entry.company == "BDO")
    assert bdo.known and bdo.fingerprint == scraped.fingerprint

    history.apply(rows, store, spec=SPEC, now=NOW)
    posting = store.get_posting(scraped.fingerprint)
    assert posting["site"] == "alljobs", "the import overwrote a scraped posting"
    assert posting["body"] == "the board's own text"


def test_importing_twice_changes_nothing_the_second_time(store, tracker):
    first = load(store, tracker)
    second = load(store, tracker)

    assert first.summary()["written"] == 3
    assert second.summary() == {
        "written": 0,
        "skipped": 3,
        "refused": 0,
        "reclocked": 0,
        "matched_existing": 0,
        "created_manual": 0,
    }
    assert len(store.all_postings()) == 3
    assert len(store.state_history(history.read(tracker)[1].fingerprint)) == 1


def test_a_rerun_fixes_a_missing_clock_without_inventing_a_move(store, tracker):
    """The clock is derived from the application, so correcting it is not a transition."""
    load(store, tracker)
    bdo = next(e for e in history.read(tracker) if e.company == "BDO")
    store.set_due_at(bdo.fingerprint, None)

    result = load(store, tracker)

    assert result.summary()["reclocked"] == 1
    assert store.state(bdo.fingerprint)["due_at"].startswith("2026-08-23")
    assert len(store.state_history(bdo.fingerprint)) == 1, "a no-op move was logged"


def test_editing_a_tracker_row_abandons_the_posting_the_old_text_minted(store, tmp_path):
    """The failure this exists to catch: a corrected title becomes a second row."""
    tracker = write_csv(tmp_path / "t.csv", [ROWS[1]])
    load(store, tracker)
    assert len(store.postings_from(history.SITE)) == 1

    edited = ("2026-08-16", "BDO", "AI Specialist II") + ROWS[1][3:]
    corrected = write_csv(tmp_path / "t.csv", [edited])
    entries = history.read(corrected)
    history.apply(history.plan(entries, store), store, spec=SPEC, now=NOW)

    assert len(store.postings_from(history.SITE)) == 2, "the edit did not mint a second identity"
    stale = history.orphans(entries, store)
    assert len(stale) == 1 and stale[0]["title"] == "AI Specialist"

    assert history.prune(stale, store) == 1
    assert len(store.postings_from(history.SITE)) == 1
    assert not store.has_applied(stale[0]["fingerprint"])
    assert store.state(stale[0]["fingerprint"]) is None


# --- the job descriptions -------------------------------------------------


def descriptions_dir(tmp_path: Path, **files: str) -> Path:
    folder = tmp_path / history.DESCRIPTIONS
    folder.mkdir(exist_ok=True)
    for stem, text in files.items():
        (folder / f"{stem}.md").write_text(text, encoding="utf-8")
    return folder


def test_a_saved_description_becomes_the_posting_body(store, tracker, tmp_path):
    """Without it the resolver has a title and nothing else to compare."""
    folder = descriptions_dir(tmp_path, BDO="Looking for an AI Specialist. Python, RAG, Azure.")
    saved = history.descriptions(folder)

    rows = history.plan(history.read(tracker), store, saved)
    history.apply(rows, store, spec=SPEC, now=NOW)

    bdo = next(e for e in history.read(tracker) if e.company == "BDO")
    assert "RAG" in store.get_posting(bdo.fingerprint)["body"]


def test_the_role_specific_file_wins_over_the_company_one(store, tmp_path):
    """abra is in the tracker three times, for three different roles."""
    rows = [
        ("2026-08-07", "abra", "AI Consultant", "", "", "", "applied", ""),
        ("2026-08-07", "abra", "AI Adoption", "", "", "", "applied", ""),
    ]
    tracker = write_csv(tmp_path / "t.csv", rows)
    folder = descriptions_dir(
        tmp_path,
        **{"abra": "the generic one", "abra - AI Adoption": "the adoption one"},
    )
    saved = history.descriptions(folder)

    planned = history.plan(history.read(tracker), store, saved)
    bodies = {r.entry.role: r.body for r in planned}
    assert bodies["AI Adoption"] == "the adoption one"
    assert bodies["AI Consultant"] == "the generic one"


def test_a_description_saved_later_reaches_a_row_imported_earlier(store, tracker, tmp_path):
    """The row that most needs a body is the one already in the store."""
    load(store, tracker)
    bdo = next(e for e in history.read(tracker) if e.company == "BDO")
    assert "RAG" not in (store.get_posting(bdo.fingerprint)["body"] or "")

    saved = history.descriptions(descriptions_dir(tmp_path, BDO="Python, RAG, Azure."))
    history.apply(history.plan(history.read(tracker), store, saved), store, spec=SPEC, now=NOW)

    assert "RAG" in store.get_posting(bdo.fingerprint)["body"]


def test_a_description_never_overwrites_a_scraped_body(store, tracker, tmp_path):
    scraped = Posting(
        site="alljobs",
        external_id="8788888",
        title="AI Specialist",
        company="BDO",
        body="the board's own text",
    )
    store.upsert_posting(scraped, now="2026-08-16T09:00:00", run_id="fetch")

    saved = history.descriptions(descriptions_dir(tmp_path, BDO="pasted by hand"))
    history.apply(history.plan(history.read(tracker), store, saved), store, spec=SPEC, now=NOW)

    posting = store.get_posting(scraped.fingerprint)
    assert posting["body"] == "the board's own text" and posting["site"] == "alljobs"


def test_no_descriptions_folder_is_not_an_error(store, tmp_path):
    assert history.descriptions(tmp_path / "nothing here") == {}


def test_pruning_refuses_to_touch_a_scraped_posting(store):
    """`forget` is the one destructive path here, so it checks rather than trusts."""
    scraped = Posting(site="alljobs", external_id="1", title="Data Analyst", company="Acme")
    store.upsert_posting(scraped, now="2026-08-01T00:00:00", run_id="fetch")

    assert history.prune([{"fingerprint": scraped.fingerprint}], store) == 0
    assert store.get_posting(scraped.fingerprint) is not None


def test_a_state_the_manager_would_not_allow_is_refused_and_reported(store, tracker):
    """`closed` is terminal. An import that walks one backwards says so out loud."""
    load(store, tracker)
    classiq = next(e for e in history.read(tracker) if e.company == "Classiq")

    reopened = history.Row(
        entry=classiq,
        fingerprint=classiq.fingerprint,
        state="interview",
        known=True,
        current="closed",
    )
    result = history.apply([reopened], store, spec=SPEC, now=NOW)

    assert not result.written and len(result.refused) == 1
    assert states.current(store, classiq.fingerprint) == "closed"


def test_the_history_records_that_the_move_came_from_the_import(store, tracker):
    load(store, tracker)
    bdo = next(e for e in history.read(tracker) if e.company == "BDO")
    events = store.state_history(bdo.fingerprint)
    assert events and events[-1]["source"] == "import"
    assert "imported from the tracker" in events[-1]["note"]


def test_the_follow_up_clock_counts_from_when_he_applied(store, tracker):
    """A three-week silence is not fresh because the import ran today."""
    load(store, tracker)
    bdo = next(e for e in history.read(tracker) if e.company == "BDO")

    due_at = store.state(bdo.fingerprint)["due_at"]
    assert due_at.startswith("2026-08-23"), "the clock restarted at the import"


def test_an_undated_application_gets_no_clock_at_all(store, tracker):
    """Nothing to count from, and counting from today is the invented date."""
    load(store, tracker)
    technion = next(e for e in history.read(tracker) if e.company == "הטכניון")
    assert not store.state(technion.fingerprint)["due_at"]


def test_only_the_state_that_waits_on_silence_carries_a_clock(store, tracker):
    load(store, tracker)
    closed = next(e for e in history.read(tracker) if e.company == "Classiq")
    assert not store.state(closed.fingerprint)["due_at"], "a closed row is still nudging"
