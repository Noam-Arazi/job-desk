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

from ..gates.seniority import title_is_blocked
from . import http, linkedin
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

# An id ending in `-in` is a LinkedIn job id that Jobify mirrors (02.11.2026).
# Some mirrors arrive with no description at all — 20 of 375 in the store on
# 13.11.2026 — and a posting with no body passes the seniority gate on silence:
# a Technical PM that LinkedIn itself marked 5+ years reached the digest as
# `unknown`. Where both copies had a body, the gate agreed on every one of 305,
# so the source is only asked when the mirror came back thin.
_FROM_LINKEDIN = re.compile(r"^(\d+)-in$")
THIN_BODY = 200


def linkedin_source(
    external_id: str, body: str, *, http_fetcher: Fetcher | None, browser: Fetcher
) -> dict[str, str] | None:
    """LinkedIn's own description and seniority band for a thin `-in` mirror."""
    mirrored = _FROM_LINKEDIN.match(external_id)
    if not mirrored or len(body.strip()) >= THIN_BODY:
        return None
    job_id = mirrored.group(1)
    # The logged-out endpoint first: no browser wait, and it carries the
    # criteria list. The signed-in page is the fallback when there is no HTTP.
    if http_fetcher is not None:
        detail = linkedin.parse_detail(http_fetcher.get(linkedin.detail_url(job_id)))
    else:
        detail = linkedin.parse_detail(browser.get(linkedin.view_url(job_id)))
    if len(detail["body"].strip()) <= len(body.strip()):
        return None
    return detail


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


# 04.11.2026. A shelf opens on the account's own date filter, and its default is
# "since my last visit". The morning pass read it at 04:11, Noam read it at noon,
# and whatever arrived in between was his visit's and never the store's: Matrix's
# AI Builder reached him and not the digest. A fixed window that overlaps the day
# before cannot lose a posting to anyone's visit; the store already drops what it
# holds. The filter is the site's own control, set the way its button sets it,
# and Jobify keeps it on the account. A shelf also shows 25 cards and loads the
# rest on scroll, so the script asks for more until it holds the shelf's total.
_WIDEN = """async (days) => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const shelf = () => {
    const el = [...document.querySelectorAll('[wire\\\\:id]')]
      .find((e) => (e.getAttribute('wire:snapshot') || '').includes('sanitizeRole'));
    return el ? Livewire.find(el.getAttribute('wire:id')) : null;
  };
  let c = shelf();
  if (!c) return {error: 'no shelf component'};
  if (String(c.get('publishDate')) !== String(days)) {
    const apply = document.querySelector('[onclick*="applyFilters(1"]');
    if (!apply) return {error: 'no filter button'};
    changepublishDate(String(days));
    $('#publishDateDropdown').val(String(days));
    apply.click();
    const widened = () => String(shelf().get('publishDate')) === String(days);
    for (let i = 0; i < 20 && !widened(); i++) await sleep(500);
    await sleep(2000);
    c = shelf();
  }
  const cards = () => document.querySelectorAll("a.title[href*='/jobs/']").length;
  for (let i = 0; i < 12 && cards() < Number(c.get('totalJobs') || 0); i++) {
    const before = cards();
    await c.call('loadMoreRoleJobsData');
    await sleep(1500);
    if (cards() === before) break;
  }
  const total = Number(c.get('totalJobs') || 0);
  return {window: String(c.get('publishDate')), total: total, cards: cards()};
}"""


def settings_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    for entry in spec.get("sites", []):
        if entry.get("id") == SITE:
            return {
                "max_details": int(entry.get("max_details", 150)),
                "window_days": int(entry.get("window_days", 3)),
            }
    return {"max_details": 150, "window_days": 3}


def read_shelf(browser: Fetcher, slug: str, window_days: int) -> tuple[str, str]:
    """One shelf's HTML, widened to the window and loaded to its last card.

    The second value is a note for the run when the shelf could not be widened
    or was left short; empty when it holds what the site says it holds. A
    fetcher that cannot run a script (the test fakes) reads the page as served.
    """
    html = browser.get(shelf_url(slug))
    run = getattr(browser, "run", None)
    if run is None:
        return html, ""
    state = run(_WIDEN, window_days)
    html = browser.html()  # type: ignore[attr-defined]
    if not isinstance(state, dict) or state.get("error"):
        return html, f"shelf {slug}: {state.get('error') if isinstance(state, dict) else state}"
    if state.get("window") != str(window_days):
        return html, f"shelf {slug}: window stayed '{state.get('window')}'"
    if state.get("cards", 0) < state.get("total", 0):
        return html, f"shelf {slug}: {state['cards']} of {state['total']} cards loaded"
    return html, ""


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

    `fetcher` is the plain HTTP one every module receives. Jobify itself is not
    read through it — a logged-out request gets the sign-in page — only
    LinkedIn's public page for a thin `-in` mirror. The browser is
    `detail_fetcher`.
    """
    result = SiteResult(site=SITE)
    if detail_fetcher is None:
        result.errors.append("the job-desk Chrome window is not open; Jobify needs it signed in")
        return result
    browser = detail_fetcher
    settings = settings_from_spec(spec)
    budget = settings["max_details"] if max_details is None else max_details
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
            html, short = read_shelf(browser, slug, settings["window_days"])
            cards = parse_shelf(html)
        except Exception as exc:
            result.errors.append(f"shelf {slug}: {exc}")
            continue
        if short:
            result.errors.append(short)
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

    wanted = [
        p
        for p in found.values()
        if p.external_id not in known and not title_is_blocked(p.title, spec)
    ]
    wanted.sort(key=lambda p: not in_direction(p.title, spec))
    thin_filled = 0
    for posting in wanted[:budget]:
        try:
            detail = parse_detail(browser.get(posting.url))
        except Exception as exc:
            result.errors.append(f"detail {posting.external_id}: {exc}")
            continue
        result.pages_fetched += 1
        body, stated = detail["body"], ""
        try:
            source = linkedin_source(
                posting.external_id, body, http_fetcher=fetcher, browser=browser
            )
        except Exception as exc:
            source = None
            result.errors.append(f"linkedin source of {posting.external_id}: {exc}")
        if source is not None:
            result.pages_fetched += 1
            body, stated = source["body"], source["stated_experience"]
            thin_filled += 1
        found[posting.external_id] = RawPosting(
            site=SITE,
            external_id=posting.external_id,
            title=posting.title,
            company=detail["company"] or posting.company,
            location=detail["location"],
            url=posting.url,
            body=body,
            stated_experience=stated,
        )
    if len(wanted) > budget:
        result.notes.append(
            f"{len(wanted) - budget} postings kept their card only: the detail ceiling is {budget}"
        )
    if thin_filled:
        result.notes.append(f"{thin_filled} thin LinkedIn mirrors read from LinkedIn itself")
    result.notes.append(f"{len(slugs)} shelves read; first seen {stamp}")

    result.postings = list(found.values())
    result.matched_terms = matched
    result.stopped_because = f"read every shelf ({len(slugs)})"
    return result
