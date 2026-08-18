"""GotFriends — the third site module, and the one that breaks the mould.

AllJobs and Drushim are job boards: employers post, the board indexes, and a
free-text search returns what matched. GotFriends is a placement agency that
publishes its own open roles. Three consequences follow, and each of them is a
design change rather than a parsing detail.

**There is no free-text search.** The filter panel offers a field radio, a
profession checkbox list and a region checkbox list, and nothing else — no
query box exists to send a term to. So this module is not driven by the
spec's search terms. It walks the agency's own profession pages, and the
category list lives in `spec/search.yaml` under the site entry. A term passed
to `crawl` is not silently ignored; it is reported in `result.notes`.

That is a loss of reach and a gain in precision. `/jobslobby/ai/ai-engineer/`
is a hand-curated shelf, so its page one is worth more than a keyword's page
one — but a role the agency filed under a category we do not walk is
invisible to us, and no amount of paging will surface it.

**No posting carries a date.** Not on the card, not on the posting's own page,
not in a `<time>` element, not in JSON-LD — checked on all three, 2026-08-17.
The only date the site states is its own, in the header: "האתר עודכן בתאריך".
So `posted_at` comes back empty on every posting from this board, per the rule
in `dates.py`: a date that cannot be read is not invented. The freshness gate
has nothing from the board to gate on, and recency here means the store's
first-seen — the fingerprints table already records it. This is stated in
`result.notes` on every run so it can never be mistaken for a parse failure.

**No posting names its employer.** The agency anonymises the client and sells
it in prose instead: "בחברת סטארט-אפ בתחום הסייבר". `company` is therefore
empty rather than filled with the agency's name or with that phrase. Both
alternatives would be inventions, and both would poison the fingerprint —
which is title + company + location — for the cross-site resolver. The honest
cost is that the same role reaching us from AllJobs with its real employer
named will not fingerprint-match its GotFriends copy. That is the resolver's
problem to solve on content, and it is better than a resolver quietly matching
on a name we made up.

Three traps found by reading real pages, each pinned by a test:

**Paging past the end returns the last page, forever.** `?page=99` on a
five-page category comes back 200 with the last page's three cards, byte for
byte identical to `?page=5`. A crawl that walked until it got an empty page
would never get one. So the stop condition is a page that contributed no id
this category had not already seen — which catches the clamp on the first
repeat. Note that this is the exact mirror of Drushim, where the trap is at
the front: there `?page=2` silently re-serves page one.

**The printed job number is not reliable, and it is not unique.** One card in
the fixture prints no "מס' משרה" at all. Two others print the same number and
differ only by a `-1` suffix on the URL: the agency published one role twice.
So identity is the printed number where the card has one, the URL's numeric id
where it does not, and the second card of a duplicated number is dropped with
its reason counted.

**Old listings have text slugs, not numeric ones.** `/llm-engineer/154095/`
on page one, `/llm-engineer/tech-lead-nlp/` on page five. Anything that read
recency off the URL would work for a while and then quietly stop working, so
nothing here does.
"""

from __future__ import annotations

import re
from typing import Any

from . import http
from .base import Fetcher, RawPosting, SiteResult

SITE = "gotfriends"
BASE_URL = "https://www.gotfriends.co.il"

# The agency's profession shelves, by the slug `spec/search.yaml` names. The
# area segment is the site's own filing and is not derivable from the slug —
# `data-engineer` sits under software while `data-analyst` sits under BI — so
# the full path is written out. A spec category missing from this map is an
# error rather than a skip: it means the spec and this module disagree about
# what exists, and that should stop being true, not be tolerated.
CATEGORIES: dict[str, str] = {
    "ai-engineer": "/jobslobby/ai/ai-engineer/",
    "llm-engineer": "/jobslobby/ai/llm-engineer/",
    "solution-architect": "/jobslobby/ai/solution-architect/",
    "data-analyst": "/jobslobby/bibig_data/data-analyst/",
    "bi-developer": "/jobslobby/bibig_data/bi-developer/",
    "product-analyst": "/jobslobby/bibig_data/product-analyst/",
    "data-engineer": "/jobslobby/software/data-engineer/",
    "data-scientist": "/jobslobby/algorithm/data-scientist/",
    "product-manager": "/jobslobby/projects/product-manager/",
}

# The board files every role into one of eight coarse buckets — there are no
# city names anywhere on a card. The spec's geography is a list of cities, so
# a bucket cannot decide a posting on its own; it can only narrow it. Mapped
# here so the gates in session 5 read one table instead of guessing, and so
# the two lossy edges are written down rather than discovered later:
# the board fuses Haifa with the whole north, and "אחר" is its own honest
# admission that the role is somewhere else entirely.
REGIONS: dict[str, str] = {
    'ת"א והמרכז': "center",
    "שפלה": "center",  # the spec counts Rishon LeZion and Rehovot as centre
    "השרון": "sharon",
    "חיפה והצפון": "haifa+north",  # one bucket for two of the spec's regions
    "ירושלים": "jerusalem",  # excluded by the spec
    "באר שבע והדרום": "south",  # excluded by the spec
    "אילת": "eilat",  # excluded by the spec
    "אחר": "",  # the board's own bucket for unknown. Not a parse failure.
}

CARD = ".careers_list .item"

FIELDS: dict[str, tuple[str, ...]] = {
    "link": ("a.position", "a[href*='/jobslobby/']"),
    "title": ("h2.title", ".title"),
    "career_num": (".career_num",),
    "location": (".info .info-data", ".info-data"),
    "section": (".desc",),
    "section_label": (".title_c",),
}

_CAREER_NUM = re.compile(r"(\d+)")
_SLUG_ID = re.compile(r"/(\d+)(?:-\d+)?/?$")


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


def _identity(card: Any, href: str) -> tuple[str, str]:
    """The posting's id, and where it came from.

    The printed number first, because it is the agency's own identity for the
    role and it is what collapses a role the agency published twice. The URL's
    number second, for the cards that print none. The bare slug last, so a
    posting is never dropped merely for being old enough to have a text URL.
    """
    printed = _CAREER_NUM.search(_text(card, FIELDS["career_num"]))
    if printed:
        return printed.group(1), "career number"

    in_url = _SLUG_ID.search(href)
    if in_url:
        return in_url.group(1), "url"

    slug = href.rstrip("/").rsplit("/", 1)[-1]
    return slug, "slug"


def _body(card: Any) -> str:
    """Description and requirements, with the board's own headings kept.

    The requirements block is where this board states the years it wants and
    the degree it asks for, so dropping it would blind both gates. The heading
    stays because the seam between "what the job is" and "what it demands"
    is worth something to the analyst, and the board draws it for free.
    """
    parts: list[str] = []
    for section in card.css(FIELDS["section"][0]):
        text = " ".join(str(section.get_all_text()).split())
        if text:
            parts.append(text)
    return "\n".join(parts)


def parse(html: str) -> dict[str, Any]:
    """Pure. No network, no store, and — on this board — no clock either.

    `now` is not a parameter, unlike the other two modules, because there is
    no date on the page for it to resolve against. Taking one and ignoring it
    would suggest otherwise.
    """
    page = http.selector(html)
    postings: list[RawPosting] = []
    skipped: dict[str, int] = {}
    missing: dict[str, int] = {}
    seen: set[str] = set()

    for card in page.css(CARD):
        link = _first(card, FIELDS["link"])
        href = str(link.attrib.get("href", "")) if link is not None else ""
        if not href:
            skipped["no link"] = skipped.get("no link", 0) + 1
            continue

        external_id, source = _identity(card, href)
        if source != "career number":
            missing["career_num"] = missing.get("career_num", 0) + 1
        if external_id in seen:
            # The agency published one role under two nodes. Same job number,
            # two URLs. Collapsed here and counted, rather than left for the
            # store's UNIQUE(site, external_id) to swallow without a word.
            skipped["a second card with the same job number"] = (
                skipped.get("a second card with the same job number", 0) + 1
            )
            continue
        seen.add(external_id)

        title = _text(card, FIELDS["title"])
        location = _text(card, FIELDS["location"])
        body = _body(card)
        for name, value in (("title", title), ("location", location), ("body", body)):
            if not value:
                missing[name] = missing.get(name, 0) + 1

        postings.append(
            RawPosting(
                site=SITE,
                external_id=external_id,
                title=title,
                # Deliberately empty. The agency anonymises its clients; see
                # the module docstring for why neither the agency's name nor
                # the "בחברת ..." phrase goes here.
                company="",
                location=location,
                url=BASE_URL + href if href.startswith("/") else href,
                body=body,
                # Empty on every posting: this board publishes no dates.
                posted_at="",
                posted_raw="",
            )
        )

    return {"postings": postings, "skipped": skipped, "missing": missing}


def url_for(category: str, page: int) -> str:
    """Page one is the bare shelf; later pages take `?page=`.

    The board also emits `&total=` in its own pager links. It is the page
    count, not the result count, and the server does not need it — omitted so
    a stale number cannot become part of a request.
    """
    path = CATEGORIES[category]
    return BASE_URL + path + ("" if page <= 1 else f"?page={page}")


def categories_from_spec(spec: dict[str, Any]) -> list[str]:
    for entry in spec.get("sites", []):
        if entry["id"] == SITE:
            return list(entry.get("categories", []))
    raise KeyError(f"{SITE} is not in the spec")


def crawl(
    fetcher: Fetcher,
    *,
    spec: dict[str, Any],
    max_pages: int = 6,
    categories: list[str] | None = None,
    terms: list[str] | None = None,
    **_ignored: Any,
) -> SiteResult:
    """Walk each category until a page brings nothing this walk has not seen.

    The stop condition is not "an empty page" and it cannot be. Paging past
    the last page returns the last page again, with a 200 and a full card
    list, so a crawl waiting for emptiness waits forever. It is not "the first
    old item" either — there are no dates to be old.

    What is left is the honest one: a page whose ids are all already in hand.
    That fires on the first repeat at the clamp, and it also fires early on a
    shelf that simply has nothing more to give.

    A category that raises is recorded and the walk moves to the next one. One
    dead shelf never ends the run.
    """
    result = SiteResult(site=SITE)
    found: dict[str, RawPosting] = {}
    matched: dict[str, list[str]] = {}
    stops: list[str] = []

    result.notes.append(
        "this board publishes no posting dates, so posted_at is empty on every "
        "row here by design — recency is the store's first-seen, not the board's word"
    )
    if terms:
        result.notes.append(
            f"the board has no free-text search, so the {len(terms)} search term(s) "
            "passed did not reach it; the crawl was driven by categories"
        )

    wanted = categories if categories is not None else categories_from_spec(spec)
    for category in wanted:
        if category not in CATEGORIES:
            result.errors.append(f"{category!r} is not a known category page")
            continue

        seen_here: set[str] = set()
        for page in range(1, max_pages + 1):
            try:
                parsed = parse(fetcher.get(url_for(category, page)))
            except Exception as exc:  # this shelf's problem, not the run's
                result.errors.append(f"{category} page {page}: {exc}")
                break

            result.pages_fetched += 1
            for reason, count in parsed["skipped"].items():
                result.skipped[reason] = result.skipped.get(reason, 0) + count

            new = 0
            for posting in parsed["postings"]:
                if posting.external_id not in seen_here:
                    seen_here.add(posting.external_id)
                    new += 1
                if posting.external_id not in found:
                    found[posting.external_id] = posting
                    matched[posting.external_id] = []
                if category not in matched[posting.external_id]:
                    # Which shelf a role sits on is a stronger prior for
                    # routing it to a CV family than a keyword that merely
                    # appeared in it. Recorded in the same place either way.
                    matched[posting.external_id].append(category)

            if not parsed["postings"]:
                stops.append(f"{category}: empty page {page}")
                break
            if new == 0:
                stops.append(
                    f"{category}: page {page} repeated cards already held — "
                    "the board clamps past its last page"
                )
                break
        else:
            stops.append(f"{category}: hit the {max_pages}-page ceiling")

    result.postings = list(found.values())
    result.matched_terms = matched
    result.stopped_because = " · ".join(stops)
    return result
