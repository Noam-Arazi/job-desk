"""Drushim — the second site module.

It overlaps AllJobs heavily, which is the point: the resolver collapses the
same role arriving from both, and a role that only one board carries is the
reason to read both.

**It returned nothing from August to November 2026, and said nothing.** The
site was rebuilt as a Next.js app. Every search URL went on answering 200, the
old card selector matched nothing, and each daily run reported zero postings as
a clean run. That is why `crawl` now fails loudly when a whole run parses zero
postings — see the bottom of this file.

What was measured on 2026-11-07, and what it cost to find out:

**The data comes from the site's own JSON API, the one its search page calls.**
`https://webapi.drushim.co.il/api/jobs/search?SearchTerm=<term>&ssaen=1&isAA=true`
(found in the page's `/_next/static` chunks: `getSearchJobs` →
`GET /api/jobs/search` on the `webapi` host). Plain GET, no login, no token, no
cookie; the site's robots.txt disallows only the `area=` form of this path,
which is not used here. The search page itself does still server-render page
one — the jobs sit in `__NEXT_DATA__` → `dehydratedState` → the
`search-results` query — but it renders page one only, so the API is read for
every page rather than mixing two sources.

**Pagination is a parameter again, and an uneven one.** No `page` gives the
first 25 results; `page=1, 2, …` give 10 each, continuing after those 25 with
no overlap (449 results for "אנליסט": 25 + 42 × 10 + 4). The old path form
`/jobs/search/<term>/2/` is now a 404, and `?page=` on the HTML page is
ignored. Past the end the API returns an empty list, which ends the term.

**The fetcher hands back HTML, not JSON.** `HttpFetcher` returns Scrapling's
`html_content`, which wraps the body in `<html><body>…</body></html>`, escapes
`&` as `&amp;`, and turns the `<br/>` inside descriptions into `<br>`. `_load`
undoes exactly that and nothing more. Raw JSON (the fixture) is read as is.

**The id is the old one.** The site has two codes per job. `Code` is in the
URL (`/job/38870127/…`); `JobInfo.STSJobCode` (30766447) is what the old cards
carried as `listingid` and what every stored drushim row is keyed by. Keeping
it keeps the store's history, applications and duplicates attached.

**The date is the bump, as before.** `JumpDateString` ("לפני 23 שעות") is the
age the board displays, and it is what `posted_raw` always held. `JumpDate`
is the original publication, often weeks older: a refreshed listing reads as
fresh, exactly as it did on the old cards.

**No match is not a match.** When a term finds nothing the site shows
`RecommendedJobs` instead; those are counted as skipped, never kept.
`ExtentionResultList` (the site's own widened search) is kept, as the page
shows it in the list.

**No VIP split.** Unlike AllJobs, every listing here is applicable, so nothing
is dropped. Expired listings, should the API ever return one, are skipped and
counted.

**A bonus field.** Each listing states the experience it wants — "ללא נסיון",
"1-2 שנים", "7 שנים ומעלה" — as its own field. The seniority gate reads that
directly instead of inferring it from prose.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from .base import Fetcher, RawPosting, SiteResult, crawl_terms, search_terms
from .dates import parse_date

SITE = "drushim"
BASE_URL = "https://www.drushim.co.il"
API = "https://webapi.drushim.co.il/api/jobs/search?SearchTerm={term}&ssaen=1&isAA=true"

# The range alternative comes first on purpose. Without it "3-5 שנים" matched
# only its tail, "5 שנים", and the stored field said the board demanded five
# years when it had written a range starting at three. The spec reads a range at
# its lower bound, so the truncation did not merely lose information: it turned
# a posting that passes the seniority ceiling into one that is blocked, and the
# evidence quoted back to the human was a sentence the board never wrote.
_EXPERIENCE = re.compile(
    r"(ללא נסיון|ללא ניסיון|\d+\s*[-–—]\s*\d+\s*(?:שנים|שנה)|\d+\s*(?:שנים|שנה))"
)

_WRAPPER = re.compile(r"^\s*<html><body>(.*)</body></html>\s*$", re.S)
_BREAK = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")


def _load(text: str) -> dict[str, Any]:
    """The API's JSON, from either the raw body or Scrapling's HTML wrapping of it."""
    wrapped = _WRAPPER.match(text)
    if wrapped:
        text = html.unescape(wrapped.group(1))
    try:
        data = json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not the search API's JSON ({exc}); starts {text[:80]!r}") from exc
    if not isinstance(data, dict) or "ResultList" not in data:
        raise ValueError("JSON without a ResultList — the API's shape changed")
    if data.get("IsError"):
        raise ValueError(f"the API reported an error: {data.get('ErrorsMessages')}")
    return data


def _plain(value: Any) -> str:
    """Advert HTML to one line of text. Line breaks become spaces, not nothing,
    or the last word of one line runs into the first word of the next."""
    text = _BREAK.sub(" ", str(value or ""))
    return " ".join(_TAG.sub(" ", html.unescape(text)).split())


def _experience(value: str) -> str:
    match = _EXPERIENCE.search(value)
    return match.group(1) if match else ""


def _posting(job: dict[str, Any], *, now: datetime, missing: dict[str, int]) -> RawPosting | None:
    info = job.get("JobInfo") or {}
    content = job.get("JobContent") or {}
    company = job.get("Company") or {}

    external_id = str(info.get("STSJobCode") or "")
    if not external_id:
        return None

    raw_date = str(info.get("JumpDateString") or "")
    posted_at, parsed = parse_date(raw_date, now=now)
    if not parsed:
        missing["posted_at"] = missing.get("posted_at", 0) + 1

    # The first city only. Location is part of the fingerprint, and a listing
    # posted to nine northern cities is still one role; joining them all would
    # make it a different role from the same listing seen anywhere else.
    cities = [str(a.get("City", "")).strip() for a in content.get("Addresses") or []]
    if not any(cities):
        cities = [str(r.get("NameInHebrew", "")).strip() for r in content.get("Regions") or []]
    location = next((c for c in cities if c), "")

    title = " ".join(str(content.get("Name") or "").split())
    company_name = " ".join(
        str(company.get("CompanyDisplayName") or company.get("NameInHebrew") or "").split()
    )
    for name, value in (("title", title), ("company", company_name), ("location", location)):
        if not value:
            missing[name] = missing.get(name, 0) + 1

    description = _plain(content.get("Description"))
    requirements = _plain(content.get("Requirements"))
    body = description + (f"\nדרישות: {requirements}" if requirements else "")

    link = str(info.get("Link") or "")
    return RawPosting(
        site=SITE,
        external_id=external_id,
        title=title,
        company=company_name,
        location=location,
        url=BASE_URL + link if link.startswith("/") else link,
        body=body,
        posted_at=posted_at,
        posted_raw=raw_date,
        work_arrangement=", ".join(
            str(s.get("NameInHebrew", "")) for s in content.get("Scopes") or []
        ),
        stated_experience=_experience(
            str((content.get("Experience") or {}).get("NameInHebrew") or "")
        ),
    )


def parse(text: str, *, now: datetime) -> dict[str, Any]:
    """Pure. No network, no store, no clock of its own — hence testable."""
    data = _load(text)
    postings: list[RawPosting] = []
    skipped: dict[str, int] = {}
    missing: dict[str, int] = {}

    jobs = list(data.get("ResultList") or []) + list(data.get("ExtentionResultList") or [])
    if not jobs and data.get("RecommendedJobs"):
        skipped["recommended in place of a match"] = len(data["RecommendedJobs"])

    for job in jobs:
        if (job.get("JobInfo") or {}).get("IsExpired"):
            skipped["expired"] = skipped.get("expired", 0) + 1
            continue
        posting = _posting(job, now=now, missing=missing)
        if posting is None:
            skipped["no id"] = skipped.get("no id", 0) + 1
            continue
        postings.append(posting)

    return {"postings": postings, "skipped": skipped, "missing": missing}


def url_for(term: str, page: int, _variant: Any = "") -> str:
    """Crawler page 1 is the API's unnumbered first page (25 results); crawler
    page n is the API's `page=n-1` (10 results each, continuing after those 25)."""
    url = API.format(term=quote(term))
    return url if page <= 1 else f"{url}&page={page - 1}"


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

    parsed_total = 0

    def parse_page(text: str) -> dict[str, Any]:
        nonlocal parsed_total
        result = parse(text, now=now)
        parsed_total += len(result["postings"])
        return result

    result = crawl_terms(
        site=SITE,
        fetcher=fetcher,
        url_for=url_for,
        parse_page=parse_page,
        terms=terms if terms is not None else search_terms(spec),
        variants=[""],
        cutoff=(now - timedelta(days=max_age_days)).isoformat(timespec="seconds"),
        max_pages=max_pages,
    )

    # The failure this module lived with for three months: every request a 200,
    # every page parsed, nothing in any of them. A run that reads zero postings
    # across every term is far likelier a changed site than an empty market, so
    # it is reported as a failed fetch rather than as a quiet, clean nothing.
    if result.pages_fetched and parsed_total == 0:
        result.errors.append(
            f"0 postings parsed across {result.pages_fetched} pages that answered — "
            "the site or its API has most likely changed shape; check before trusting "
            "an empty drushim run"
        )
    return result
