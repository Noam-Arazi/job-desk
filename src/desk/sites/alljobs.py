"""AllJobs — the first site module.

First because it carries the largest inventory in `spec/search.yaml`, and
because it hands back the entire posting body inside the results page. One
request yields thirty complete postings; there is no per-posting detail fetch
and therefore no request amplification.

Two things learned by reading the real pages, both of which shape the design:

**Half the results cannot be applied to.** A results page is two boards, and
the page announces the seam with a banner reading "לוח ללקוחות VIP בלבד".
Everything above it sits in a container classed `open-board`: the ordinary
listings, employer named, applications open. Everything below sits inside
`divOrganicContainer` and is classed `organic-board` — the VIP-client board,
employer hidden, and a human reaching one has no way to submit. The split is
almost exactly even, so keeping the VIP half would double the digest with
items that dead-end.

The naming is the board's own and it is inverted from what the words suggest,
so it is worth stating plainly: `organic-board` is the VIP board and it is the
one dropped. The check is on the container's class rather than on the banner
text, because a class survives rewording, but a test pins the two together so
a rename cannot silently flip the filter.

Dropping is deterministic and the count is reported rather than swallowed.

**The pages are not strictly ordered by date.** Page 1 can end at seventeen
hours old and page 2 open at ten minutes, and consecutive pages can repeat a
posting. So the crawl cannot stop at the first old item. It stops on a page
that contributes nothing fresh, and the store's uniqueness on
(site, external_id) absorbs the repeats.

**The crawl is driven by the search terms, not by geography.** Browsing a
region and taking everything was the first design and it does not survive
contact with the volume: measured 2026-08-17, Haifa alone runs about eighty
pages to reach a single day, so one week across the accepted regions is
thousands of requests a day. Searching a term instead, the first page already
reaches between one and six days back. Forty-nine terms at a few pages each is
roughly a hundred and fifty requests, which is a five-minute daily job.

Geography is therefore not filtered here at all. The board's own region codes
are kept below because a targeted run can still use them, but the ordinary
path fetches nationally per term and leaves the region rules to the
deterministic gates, which read the spec's city lists directly and can be
precise in a way a region code is not.

Selectors are lists of candidates rather than single strings. When the board
edits its markup the parser tries the next candidate instead of silently
returning nothing, and `parse` reports fields it could not fill.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from . import http
from .base import Fetcher, RawPosting, SiteResult

SITE = "alljobs"
BASE_URL = "https://www.alljobs.co.il"
SEARCH = (
    BASE_URL + "/SearchResultsGuest.aspx"
    "?page={page}&position=&type=&freetxt={term}&city=&region={region}"
)

# The board's own region codes, mapped onto the regions named in
# spec/search.yaml. Established by probing every code once, 2026-08-17.
#
#   1 חיפה · 2 מרכז · 3 ירושלים · 4 אילת והערבה · 5 חו"ל · 6 שרון
#   7 דרום · 8 שפלה · 9 יהודה ושומרון · 10 צפון · 11 עבודה מהבית · 12 גוש עציון
#
# Note 8. The spec's `center` names Rishon LeZion and Rehovot, and AllJobs
# files both under שפלה, not under מרכז. Querying only code 2 would silently
# lose them, which is exactly the kind of miss a scraper never reports.
REGION_CODES: dict[str, tuple[int, ...]] = {
    "haifa": (1,),
    "north": (10,),
    "sharon": (6,),
    "center": (2, 8),
    "remote": (11,),
}

EXCLUDED_CODES = {
    3: "jerusalem",
    4: "eilat and the arava",
    5: "abroad",
    7: "south",
    9: "judea and samaria",
    12: "gush etzion",
}

# The board a card sits on. Only the first can be applied to.
APPLICABLE_BOARD = "open-board"
UNAPPLICABLE_BOARDS = {"organic-board": "VIP clients only — no way to submit"}

# The banner and wrapper the board puts above its VIP half. Not used to filter
# — the class above does that — but pinned by a test so a rename is caught.
VIP_CONTAINER = "divOrganicContainer"
VIP_BANNER = "לוח ללקוחות"

CARD = "div[id^=job-box-container]"

# Substring class matching, not exact: a promoted listing renames its title
# block to `job-content-top-title-highlight`, which an exact `.job-content-top-title`
# silently misses. That miss cost an afternoon, so it is pinned by a test.
FIELDS: dict[str, tuple[str, ...]] = {
    "title": ("[class*=job-content-top-title] h2", "h2", "a[href*=UploadSingle][title]"),
    "company": ("[class*=job-content-top-title] .T14 a", ".T14 a", ".T14"),
    "location": (".job-content-top-location",),
    "arrangement": ("[class*=job-content-top-type]",),
    "date": (".job-content-top-date",),
    "link": ("a[href*=UploadSingle]",),
}

_LABEL = re.compile(r"^\s*(מיקום המשרה|סוג משרה)\s*:\s*")
_JOB_ID = re.compile(r"JobID=(\d+)")
_CONTAINER_ID = re.compile(r"job-box-container(\d+)")


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------

_REL = re.compile(r"לפני\s+(\d+)?\s*(דקה|דקות|שעה|שעות|יום|ימים|שבוע|שבועות)")
_BARE_DAYS = re.compile(r"^(\d+)\s+ימים")
_ABSOLUTE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})")

_UNITS = {
    "דקה": "minutes",
    "דקות": "minutes",
    "שעה": "hours",
    "שעות": "hours",
    "יום": "days",
    "ימים": "days",
    "שבוע": "weeks",
    "שבועות": "weeks",
}


def parse_date(raw: str, *, now: datetime) -> tuple[str, bool]:
    """Turn the board's wording into ISO 8601.

    Returns the timestamp and whether it parsed. An unparsed date is not
    guessed at and not dropped — it travels as an empty string with the
    original wording preserved, and the freshness gate decides in session 5.
    A scraper that invents a date it could not read is worse than one that
    admits it does not know.
    """
    text = (raw or "").strip()
    if not text:
        return "", False

    if "אתמול" in text:
        return (now - timedelta(days=1)).isoformat(timespec="seconds"), True
    if "היום" in text:
        return now.isoformat(timespec="seconds"), True

    match = _REL.search(text)
    if match:
        amount = int(match.group(1)) if match.group(1) else 1
        unit = _UNITS[match.group(2)]
        return (now - timedelta(**{unit: amount})).isoformat(timespec="seconds"), True

    bare = _BARE_DAYS.match(text)
    if bare:
        return (now - timedelta(days=int(bare.group(1)))).isoformat(timespec="seconds"), True

    absolute = _ABSOLUTE.search(text)
    if absolute:
        day, month, year = (int(g) for g in absolute.groups())
        year += 2000 if year < 100 else 0
        try:
            return datetime(year, month, day).isoformat(timespec="seconds"), True
        except ValueError:
            return "", False

    return "", False


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


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
    return _LABEL.sub("", " ".join(str(node.get_all_text()).split())).strip()


def _board_of(card: Any) -> str:
    classes = str(card.attrib.get("class", ""))
    for name in (APPLICABLE_BOARD, *UNAPPLICABLE_BOARDS):
        if name in classes:
            return name
    return ""


def parse(html: str, *, now: datetime) -> dict[str, Any]:
    """Pure. No network, no store, no clock of its own — hence testable.

    Returns the postings that can be applied to, plus a per-reason count of
    what was dropped and which fields came back empty.
    """
    page = http.selector(html)
    postings: list[RawPosting] = []
    skipped: dict[str, int] = {}
    missing: dict[str, int] = {}

    for card in page.css(CARD):
        board = _board_of(card)
        if board != APPLICABLE_BOARD:
            reason = UNAPPLICABLE_BOARDS.get(board, f"unknown board {board or '(none)'}")
            skipped[reason] = skipped.get(reason, 0) + 1
            continue

        link = _first(card, FIELDS["link"])
        href = str(link.attrib.get("href", "")) if link is not None else ""
        job_id = _JOB_ID.search(href)
        container = _CONTAINER_ID.search(str(card.attrib.get("id", "")))
        external_id = job_id.group(1) if job_id else (container.group(1) if container else "")
        if not external_id:
            skipped["no id"] = skipped.get("no id", 0) + 1
            continue

        raw_date = _text(card, FIELDS["date"])
        posted_at, parsed = parse_date(raw_date, now=now)
        if not parsed:
            missing["posted_at"] = missing.get("posted_at", 0) + 1

        title = _text(card, FIELDS["title"])
        company = _text(card, FIELDS["company"])
        for name, value in (("title", title), ("company", company)):
            if not value:
                missing[name] = missing.get(name, 0) + 1

        postings.append(
            RawPosting(
                site=SITE,
                external_id=external_id,
                title=title,
                company=company,
                location=_text(card, FIELDS["location"]),
                url=BASE_URL + href if href.startswith("/") else href,
                body=_body(card),
                posted_at=posted_at,
                posted_raw=raw_date,
                work_arrangement=_text(card, FIELDS["arrangement"]),
            )
        )

    return {"postings": postings, "skipped": skipped, "missing": missing}


def _body(card: Any) -> str:
    """The description, without the 'more jobs at this company' trailer.

    The card carries two `.job-content-top-desc` blocks. The first is the ad;
    the second is a link to the employer's other listings and is not content.
    """
    blocks = card.css(".job-content-top-acord .job-content-top-desc")
    if not blocks:
        blocks = card.css(".job-content-top-desc")
    if not blocks:
        return ""
    return "\n".join(line for line in str(blocks[0].get_all_text()).splitlines() if line.strip())


# --------------------------------------------------------------------------
# crawling
# --------------------------------------------------------------------------


def search_terms(spec: dict[str, Any]) -> list[str]:
    """Every term the spec declares, in family order, without repeats.

    A term that finds nothing is not pruned here. The spec says the width was
    raised deliberately and comes down on measurement in session 5, not on a
    hunch in the fetching layer.
    """
    terms: list[str] = []
    for family in spec.get("families", {}).values():
        for term in [*family.get("terms_he", []), *family.get("terms_en", [])]:
            if term not in terms:
                terms.append(term)
    return terms


def regions_for(spec: dict[str, Any]) -> list[int]:
    """The board's codes for the regions the spec accepts. Order is stable."""
    geography = spec["geography"]
    wanted = list(geography["regions"])
    if geography.get("remote", {}).get("israel_based"):
        wanted.append("remote")

    codes: list[int] = []
    for region in wanted:
        for code in REGION_CODES.get(region, ()):
            if code not in codes:
                codes.append(code)
    return codes


def crawl(
    fetcher: Fetcher,
    *,
    spec: dict[str, Any],
    now: datetime,
    max_pages: int = 6,
    max_age_days: int | None = None,
    terms: list[str] | None = None,
    regions: list[int] | None = None,
) -> SiteResult:
    """Search each term until it stops yielding anything inside the window.

    A page that raises is recorded and that term moves on; one broken query
    does not end the crawl, and the result still carries what was collected.

    The same posting is reached by several terms, which is not waste — the
    terms that found it are recorded, and they are a strong prior for routing
    it to a CV family later. It is still stored once.
    """
    if max_age_days is None:
        max_age_days = int(spec["gates"]["freshness"]["max_age_days"])
    cutoff = (now - timedelta(days=max_age_days)).isoformat(timespec="seconds")

    queries = terms if terms is not None else search_terms(spec)
    region_codes = regions if regions is not None else [""]

    result = SiteResult(site=SITE)
    found: dict[str, RawPosting] = {}
    matched: dict[str, list[str]] = {}
    skipped: dict[str, int] = {}
    stops: list[str] = []

    for term in queries:
        for region in region_codes:
            label = f"{term!r}" + (f" region {region}" if region != "" else "")
            for page in range(1, max_pages + 1):
                url = SEARCH.format(page=page, term=quote(term), region=region)
                try:
                    html = fetcher.get(url)
                    parsed = parse(html, now=now)
                except Exception as exc:  # this page's problem, not the run's
                    result.errors.append(f"{label} page {page}: {exc}")
                    break

                result.pages_fetched += 1
                for reason, count in parsed["skipped"].items():
                    skipped[reason] = skipped.get(reason, 0) + count

                fresh = 0
                for posting in parsed["postings"]:
                    if posting.posted_at and posting.posted_at < cutoff:
                        continue
                    fresh += 1
                    if posting.external_id not in found:
                        found[posting.external_id] = posting
                        matched[posting.external_id] = []
                    if term not in matched[posting.external_id]:
                        matched[posting.external_id].append(term)

                if not parsed["postings"]:
                    stops.append(f"{label}: empty page {page}")
                    break
                if fresh == 0:
                    stops.append(f"{label}: page {page} was entirely older than the window")
                    break
            else:
                stops.append(f"{label}: hit the {max_pages}-page ceiling")

    result.postings = list(found.values())
    result.matched_terms = matched
    result.stopped_because = " · ".join(stops)
    result.skipped = skipped
    return result
