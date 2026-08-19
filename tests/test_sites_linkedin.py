"""What the LinkedIn module has to keep being true.

The parser tests run against a trimmed copy of a real guest-search response and
a real posting page, both saved 2026-08-19. The three cards were kept because
between them they carry the fields this board is the only one to state — an
exact ISO date on every card — and the tracking parameters that must not be
stored.

The end-of-results fixture is 26 bytes and is the real thing, byte for byte:
that sentinel is load-bearing here in a way it is on no other board, so it is
not hand-written.
"""

from __future__ import annotations

import pytest

from desk.config import SAMPLES_DIR, load_spec
from desk.sites import linkedin
from desk.sites.http import FixtureFetcher

FIXTURE = SAMPLES_DIR / "linkedin_page.html"
DETAIL = SAMPLES_DIR / "linkedin_detail.html"
EMPTY = SAMPLES_DIR / "linkedin_empty.html"


@pytest.fixture
def page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def detail() -> str:
    return DETAIL.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# urls
# --------------------------------------------------------------------------


def test_the_window_is_sent_to_the_server_in_seconds() -> None:
    """LinkedIn applies the freshness window itself. Seven days is the spec's
    number expressed the way the endpoint wants it, and getting the arithmetic
    wrong here would silently widen the crawl rather than fail."""
    assert "f_TPR=r604800" in linkedin.url_for("data analyst", 0, days=7, location="Israel")
    assert "f_TPR=r86400" in linkedin.url_for("data analyst", 0, days=1, location="Israel")


def test_no_window_is_sent_when_none_is_asked_for() -> None:
    assert "f_TPR" not in linkedin.url_for("data analyst", 0, days=0, location="Israel")


def test_the_offset_is_a_row_count_and_not_a_page_number() -> None:
    """`start` counts results, not pages. A module that sent the page number
    would fetch rows 1-10, then 2-11, and call the overlap progress."""
    assert "start=0" in linkedin.url_for("t", 0, days=7, location="Israel")
    assert "start=30" in linkedin.url_for("t", 30, days=7, location="Israel")


def test_a_negative_offset_never_reaches_the_endpoint() -> None:
    assert "start=0" in linkedin.url_for("t", -10, days=7, location="Israel")


def test_the_location_is_the_specs_and_not_the_modules() -> None:
    spec = {"sites": [{"id": "linkedin", "location": "Haifa, Israel"}]}
    assert linkedin.settings_from_spec(spec)["location"] == "Haifa, Israel"


# --------------------------------------------------------------------------
# fields
# --------------------------------------------------------------------------


def test_every_card_is_read_with_all_of_its_fields(page: str) -> None:
    parsed = linkedin.parse(page)

    assert len(parsed["postings"]) == 3
    assert parsed["missing"] == {}
    assert parsed["skipped"] == {}


def test_the_board_states_an_exact_date_and_it_is_taken_as_stated(page: str) -> None:
    """The one thing LinkedIn gives that no other board here does. AllJobs and
    Drushim publish relative Hebrew wording resolved against a clock, and
    GotFriends publishes nothing — so this is the only module whose dates
    cannot drift with the machine's clock."""
    postings = linkedin.parse(page)["postings"]

    assert [p.posted_at for p in postings] == ["2026-08-17", "2026-08-16", "2026-08-12"]
    assert postings[0].posted_raw == "1 day ago"


def test_the_boards_own_wording_is_kept_next_to_the_parsed_date(page: str) -> None:
    """When a date is wrong, the trace has to show what the board said and not
    only what we made of it."""
    assert all(p.posted_raw for p in linkedin.parse(page)["postings"])


def test_the_stored_url_carries_no_tracking_parameters(page: str) -> None:
    """`refId` and `trackingId` are minted per response. Stored, they would
    make one posting look like a new row on every run."""
    for posting in linkedin.parse(page)["postings"]:
        assert "?" not in posting.url
        assert "trackingId" not in posting.url
        assert "/jobs/view/" in posting.url


def test_the_identity_is_linkedins_own_urn(page: str) -> None:
    assert [p.external_id for p in linkedin.parse(page)["postings"]] == [
        "4454903277",
        "4428612794",
        "4453110969",
    ]


def test_a_card_without_a_urn_falls_back_to_the_id_in_its_url() -> None:
    """A missing attribute is a reason to read the id elsewhere, not a reason
    to drop a posting that is perfectly readable."""
    html = """
    <li><div class="base-card job-search-card">
      <a class="base-card__full-link"
         href="https://il.linkedin.com/jobs/view/analyst-at-x-999?refId=z"></a>
      <h3 class="base-search-card__title">Analyst</h3>
      <h4 class="base-search-card__subtitle"><a class="hidden-nested-link">X</a></h4>
      <span class="job-search-card__location">Haifa, Israel</span>
      <time class="job-search-card__listdate" datetime="2026-08-18">1 day ago</time>
    </div></li>
    """
    parsed = linkedin.parse(html)

    assert [p.external_id for p in parsed["postings"]] == ["999"]
    assert parsed["missing"]["urn"] == 1


def test_a_card_with_no_id_at_all_is_dropped_and_counted() -> None:
    """Never silently: a scraper that discards input without saying so reads as
    complete coverage when it is not."""
    html = (
        '<li><div class="base-card job-search-card">'
        '<h3 class="base-search-card__title">X</h3></div></li>'
    )
    parsed = linkedin.parse(html)

    assert parsed["postings"] == []
    assert parsed["skipped"]["no id on the card"] == 1


def test_the_same_id_twice_in_one_response_is_collapsed_and_counted(page: str) -> None:
    first_card = "<li>" + page.split("<li>", 2)[1]
    doubled = page + first_card
    parsed = linkedin.parse(doubled)

    assert len(parsed["postings"]) == 3
    assert parsed["skipped"]["a second card with the same id"] == 1


def test_the_card_alone_carries_no_body(page: str) -> None:
    """`parse` makes no requests, so it cannot invent a description. The body
    arrives from the detail endpoint in `crawl`, or it is counted as absent."""
    assert all(p.body == "" for p in linkedin.parse(page)["postings"])


# --------------------------------------------------------------------------
# the detail endpoint
# --------------------------------------------------------------------------


def test_the_description_and_the_criteria_arrive_together(detail: str) -> None:
    parsed = linkedin.parse_detail(detail)

    assert len(parsed["body"]) > 400
    assert "Employment type: Full-time" in parsed["body"]
    assert "Industries: Software Development" in parsed["body"]


def test_the_seniority_band_is_passed_through_as_the_board_stated_it(detail: str) -> None:
    assert linkedin.parse_detail(detail)["stated_experience"] == "Not Applicable"


def test_a_seniority_band_is_not_a_year_count_and_the_gate_falls_through() -> None:
    """The gate consults `stated_experience` before the prose and, where it
    finds a figure there, trusts it without the proximity test it applies to
    text. LinkedIn's bands carry no figure, so the gate has to keep reading —
    otherwise a posting demanding seven years in its body would pass on the
    strength of the word "Not Applicable"."""
    from desk.gates import seniority

    spec = load_spec()
    result = seniority.check(
        spec=spec,
        title="Senior Data Analyst",
        body="דרוש ניסיון של 7 שנים בניתוח נתונים",
        stated_experience="Not Applicable",
    )

    assert result.verdict.name == "BLOCK"


def test_an_entry_level_band_is_a_floor_of_zero_and_passes() -> None:
    from desk.gates import seniority

    result = seniority.check(
        spec=load_spec(),
        title="Data Analyst",
        body="",
        stated_experience="Entry level",
    )

    assert result.verdict.name == "PASS"


# --------------------------------------------------------------------------
# the walk
# --------------------------------------------------------------------------


def test_the_sentinel_ends_the_walk_and_is_not_a_parse_failure() -> None:
    """Paging past the last result is a 200 with 26 bytes. Counted as a broken
    parse, an exhausted feed would look like a broken scraper — and counted as
    a page, the walk would run to the ceiling on every term."""
    fetcher = FixtureFetcher({"start=0": FIXTURE, "start=10": EMPTY})

    result = linkedin.crawl(
        fetcher, spec=load_spec(), terms=["data analyst"], max_pages=6, with_details=False
    )

    assert result.ok
    assert len(result.postings) == 3
    assert "end of results at offset 10" in result.stopped_because


def test_a_page_that_repeats_what_came_before_does_not_stop_the_walk() -> None:
    """Offsets overlap here — one measured page brought six new ids out of ten
    — so "nothing new" is not an end. Only the sentinel is. A module that
    stopped on the overlap would quietly truncate every busy term."""
    fetcher = FixtureFetcher({"start=0": FIXTURE, "start=10": FIXTURE, "start=20": EMPTY})

    result = linkedin.crawl(
        fetcher, spec=load_spec(), terms=["data analyst"], max_pages=6, with_details=False
    )

    assert result.pages_fetched == 3
    assert len(result.postings) == 3  # kept once
    assert "end of results at offset 20" in result.stopped_because


def test_the_offset_ceiling_is_reported_when_the_feed_never_ends() -> None:
    fetcher = FixtureFetcher({"start=": FIXTURE})

    result = linkedin.crawl(
        fetcher, spec=load_spec(), terms=["data analyst"], max_pages=3, with_details=False
    )

    assert result.pages_fetched == 3
    assert "hit the 3-offset ceiling" in result.stopped_because


def test_a_term_that_raises_is_recorded_and_the_next_one_still_runs() -> None:
    fetcher = FixtureFetcher({"keywords=good": FIXTURE, "start=10": EMPTY})

    result = linkedin.crawl(
        fetcher, spec=load_spec(), terms=["bad", "good"], max_pages=6, with_details=False
    )

    assert not result.ok
    assert "'bad'" in result.errors[0]
    assert len(result.postings) == 3


def test_a_posting_found_by_two_terms_is_kept_once_and_both_terms_recorded() -> None:
    fetcher = FixtureFetcher({"start=0": FIXTURE, "start=10": EMPTY})

    result = linkedin.crawl(
        fetcher,
        spec=load_spec(),
        terms=["data analyst", "analyst"],
        max_pages=6,
        with_details=False,
    )

    assert len(result.postings) == 3
    assert result.matched_terms["4454903277"] == ["data analyst", "analyst"]


def test_details_are_fetched_and_land_on_the_posting() -> None:
    fetcher = FixtureFetcher(
        {"seeMoreJobPostings": FIXTURE, "start=10": EMPTY, "jobPosting/": DETAIL}
    )

    result = linkedin.crawl(fetcher, spec=load_spec(), terms=["data analyst"], max_pages=6)

    assert all(p.body for p in result.postings)
    assert all(p.stated_experience == "Not Applicable" for p in result.postings)


def test_the_detail_ceiling_is_reported_rather_than_quietly_truncating() -> None:
    """A posting with no body is invisible to the resolver and to both prose
    gates. A truncated batch is not a smaller result — it is a result some of
    whose rows cannot be judged, and that has to be said out loud."""
    fetcher = FixtureFetcher(
        {"seeMoreJobPostings": FIXTURE, "start=10": EMPTY, "jobPosting/": DETAIL}
    )

    result = linkedin.crawl(
        fetcher, spec=load_spec(), terms=["data analyst"], max_pages=6, max_details=1
    )

    assert sum(1 for p in result.postings if p.body) == 1
    assert result.skipped["a body left unfetched at the detail ceiling"] == 2
    assert "detail ceiling is 1" in result.notes[0]


def test_a_detail_request_that_fails_costs_one_body_and_not_the_run() -> None:
    fetcher = FixtureFetcher({"seeMoreJobPostings": FIXTURE, "start=10": EMPTY})

    result = linkedin.crawl(fetcher, spec=load_spec(), terms=["data analyst"], max_pages=6)

    assert len(result.postings) == 3  # every card survived
    assert result.skipped["a detail request that failed"] == 3
    assert len(result.errors) == 3


def test_skipping_details_is_said_out_loud() -> None:
    fetcher = FixtureFetcher({"start=0": FIXTURE, "start=10": EMPTY})

    result = linkedin.crawl(
        fetcher, spec=load_spec(), terms=["data analyst"], max_pages=6, with_details=False
    )

    assert "card only" in result.notes[0]


# --------------------------------------------------------------------------
# the position the module takes
# --------------------------------------------------------------------------


def test_no_url_this_module_builds_reaches_an_authenticated_path() -> None:
    """The claim in the module docstring, as a check rather than a sentence.
    Everything here is the logged-out guest surface; nothing touches a session,
    a login, or the authenticated API."""
    urls = [
        linkedin.url_for("data analyst", 0, days=7, location="Israel"),
        linkedin.detail_url("4454903277"),
    ]

    for url in urls:
        assert url.startswith("https://www.linkedin.com/jobs-guest/")
        assert "/uas/" not in url
        assert "voyager" not in url
        assert "li_at" not in url


def test_the_spec_pins_the_logged_out_guest_surface() -> None:
    """The entry pins the surface and the absent bypass."""
    entry = next(e for e in load_spec()["sites"] if e["id"] == "linkedin")

    assert entry["fetch"] == "guest_api"
    assert entry["stealth"] is False
    assert "the fetch rules" in entry["notes"]
