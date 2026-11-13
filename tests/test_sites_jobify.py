"""Jobify, read through the attached browser. Markup trimmed from the live
pages on 28.10.2026."""

# ruff: noqa: E501 — the fixtures are markup as the site serves it

from __future__ import annotations

from desk.config import load_spec
from desk.sites import jobify

SHELVES = """
<a href="javascript:;" onclick="selectLobbyRole('ai-engineer-7312708', 'myjob', 'AI Engineer')">AI</a>
<a href="javascript:;" onclick="selectLobbyRole('product-manager-7312742', 'myjob', 'PM')">PM</a>
<a href="javascript:;" onclick="selectLobbyRole('ai-engineer-7312708', 'myjob', 'AI Engineer')">AI</a>
"""

SHELF = """
<div class="card-header-start d-flex flex-column">
  <a wire:navigate="" href="https://jobify360.co.il/jobs/d8276eaefe2ea97b-id" class="h6 title"> AI Engineer </a>
  <p class="nm">365scores</p>
</div>
<a href="https://jobify360.co.il/jobs/d8276eaefe2ea97b-id" class="btn">פרטים נוספים</a>
"""

DETAIL = """
<p class="company-name">Unframe</p>
<ul><li class="job-info-item"><svg></svg> <span>תל אביב - יפו</span></li></ul>
<div class="content-text-main">
  <div>כל מה שצריך לדעת – רגע לפני ששולחים קורות חיים</div>
  <div class="white-border-box">About The Role
We build AI agents.</div>
  <div class="modal-content">מה הסיבה לפנייה שלך לגבי המשרה הזו?</div>
</div>
"""


class Pages:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str) -> str:
        self.urls.append(url)
        if url == jobify.SHELVES_URL:
            return SHELVES
        if "/myjob-roles/" in url:
            return SHELF
        return DETAIL


def test_the_shelves_are_the_roles_in_the_account_once_each() -> None:
    assert jobify.parse_shelves(SHELVES) == ["ai-engineer-7312708", "product-manager-7312742"]


def test_a_card_is_its_title_link_and_the_company_under_it() -> None:
    assert jobify.parse_shelf(SHELF) == [
        {
            "external_id": "d8276eaefe2ea97b-id",
            "url": "https://jobify360.co.il/jobs/d8276eaefe2ea97b-id",
            "title": "AI Engineer",
            "company": "365scores",
        }
    ]


def test_the_description_leaves_jobifys_own_tools_and_dialog_out() -> None:
    detail = jobify.parse_detail(DETAIL)
    assert detail["body"].startswith("About The Role")
    assert "רגע לפני" not in detail["body"] and "הסיבה לפנייה" not in detail["body"]
    assert detail["location"] == "תל אביב - יפו"
    assert detail["company"] == "Unframe"


def test_a_job_on_two_shelves_is_one_posting_found_by_both() -> None:
    result = jobify.crawl(None, spec=load_spec(), detail_fetcher=Pages())  # type: ignore[arg-type]

    assert len(result.postings) == 1
    posting = result.postings[0]
    assert posting.body.startswith("About The Role")
    assert result.matched_terms[posting.external_id] == [
        "ai-engineer-7312708",
        "product-manager-7312742",
    ]


def test_no_window_is_an_error_and_not_an_empty_board() -> None:
    result = jobify.crawl(None, spec=load_spec(), detail_fetcher=None)  # type: ignore[arg-type]
    assert result.errors and not result.postings


def test_a_body_already_stored_is_not_fetched_again() -> None:
    pages = Pages()
    jobify.crawl(None, spec=load_spec(), detail_fetcher=pages, have_body={"d8276eaefe2ea97b-id"})  # type: ignore[arg-type]
    assert not any("/jobs/" in url for url in pages.urls)


class Browser(Pages):
    """A fetcher that can run the shelf script, as the attached browser can."""

    def __init__(self, state: dict) -> None:
        super().__init__()
        self.state = state
        self.windows: list[int] = []

    def run(self, script: str, arg: int) -> dict:
        self.windows.append(arg)
        return self.state

    def html(self) -> str:
        return SHELF


def test_every_shelf_is_read_on_the_spec_window_and_not_the_last_visit() -> None:
    browser = Browser({"window": "3", "total": 1, "cards": 1})
    result = jobify.crawl(None, spec=load_spec(), detail_fetcher=browser)  # type: ignore[arg-type]

    assert browser.windows == [3, 3]
    assert not result.errors


def test_a_shelf_left_short_or_on_another_window_is_said_out_loud() -> None:
    left_short = Browser({"window": "3", "total": 31, "cards": 25})
    not_widened = Browser({"window": "last_visit", "total": 9, "cards": 9})
    short = jobify.crawl(None, spec=load_spec(), detail_fetcher=left_short)  # type: ignore[arg-type]
    stuck = jobify.crawl(None, spec=load_spec(), detail_fetcher=not_widened)  # type: ignore[arg-type]

    assert any("25 of 31" in e for e in short.errors)
    assert any("window stayed 'last_visit'" in e for e in stuck.errors)
    assert short.postings and stuck.postings


# 13.11.2026 — a LinkedIn mirror that arrives with no description passed the
# seniority gate on silence. The source page is asked only for a thin mirror.

LINKEDIN_DETAIL = """
<div class="show-more-less-html__markup">About the role. Requirements: 5+ years of experience
in product management, including hands-on work with technical teams and enterprise customers,
roadmaps, discovery and delivery of complex platform features end to end across many teams.</div>
<ul><li><h3 class="description__job-criteria-subheader">Seniority level</h3>
<span class="description__job-criteria-text">Mid-Senior level</span></li></ul>
"""

MIRROR_SHELF = SHELF.replace("d8276eaefe2ea97b-id", "4481327179-in")
EMPTY_DETAIL = '<p class="company-name">Unframe</p><div class="content-text-main"></div>'


class MirrorPages(Pages):
    def __init__(self, detail: str) -> None:
        super().__init__()
        self.detail = detail

    def get(self, url: str) -> str:
        if "/myjob-roles/" in url:
            self.urls.append(url)
            return MIRROR_SHELF
        if "/jobs/4481327179-in" in url:
            self.urls.append(url)
            return self.detail
        return super().get(url)


class Http:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str) -> str:
        self.urls.append(url)
        return LINKEDIN_DETAIL


def test_a_thin_linkedin_mirror_takes_linkedins_own_description() -> None:
    http = Http()
    result = jobify.crawl(http, spec=load_spec(), detail_fetcher=MirrorPages(EMPTY_DETAIL))  # type: ignore[arg-type]

    posting = result.postings[0]
    assert http.urls == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4481327179"]
    assert "5+ years of experience" in posting.body
    assert posting.stated_experience == "Mid-Senior level"


def test_a_mirror_with_its_own_description_does_not_ask_linkedin() -> None:
    http = Http()
    full = DETAIL.replace("We build AI agents.", "We build AI agents. " * 20)
    result = jobify.crawl(http, spec=load_spec(), detail_fetcher=MirrorPages(full))  # type: ignore[arg-type]

    assert http.urls == []
    assert result.postings[0].body.startswith("About The Role")


def test_linkedin_failing_keeps_the_mirror_and_says_so() -> None:
    class Down:
        def get(self, url: str) -> str:
            raise RuntimeError("429")

    result = jobify.crawl(Down(), spec=load_spec(), detail_fetcher=MirrorPages(EMPTY_DETAIL))  # type: ignore[arg-type]

    assert len(result.postings) == 1
    assert any("linkedin source" in e for e in result.errors)
