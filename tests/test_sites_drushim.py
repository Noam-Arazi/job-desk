"""What the Drushim module has to keep being true.

The parser tests run against real responses of the site's search API, saved
2026-11-07: `drushim_search.json` is the server's JSON for "אנליסט", trimmed
to four listings; `drushim_no_match.html` is exactly what `HttpFetcher`
returned for a term with no results, Scrapling's HTML wrapping included.
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

import pytest

from desk.config import SAMPLES_DIR, load_spec
from desk.sites import drushim
from desk.sites.http import FetchError, FixtureFetcher, selector

NOW = datetime(2026, 11, 7, 13, 57, 0)


@pytest.fixture
def page() -> str:
    return (SAMPLES_DIR / "drushim_search.json").read_text(encoding="utf-8")


@pytest.fixture
def no_match() -> str:
    return (SAMPLES_DIR / "drushim_no_match.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the URL
# --------------------------------------------------------------------------


def test_the_search_is_the_json_api_not_the_html_page() -> None:
    url = drushim.url_for("אנליסט", 1)

    assert url.startswith("https://webapi.drushim.co.il/api/jobs/search?")
    assert f"SearchTerm={quote('אנליסט')}" in url
    # robots.txt disallows this path only with `area=`.
    assert "area=" not in url


def test_crawler_page_n_is_api_page_n_minus_one() -> None:
    """The API's unnumbered page is the first 25; `page=1` is the next 10.
    Sending `page=1` for the first page would skip those 25 without a trace."""
    first = drushim.url_for("אנליסט", 1)

    assert "page=" not in first
    assert drushim.url_for("אנליסט", 2) == first + "&page=1"
    assert drushim.url_for("אנליסט", 3) == first + "&page=2"


def test_the_term_reaches_the_url_encoded() -> None:
    assert quote("בינה מלאכותית") in drushim.url_for("בינה מלאכותית", 1)


# --------------------------------------------------------------------------
# reading what the fetcher actually hands over
# --------------------------------------------------------------------------


def test_the_fetchers_html_wrapping_reads_the_same_as_raw_json(page: str) -> None:
    """`HttpFetcher` returns Scrapling's re-serialization: the JSON inside
    `<html><body>`, `&` escaped, `<br/>` rewritten. Both forms must agree."""
    wrapped = str(selector(page).html_content)
    assert wrapped.startswith("<html><body>")

    raw = drushim.parse(page, now=NOW)["postings"]
    via_fetcher = drushim.parse(wrapped, now=NOW)["postings"]

    assert raw == via_fetcher


def test_an_html_quote_in_an_advert_survives_the_fetchers_wrapping(page: str) -> None:
    """Live 2026-11-07: an advert wrote "גו&quot;ח" inside the JSON. The HTML
    parser decodes that to a bare quote, which ended the JSON string early and
    lost two pages of the first run."""
    data = json.loads(page)
    data["ResultList"][0]["JobContent"]["Description"] = "קשר מול גו&quot;ח ו&quot;רשויות&quot;"
    data["ResultList"][1]["JobContent"]["Requirements"] = "תואר &quot;ראשון&quot;, חובה"
    raw = json.dumps(data, ensure_ascii=False)
    wrapped = str(selector(raw).html_content)

    via_fetcher = drushim.parse(wrapped, now=NOW)["postings"]

    assert via_fetcher == drushim.parse(raw, now=NOW)["postings"]
    assert 'גו"ח' in via_fetcher[0].body


def test_a_no_match_page_as_the_fetcher_returned_it_parses_to_nothing(no_match: str) -> None:
    result = drushim.parse(no_match, now=NOW)

    assert result["postings"] == []


def test_recommended_jobs_in_place_of_a_match_are_not_kept(page: str) -> None:
    data = json.loads(page)
    recommended = data["ResultList"][:2]
    data["ResultList"] = []
    data["ExtentionResultList"] = []
    data["RecommendedJobs"] = recommended

    result = drushim.parse(json.dumps(data), now=NOW)

    assert result["postings"] == []
    assert result["skipped"] == {"recommended in place of a match": 2}


def test_something_that_is_not_the_api_raises_rather_than_parsing_empty() -> None:
    """The old HTML page, or any other shape, must not read as zero results."""
    with pytest.raises(ValueError):
        drushim.parse("<html><body><div>jobs</div></body></html>", now=NOW)
    with pytest.raises(ValueError):
        drushim.parse('{"props": {}}', now=NOW)


# --------------------------------------------------------------------------
# fields
# --------------------------------------------------------------------------


def test_every_listing_parses_completely(page: str) -> None:
    result = drushim.parse(page, now=NOW)

    assert len(result["postings"]) == 4
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
        assert posting.posted_raw.startswith("לפני")


def test_the_id_is_the_one_the_store_already_holds(page: str) -> None:
    """The old cards' `listingid` was the STS code, not the code in the URL.
    Switching to the URL code would orphan every stored drushim row."""
    first = drushim.parse(page, now=NOW)["postings"][0]

    assert first.external_id == "30766447"
    assert "/job/38870127/" in first.url


def test_line_breaks_in_the_advert_do_not_glue_words(page: str) -> None:
    for posting in drushim.parse(page, now=NOW)["postings"]:
        assert "<br" not in posting.body
        assert "&amp;" not in posting.body


def test_the_body_is_the_advert_not_the_company_name(page: str) -> None:
    for posting in drushim.parse(page, now=NOW)["postings"]:
        assert posting.body != posting.company
        assert len(posting.body) > len(posting.company)


@pytest.mark.parametrize(
    ("external_id", "experience"),
    [
        ("30864149", "ללא נסיון"),
        ("30766447", "1-2 שנים"),
        ("30618882", "3-4 שנים"),
        ("30657514", "5-6 שנים"),
    ],
)
def test_the_required_experience_is_read_off_the_listing(
    page: str, external_id: str, experience: str
) -> None:
    """A range stays a range: the spec reads it at its lower bound, and
    storing only its upper end once moved postings across the seniority
    ceiling (2026-08-19)."""
    postings = {p.external_id: p for p in drushim.parse(page, now=NOW)["postings"]}

    assert postings[external_id].stated_experience == experience


def test_open_ended_experience_keeps_its_number() -> None:
    assert drushim._experience("7 שנים ומעלה") == "7 שנים"


def test_the_location_is_not_the_experience(page: str) -> None:
    for posting in drushim.parse(page, now=NOW)["postings"]:
        assert "נסיון" not in posting.location
        assert "שנים" not in posting.location


# --------------------------------------------------------------------------
# crawling
# --------------------------------------------------------------------------


def test_a_posting_reached_by_two_terms_is_kept_once(page: str) -> None:
    fetcher = FixtureFetcher({"/api/jobs/search": page})

    result = drushim.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["אנליסט", "דאטה"], max_pages=1
    )

    ids = [posting.external_id for posting in result.postings]
    assert len(ids) == len(set(ids)) == 4
    for external_id in ids:
        assert result.matched_terms[external_id] == ["אנליסט", "דאטה"]
    assert result.ok


def test_a_run_that_parses_nothing_is_a_failed_fetch(no_match: str) -> None:
    """Every request a 200, every page parsed, zero postings: how this module
    reported a clean run every morning from August to November 2026."""
    fetcher = FixtureFetcher({"/api/jobs/search": no_match})

    result = drushim.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["אנליסט", "דאטה"], max_pages=3
    )

    assert result.pages_fetched == 2
    assert not result.ok
    assert any("0 postings parsed" in error for error in result.errors)


def test_a_run_whose_postings_are_all_too_old_is_not_called_empty(page: str) -> None:
    """Zero kept is not zero parsed: an old-only window is a quiet day.
    A zero-day window puts every listing ("לפני N שעות") outside it."""
    result = drushim.crawl(
        FixtureFetcher({"/api/jobs/search": page}),
        spec=load_spec(),
        now=NOW,
        terms=["אנליסט"],
        max_pages=1,
        max_age_days=0,
    )

    assert result.postings == []
    assert result.ok


class BrokenFetcher:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on

    def get(self, url: str) -> str:
        if quote(self.fail_on) in url:
            raise FetchError("boom")
        return (SAMPLES_DIR / "drushim_search.json").read_text(encoding="utf-8")


def test_a_failed_term_does_not_take_the_others_down() -> None:
    fetcher = BrokenFetcher(fail_on="אנליסט")

    result = drushim.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["אנליסט", "דאטה"], max_pages=1
    )

    assert result.errors
    assert not result.ok
    assert result.postings
