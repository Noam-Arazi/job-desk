"""What the deterministic gates have to keep being true.

The cases are not invented. Every shape here was measured against the live store
— 191 AllJobs rows and 178 GotFriends rows, read on 2026-08-18 — and each one is
a trap that a gate comparing raw strings falls into.

The `spec` fixture is the real `spec/search.yaml`, deep-copied so a test that
tightens a rule cannot leak it into the next one. Two of these gates were unable
to match anything at all until 2026-08-18, when the city lists and the Hebrew
degree spellings were promoted out of YAML comments into data — which is why
`test_every_region_the_spec_names_has_cities` exists rather than being obvious.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pytest

from desk.config import load_spec
from desk.gates import (
    Candidate,
    Verdict,
    applied,
    degree,
    freshness,
    geography,
    run_gates,
    seniority,
)
from desk.gates.chain import store_first_seen
from desk.store import Store

NOW = datetime(2026, 8, 18, 9, 0, 0)


@pytest.fixture
def spec() -> dict:
    return copy.deepcopy(load_spec())


def candidate(**overrides) -> Candidate:
    return Candidate(**{"site": "alljobs", "title": "אנליסט", **overrides})


def seniority_check(spec, *, title="אנליסט נתונים", body="", stated_experience=""):
    return seniority.check(spec=spec, title=title, body=body, stated_experience=stated_experience)


# --------------------------------------------------------------------------
# geography — the field is a list of places at least half the time
# --------------------------------------------------------------------------


def test_the_spec_without_city_data_places_nothing_and_blocks_nobody(spec) -> None:
    """Regions with no cities under them is a real state, not a parse failure.

    It was also the state the spec shipped in for two days, so the honest answer
    is `unknown` — never a silent pass that looks like a decision.
    """
    bare = copy.deepcopy(spec)
    bare["geography"].pop("cities")

    result = geography.check(spec=bare, location="חיפה")

    assert result.verdict is Verdict.UNKNOWN
    assert not result.blocks


def test_one_accepted_city_in_an_unpunctuated_run_is_enough(spec) -> None:
    """101 of 191 AllJobs rows read like this. A role offered in Haifa and in
    Beer Sheva is a role in Haifa."""
    result = geography.check(spec=spec, location="מספר מקומות באר שבע חיפה תל אביב")

    assert result.verdict is Verdict.PASS
    assert "haifa" in result.details["accepted_regions"]


def test_a_posting_only_in_excluded_regions_is_blocked(spec) -> None:
    result = geography.check(spec=spec, location="מספר מקומות באר שבע אשדוד ירושלים")

    assert result.verdict is Verdict.BLOCK
    assert "באר שבע" in result.evidence


def test_a_longer_city_name_is_not_double_counted_as_the_short_one_inside_it(spec) -> None:
    """ "קרית ביאליק" contains "קריות" nowhere, but it does contain "קרית" as a
    prefix of two other entries; the row names one city, not three."""
    result = geography.check(spec=spec, location="קרית ביאליק")

    assert result.details["cities"] == ["קרית ביאליק"]


def test_work_from_home_with_no_city_passes_as_remote(spec) -> None:
    """43 AllJobs rows name no city at all. The spec accepts Israel-based
    remote, and every enabled site is an Israeli board."""
    result = geography.check(spec=spec, location="עבודה מהבית", site="alljobs")

    assert result.verdict is Verdict.PASS
    assert result.details["remote"] is True


def test_the_abbreviation_the_boards_actually_write_is_a_city(spec) -> None:
    """167 of the 191 AllJobs rows say ת"א and never Tel Aviv. Without the
    abbreviation the commonest location string in the store places nowhere and
    the gate falls back to guessing from the body."""
    result = geography.check(spec=spec, location='ת"א והמרכז')

    assert result.verdict is Verdict.PASS
    assert result.details["read_from"] == "location"


def test_a_city_named_only_in_the_body_is_read_but_labelled_as_weaker(spec) -> None:
    result = geography.check(spec=spec, location="", body="המשרד שלנו ברעננה, צוות של 12 אנשים")

    assert result.verdict is Verdict.PASS
    assert result.details["read_from"] == "title and body"


def test_a_location_naming_nothing_recognisable_does_not_block(spec) -> None:
    result = geography.check(spec=spec, location="השרון והסביבה")

    assert result.verdict is Verdict.UNKNOWN
    assert not result.blocks


def test_every_region_the_spec_names_has_cities(spec) -> None:
    """The gate can only place a posting in a region it has names for.

    This holds whether the city data lives in the overlay above or in the spec,
    which is what keeps the two from drifting while the edit is pending.
    """
    named = set(spec["geography"]["regions"]) | set(spec["geography"]["exclude_regions"])
    have_cities = {region for region, cities in spec["geography"]["cities"].items() if cities}

    assert named <= have_cities


# --------------------------------------------------------------------------
# seniority — the board's own field first, prose only as a fallback
# --------------------------------------------------------------------------


def test_drushims_stated_field_is_read_without_a_proximity_test(spec) -> None:
    result = seniority_check(spec, stated_experience="4 שנים")

    assert result.verdict is Verdict.BLOCK
    assert result.details["source"] == "stated field"


def test_no_experience_required_is_a_stated_zero_and_not_an_absence(spec) -> None:
    """ "ללא נסיון" and an empty field both pass, but only one of them is a fact,
    and the digest has to be able to tell the human which it was."""
    result = seniority_check(spec, stated_experience="ללא נסיון")

    assert result.verdict is Verdict.PASS
    assert result.details["years"] == 0


def test_a_range_is_read_at_its_lower_bound_as_the_spec_says(spec) -> None:
    result = seniority_check(spec, body="דרוש ניסיון של 3-5 שנים בניתוח נתונים")

    assert result.verdict is Verdict.PASS
    assert result.details["years"] == 3


def test_the_range_rule_is_the_specs_to_change(spec) -> None:
    tightened = copy.deepcopy(spec)
    tightened["gates"]["seniority"]["range_rule"] = "use_upper_bound"

    result = seniority_check(tightened, body="דרוש ניסיון של 3-5 שנים")

    assert result.verdict is Verdict.BLOCK
    assert result.details["years"] == 5


def test_a_plus_figure_blocks_above_the_ceiling(spec) -> None:
    result = seniority_check(spec, body="At least 5+ years of experience in BI")

    assert result.verdict is Verdict.BLOCK


def test_two_years_written_as_one_hebrew_word_is_still_two_years(spec) -> None:
    """ "שנתיים" carries no digit, so every numeric pattern misses it."""
    result = seniority_check(spec, body="שנתיים ניסיון בעבודה מול בסיסי נתונים")

    assert result.details["years"] == 2


def test_twelve_years_of_schooling_is_a_high_school_diploma_not_a_seniority_bar(spec) -> None:
    """The loudest false block on the first run over the store. It is a year
    count, and it sits one comma away from the word for experience."""
    result = seniority_check(spec, body="12 שנות לימוד, בגרות מלאה. ניסיון קודם - יתרון")

    assert result.verdict is Verdict.UNKNOWN


def test_a_year_word_hiding_inside_another_word_is_not_a_year(spec) -> None:
    """Hebrew has no casing to fall back on, and "שש" sits inside "חשש". The
    store had a warehouse job blocked for demanding six years by a regex
    reading the word for "worry"."""
    result = seniority_check(spec, body="עבודה בגובה ושהות בשטח ללא חשש. עדיפות לבעלי ניסיון קודם")

    assert result.verdict is Verdict.UNKNOWN


def test_a_bare_word_number_is_not_a_year_count_without_its_unit(spec) -> None:
    """ "שלוש" is three of something; "שלוש שנים" is three years."""
    assert seniority_check(spec, body="ניסיון בשלוש מערכות מידע").verdict is Verdict.UNKNOWN
    assert seniority_check(spec, body="ניסיון של שלוש שנים").details["years"] == 3


def test_a_year_count_that_is_not_about_experience_is_not_a_requirement(spec) -> None:
    """The company's age is the commonest false block on this board."""
    result = seniority_check(spec, body="החברה פועלת 12 שנים ומעסיקה 300 עובדים")

    assert result.verdict is Verdict.UNKNOWN
    assert not result.blocks


def test_the_lowest_stated_figure_decides(spec) -> None:
    """A high figure almost always belongs to the nice-to-have half of a list,
    and the analyst reads the requirements properly in the next stage."""
    result = seniority_check(spec, body="ניסיון של 3 שנים ב-SQL, ניסיון של 6 שנים ב-BI")

    assert result.verdict is Verdict.PASS
    assert result.details["years"] == 3


def test_an_unstated_requirement_passes_because_the_spec_says_so(spec) -> None:
    result = seniority_check(spec, body="תיאור התפקיד: ניתוח נתונים וממשק מול לקוחות")

    assert result.verdict is Verdict.UNKNOWN
    assert not result.blocks


def test_the_ceiling_is_the_specs_to_raise(spec) -> None:
    loosened = copy.deepcopy(spec)
    loosened["gates"]["seniority"]["max_required_years"] = 6

    assert seniority_check(loosened, stated_experience="5 שנים").verdict is Verdict.PASS


# --------------------------------------------------------------------------
# degree — the list is closed until the posting says it is not
# --------------------------------------------------------------------------


def test_a_hebrew_closed_list_blocks(spec) -> None:
    """The spec's lists are English and every posting in the store is Hebrew.
    Without the aliases this gate matches nothing at all."""
    result = degree.check(spec=spec, body="דרישות: תואר ראשון במדעי המחשב או הנדסת תוכנה")

    assert result.verdict is Verdict.BLOCK
    assert "computer science" in result.details["closed_lists"]


def test_an_open_clause_cancels_the_list(spec) -> None:
    result = degree.check(spec=spec, body="תואר ראשון במדעי המחשב או תואר רלוונטי אחר")

    assert result.verdict is Verdict.PASS
    assert result.details["open_clause"] == "או תואר רלוונטי אחר"


def test_the_open_clause_the_store_actually_uses_cancels_the_list(spec) -> None:
    """Measured, not imagined: this exact wording was one of two false blocks
    the degree gate produced on its first run over the store."""
    result = degree.check(
        spec=spec, body="תואר במדעי המחשב/מתמטיקה/סטטיסטיקה/פיזיקה או תחום כמותי דומה"
    )

    assert result.verdict is Verdict.PASS


def test_a_field_named_without_a_diploma_demanded_is_not_a_requirement(spec) -> None:
    result = degree.check(spec=spec, body="הצוות עוסק בסטטיסטיקה יישומית ובמידול")

    assert not result.blocks


def test_a_degree_that_is_not_on_a_closed_list_passes_on_evidence(spec) -> None:
    result = degree.check(spec=spec, body="תואר ראשון במדעי החברה או במנהל עסקים")

    assert result.verdict is Verdict.PASS


def test_a_posting_that_asks_for_no_degree_is_unknown_not_passed(spec) -> None:
    result = degree.check(spec=spec, body="דרוש אנליסט עם יכולת עבודה עצמאית")

    assert result.verdict is Verdict.UNKNOWN


def test_a_list_written_without_spellings_is_still_a_list(spec) -> None:
    """The spec shipped its closed lists as bare English strings, and tightening
    one back to that shape is an edit a human might make. It has to keep working
    — it just stops matching the Hebrew, which is the state this gate was in
    until the spellings were promoted to data."""
    old_shape = copy.deepcopy(spec)
    old_shape["gates"]["degree"]["closed_lists"] = ["computer science", "statistics"]

    result = degree.check(spec=old_shape, body="B.Sc. in Computer Science required")

    assert result.verdict is Verdict.BLOCK


# --------------------------------------------------------------------------
# freshness — one board never states a date, and that is not staleness
# --------------------------------------------------------------------------


def test_a_stated_date_inside_the_window_passes(spec) -> None:
    result = freshness.check(spec=spec, now=NOW, posted_at=iso(NOW - timedelta(days=2)))

    assert result.verdict is Verdict.PASS
    assert result.details["basis"] == "posted_at"


def test_a_stated_date_outside_the_window_blocks(spec) -> None:
    result = freshness.check(spec=spec, now=NOW, posted_at=iso(NOW - timedelta(days=20)))

    assert result.verdict is Verdict.BLOCK


def test_an_empty_date_falls_back_to_the_store_and_says_so(spec) -> None:
    """All 178 GotFriends rows are permanently dateless, by the agency's design."""
    result = freshness.check(
        spec=spec, now=NOW, posted_at="", first_seen_at=iso(NOW - timedelta(days=1))
    )

    assert result.verdict is Verdict.PASS
    assert result.details["basis"] == "first seen in the store"


def test_an_empty_date_with_nothing_to_fall_back_on_never_blocks(spec) -> None:
    """A board that publishes no dates is not a board publishing stale jobs."""
    result = freshness.check(spec=spec, now=NOW, posted_at="", first_seen_at="")

    assert result.verdict is Verdict.UNKNOWN
    assert not result.blocks


def test_a_timestamp_that_will_not_parse_is_surfaced_and_not_acted_on(spec) -> None:
    result = freshness.check(spec=spec, now=NOW, posted_at="לפני שבועיים")

    assert result.verdict is Verdict.UNKNOWN
    assert result.evidence == "לפני שבועיים"


def test_the_first_run_backfill_widens_the_window(spec) -> None:
    twenty_days = iso(NOW - timedelta(days=20))

    assert freshness.check(spec=spec, now=NOW, posted_at=twenty_days).blocks
    assert not freshness.check(spec=spec, now=NOW, posted_at=twenty_days, first_run=True).blocks


def iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# already applied — finished is not the same as irrelevant
# --------------------------------------------------------------------------


def test_an_applied_role_is_suppressed() -> None:
    result = applied.check(fingerprint="abc123", has_applied=lambda fp: fp == "abc123")

    assert result.verdict is Verdict.BLOCK


def test_with_no_history_consulted_the_gate_admits_it() -> None:
    result = applied.check(fingerprint="abc123", has_applied=None)

    assert result.verdict is Verdict.UNKNOWN


# --------------------------------------------------------------------------
# the chain
# --------------------------------------------------------------------------


def test_every_gate_runs_even_after_one_has_already_blocked(spec) -> None:
    """Short-circuiting saves nothing — none of these costs a token — and it
    costs the human the answer to "what else was wrong with it"."""
    report = run_gates(
        candidate(
            location="ירושלים",
            body="דרוש תואר במדעי המחשב, ניסיון של 7 שנים",
            posted_at=iso(NOW - timedelta(days=40)),
        ),
        spec=spec,
        now=NOW,
    )

    assert {r.gate for r in report.results} == {
        "already_applied",
        "freshness",
        "geography",
        "seniority",
        "degree",
    }
    assert {r.gate for r in report.blocking} == {"freshness", "geography", "seniority", "degree"}


def test_the_one_line_reason_names_every_blocking_gate(spec) -> None:
    report = run_gates(
        candidate(location="ירושלים", body="ניסיון של 8 שנים נדרש"), spec=spec, now=NOW
    )

    assert "geography" in report.reason and "seniority" in report.reason


def test_a_clean_posting_passes_and_reports_what_it_passed_on_silence(spec) -> None:
    report = run_gates(
        candidate(
            location="חיפה",
            body="ניסיון של שנתיים בניתוח נתונים",
            posted_at=iso(NOW - timedelta(days=1)),
        ),
        spec=spec,
        now=NOW,
    )

    assert report.passed
    assert "already_applied" in report.reason
    assert report.verdict_of("degree") is Verdict.UNKNOWN


def test_a_candidate_arrives_the_same_from_a_scraper_and_from_the_store(spec) -> None:
    from desk.sites.base import RawPosting

    raw = RawPosting(
        site="drushim",
        external_id="1",
        title="אנליסט",
        company="חברה",
        location="חיפה",
        stated_experience="2 שנים",
    )
    row = {
        "site": "drushim",
        "title": "אנליסט",
        "company": "חברה",
        "location": "חיפה",
        "stated_experience": "2 שנים",
    }

    from_scraper = Candidate.from_raw(raw)
    from_store = Candidate.from_row(row)

    assert from_scraper.stated_experience == from_store.stated_experience == "2 שנים"
    assert from_scraper.location == from_store.location == "חיפה"


def test_the_chain_reads_the_clusters_first_seen_when_the_store_offers_one() -> None:
    """The per-fingerprint value is wrong once the resolver links a role across
    sites: a job that sat on a board three weeks looks new the day an agency
    relists it, and clears the window every time it moves."""

    class Merged:
        def cluster_first_seen(self, fingerprint: str) -> str:
            return "2026-07-01T00:00:00"

    assert store_first_seen(Merged())("anything") == "2026-07-01T00:00:00"


def test_a_store_without_the_linking_table_still_gates_on_dates(tmp_path) -> None:
    """A clean clone predates the resolver and must not fall back to nothing."""
    from desk.store import Posting

    with Store(tmp_path / "desk.sqlite") as store:
        posting = Posting(site="alljobs", external_id="1", title="אנליסט", company="חברה")
        store.upsert_posting(posting, now="2026-08-11T09:00:00")

        assert store_first_seen(store)(posting.fingerprint) == "2026-08-11T09:00:00"
