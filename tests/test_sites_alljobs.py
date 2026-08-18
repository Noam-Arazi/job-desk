"""What the AllJobs module has to keep being true.

The parser tests run against a trimmed copy of a real results page, saved
2026-08-17. Two of its three cards can be applied to and one cannot, which is
the ratio the live board actually serves.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import quote

import pytest

from desk.config import SAMPLES_DIR, load_spec
from desk.sites import alljobs
from desk.sites.base import Throttle, ThrottledFetcher
from desk.sites.dates import parse_date
from desk.sites.http import FetchError, FixtureFetcher

NOW = datetime(2026, 8, 17, 12, 0, 0)


@pytest.fixture
def page() -> str:
    return (SAMPLES_DIR / "alljobs_page.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# the VIP board
# --------------------------------------------------------------------------


def test_vip_listings_are_dropped(page: str) -> None:
    """Half of what the board serves cannot be applied to.

    A guest reaching one has no way to submit, so a digest item built from one
    dead-ends on the human. They are identified structurally, by the board
    their container names, not by matching Hebrew text the site may reword.
    """
    result = alljobs.parse(page, now=NOW)

    assert len(result["postings"]) == 2
    assert result["skipped"] == {"VIP clients only — no way to submit": 1}


def test_the_dropped_board_is_the_one_under_the_vip_banner(page: str) -> None:
    """The filter keys on a class, and the class name is inverted from its
    meaning — `organic-board` is the VIP half. This pins the class to the
    banner that states it in words, so a rename cannot quietly flip the filter
    and fill the digest with listings nobody can apply to."""
    head, _, tail = page.partition(alljobs.VIP_CONTAINER)

    assert alljobs.VIP_BANNER in tail
    for dropped in alljobs.UNAPPLICABLE_BOARDS:
        assert f'class="{dropped}"' in tail
        assert f'class="{dropped}"' not in head
    assert f'class="{alljobs.APPLICABLE_BOARD}"' in head


def test_dropped_listings_are_counted_not_swallowed(page: str) -> None:
    """Silent discarding reads as full coverage. The count has to survive."""
    result = alljobs.parse(page, now=NOW)
    assert sum(result["skipped"].values()) > 0


# --------------------------------------------------------------------------
# fields
# --------------------------------------------------------------------------


def test_every_posting_carries_an_id_a_title_and_a_body(page: str) -> None:
    for posting in alljobs.parse(page, now=NOW)["postings"]:
        assert posting.external_id.isdigit()
        assert posting.title
        assert posting.body
        assert posting.url.startswith("https://www.alljobs.co.il/")


def test_the_body_excludes_the_employer_trailer(page: str) -> None:
    """The card carries a second description block that is a link, not content."""
    for posting in alljobs.parse(page, now=NOW)["postings"]:
        assert "לעוד משרות ומידע על" not in posting.body


def test_no_field_comes_back_empty(page: str) -> None:
    """The employer is named on this board, and the fingerprint needs it."""
    result = alljobs.parse(page, now=NOW)

    assert result["missing"] == {}
    for posting in result["postings"]:
        assert posting.company


def test_a_promoted_listing_parses_like_an_ordinary_one(page: str) -> None:
    """A promoted card renames its title block to `...-title-highlight`, and an
    exact class match returns nothing for it — silently, with no error. The
    fixture holds one of each, so both shapes stay covered."""
    assert "job-content-top-title-highlight" in page

    for posting in alljobs.parse(page, now=NOW)["postings"]:
        assert posting.title
        assert posting.company


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("לפני דקה", NOW - timedelta(minutes=1)),
        ("לפני 3 דקות", NOW - timedelta(minutes=3)),
        ("לפני שעה", NOW - timedelta(hours=1)),
        ("לפני 17 שעות", NOW - timedelta(hours=17)),
        ("1 ימים", NOW - timedelta(days=1)),
        ("לפני 6 ימים", NOW - timedelta(days=6)),
        ("אתמול", NOW - timedelta(days=1)),
        ("12/08/2026", datetime(2026, 8, 12)),
    ],
)
def test_the_boards_wording_parses(raw: str, expected: datetime) -> None:
    stamp, ok = parse_date(raw, now=NOW)
    assert ok
    assert stamp == expected.isoformat(timespec="seconds")


@pytest.mark.parametrize("raw", ["", "בקרוב", "לפני זמן מה", "32/13/2026"])
def test_an_unreadable_date_is_admitted_not_invented(raw: str) -> None:
    """A guessed timestamp would silently pass or fail the freshness gate."""
    stamp, ok = parse_date(raw, now=NOW)
    assert not ok
    assert stamp == ""


def test_the_original_wording_survives_next_to_the_parse(page: str) -> None:
    for posting in alljobs.parse(page, now=NOW)["postings"]:
        assert posting.posted_raw


# --------------------------------------------------------------------------
# regions
# --------------------------------------------------------------------------


def test_the_excluded_regions_are_never_requested() -> None:
    codes = alljobs.regions_for(load_spec())
    assert not set(codes) & set(alljobs.EXCLUDED_CODES)


def test_the_shfela_code_is_included_with_the_centre() -> None:
    """The spec's `center` names Rishon LeZion and Rehovot, which the board
    files under שפלה. Querying only the מרכז code loses them."""
    codes = alljobs.regions_for(load_spec())
    assert 8 in codes
    assert 2 in codes


def test_israeli_remote_is_requested_and_abroad_is_not() -> None:
    spec = load_spec()
    assert spec["geography"]["remote"]["israel_based"] is True
    assert spec["geography"]["remote"]["international"] is False
    codes = alljobs.regions_for(spec)
    assert 11 in codes
    assert 5 not in codes


# --------------------------------------------------------------------------
# search terms
# --------------------------------------------------------------------------


def test_the_crawl_is_driven_by_every_term_in_the_spec() -> None:
    """Browsing regions and taking everything does not survive the volume:
    Haifa alone runs about eighty pages to reach one day. Searching terms is
    what makes a daily run a few minutes instead of thousands of requests."""
    spec = load_spec()
    terms = alljobs.search_terms(spec)

    assert len(terms) == len(set(terms))  # a term shared by two families is asked once
    for family in spec["families"].values():
        for term in [*family["terms_he"], *family["terms_en"]]:
            assert term in terms


def test_the_term_reaches_the_url_encoded() -> None:
    fetcher = FixtureFetcher({"freetxt=": (SAMPLES_DIR / "alljobs_page.html")})
    alljobs.crawl(fetcher, spec=load_spec(), now=NOW, terms=["בינה מלאכותית"], max_pages=1)

    encoded = quote("בינה מלאכותית")
    assert f"freetxt={encoded}" in fetcher.requested[0]


def test_a_posting_found_by_several_terms_is_kept_once_and_credits_both(page: str) -> None:
    """Overlap between terms is expected. It is a routing signal, not waste."""
    fetcher = FixtureFetcher({"freetxt=": page})

    result = alljobs.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["אנליסט", "דאטה"], max_pages=1
    )

    ids = [posting.external_id for posting in result.postings]
    assert len(ids) == len(set(ids))
    for external_id in ids:
        assert result.matched_terms[external_id] == ["אנליסט", "דאטה"]


# --------------------------------------------------------------------------
# the throttle
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def test_the_throttle_paces_requests() -> None:
    clock = FakeClock()
    throttle = Throttle(0.5, clock=clock, sleeper=clock.sleep)
    for _ in range(3):
        throttle.wait()
    assert clock.t == pytest.approx(4.0)  # two gaps of two seconds


def test_no_request_bypasses_the_throttle(page: str) -> None:
    clock = FakeClock()
    throttle = Throttle(0.5, clock=clock, sleeper=clock.sleep)
    fetcher = ThrottledFetcher(FixtureFetcher({"region=1": page}), throttle)

    alljobs.crawl(fetcher, spec=load_spec(), now=NOW, terms=["בדיקה"], regions=[1], max_pages=3)

    assert len(fetcher.urls) >= 1
    assert clock.t == pytest.approx(2.0 * (len(fetcher.urls) - 1))


# --------------------------------------------------------------------------
# crawling
# --------------------------------------------------------------------------


def test_a_repeated_posting_is_stored_once(page: str) -> None:
    """Consecutive pages on this board do repeat items. Observed, not assumed."""
    fetcher = FixtureFetcher({"region=1": page})
    result = alljobs.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["בדיקה"], regions=[1], max_pages=3
    )

    ids = [posting.external_id for posting in result.postings]
    assert len(ids) == len(set(ids))


def test_the_crawl_stops_when_a_page_is_entirely_stale(page: str) -> None:
    # The saved page states its dates relatively, so it is fresh whenever it is
    # read. Restating them absolutely is what makes the page old.
    stale = re.sub(r'(job-content-top-date">)[^<]*', r"\g<1>01/01/2026", page)
    fetcher = FixtureFetcher({"region=1": stale})

    result = alljobs.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["בדיקה"], regions=[1], max_pages=6
    )

    # Assert the reason, not only the empty list. An empty result is also what
    # a failed fetch produces, and that is exactly how this test passed on
    # macOS while the fetch was raising on Linux.
    assert result.errors == []
    assert result.postings == []
    assert "older than the window" in result.stopped_because
    assert result.pages_fetched == 1  # it did not keep paging into stale ground


def test_the_fixture_fetcher_serves_a_document_without_touching_the_disk() -> None:
    """A fixture value is either a path or a document, decided by its type.

    Deciding by asking whether the value exists as a path is what broke: a
    whole HTML document answers False on macOS and raises ENAMETOOLONG on
    Linux, so the sniffing version passed locally and failed in CI.
    """
    document = "<html>" + ("x" * 5000) + "</html>"
    fetcher = FixtureFetcher({"any": document})

    assert fetcher.get("https://example.test/any") == document


class BrokenFetcher:
    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.calls = 0

    def get(self, url: str) -> str:
        self.calls += 1
        # endswith, not `in`: "region=1" is a prefix of "region=10", and a
        # substring match here would fail both regions and hide the bug.
        if url.endswith(self.fail_on):
            raise FetchError("boom")
        return (SAMPLES_DIR / "alljobs_page.html").read_text(encoding="utf-8")


def test_a_failed_query_does_not_take_the_others_down() -> None:
    fetcher = BrokenFetcher(fail_on="region=1")

    result = alljobs.crawl(
        fetcher, spec=load_spec(), now=NOW, terms=["בדיקה"], regions=[1, 10], max_pages=1
    )

    assert result.errors  # the failure is reported
    assert not result.ok
    assert result.postings  # and the healthy region still produced
