"""XPlace — the sixth site module, and the only one that is not a job board.

Everything else in this package reads a board where employers advertise
positions. XPlace is a freelance marketplace: a client posts a piece of work,
freelancers bid on it, and one of them is picked. The spec marks this site
`pipeline: freelance` for that reason, and the difference is not cosmetic. A
project has no seniority requirement and no degree requirement, so the two
gates that decide most postings in this repo have nothing to read here and
would pass every project on silence. What decides a project is its scope, its
budget, its deadline and how many freelancers have already bid — and this
module exists to carry those four facts intact to `desk.freelance`, which is
the only consumer that knows what to do with them.

What the live site turned out to be, probed on 2026-08-19 and re-verified the
same day against a second fetch. Each of these cost something to find out and
each one is pinned by a test:

**The project feed is public.** No login, no cookie, no session. `/jobs` is
every industry at once and `/<industry>/jobs` is one shelf of the same feed;
both come back 200 to a plain GET and both carry their projects fully. So the
attached-browser path this repo reserves for LinkedIn and Jobify is not needed
here, and is not used.

**The shelves are walked, never queried.** A jobs URL carrying a query string
is off-limits here, which is why this module walks the shelf paths and never
paginates by parameter. `rate_limit_rps: 0.1` in the spec is one request every
ten seconds, the slowest rate of any site in this repo.

**There is no pagination, and the site is not shy about how much it is
withholding.** `?page=`, `?page=2` and `?pageNumber=` are all accepted, all
ignored, and all return page zero again — byte-identical project ids and
identical meta — the same silent-200 trap Drushim sets, except that here no URL
shape works, because the feed's "load more" is a client-side call rather than a
link. That is also the shape this module must not build, so the absence of a page
parameter on `url_for` is compliance as much as it is a workaround, and a test
asserts no URL built here can ever carry one. Every shelf therefore yields its
newest twenty projects and no more. The payload states the rest of the
arithmetic itself — `{"page":0,"size":20,"total":61,"totalPages":4}` on the
developer shelf — so the module reports the projects it could not reach as a
number rather than leaving a truncated feed looking complete. Walking all 21
shelves is 21 requests and reaches at most 420 of the site's ~481 open
projects.

**There is no free-text search.** Like GotFriends, this module is driven by the
site's own shelves rather than by the spec's family terms, and a term passed to
`crawl` is reported in `result.notes` rather than silently dropped.

**The rendered card is not enough, and the page's own data payload is.** A card
states a title, a posted date and a budget. It does not state the description,
the deadline, or the number of bids already in — which are three of the four
facts the freelance flow judges on. Those live in the React Server Components
payload the page ships to its own client, server-rendered into the HTML as a
series of `self.__next_f.push` chunks. That payload is JSON, unlike the Nuxt
expression Drushim carries, so it is read rather than evaluated. The chunks are
cut at arbitrary byte offsets, mid-object and mid-string, so they are joined
before anything is parsed; a parser that read one script tag alone would work
on short pages and fail on long ones.

Reading the payload rather than the card is a real bet on a private shape, and
it is taken with its eyes open: if the payload disappears the module says so
and returns nothing, rather than falling back to the card. A row without a
budget, a deadline and a bid count is not a thin freelance row, it is a row the
freelance flow cannot judge, and quietly producing one would be worse than
producing none.

**No project page is ever fetched.** The feed payload already carries the full
description and the exact bid count, so a per-project request would buy
nothing. It would also cost something, and this was checked rather than
assumed: a single project page fetched during the probe carried the client's
given name, surname, company name and an Israeli mobile number in plain text —
the identity the shelf deliberately hides behind "לצפייה בלקוח". That is a
reason to stay on the feed, not a field to collect. Nothing in this module
requests a project page, and none of that data is stored anywhere.

Two fields come back deliberately empty, for the same reason they do on
GotFriends — filling them would be an invention:

    `company` — the client is behind a login. Every card says "לצפייה בלקוח"
    and nothing else. Writing "XPlace" there would poison the fingerprint with
    a name no client has.

    `location` — the site states none, anywhere. A freelance project is remote
    unless its text says otherwise, and its text is the description.

The consequence is that a project's fingerprint is its title alone. Titles here
are long free text written by the client, so collisions are unlikely rather
than impossible, and `UNIQUE(site, external_id)` keeps two projects as two rows
regardless. It is stated here so that a future collision reads as a known cost
rather than a bug.

**`payment_model` is carried as the integer the site sends and is not mapped.**
The observed values are 1, 2 and 5; the site never renders a unit next to the
amount, so ₪200 at model 1 and ₪15,000 at model 5 cannot be told apart as
hourly, fixed or retainer from anything reachable without a login. Guessing
would put a number in front of a human with a unit nobody verified. The integer
travels, and naming it is a question for the owner.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .base import Fetcher, RawPosting, SiteResult

SITE = "xplace"
BASE_URL = "https://www.xplace.com"

# The site's own industry filing, taken from `/il/browse` on 2026-08-19. The
# spec names no subset for this site — unlike GotFriends, whose entry carries a
# `categories:` list — so the default is everything the site publishes, and
# narrowing it is an argument to `crawl` or an edit to `spec/search.yaml`.
#
# "all" is the site's undivided feed. It is listed first and it is not a
# superset in practice: it returns the newest twenty of everything, which on a
# busy day is twenty projects none of the shelves' first pages would show.
SHELVES: dict[str, str] = {
    "all": "/jobs",
    "admin": "/admin/jobs",
    "architecture": "/architecture/jobs",
    "blockchain": "/blockchain/jobs",
    "coaching": "/coaching/jobs",
    "design": "/design/jobs",
    "dev": "/dev/jobs",
    "engineering": "/engineering/jobs",
    "executives": "/executives/jobs",
    "finance": "/finance/jobs",
    "legal": "/legal/jobs",
    "manufacturing": "/manufacturing/jobs",
    "marketing": "/marketing/jobs",
    "music": "/music/jobs",
    "photography": "/photography/jobs",
    "sap": "/sap/jobs",
    "tech": "/tech/jobs",
    "training": "/training/jobs",
    "translation": "/translation/jobs",
    "web": "/web/jobs",
    "work_from_home": "/work_from_home/jobs",
    "writing": "/writing/jobs",
}

# The site's own crowding ladder, in its own words, least crowded first. It is
# recorded rather than converted to a threshold: how many bids is too many is a
# judgment, and this module does not make judgments. The exact count travels
# next to it, because the site sends that too and it is strictly more
# informative — the developer shelf routinely shows forty to eighty bids inside
# a single band.
BID_BANDS: tuple[str, ...] = (
    "NO_BIDS_YET",
    "HAS_1_3_BIDS",
    "HAS_4_10_BIDS",
    "HAS_11_20_BIDS",
    "HAS_21_PLUS_BIDS",
)

# The currency the site quotes in. It renders a bare ₪ and states no code, so
# this is the one piece of vocabulary here that is ours rather than the site's,
# and it is a label on a symbol rather than a conversion.
CURRENCY = "ILS"

_PUSH = re.compile(r"self\.__next_f\.push\((\[.*?\])\)\s*</script>", re.DOTALL)
_META = re.compile(r'"meta":\s*(\{[^}]*\})')

# The seam between the facts and the client's own prose in a stored body.
FACTS_MARK = "xplace-project"
BODY_SEPARATOR = "---"

# Category names contain commas of their own — "VBA, Office, Excel Programming" is
# one shelf, not three — so the list separator has to be something a name cannot
# hold. The same middle dot the site results use elsewhere in this repo.
CATEGORY_SEPARATOR = " · "


class PayloadMissing(Exception):
    """The page carried no readable project payload. Not downgraded to a card."""


@dataclass(frozen=True)
class Project:
    """One freelance project as XPlace stated it, with nothing inferred.

    Every optional field is `None` when the site said nothing, never zero and
    never a default. A project with no budget and a project budgeted at nothing
    are different facts, and the flow that reads this has to be able to tell
    them apart — a budget silently defaulted to 0 would read as an insulting
    offer rather than as an open question to put to the client.
    """

    external_id: str
    title: str
    description: str
    budget: float | None = None
    payment_model: int | None = None
    posted_at: str = ""
    due_date: str = ""
    bids_close_at: str = ""
    bids: int | None = None
    bids_band: str = ""
    categories: tuple[str, ...] = ()
    urgent: bool = False
    nda: bool = False

    @property
    def url(self) -> str:
        return f"{BASE_URL}/project?id={self.external_id}"


def url_for(shelf: str) -> str:
    """Where a shelf lives. There is no page argument, and that is the point.

    Drushim's trap is that `?page=2` silently re-serves page one, and the fix
    there was a different URL shape. Here there is no shape that works: the
    feed's own pager is a client-side call, so page zero is the whole public
    surface. A `page` parameter on this function would be a promise the site
    does not keep, so it does not exist and a test asserts no URL built here
    ever carries one.
    """
    try:
        path = SHELVES[shelf]
    except KeyError as exc:
        raise KeyError(f"{shelf!r} is not a known xplace shelf") from exc
    return BASE_URL + path


def flight(html: str) -> str:
    """The page's data payload, reassembled.

    Next.js streams its server payload as a run of `self.__next_f.push([1, "…"])`
    calls whose string chunks concatenate into one document. The cuts fall
    wherever the stream happened to flush — inside an object, inside a string,
    inside a Hebrew word — so every chunk is joined before anything looks for a
    field. Chunks that are not the `[1, "…"]` shape are the framework's own
    bookkeeping and are skipped rather than treated as an error.
    """
    parts: list[str] = []
    for raw in _PUSH.findall(html):
        try:
            call = json.loads(raw)
        except ValueError:
            continue
        if isinstance(call, list) and len(call) > 1 and isinstance(call[1], str):
            parts.append(call[1])
    return "".join(parts)


def _json_after(payload: str, key: str, opener: str, closer: str) -> Any:
    """Read one JSON value out of a larger document by matching its brackets.

    The payload is a JSON document embedded in a JavaScript string with other
    JavaScript around it, so it cannot be parsed whole. Matching brackets from
    the key is what turns "somewhere in this stream there is an items array"
    into a value, and the scan tracks string state so a bracket inside a
    description — which Hebrew project text does contain — does not end it.
    """
    start = payload.find(key)
    if start < 0:
        raise PayloadMissing(f"the payload carries no {key}")
    start = payload.find(opener, start + len(key))
    if start < 0:
        raise PayloadMissing(f"{key} is present but opens nothing")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(payload)):
        char = payload[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return json.loads(payload[start : index + 1])
    raise PayloadMissing(f"{key} was never closed")


def _epoch(value: Any) -> str:
    """Epoch milliseconds to an ISO date, or the empty string.

    Same rule as `dates.py`: a timestamp that cannot be read comes back empty
    rather than invented. UTC because the site sends UTC milliseconds and a
    local-time guess would move a deadline by a day at the wrong hour.
    """
    if not isinstance(value, int | float) or value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(number)


def project_from_item(item: Mapping[str, Any]) -> Project:
    """One feed row to a `Project`. Pure, and it invents nothing."""
    categories = tuple(
        str(c.get("nameHe") or c.get("nameEn") or "").strip()
        for c in item.get("categories") or ()
        if isinstance(c, Mapping) and (c.get("nameHe") or c.get("nameEn"))
    )
    return Project(
        external_id=str(item.get("projectId", "")).strip(),
        title=" ".join(str(item.get("name") or "").split()),
        description=str(item.get("description") or "").strip(),
        budget=_number(item.get("amount_pj")),
        payment_model=_int(item.get("payment_model")),
        posted_at=_epoch(item.get("date_posted_facet")),
        due_date=_epoch(item.get("project_due_date")),
        bids_close_at=_epoch(item.get("expirationDate")),
        bids=_int(item.get("bids_number_facet")),
        bids_band=str(item.get("projectBidsNumberRangeFacet") or "").strip(),
        categories=categories,
        urgent=bool(item.get("urgent")),
        nda=bool(item.get("nda")),
    )


def render_body(project: Project) -> str:
    """The project's facts and its prose, in one text column.

    This is the awkward part of the design and it is deliberate rather than
    lazy. `RawPosting` and the `postings` table were both shaped for a job, and
    neither has a column for a budget, a deadline or a bid count; adding one is
    a change to files this module does not own. So the four facts the freelance
    flow judges on ride in the body, above a separator, in a fixed labelled
    block that `parse_body` reads back. One format, one writer, one reader.

    A field the site did not state is written as an empty value rather than
    omitted. The block then has the same shape on every project, and "the
    client stated no budget" is visible in the stored text instead of being
    something a reader has to notice is missing.
    """
    fields = (
        ("budget", "" if project.budget is None else f"{project.budget:g}"),
        ("currency", CURRENCY if project.budget is not None else ""),
        ("payment_model", "" if project.payment_model is None else str(project.payment_model)),
        ("due_date", project.due_date),
        ("bids_close_at", project.bids_close_at),
        ("bids", "" if project.bids is None else str(project.bids)),
        ("bids_band", project.bids_band),
        ("categories", CATEGORY_SEPARATOR.join(project.categories)),
        ("urgent", "yes" if project.urgent else "no"),
        ("nda", "yes" if project.nda else "no"),
    )
    header = "\n".join([FACTS_MARK, *(f"{name}: {value}" for name, value in fields)])
    return f"{header}\n{BODY_SEPARATOR}\n{project.description}"


def parse_body(body: str) -> tuple[dict[str, str], str]:
    """The facts block and the client's prose, back out of a stored body.

    A body without the block is not an error here. Any posting in the store can
    be handed to this — a Drushim row reached by fingerprint, a project stored
    before the block existed — and the honest answer for those is no facts and
    all prose, which is what the freelance flow needs to see so it can refuse
    the project instead of judging it on defaults.
    """
    text = body or ""
    if not text.lstrip().startswith(FACTS_MARK):
        return {}, text.strip()

    head, separator, prose = text.partition(f"\n{BODY_SEPARATOR}\n")
    if not separator:
        return {}, text.strip()

    facts: dict[str, str] = {}
    for line in head.splitlines()[1:]:
        name, colon, value = line.partition(":")
        if colon:
            facts[name.strip()] = value.strip()
    return facts, prose.strip()


def to_raw(project: Project) -> RawPosting:
    """A `Project` as the rest of the pipeline sees it.

    `company` and `location` stay empty on purpose; the module docstring says
    why. `posted_raw` carries the site's own epoch-derived date so a wrong
    parse can be told from a wrong feed.
    """
    return RawPosting(
        site=SITE,
        external_id=project.external_id,
        title=project.title,
        company="",
        location="",
        url=project.url,
        body=render_body(project),
        posted_at=project.posted_at,
        posted_raw=project.posted_at,
        # A freelance project is remote unless its own text says otherwise, and
        # the site offers no field that says either. Left blank rather than
        # asserted, for the same reason `location` is.
        work_arrangement="",
    )


def parse(html: str) -> dict[str, Any]:
    """Pure. No network, no store, and no clock.

    `now` is not a parameter, for the reason it is not one on GotFriends: every
    date on this feed is an absolute epoch the site sends, so there is nothing
    to resolve a relative wording against and taking a clock would suggest
    otherwise.

    A page with no payload raises rather than returning nothing. Nothing is the
    same answer this feed gives on a genuinely empty shelf, and the two must
    not look alike: one is a shelf with no work on it and the other is a layout
    change that has silently blinded the crawler.
    """
    payload = flight(html)
    if not payload:
        raise PayloadMissing("the page carried no self.__next_f payload at all")

    items = _json_after(payload, '"items"', "[", "]")
    if not isinstance(items, list):
        raise PayloadMissing("the payload's items field is not a list")

    meta_match = _META.search(payload)
    meta: dict[str, Any] = {}
    if meta_match:
        try:
            meta = json.loads(meta_match.group(1))
        except ValueError:
            meta = {}

    postings: list[RawPosting] = []
    projects: list[Project] = []
    skipped: dict[str, int] = {}
    missing: dict[str, int] = {}
    seen: set[str] = set()

    for item in items:
        if not isinstance(item, Mapping):
            skipped["a feed row that was not an object"] = (
                skipped.get("a feed row that was not an object", 0) + 1
            )
            continue

        project = project_from_item(item)
        if not project.external_id:
            skipped["no project id"] = skipped.get("no project id", 0) + 1
            continue
        if project.external_id in seen:
            skipped["the same project twice in one feed"] = (
                skipped.get("the same project twice in one feed", 0) + 1
            )
            continue
        seen.add(project.external_id)

        # Counted, never dropped. A project the client left silent about its
        # budget is the commonest shape on this site, and the freelance flow
        # exists partly to say so out loud.
        for name, stated in (
            ("title", bool(project.title)),
            ("description", bool(project.description)),
            ("budget", project.budget is not None),
            ("due_date", bool(project.due_date)),
            ("bids", project.bids is not None),
            ("posted_at", bool(project.posted_at)),
        ):
            if not stated:
                missing[name] = missing.get(name, 0) + 1

        projects.append(project)
        postings.append(to_raw(project))

    return {
        "postings": postings,
        "projects": projects,
        "skipped": skipped,
        "missing": missing,
        "meta": meta,
    }


def shelves_from_spec(spec: Mapping[str, Any]) -> list[str]:
    """Which shelves to walk.

    The spec's `xplace` entry carries no shelf list today, so this returns all
    of them. It reads the spec anyway rather than returning the constant
    directly, so that the day a `shelves:` key is added under the site entry it
    takes effect without a code change — which is the rule the whole repo runs
    on.
    """
    for entry in spec.get("sites", []):
        if entry["id"] == SITE:
            named = list(entry.get("shelves", []) or [])
            return named or list(SHELVES)
    raise KeyError(f"{SITE} is not in the spec")


def crawl(
    fetcher: Fetcher,
    *,
    spec: Mapping[str, Any],
    shelves: Sequence[str] | None = None,
    terms: Sequence[str] | None = None,
    **_ignored: Any,
) -> SiteResult:
    """One request per shelf, because one request per shelf is all there is.

    There is no page loop here and its absence is the finding. The other
    modules stop on an empty page, on a page entirely outside the freshness
    window, or on a page that repeated what was already held. This feed serves
    page zero to every request, so the only honest loop is no loop, and what
    would have been a stop condition becomes a count of what could not be
    reached — reported per shelf out of the feed's own `meta`.

    `max_age_days` is accepted through `**_ignored` and does not filter
    anything. The spec's freshness window exists because a job board leaves
    dead adverts up; this site states per project the date bidding closes, so
    a project is live by its client's own word and a seven-day window would
    drop work that is open for another month. That is said in `notes` on every
    run rather than left to be discovered.
    """
    result = SiteResult(site=SITE)
    found: dict[str, RawPosting] = {}
    matched: dict[str, list[str]] = {}
    stops: list[str] = []

    result.notes.append(
        "this site paginates client-side, so every shelf yields its newest 20 projects "
        "and no more; the unreached count per shelf is the feed's own arithmetic"
    )
    result.notes.append(
        "the client is behind a login and the site states no location, so company and "
        "location are empty on every row here by design"
    )
    result.notes.append(
        "freelance pipeline: the seniority and degree gates have nothing to read on a "
        "project and would pass every one of them on silence; scope, budget, deadline "
        "and bid count travel in the body instead"
    )
    if terms:
        result.notes.append(
            f"the site has no free-text search, so the {len(terms)} search term(s) passed "
            "did not reach it; the crawl was driven by shelves"
        )

    unreached = 0
    wanted = list(shelves) if shelves is not None else shelves_from_spec(spec)
    for shelf in wanted:
        if shelf not in SHELVES:
            result.errors.append(f"{shelf!r} is not a known xplace shelf")
            continue

        try:
            parsed = parse(fetcher.get(url_for(shelf)))
        except Exception as exc:  # this shelf's problem, not the run's
            result.errors.append(f"{shelf}: {exc}")
            continue

        result.pages_fetched += 1
        for reason, count in parsed["skipped"].items():
            result.skipped[reason] = result.skipped.get(reason, 0) + count

        for posting in parsed["postings"]:
            if posting.external_id not in found:
                found[posting.external_id] = posting
                matched[posting.external_id] = []
            if shelf not in matched[posting.external_id]:
                # Which shelf a project sits on is the site's own filing and
                # the only classification it offers, so it is recorded where
                # the other modules record their search terms.
                matched[posting.external_id].append(shelf)

        meta = parsed["meta"]
        total = int(meta.get("total") or 0)
        served = len(parsed["postings"])
        behind = max(0, total - served)
        unreached += behind
        stops.append(
            f"{shelf}: the feed served {served} of {total} and paginates client-side"
            if behind
            else f"{shelf}: the feed served all {served} it has"
        )

    if unreached:
        result.notes.append(
            f"{unreached} open projects across the walked shelves are behind the "
            "client-side pager and were not reached"
        )

    result.postings = list(found.values())
    result.matched_terms = matched
    result.stopped_because = " · ".join(stops)
    return result
