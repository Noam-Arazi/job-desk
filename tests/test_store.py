from __future__ import annotations

from desk.gates import Candidate
from desk.store import Posting, Store

NOW = "2026-01-01T00:00:00+00:00"


def make_store() -> Store:
    return Store(":memory:")


def test_the_same_role_from_two_sites_is_one_fingerprint():
    store = make_store()
    a = Posting(site="alljobs", external_id="1", title="Data Analyst", company="Bluewick")
    b = Posting(site="drushim", external_id="9", title="  data   analyst ", company="bluewick")
    assert a.fingerprint == b.fingerprint

    assert store.upsert_posting(a, now=NOW) is True
    assert store.upsert_posting(b, now=NOW) is False  # not new; the role was already known
    assert len(store.duplicates_of(a.fingerprint)) == 2
    assert store.counts()["fingerprints"] == 1


def test_rerunning_a_day_is_idempotent():
    store = make_store()
    posting = Posting(site="alljobs", external_id="1", title="Analyst", company="Bluewick")
    for _ in range(3):
        store.upsert_posting(posting, now=NOW)
    assert store.counts()["postings"] == 1
    assert store.counts()["fingerprints"] == 1


def test_the_applied_blocklist_suppresses_a_role():
    store = make_store()
    posting = Posting(site="alljobs", external_id="1", title="Analyst", company="Bluewick")
    store.upsert_posting(posting, now=NOW)
    assert [p["fingerprint"] for p in store.unseen_postings()] == [posting.fingerprint]

    store.mark_applied(posting.fingerprint, now=NOW)
    assert store.has_applied(posting.fingerprint) is True
    assert store.unseen_postings() == []


def test_the_blocklist_suppresses_the_role_not_the_listing():
    """Applied through AllJobs means the Drushim copy is suppressed too."""
    store = make_store()
    a = Posting(site="alljobs", external_id="1", title="Analyst", company="Bluewick")
    b = Posting(site="drushim", external_id="2", title="Analyst", company="Bluewick")
    store.upsert_posting(a, now=NOW)
    store.upsert_posting(b, now=NOW)
    store.mark_applied(a.fingerprint, now=NOW)
    assert store.unseen_postings() == []


def test_decisions_accumulate_per_role():
    store = make_store()
    store.record_decision(
        run_id="r1", fingerprint="fp", stage="gates", verdict="pass", now=NOW, score=0.8
    )
    store.record_decision(
        run_id="r1", fingerprint="fp", stage="fit_score", verdict="hold", now=NOW, score=0.55
    )
    decisions = store.decisions_for("fp")
    assert [d["stage"] for d in decisions] == ["gates", "fit_score"]
    assert decisions[1]["score"] == 0.55


def test_cv_bases_are_hash_pinned():
    store = make_store()
    store.put_cv_base("ai_builder", "he", "/x/base.md", "deadbeef", NOW)
    assert store.cv_base("ai_builder", "he")["sha256"] == "deadbeef"
    assert store.cv_base("ai_builder", "en") is None


def test_the_board_s_own_experience_line_survives_the_store() -> None:
    """Drushim states required experience as a field; it was dropped on the way in.

    That field is the one place in this system where a gate is handed a stated
    answer instead of having to read one out of prose, and the store had no
    column for it — so the seniority gate fell back to the body for every one of
    those postings.
    """
    store = Store()
    store.upsert_posting(
        Posting(
            site="drushim",
            external_id="1",
            title="אנליסט/ית",
            company="סונול",
            location="נתניה",
            stated_experience="1-2 שנים",
        ),
        now="2026-08-19T09:00:00",
    )
    row = store.all_postings()[0]
    assert row["stated_experience"] == "1-2 שנים"
    assert Candidate.from_row(row).stated_experience == "1-2 שנים"
    store.close()


def test_a_store_written_before_that_column_existed_still_opens() -> None:
    """The corpus is not something anybody wants to rebuild by re-scraping.

    `CREATE TABLE IF NOT EXISTS` is silent about a table that already exists
    with the wrong shape, so a column added to the schema never reaches a
    database that predates it.
    """
    import sqlite3
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "old.sqlite"
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE fingerprints (fingerprint TEXT PRIMARY KEY, first_seen_at TEXT NOT NULL,
                first_seen_run TEXT, times_seen INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE postings (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL,
                site TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT, title TEXT NOT NULL,
                company TEXT NOT NULL, location TEXT, body TEXT, posted_at TEXT,
                fetched_at TEXT NOT NULL, run_id TEXT, UNIQUE (site, external_id));
            """
        )
        legacy.commit()
        legacy.close()

        store = Store(path)
        store.upsert_posting(
            Posting(
                site="drushim",
                external_id="1",
                title="t",
                company="c",
                stated_experience="3-5 שנים",
            ),
            now="2026-08-19T09:00:00",
        )
        assert store.all_postings()[0]["stated_experience"] == "3-5 שנים"
        store.close()


def _linked(store: Store) -> tuple[str, str]:
    """Two rows for one role, merged by the resolver, as happens across boards."""
    left = Posting(
        site="alljobs",
        external_id="1",
        title="אנליסט/ית",
        company="סונול",
        location="נתניה",
        posted_at="2026-08-18T09:00:00",
    )
    right = Posting(
        site="drushim",
        external_id="2",
        title="דרוש/ה אנליסט/ית",
        company="",
        location="נתניה",
        posted_at="2026-08-17T09:00:00",
    )
    store.upsert_posting(left, now="2026-08-18T10:00:00")
    store.upsert_posting(right, now="2026-08-18T10:00:00")
    store.record_link(
        left.fingerprint,
        right.fingerprint,
        score=0.9,
        band="duplicate",
        method="content",
        now="2026-08-18T10:00:00",
    )
    return left.fingerprint, right.fingerprint


def test_applying_through_one_board_blocks_the_role_on_the_other() -> None:
    """The resolver merged them, so they are one job — and one application.

    Keyed on the raw fingerprint, the blocklist knew only the board the human
    happened to apply through, and offered the same role back the next morning
    under its twin. That is precisely the failure the resolver exists to
    prevent.
    """
    store = Store()
    left, right = _linked(store)
    store.mark_applied(left, now="2026-08-18T11:00:00")

    assert store.has_applied(left) is True
    assert store.has_applied(right) is True
    assert [row["fingerprint"] for row in store.unseen_postings()] == []
    store.close()


def test_unseen_postings_is_newest_by_the_date_the_board_printed() -> None:
    """ "Newest first" ordered by rowid, which is crawl order and not time."""
    store = Store()
    for index, (external, posted) in enumerate(
        [("a", "2026-08-01T09:00:00"), ("b", "2026-08-18T09:00:00"), ("c", "2026-08-10T09:00:00")]
    ):
        store.upsert_posting(
            Posting(
                site="alljobs",
                external_id=external,
                title=f"role {index}",
                company=f"company {index}",
                posted_at=posted,
            ),
            now="2026-08-18T10:00:00",
        )
    assert [row["external_id"] for row in store.unseen_postings(2)] == ["b", "c"]
    store.close()


def test_one_role_is_analysed_once_however_many_boards_carry_it() -> None:
    """Two rows, one fingerprint: the second analysis buys nothing and costs a
    full extract-and-score chain at judgment tier."""
    store = Store()
    store.upsert_posting(
        Posting(
            site="alljobs",
            external_id="1",
            title="אנליסט/ית",
            company="סונול",
            location="נתניה",
            posted_at="2026-08-18T09:00:00",
        ),
        now="2026-08-18T10:00:00",
    )
    store.upsert_posting(
        Posting(
            site="drushim",
            external_id="2",
            title="אנליסט/ית",
            company="סונול",
            location="נתניה",
            posted_at="",
        ),
        now="2026-08-18T10:00:00",
    )
    rows = store.unanalysed_postings()
    assert len(rows) == 1
    assert rows[0]["site"] == "alljobs", "the copy that carries a date wins"
    store.close()


def test_a_card_only_crawl_never_erases_a_stored_body() -> None:
    """28.10.2026: every LinkedIn crawl brings most postings back as a card
    only, and the upsert used to write that empty body over yesterday's."""
    store = Store()
    full = Posting(
        site="linkedin",
        external_id="7",
        title="AI Builder",
        company="Bluewick",
        body="Build agents.",
    )
    card = Posting(site="linkedin", external_id="7", title="AI Builder", company="Bluewick")
    store.upsert_posting(full, now="2026-10-27T06:00:00")
    store.upsert_posting(card, now="2026-10-28T06:00:00")

    assert store.get_posting(full.fingerprint)["body"] == "Build agents."


def test_an_empty_board_date_does_not_sort_a_posting_last() -> None:
    """01.11.2026: Jobify and GotFriends store posted_at = '', and COALESCE
    keeps '' — every one of them sorted below every dated posting and never
    fit inside the daily limit. 332 of 361 Jobify postings were never read."""
    store = Store()
    store.upsert_posting(
        Posting(site="alljobs", external_id="old", title="a", company="x",
                posted_at="2026-10-20"),
        now="2026-10-20T06:00:00",
    )
    store.upsert_posting(
        Posting(site="jobify", external_id="new", title="b", company="y", posted_at=""),
        now="2026-10-31T06:00:00",
    )
    assert [r["site"] for r in store.unanalysed_postings(1)] == ["jobify"]
    store.close()


def test_priority_sites_are_read_before_newer_postings_elsewhere() -> None:
    store = Store()
    store.upsert_posting(
        Posting(site="jobify", external_id="j", title="a", company="x", posted_at=""),
        now="2026-10-20T06:00:00",
    )
    store.upsert_posting(
        Posting(site="alljobs", external_id="a", title="b", company="y",
                posted_at="2026-10-31"),
        now="2026-10-31T06:00:00",
    )
    assert [r["site"] for r in store.unanalysed_postings(2)] == ["alljobs", "jobify"]
    assert [r["site"] for r in store.unanalysed_postings(2, first=("jobify",))] == [
        "jobify", "alljobs"]
    assert len(store.unanalysed_postings(None)) == 2
    store.close()


# --- the blocklist by role, not only by fingerprint -------------------------


def _seen(store, site, title, company, location=""):
    p = Posting(
        site=site,
        external_id=f"{site}-{title}-{company}",
        title=title,
        company=company,
        location=location,
        posted_at="2026-10-30",
    )
    store.upsert_posting(p, now="2026-10-30T06:00:00")
    return p.fingerprint


def test_applied_through_the_tracker_blocks_the_scraped_copy(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    typed = _seen(store, "manual", "AI Specialist", "BDO")
    scraped = _seen(store, "linkedin", "AI Specialist", "BDO Israel", "Tel Aviv District, Israel")
    assert typed != scraped
    store.mark_applied(typed, now="2026-08-16")

    assert store.has_applied(scraped)
    store.close()


def test_another_role_at_the_same_employer_is_not_blocked(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    brm = _seen(store, "manual", "Strategic Project Manager - BRM (משרה 5994)", "Nogamy")
    store.mark_applied(brm, now="2026-11-01")

    title = "Strategic Project Manager / Strategy & Planning Analyst"
    same = _seen(store, "linkedin", title, "Nogamy")
    assert store.has_applied(same)
    assert not store.has_applied(_seen(store, "linkedin", "Project Manager", "Nogamy"))
    assert not store.has_applied(_seen(store, "linkedin", "AI Product Manager", "Nogamy"))
    store.close()


def test_an_anonymous_employer_never_matches_on_name(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    haifa = _seen(store, "alljobs", "Data Analyst", "חברה חסויה", "חיפה")
    store.mark_applied(haifa, now="2026-10-01")

    assert not store.has_applied(_seen(store, "alljobs", "Data Analyst", "חברה חסויה", "תל אביב"))
    store.close()


def test_hebrew_gender_endings_are_not_title_separators():
    from desk.store.fingerprint import role_key

    title = "מפתח/ת AI ג׳וניור/ית — אגף מערכות מידע"
    assert role_key(title, "איכילוב") == ("איכילוב", "מפתח ai ג׳וניור")


def test_a_bracketed_alias_in_the_tracker_matches_either_name(tmp_path):
    store = Store(tmp_path / "d.sqlite")
    title = "מנהל/ת פרויקטים טכנולוגיים במערכות מידע"
    tracked = _seen(store, "manual", title, "פרוסיד (Proceed)", "ירושלים / מודיעין / חולון")
    store.mark_applied(tracked, now="2026-11-03")

    spaced = "מנהל /ת פרויקטים טכנולוגיים במערכות מידע"
    assert store.has_applied(_seen(store, "alljobs", spaced, "פרוסיד", "חולון"))
    assert store.has_applied(_seen(store, "drushim", title, "Proceed‏", "חולון"))
    other = _seen(store, "alljobs", "PMO - מתאם /ת פרויקטים", "פרוסיד", "ירושלים")
    assert not store.has_applied(other)
    store.close()


def test_a_bracket_never_rescues_an_anonymous_employer(tmp_path):
    from desk.store.fingerprint import role_keys

    assert role_keys("אנליסט/ית פיננסי/ת", "סיאל גיוס והשמה (לקוח חסוי)") == frozenset()
