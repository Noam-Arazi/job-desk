"""Which stored projects can be proposed on at all, decided before a token.

This is the freelance flow's equivalent of the gate chain, and it is much
smaller on purpose. The gate chain has five gates because a job posting states
five things worth checking; a freelance project states almost none of them.
There is no seniority requirement to read, no degree demand, no city, and no
employer. So what is left to check deterministically is short, and pretending
otherwise — inventing a scope gate, a budget floor, a crowding ceiling — would
put thresholds in code that the specification has never agreed to.

What is checked here is only what can be answered from the project's own words:

    it has to be a freelance project. A posting whose body carries no facts
    block did not come through the xplace module, and a proposal drafted from a
    Drushim advert reached by fingerprint would be a category error the user
    would only discover by reading the output.

    bidding has to still be open. The site states, per project, the date it
    stops accepting bids. That is the client's own word about whether the work
    is live, and it is a far better answer than the spec's seven-day freshness
    window, which exists because job boards leave dead adverts up.

    it must not already be bid on. `already_applied: suppress` in the spec is
    about not resurfacing work Noam has already answered, and a bid is an
    answer.

Everything else — is the scope real, is the budget serious, are forty bidders
too many — is judgment, and judgment is the model's half in `proposal.py`. What
this file does instead is compute the facts that judgment needs and hand them
over unargued: how many days are left, where the project sits on the site's own
crowding ladder, whether a budget was stated at all.

The one threshold in this package comes from `spec/search.yaml` and is
borrowed rather than invented. The spec has no `freelance:` block today, so
there is no freelance-specific floor to read; writing one into this file would
put a number where nobody can edit it. `analyst.score.channel.skip_below` is
the floor the spec already states for "below this, recommend nothing", and it
is what the verdict here uses. A freelance-specific number is an edit to the
specification, made by the owner, and this code will read it the day it exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..sites.xplace import BID_BANDS, CATEGORY_SEPARATOR, SITE, parse_body

# What the command may conclude. Advice in both cases: `skip` is a
# recommendation not to bid, never a refusal to show the draft.
DRAFT = "draft"
SKIP = "skip"


class Refusal(Exception):
    """This project cannot be proposed on, and the reason says which check."""


@dataclass(frozen=True)
class ProjectView:
    """One stored project, with its facts read back out of its body.

    A view rather than a `Project`: the site module builds projects out of a
    live feed, and this is built out of a store row that may be weeks old and
    may have arrived before a field existed. Optional fields are `None` for
    "the client said nothing" and never zero — a project with no stated budget
    and a project budgeted at zero are different conversations to have.
    """

    fingerprint: str
    title: str
    url: str
    description: str
    site: str = SITE
    budget: float | None = None
    currency: str = ""
    payment_model: str = ""
    due_date: str = ""
    bids_close_at: str = ""
    bids: int | None = None
    bids_band: str = ""
    categories: tuple[str, ...] = ()
    urgent: bool = False
    nda: bool = False

    @property
    def budget_stated(self) -> bool:
        return self.budget is not None

    @property
    def deadline_stated(self) -> bool:
        return bool(self.due_date)

    def days_left(self, today: date) -> int | None:
        """Days until bidding closes, or None when the site stated no close."""
        if not self.bids_close_at:
            return None
        try:
            return (date.fromisoformat(self.bids_close_at) - today).days
        except ValueError:
            return None

    def crowding(self) -> str:
        """Where the project sits on the site's own ladder, in plain words.

        The ladder is the site's vocabulary and the position is arithmetic on
        it. Neither is a judgment about whether the project is worth bidding
        on — that sentence belongs to the model, which is shown this line and
        the exact count together.
        """
        if not self.bids_band:
            return "the site stated no bid count"
        try:
            rung = BID_BANDS.index(self.bids_band)
        except ValueError:
            return f"an unfamiliar bid band, {self.bids_band}"
        exact = "" if self.bids is None else f", {self.bids} so far"
        return f"rung {rung + 1} of {len(BID_BANDS)} on the site's bid ladder{exact}"


def _optional_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: str) -> int | None:
    number = _optional_float(value)
    return None if number is None else int(number)


def view_of(row: Mapping[str, Any]) -> ProjectView:
    """A store row to a view. Raises rather than guessing at a missing block.

    The refusal is the useful part. `desk propose --fingerprint <anything>`
    accepts any fingerprint in the store, and the commonest mistake this
    command invites is pointing it at a salaried posting from one of the three
    job boards. Answering that with a proposal about scope and bids would be
    confidently wrong; answering it with "this is not a freelance project" is
    correct and takes one line.
    """
    facts, prose = parse_body(str(row.get("body") or ""))
    if not facts:
        raise Refusal(
            f"{row.get('site') or 'this posting'} carries no freelance project block; "
            "`desk propose` reads projects fetched from xplace, not salaried postings"
        )

    raw_categories = facts.get("categories", "")
    return ProjectView(
        fingerprint=str(row.get("fingerprint") or ""),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        description=prose,
        site=str(row.get("site") or SITE),
        budget=_optional_float(facts.get("budget", "")),
        currency=facts.get("currency", ""),
        payment_model=facts.get("payment_model", ""),
        due_date=facts.get("due_date", ""),
        bids_close_at=facts.get("bids_close_at", ""),
        bids=_optional_int(facts.get("bids", "")),
        bids_band=facts.get("bids_band", ""),
        categories=tuple(
            part.strip() for part in raw_categories.split(CATEGORY_SEPARATOR) if part.strip()
        ),
        urgent=facts.get("urgent", "") == "yes",
        nda=facts.get("nda", "") == "yes",
    )


def screen(view: ProjectView, *, today: date, has_bid: bool = False) -> None:
    """The deterministic half. Raises `Refusal`, or returns having said nothing.

    Silence on success is deliberate: there is no "passed" object to store,
    because passing here establishes nothing about the project except that
    drafting a proposal for it is not obviously pointless.

    A project whose close date could not be read is allowed through rather than
    refused. The site states one on every project it has served so far, so a
    missing close date means the format moved, and refusing to draft because a
    scraper aged out would be blaming the wrong party. The model is told the
    date is unknown instead.
    """
    if has_bid:
        raise Refusal("a bid was already recorded for this project; nothing to draft again")

    remaining = view.days_left(today)
    if remaining is not None and remaining < 0:
        raise Refusal(
            f"bidding closed on {view.bids_close_at}, {abs(remaining)} day(s) ago; "
            "the client is no longer accepting proposals"
        )


def floor(spec: Mapping[str, Any]) -> float:
    """The score below which nothing is recommended, read from the spec.

    Borrowed from the analyst rather than invented here; the module docstring
    says why. Changing what counts as worth bidding on stays an edit to
    `spec/search.yaml`.
    """
    channel = ((spec.get("analyst") or {}).get("score") or {}).get("channel") or {}
    return float(channel.get("skip_below", 0.6))


def verdict_for(fit: float, *, spec: Mapping[str, Any]) -> str:
    """Derived from the score, never chosen by the model.

    The same rule the analyst's channel follows, and for the same reason: a
    verdict a model picks is a verdict that drifts warm, and there is no later
    stage that could notice. This one can be re-derived from a stored score and
    argued with by editing one line of the specification.
    """
    return DRAFT if fit >= floor(spec) else SKIP
