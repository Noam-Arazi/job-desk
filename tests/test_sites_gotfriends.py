"""What the GotFriends module has to keep being true.

The parser tests run against a trimmed copy of a real category page, saved
2026-08-17. Its five cards were chosen because between them they carry every
trap this board sets.
"""

from __future__ import annotations

import pytest

from desk.config import SAMPLES_DIR, load_spec
from desk.sites import gotfriends
from desk.sites.http import FetchError, FixtureFetcher

FIXTURE = SAMPLES_DIR / "gotfriends_page.html"


@pytest.fixture
def page() -> str:
    return FIXTURE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# urls
# --------------------------------------------------------------------------


def test_page_one_is_the_bare_shelf_and_later_pages_take_a_parameter() -> None:
    first = gotfriends.url_for("data-analyst", 1)
    second = gotfriends.url_for("data-analyst", 2)

    assert first.endswith("/jobslobby/bibig_data/data-analyst/")
    assert second == first + "?page=2"


def test_the_boards_own_total_parameter_is_not_sent() -> None:
    """The board's pager links carry `&total=`, which is the page count and
    not the result count. The server does not need it, so a number that goes
    stale between runs never becomes part of a request."""
    assert "total=" not in gotfriends.url_for("data-analyst", 4)


def test_every_category_the_spec_names_has_a_path() -> None:
    """The spec decides which shelves are walked; this module knows where they
    are. A category in one and not the other is a disagreement, and it should
    fail here rather than at 2am against the live site."""
    for category in gotfriends.categories_from_spec(load_spec()):
        assert category in gotfriends.CATEGORIES


def test_an_unknown_category_is_an_error_not_a_shrug() -> None:
    result = gotfriends.crawl(
        FixtureFetcher({"/jobslobby/": FIXTURE}), spec=load_spec(), categories=["no-such-shelf"]
    )

    assert not result.ok
    assert "no-such-shelf" in result.errors[0]


# --------------------------------------------------------------------------
# fields
# --------------------------------------------------------------------------


def test_the_duplicate_is_collapsed_and_counted(page: str) -> None:
    """Two cards, one printed job number, URLs differing by a `-1` suffix: the
    agency published one role twice. Five cards in, four postings out, and the
    fifth is accounted for rather than swallowed by the store's uniqueness
    constraint further down."""
    result = gotfriends.parse(page)

    assert len(result["postings"]) == 4
    assert result["skipped"] == {"a second card with the same job number": 1}


def test_a_card_that_prints_no_job_number_still_gets_an_id(page: str) -> None:
    """One card in the fixture has no "מס' משרה" block at all. Its URL still
    carries the number, so it is kept — and the fallback is reported, because
    a parser quietly changing where identity comes from is how two runs end up
    disagreeing about whether a posting is new."""
    postings = {p.external_id: p for p in gotfriends.parse(page)["postings"]}

    assert "152550" in postings
    assert gotfriends.parse(page)["missing"]["career_num"] == 1


def test_the_fields_are_the_ones_the_pipeline_needs(page: str) -> None:
    for posting in gotfriends.parse(page)["postings"]:
        assert posting.external_id
        assert posting.title
        assert posting.location
        assert posting.body
        assert posting.url.startswith("https://www.gotfriends.co.il/jobslobby/")


def test_the_employer_is_never_invented(page: str) -> None:
    """The agency anonymises its clients. Neither its own name nor the
    "בחברת סטארט-אפ" phrase from the title goes in the company field: both
    would be fabrications, and both would poison the fingerprint that the
    cross-site resolver depends on."""
    for posting in gotfriends.parse(page)["postings"]:
        assert posting.company == ""


def test_no_posting_claims_a_date(page: str) -> None:
    """This board publishes none — not on the card, not on the posting's page,
    not in JSON-LD. If one ever appears, this test failing is the notice to go
    and read it."""
    for posting in gotfriends.parse(page)["postings"]:
        assert posting.posted_at == ""
        assert posting.posted_raw == ""


def test_the_body_carries_the_requirements_not_only_the_pitch(page: str) -> None:
    """The requirements block is where this board states years and degree, so
    a body that kept only the description would blind both gates."""
    posting = {p.external_id: p for p in gotfriends.parse(page)["postings"]}["154868"]

    assert "תיאור המשרה:" in posting.body
    assert "דרישות המשרה:" in posting.body
    assert "5 שנות ניסיון" in posting.body


def test_the_location_is_one_of_the_boards_own_buckets(page: str) -> None:
    """There are no city names anywhere on a card — the board files into eight
    coarse regions. The gates need to know that, so the buckets are mapped
    rather than parsed as if they were places."""
    for posting in gotfriends.parse(page)["postings"]:
        assert posting.location in gotfriends.REGIONS


def test_the_unknown_bucket_maps_to_nothing_rather_than_to_somewhere(page: str) -> None:
    """ "אחר" is the board admitting it will not say. Mapping it onto a region
    would hand the geography gate a location the board never claimed."""
    postings = {p.external_id: p for p in gotfriends.parse(page)["postings"]}

    assert postings["142949"].location == "אחר"
    assert gotfriends.REGIONS["אחר"] == ""


# --------------------------------------------------------------------------
# crawling
# --------------------------------------------------------------------------


def test_the_walk_stops_when_a_page_repeats_what_is_already_held() -> None:
    """Paging past the last page returns the last page again — 200, full card
    list, byte for byte identical. Waiting for an empty page would wait
    forever, so the stop is a page that brought nothing new.

    This is the exact mirror of Drushim, where the same silent-repeat trap
    sits at the front of the walk instead of at the end.
    """
    fetcher = FixtureFetcher({"/jobslobby/": FIXTURE})

    result = gotfriends.crawl(fetcher, spec=load_spec(), categories=["data-analyst"], max_pages=10)

    assert result.pages_fetched == 2  # page one, then the repeat that stopped it
    assert "clamps past its last page" in result.stopped_because
    assert len(result.postings) == 4


def test_a_role_on_two_shelves_is_kept_once_and_both_shelves_recorded() -> None:
    fetcher = FixtureFetcher({"/jobslobby/": FIXTURE})

    result = gotfriends.crawl(
        fetcher, spec=load_spec(), categories=["data-analyst", "ai-engineer"], max_pages=1
    )

    ids = [posting.external_id for posting in result.postings]
    assert len(ids) == len(set(ids)) == 4
    for external_id in ids:
        assert result.matched_terms[external_id] == ["data-analyst", "ai-engineer"]


def test_the_run_says_out_loud_that_the_board_has_no_dates() -> None:
    """Every posting from here is undated. Left unsaid, that reads downstream
    as a parser that failed rather than as a board that does not publish."""
    result = gotfriends.crawl(
        FixtureFetcher({"/jobslobby/": FIXTURE}),
        spec=load_spec(),
        categories=["data-analyst"],
        max_pages=1,
    )

    assert any("no posting dates" in note for note in result.notes)


def test_a_search_term_is_reported_rather_than_ignored() -> None:
    """There is no query box on this board. A term passed to it reaches
    nothing, and the run should say so instead of returning category results
    that look like they answered the term."""
    result = gotfriends.crawl(
        FixtureFetcher({"/jobslobby/": FIXTURE}),
        spec=load_spec(),
        categories=["data-analyst"],
        terms=["אנליסט", "דאטה"],
        max_pages=1,
    )

    assert any("no free-text search" in note for note in result.notes)
    assert result.ok


class BrokenFetcher:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on

    def get(self, url: str) -> str:
        if self.fail_on in url:
            raise FetchError("boom")
        return FIXTURE.read_text(encoding="utf-8")


def test_a_dead_shelf_does_not_take_the_others_down() -> None:
    fetcher = BrokenFetcher(fail_on="data-analyst")

    result = gotfriends.crawl(
        fetcher, spec=load_spec(), categories=["data-analyst", "ai-engineer"], max_pages=1
    )

    assert result.errors
    assert not result.ok
    assert result.postings
