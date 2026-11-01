"""Jobify — read through Noam's signed-in Chrome.

28.10.2026. Jobify sorts its whole inventory against the CV in Noam's account
and shelves it under the roles he picked there (`/myjobs`). That list exists
only signed in, so this module reads it through the attached browser
(`sites/browser.py`) and through nothing else; with the window closed it
returns an error and the morning goes on without it.

Two kinds of page, both read as HTML:

    /myjobs                      the shelves: one `selectLobbyRole('<slug>')` per role
    /myjob-roles/<slug>          the cards on one shelf: title, company, match
    /jobs/<id>-<origin>          one posting: city, and the full description

The match percentage and the salary band are Jobify's own estimates — the
site labels the salary AI-derived. Neither is stored as a fact; the analyst
scores the description against Noam's spec like any other board's.

Nothing is clicked, typed or submitted. Pages are loaded at the browser's
pace and read.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from . import http
from .base import Fetcher, RawPosting, SiteResult
from .linkedin import in_direction

SITE = "jobify"
ROOT = "https://jobify360.co.il"
SHELVES_URL = f"{ROOT}/myjobs"

# What the browser waits for before reading: whichever of the three page kinds
# it is on. The shelves and cards arrive after the first paint (Livewire).
READY = "[onclick*=selectLobbyRole], a.title[href*='/jobs/'], div.content-text-main"

_SHELF = re.compile(r"selectLobbyRole\('([^']+)'")
_JOB_ID = re.compile(r"/jobs/([^/?#]+)")


def shelf_url(slug: str) -> str:
    return f"{ROOT}/myjob-roles/{slug}"


def parse_shelves(html: str) -> list[str]:
    """The role slugs on `/myjobs`, in the order the site shows them."""
    slugs: list[str] = []
    for slug in _SHELF.findall(html):
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def parse_shelf(html: str) -> list[dict[str, str]]:
    """The cards on one shelf. A card is its title link and the company under it."""
    page = http.selector(html)
    cards: list[dict[str, str]] = []
    for link in page.css("a.title[href*='/jobs/']"):
        href = str(link.attrib.get("href", ""))
        found = _JOB_ID.search(href)
        if not found:
            continue
        header = link.parent
        company = header.css("p.nm") if header is not None else []
        cards.append(
            {
                "external_id": found.group(1),
                "url": href if href.startswith("http") else ROOT + href,
                "title": " ".join(str(link.get_all_text()).split()),
                "company": " ".join(str(company[0].get_all_text()).split()) if company else "",
            }
        )
    return cards


def parse_detail(html: str) -> dict[str, str]:
    """City and description from one posting page."""
    page = http.selector(html)
    info = page.css("li.job-info-item span")
    body = page.css("div.content-text-main div.white-border-box")
    company = page.css("p.company-name")
    return {
        "location": " ".join(str(info[0].get_all_text()).split()) if info else "",
        # The outer block also carries Jobify's AI-tools strip and its
        # feedback dialog; the description is the box inside it.
        "body": max(
            (str(box.get_all_text(separator="\n")).strip() for box in body), key=len, default=""
        ),
        "company": " ".join(str(company[0].get_all_text()).split()) if company else "",
    }


def settings_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    for entry in spec.get("sites", []):
        if entry.get("id") == SITE:
            return {"max_details": int(entry.get("max_details", 150))}
    return {"max_details": 150}


def crawl(
    fetcher: Fetcher,
    *,
    spec: dict[str, Any],
    detail_fetcher: Fetcher | None = None,
    have_body: set[str] | None = None,
    max_details: int | None = None,
    now: datetime | None = None,
    **_ignored: Any,
) -> SiteResult:
    """Every shelf, every card, and a description for the ones still missing one.

    `fetcher` is the plain HTTP one every module receives and is not used: a
    logged-out request gets the sign-in page. The browser is `detail_fetcher`.
    """
    result = SiteResult(site=SITE)
    if detail_fetcher is None:
        result.errors.append("the job-desk Chrome window is not open; Jobify needs it signed in")
        return result
    browser = detail_fetcher
    budget = settings_from_spec(spec)["max_details"] if max_details is None else max_details
    known = have_body or set()
    stamp = (now or datetime.now()).isoformat(timespec="seconds")

    slugs = parse_shelves(browser.get(SHELVES_URL))
    result.pages_fetched += 1
    if not slugs:
        result.errors.append("no shelves on /myjobs — signed out, or the page changed")
        return result

    found: dict[str, RawPosting] = {}
    matched: dict[str, list[str]] = {}
    for slug in slugs:
        try:
            cards = parse_shelf(browser.get(shelf_url(slug)))
        except Exception as exc:
            result.errors.append(f"shelf {slug}: {exc}")
            continue
        result.pages_fetched += 1
        for card in cards:
            key = card["external_id"]
            if key not in found:
                found[key] = RawPosting(
                    site=SITE,
                    external_id=key,
                    title=card["title"],
                    company=card["company"],
                    url=card["url"],
                )
                matched[key] = []
            matched[key].append(slug)

    wanted = [p for p in found.values() if p.external_id not in known]
    wanted.sort(key=lambda p: not in_direction(p.title, spec))
    for posting in wanted[:budget]:
        try:
            detail = parse_detail(browser.get(posting.url))
        except Exception as exc:
            result.errors.append(f"detail {posting.external_id}: {exc}")
            continue
        result.pages_fetched += 1
        found[posting.external_id] = RawPosting(
            site=SITE,
            external_id=posting.external_id,
            title=posting.title,
            company=detail["company"] or posting.company,
            location=detail["location"],
            url=posting.url,
            body=detail["body"],
        )
    if len(wanted) > budget:
        result.notes.append(
            f"{len(wanted) - budget} postings kept their card only: the detail ceiling is {budget}"
        )
    result.notes.append(f"{len(slugs)} shelves read; first seen {stamp}")

    result.postings = list(found.values())
    result.matched_terms = matched
    result.stopped_because = f"read every shelf ({len(slugs)})"
    return result
