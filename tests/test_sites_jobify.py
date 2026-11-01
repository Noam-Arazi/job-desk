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
