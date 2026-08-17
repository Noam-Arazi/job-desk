"""Drushim — the second site module.

It overlaps AllJobs heavily, which is the point: the resolver collapses the
same role arriving from both, and a role that only one board carries is the
reason to read both.

What is different here, and what it cost to find out:

**Pagination is in the path, not in a parameter.** `?page=2` returns page one
again — quietly, with a 200 and a full page of results. Nothing about the
response says the parameter was ignored, so a crawler that trusted it would
fetch the same twenty-five listings twenty times and report a clean run. The
working shape is `/jobs/search/<term>/2/`, and a test pins it.

**The page is server-rendered, and that is the only reason this works.** The
site is a Nuxt application and it carries its full state as a minified script
at the bottom, richer than the HTML — but that payload is an expression with
substituted variables, not JSON, and parsing it would mean evaluating
JavaScript. The rendered markup carries every field needed, so it is what is
read.

**No VIP split.** Unlike AllJobs, every card here is applicable, so nothing is
dropped. That is asserted rather than assumed: if the board grows a closed
tier, the fixture test that counts what came back starts failing.

**A bonus field.** Each card states the experience it wants — "ללא נסיון",
"שנה", "3 שנים" — as its own element, next to the location. The seniority
gate in session 5 reads that directly instead of inferring it from prose.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from . import http
from .base import Fetcher, RawPosting, SiteResult, crawl_terms, search_terms
from .dates import parse_date

SITE = "drushim"
BASE_URL = "https://www.drushim.co.il"
SEARCH = BASE_URL + "/jobs/search/{term}/"

CARD = "div.job-item"

FIELDS: dict[str, tuple[str, ...]] = {
    "id": ("div[listingid]",),
    "title": ("span.job-url", "h3 span"),
    "company": (".job-details-top .bidi", "a.black--text span"),
    "date": ("span.display-18.inline-flex",),
    "link": ("a[href^='/job/']",),
    "arrangement": (".region-item span", ".region-item"),
}

# Location and required experience sit in the same strip, separated by pipes,
# with no class of their own to grab. Reading the strip and splitting it is
# steadier than depending on sibling order in a generated layout.
DETAILS_STRIP = ".job-details-sub"

_EXPERIENCE = re.compile(r"(ללא נסיון|ללא ניסיון|\d+\s*(?:שנים|שנה))")


def _first(card: Any, selectors: tuple[str, ...]) -> Any:
    for selector in selectors:
        found = card.css(selector)
        if found:
            return found[0]
    return None


def _text(card: Any, selectors: tuple[str, ...]) -> str:
    node = _first(card, selectors)
    if node is None:
        return ""
    return " ".join(str(node.get_all_text()).split())


def _details(card: Any) -> tuple[str, str]:
    """Location and required experience, out of the pipe-separated strip."""
    strip = _first(card, (DETAILS_STRIP,))
    if strip is None:
        return "", ""

    parts = [" ".join(part.split()) for part in str(strip.get_all_text()).split("|")]
    parts = [part for part in parts if part]
    if not parts:
        return "", ""

    experience = ""
    for part in parts:
        match = _EXPERIENCE.search(part)
        if match:
            experience = match.group(1)
            break
    return parts[0], experience


def parse(html: str, *, now: datetime) -> dict[str, Any]:
    """Pure. No network, no store, no clock of its own — hence testable."""
    page = http.selector(html)
    postings: list[RawPosting] = []
    skipped: dict[str, int] = {}
    missing: dict[str, int] = {}

    for card in page.css(CARD):
        holder = _first(card, FIELDS["id"])
        external_id = str(holder.attrib.get("listingid", "")) if holder is not None else ""
        if not external_id:
            skipped["no id"] = skipped.get("no id", 0) + 1
            continue

        link = _first(card, FIELDS["link"])
        href = str(link.attrib.get("href", "")) if link is not None else ""

        raw_date = _text(card, FIELDS["date"])
        posted_at, parsed = parse_date(raw_date, now=now)
        if not parsed:
            missing["posted_at"] = missing.get("posted_at", 0) + 1

        title = _text(card, FIELDS["title"])
        company = _text(card, FIELDS["company"])
        location, experience = _details(card)
        for name, value in (("title", title), ("company", company), ("location", location)):
            if not value:
                missing[name] = missing.get(name, 0) + 1

        postings.append(
            RawPosting(
                site=SITE,
                external_id=external_id,
                title=title,
                company=company,
                location=location,
                url=BASE_URL + href if href.startswith("/") else href,
                body=_body(card),
                posted_at=posted_at,
                posted_raw=raw_date,
                work_arrangement=_text(card, FIELDS["arrangement"]),
                stated_experience=experience,
            )
        )

    return {"postings": postings, "skipped": skipped, "missing": missing}


def _body(card: Any) -> str:
    """The description. The company name sits in a paragraph too, so the
    company's own block is excluded by class rather than by position."""
    blocks = [
        block
        for block in card.css("p.view-on-submit")
        if "display-22" not in str(block.attrib.get("class", ""))
    ]
    if not blocks:
        return ""
    return " ".join(str(blocks[0].get_all_text()).split())


def url_for(term: str, page: int, _variant: Any = "") -> str:
    """Page one is the bare path; every later page appends its number.

    `?page=` is accepted by the server and silently ignored, which is the
    failure mode this shape exists to avoid.
    """
    url = SEARCH.format(term=quote(term))
    return url if page <= 1 else f"{url}{page}/"


def crawl(
    fetcher: Fetcher,
    *,
    spec: dict[str, Any],
    now: datetime,
    max_pages: int = 6,
    max_age_days: int | None = None,
    terms: list[str] | None = None,
    **_ignored: Any,
) -> SiteResult:
    if max_age_days is None:
        max_age_days = int(spec["gates"]["freshness"]["max_age_days"])

    return crawl_terms(
        site=SITE,
        fetcher=fetcher,
        url_for=url_for,
        parse_page=lambda html: parse(html, now=now),
        terms=terms if terms is not None else search_terms(spec),
        variants=[""],
        cutoff=(now - timedelta(days=max_age_days)).isoformat(timespec="seconds"),
        max_pages=max_pages,
    )
