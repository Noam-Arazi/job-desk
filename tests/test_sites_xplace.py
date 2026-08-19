"""What the XPlace module has to keep being true.

The fixtures are trimmed copies of two real shelves, `/dev/jobs` and `/jobs`,
saved 2026-08-19 and re-verified against a fresh fetch of the live site the same
day: every one of the six projects in `shelf.html` is byte-faithful to the page
the site served, field for field. That matters more here than on the other three
site modules, because this parser reads a private React Server Components
payload rather than rendered markup, and a hand-written fixture would let it
pass against a shape the site has never actually sent.

The six were chosen because between them they carry every trap the feed sets:
a project with no budget and no payment model at all, one at ₪15,000 with
seventy-nine bids already in, one with no bids, one whose deadline is years out,
payment models 1, 2 and 5, and a category name containing commas.


"""

from __future__ import annotations

from pathlib import Path

import pytest

from desk.config import load_spec
from desk.sites import xplace
from desk.sites.http import FixtureFetcher
from desk.sites.xplace import PayloadMissing

FIXTURES = Path(__file__).parent / "fixtures" / "xplace"
SHELF = FIXTURES / "shelf.html"
NO_PAYLOAD = FIXTURES / "shelf_without_payload.html"


@pytest.fixture
def page() -> str:
    return SHELF.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# urls — and the site's own the fetch rules
# --------------------------------------------------------------------------


def test_no_url_this_module_builds_carries_a_query_string() -> None:
    """`the fetch rules` disallows `/*jobs*?`, so a paged URL would be both useless
    and impolite. The live check was done offline against saved captures: the
    site answers `?page=1`, `?page=2` and `?pageNumber=1` with page zero every
    time, identical ids and identical meta."""
    for shelf in xplace.SHELVES:
        url = xplace.url_for(shelf)
        assert "?" not in url
        assert "page" not in url.lower().replace("xplace", "")


def test_url_for_has_no_page_argument_at_all() -> None:
    """Not "pages are ignored" but "there is no way to ask for one". A promise
    the site does not keep should not be expressible."""
    import inspect

    assert list(inspect.signature(xplace.url_for).parameters) == ["shelf"]


def test_an_unknown_shelf_is_an_error_not_a_shrug() -> None:
    with pytest.raises(KeyError, match="not a known xplace shelf"):
        xplace.url_for("no-such-shelf")


def test_every_shelf_the_spec_would_name_has_a_path() -> None:
    for shelf in xplace.shelves_from_spec(load_spec()):
        assert shelf in xplace.SHELVES


def test_the_spec_marks_this_site_as_the_freelance_pipeline() -> None:
    """The whole separate flow hangs off this one word. If the spec ever calls
    xplace an ordinary board, the gates would run on projects that state
    nothing they can read."""
    entry = next(s for s in load_spec()["sites"] if s["id"] == xplace.SITE)
    assert entry["pipeline"] == "freelance"


# --------------------------------------------------------------------------
# the payload
# --------------------------------------------------------------------------


def test_the_payload_is_reassembled_from_every_chunk(page: str) -> None:
    """The stream is cut at arbitrary byte offsets, mid-object and mid-string.
    A parser that read one script tag would work on short pages and fail on
    long ones, so this asserts the join actually spans chunks."""
    assert page.count("self.__next_f.push") > 1
    payload = xplace.flight(page)
    assert '"items"' in payload
    assert len(payload) > max(len(c) for c in page.split("self.__next_f.push")[1:])


def test_a_page_with_no_payload_raises_rather_than_returning_nothing() -> None:
    """Nothing is what a genuinely empty shelf returns. A layout change that
    blinded the crawler must not be able to wear the same face."""
    with pytest.raises(PayloadMissing):
        xplace.parse(NO_PAYLOAD.read_text(encoding="utf-8"))


def test_the_card_alone_would_not_have_been_enough() -> None:
    """The rendered card carries a title, a date and a budget — and not the
    description, the deadline or the bid count, which are three of the four
    facts the freelance flow judges on. This is why the module reads the
    payload rather than the markup, and the fixture proves the card is thin."""
    card_only = NO_PAYLOAD.read_text(encoding="utf-8")
    assert "215450" in card_only
    assert "תקציב" in card_only
    assert "self.__next_f.push" not in card_only


def test_a_bracket_inside_a_hebrew_description_does_not_end_the_scan(page: str) -> None:
    parsed = xplace.parse(page)
    assert len(parsed["postings"]) == 6
    assert all(p.description for p in parsed["projects"] if p.external_id != "215408")


# --------------------------------------------------------------------------
# fields
# --------------------------------------------------------------------------


def test_the_feed_states_its_own_arithmetic(page: str) -> None:
    """The site says how much it is withholding, so the module reports the
    unreached projects as a number instead of letting 20 rows look complete."""
    assert xplace.parse(page)["meta"] == {
        "page": 0,
        "size": 20,
        "total": 61,
        "totalPages": 4,
        "hasNext": True,
    }


def test_an_unstated_budget_is_none_and_never_zero(page: str) -> None:
    """A project with no budget and a project budgeted at nothing are different
    facts. Defaulted to 0, the second reads as an insulting offer and the first
    would be silently turned into one."""
    projects = {p.external_id: p for p in xplace.parse(page)["projects"]}
    assert projects["215408"].budget is None
    assert projects["215408"].payment_model is None
    assert projects["215344"].budget == 15000.0


def test_the_bid_count_is_carried_exactly_as_well_as_the_band(page: str) -> None:
    """The band is the site's vocabulary; the count is strictly more
    informative. Seventy-nine bids and twenty-one bids share a band."""
    projects = {p.external_id: p for p in xplace.parse(page)["projects"]}
    assert projects["215344"].bids == 79
    assert projects["215344"].bids_band == "HAS_21_PLUS_BIDS"
    assert projects["215363"].bids == 0
    assert projects["215363"].bids_band == "NO_BIDS_YET"
    assert all(p.bids_band in xplace.BID_BANDS for p in projects.values() if p.bids_band)


def test_epoch_milliseconds_become_dates_and_nonsense_becomes_empty(page: str) -> None:
    projects = {p.external_id: p for p in xplace.parse(page)["projects"]}
    assert projects["215450"].posted_at == "2026-08-17"
    assert projects["215450"].bids_close_at == "2026-10-05"
    assert projects["215450"].due_date == ""  # the client stated none
    assert projects["215433"].due_date == "2026-09-15"
    assert xplace._epoch(None) == ""
    assert xplace._epoch(0) == ""
    assert xplace._epoch("yesterday") == ""


def test_company_and_location_are_empty_on_purpose(page: str) -> None:
    """The client is behind a login and the site states no location anywhere.
    Writing "XPlace" into company would poison the fingerprint with a name no
    client has."""
    for posting in xplace.parse(page)["postings"]:
        assert posting.company == ""
        assert posting.location == ""
        assert posting.work_arrangement == ""


def test_what_the_client_did_not_state_is_counted_not_dropped(page: str) -> None:
    missing = xplace.parse(page)["missing"]
    assert missing["budget"] == 1
    assert missing["due_date"] == 4
    assert len(xplace.parse(page)["postings"]) == 6  # none of them were discarded


# --------------------------------------------------------------------------
# the facts block — written by this module, read by the freelance flow
# --------------------------------------------------------------------------


def test_the_facts_survive_a_round_trip_through_the_body(page: str) -> None:
    for project in xplace.parse(page)["projects"]:
        facts, prose = xplace.parse_body(xplace.render_body(project))
        assert prose == project.description
        assert facts["bids_band"] == project.bids_band
        if project.budget is None:
            assert facts["budget"] == ""
            assert facts["currency"] == ""
        else:
            assert float(facts["budget"]) == project.budget
            assert facts["currency"] == xplace.CURRENCY


def test_a_category_name_containing_commas_is_not_split(page: str) -> None:
    """"VBA, Office, Excel Programming" is one shelf, not three. The list
    separator has to be something a category name cannot hold."""
    projects = {p.external_id: p for p in xplace.parse(page)["projects"]}
    categories = projects["215450"].categories
    assert "VBA, Office, Excel Programming" in categories

    facts, _ = xplace.parse_body(xplace.render_body(projects["215450"]))
    recovered = tuple(
        part for part in facts["categories"].split(xplace.CATEGORY_SEPARATOR) if part
    )
    assert recovered == categories


def test_a_body_without_the_block_yields_no_facts_and_all_prose() -> None:
    """Any posting in the store can be handed to this — a Drushim advert
    reached by fingerprint, a row stored before the block existed. The honest
    answer for those is no facts, so the freelance flow can refuse it."""
    facts, prose = xplace.parse_body("an ordinary salaried job advert")
    assert facts == {}
    assert prose == "an ordinary salaried job advert"


def test_a_truncated_block_is_not_half_read() -> None:
    facts, prose = xplace.parse_body(f"{xplace.FACTS_MARK}\nbudget: 500\n(no separator)")
    assert facts == {}
    assert prose.startswith(xplace.FACTS_MARK)


# --------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------


def _fetcher() -> FixtureFetcher:
    return FixtureFetcher({"/dev/jobs": SHELF})


def test_one_request_per_shelf_and_no_page_loop() -> None:
    fetcher = _fetcher()
    result = xplace.crawl(fetcher, spec=load_spec(), shelves=["dev"])

    assert result.ok
    assert result.pages_fetched == 1
    assert len(fetcher.requested) == 1
    assert len(result.postings) == 6


def test_the_unreached_projects_are_reported_as_a_number() -> None:
    """55 of the dev shelf's 61 are behind a client-side pager. A truncated
    feed that looked complete would be the worst outcome here."""
    result = xplace.crawl(_fetcher(), spec=load_spec(), shelves=["dev"])
    assert any("55 open projects" in note for note in result.notes)
    assert "the feed served 6 of 61" in result.stopped_because


def test_the_freelance_pipeline_is_announced_on_every_run() -> None:
    """A reader of the run output should not have to know that the gates were
    skipped here, or wonder whether it was an oversight."""
    result = xplace.crawl(_fetcher(), spec=load_spec(), shelves=["dev"])
    assert any("seniority and degree gates" in note for note in result.notes)


def test_search_terms_are_reported_as_unreachable_not_silently_dropped() -> None:
    """The site has no free-text search. A term that quietly vanished would
    make the crawl look narrower than it was."""
    result = xplace.crawl(
        _fetcher(), spec=load_spec(), shelves=["dev"], terms=["data analyst", "מפתח"]
    )
    assert any("no free-text search" in note and "2 search term" in note for note in result.notes)


def test_an_unknown_shelf_is_recorded_and_the_rest_still_run() -> None:
    result = xplace.crawl(_fetcher(), spec=load_spec(), shelves=["no-such-shelf", "dev"])
    assert not result.ok
    assert "no-such-shelf" in result.errors[0]
    assert len(result.postings) == 6  # the good shelf still produced


def test_a_shelf_that_raises_does_not_end_the_run() -> None:
    result = xplace.crawl(
        FixtureFetcher({"/dev/jobs": "<html>no payload here</html>"}),
        spec=load_spec(),
        shelves=["dev"],
    )
    assert not result.ok
    assert result.postings == []
    assert "dev:" in result.errors[0]


def test_the_shelf_a_project_sits_on_is_recorded_where_terms_would_be() -> None:
    """It is the site's own filing and the only classification it offers."""
    result = xplace.crawl(_fetcher(), spec=load_spec(), shelves=["dev"])
    assert all(matched == ["dev"] for matched in result.matched_terms.values())


def test_the_same_project_on_two_shelves_is_stored_once() -> None:
    result = xplace.crawl(
        FixtureFetcher({"/jobs": SHELF}), spec=load_spec(), shelves=["dev", "web"]
    )
    assert len(result.postings) == 6
    assert result.matched_terms["215450"] == ["dev", "web"]
