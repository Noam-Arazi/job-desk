"""What the Drushim module has to keep being true.

The parser tests run against a trimmed copy of a real results page, saved
2026-08-17.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import pytest

from desk.config import SAMPLES_DIR, load_spec
from desk.sites import drushim
from desk.sites.http import FetchError, FixtureFetcher

NOW = datetime(2026, 8, 17, 13, 0, 0)


@pytest.fixture
def page() -> str:
    return (SAMPLES_DIR / "drushim_page.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------


def test_pagination_lives_in_the_path_not_in_a_parameter() -> None:
    """`?page=2` returns page one again, with a 200 and a full page of results.

    Nothing in the response says the parameter was ignored, so a crawler that
    trusted it would refetch the same twenty-five listings on every page and
    report a clean run. This is the whole reason the URL is built by hand.
    """
    first = drushim.url_for("אנליסט", 1)
    second = drushim.url_for("אנליסט", 2)

    assert first.endswith(f"/jobs/search/{quote('אנליסט')}/")
    assert second == first + "2/"
    assert "page=" not in second


def test_the_term_reaches_the_url_encoded() -> None:
    assert quote("בינה מלאכותית") in drushim.url_for("בינה מלאכותית", 1)


# --------------------------------------------------------------------------
# fields
# --------------------------------------------------------------------------


def test_every_card_parses_completely(page: str) -> None:
    result = drushim.parse(page, now=NOW)

    assert len(result["postings"]) == 3
    assert result["missing"] == {}


def test_nothing_is_dropped_on_this_board(page: str) -> None:
    """Drushim has no closed tier today, unlike AllJobs. If it grows one this
    fails, which is the point — a new unapplicable half should be noticed,
    not quietly digested."""
    assert drushim.parse(page, now=NOW)["skipped"] == {}


def test_the_fields_are_the_ones_the_pipeline_needs(page: str) -> None:
    for posting in drushim.parse(page, now=NOW)["postings"]:
        assert posting.external_id.isdigit()
        assert posting.title
        assert posting.company
        assert posting.location
        assert posting.body
        assert posting.url.startswith("https://www.drushim.co.il/job/")
        assert posting.posted_at


def test_the_body_is_the_advert_not_the_company_name(page: str) -> None:
    """The company sits in a paragraph of the same kind as the description."""
    for posting in drushim.parse(page, now=NOW)["postings"]:
        assert posting.body != posting.company
        assert len(posting.body) > len(posting.company)


# The two ranges below were written as their upper bounds until 2026-08-19,
# which is what the parser produced and therefore what this test pinned. The
# spec reads a range at its LOWER bound, so storing "2 שנים" for a card that
# says "1-2 שנים" did not merely lose information — it moved the posting from
# one side of the seniority ceiling to the other, and quoted back to the human
# an experience demand the board had never written.
@pytest.mark.parametrize(
    ("external_id", "experience"),
    [("30087835", "ללא נסיון"), ("29898360", "1-2 שנים"), ("30037169", "3-4 שנים")],
)
def test_the_required_experience_is_read_off_the_card(
    page: str, external_id: str, experience: str
) -> None:
    """This board states the experience it wants as its own field. The
    seniority gate reads it directly instead of inferring it from prose."""
    postings = {p.external_id: p for p in drushim.parse(page, now=NOW)["postings"]}

    assert postings[external_id].stated_experience == experience


def test_the_location_is_not_the_experience(page: str) -> None:
    """Both come out of one pipe-separated strip, so a split that drifts by
    one field would put 'ללא נסיון' in the location and never be noticed."""
    for posting in drushim.parse(page, now=NOW)["postings"]:
        assert "נסיון" not in posting.location
        assert "שנים" not in posting.location


# --------------------------------------------------------------------------
# crawling
# --------------------------------------------------------------------------


def test_a_posting_reached_by_two_terms_is_kept_once(page: str) -> None:
    fetcher = FixtureFetcher({"/jobs/search/": page})

    result = drushim.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["אנליסט", "דאטה"], max_pages=1
    )

    ids = [posting.external_id for posting in result.postings]
    assert len(ids) == len(set(ids)) == 3
    for external_id in ids:
        assert result.matched_terms[external_id] == ["אנליסט", "דאטה"]


class BrokenFetcher:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on

    def get(self, url: str) -> str:
        if quote(self.fail_on) in url:
            raise FetchError("boom")
        return (SAMPLES_DIR / "drushim_page.html").read_text(encoding="utf-8")


def test_a_failed_term_does_not_take_the_others_down() -> None:
    fetcher = BrokenFetcher(fail_on="אנליסט")

    result = drushim.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["אנליסט", "דאטה"], max_pages=1
    )

    assert result.errors
    assert not result.ok
    assert result.postings
