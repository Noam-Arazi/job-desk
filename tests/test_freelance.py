"""What the freelance flow has to keep being true.

Three groups, and the third is the one that matters most.

The selection tests pin the small deterministic half: a project is refused
before a token is spent, or the facts a judgment needs are computed and handed
over unargued.

The proposal tests pin the price check. It is the only thing standing between a
model that has drifted into helpfulness and a number in front of somebody who is
about to negotiate, so it is tested from both sides — that the client's own
stated budget survives, and that nothing else does.

The structural tests pin the promise the package's docstring makes: nothing here
submits a bid or contacts anyone. They are deliberately blunt. A guarantee that
rests on the current shape of the code is worth exactly as much as a test that
fails when the shape changes, and "the freelance package contains no network
call" is a claim a reader should be able to see enforced rather than asserted.

The project used throughout is a real one, read out of the same saved shelf the
site module's tests use, so the flow is exercised on text a client actually
wrote rather than on a sentence invented to pass.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from desk.config import load_spec
from desk.freelance import command, select
from desk.freelance import proposal as proposal_mod
from desk.freelance.proposal import (
    PriceProposed,
    amounts,
    build_request,
    check_no_price,
    proposal_from,
)
from desk.freelance.select import DRAFT, SKIP, ProjectView, Refusal, screen, verdict_for, view_of
from desk.hooks import ToolCall
from desk.llm import routing
from desk.policy import Policy, PolicyDenied
from desk.prompts import load as load_prompt
from desk.sites import xplace
from desk.store.fingerprint import fingerprint as make_fingerprint

FIXTURE = Path(__file__).parent / "fixtures" / "xplace" / "shelf.html"
TODAY = date(2026, 8, 19)


def projects() -> dict[str, xplace.Project]:
    html = FIXTURE.read_text(encoding="utf-8")
    return {p.external_id: p for p in xplace.parse(html)["projects"]}


def row_for(external_id: str) -> dict[str, object]:
    project = projects()[external_id]
    return {
        "fingerprint": f"fp-{external_id}",
        "title": project.title,
        "url": project.url,
        "site": xplace.SITE,
        "body": xplace.render_body(project),
    }


@pytest.fixture
def view() -> ProjectView:
    return view_of(row_for("215433"))  # ₪7,500, deadline stated, 39 bids in


@pytest.fixture
def spec() -> dict:
    return load_spec()


# --------------------------------------------------------------------------
# selection — everything decided before a token
# --------------------------------------------------------------------------


def test_a_salaried_posting_is_refused_rather_than_proposed_on() -> None:
    """The commonest mistake `desk propose --fingerprint X` invites is pointing
    it at a job board advert. Answering that with a proposal about scope and
    bids would be confidently wrong."""
    with pytest.raises(Refusal, match="no freelance project block"):
        view_of({"site": "drushim", "body": "דרוש/ה מפתח/ת פייתון לחברה מובילה"})


def test_the_facts_come_back_off_the_stored_row(view: ProjectView) -> None:
    assert view.budget == 7500.0
    assert view.currency == "ILS"
    assert view.budget_stated
    assert view.deadline_stated
    assert view.due_date == "2026-09-15"
    assert view.bids == 39
    assert view.description.strip()


def test_an_unstated_budget_stays_unstated_through_the_store() -> None:
    """215408 is the project whose client named no figure. If the store round
    trip turned that into 0.0 the model would be told the client offered
    nothing, which is a different and insulting conversation."""
    unbudgeted = view_of(row_for("215408"))
    assert unbudgeted.budget is None
    assert not unbudgeted.budget_stated
    assert unbudgeted.currency == ""


def test_days_left_is_arithmetic_and_missing_dates_are_none(view: ProjectView) -> None:
    assert view.bids_close_at == "2026-10-05"
    assert view.days_left(TODAY) == (date(2026, 10, 5) - TODAY).days
    assert ProjectView("f", "t", "u", "d").days_left(TODAY) is None
    assert ProjectView("f", "t", "u", "d", bids_close_at="not-a-date").days_left(TODAY) is None


def test_crowding_reports_the_ladder_and_the_exact_count(view: ProjectView) -> None:
    """The band is the site's vocabulary and the count is strictly better. The
    model is shown both, and neither is turned into a threshold here."""
    line = view.crowding()
    assert "rung 5 of 5" in line
    assert "39 so far" in line


def test_crowding_says_so_when_the_site_stated_nothing() -> None:
    assert "stated no bid count" in ProjectView("f", "t", "u", "d").crowding()
    assert "unfamiliar" in ProjectView("f", "t", "u", "d", bids_band="HAS_500_BIDS").crowding()


def test_a_project_already_bid_on_is_refused(view: ProjectView) -> None:
    """`already_applied: suppress` in the spec is about not resurfacing work
    already answered, and a bid is an answer."""
    with pytest.raises(Refusal, match="already recorded"):
        screen(view, today=TODAY, has_bid=True)


def test_closed_bidding_is_refused_with_the_date(view: ProjectView) -> None:
    with pytest.raises(Refusal, match="bidding closed"):
        screen(view, today=date(2027, 1, 1))


def test_screening_a_live_project_says_nothing_at_all(view: ProjectView) -> None:
    """Silence on success is deliberate: passing establishes nothing about the
    project except that drafting for it is not obviously pointless."""
    assert screen(view, today=TODAY) is None


def test_a_project_with_no_close_date_is_drafted_for_not_refused() -> None:
    """The site has stated one on every project served so far, so a missing
    close date means the format moved. Refusing to draft because a scraper aged
    out would be blaming the wrong party."""
    assert screen(ProjectView("f", "t", "u", "d"), today=TODAY) is None


# --------------------------------------------------------------------------
# the floor, and the verdict nobody chooses
# --------------------------------------------------------------------------


def test_the_only_threshold_is_read_from_the_spec(spec: dict) -> None:
    """No freelance-specific floor is invented in code. The day the spec grows
    one, this reads it."""
    assert select.floor(spec) == spec["analyst"]["score"]["channel"]["skip_below"]


def test_the_verdict_is_derived_from_the_score(spec: dict) -> None:
    floor = select.floor(spec)
    assert verdict_for(floor, spec=spec) == DRAFT
    assert verdict_for(floor + 0.1, spec=spec) == DRAFT
    assert verdict_for(floor - 0.1, spec=spec) == SKIP


def test_a_warm_model_cannot_talk_its_way_to_a_draft_verdict(
    view: ProjectView, spec: dict
) -> None:
    """A verdict a model picks is a verdict that drifts warm, and there is no
    later stage here that would notice. So the model's own words are not even
    a field in the schema — only `fit` is, and the verdict follows from it."""
    result = proposal_from(
        {"fit": 0.2, "note": "Strongly recommend bidding.", "questions": [], "concerns": []},
        view=view,
        spec=spec,
    )
    assert result.verdict == SKIP
    assert "verdict" not in proposal_mod.SCHEMA["properties"]


def test_a_nonsense_fit_is_clamped_rather_than_trusted(view: ProjectView, spec: dict) -> None:
    for given, expected in ((5, 1.0), (-3, 0.0), ("high", 0.0), (None, 0.0)):
        result = proposal_from(
            {"fit": given, "note": "x", "questions": [], "concerns": []}, view=view, spec=spec
        )
        assert result.fit == expected


# --------------------------------------------------------------------------
# the price check
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("I can deliver this for ₪4,000", (4000.0,)),
        ("₪7500 is the stated budget", (7500.0,)),
        ("1200 NIS", (1200.0,)),
        ('ש"ח 900', (900.0,)),
        ("$300 or €250", (300.0, 250.0)),
        ("about 3 weeks, 2 review rounds, Python 3.12", ()),
    ],
)
def test_currency_marked_sums_are_found_and_bare_numbers_are_not(
    text: str, expected: tuple
) -> None:
    """The boundary is deliberate. A note about work is full of bare numbers
    that are durations and versions, and a check that rejected those would be
    switched off within a week."""
    assert amounts(text) == expected


def test_the_clients_own_budget_may_be_repeated(view: ProjectView) -> None:
    check_no_price("You budgeted ₪7,500 for this, which is workable.", budget=view.budget)
    check_no_price("Your stated budget of ₪7500 covers the first phase.", budget=view.budget)


def test_any_other_sum_is_refused(view: ProjectView) -> None:
    with pytest.raises(PriceProposed, match="4000"):
        check_no_price("I could do this for ₪4,000.", budget=view.budget)


def test_with_no_stated_budget_every_sum_is_the_models_invention() -> None:
    """The commonest shape on this site, and exactly where the check must not
    go quiet: there is no figure on the table, so there is none to repeat."""
    with pytest.raises(PriceProposed):
        check_no_price("A project like this usually runs ₪5,000.", budget=None)


def test_a_priced_draft_never_becomes_a_proposal_object(view: ProjectView, spec: dict) -> None:
    """The check lives in `proposal_from` rather than in the command, so there
    is no way to obtain a Proposal that has not been through it. A caller who
    forgot would be a caller who printed a price."""
    with pytest.raises(PriceProposed):
        proposal_from(
            {"fit": 0.9, "note": "My rate for this is ₪12,000.", "questions": [], "concerns": []},
            view=view,
            spec=spec,
        )


def test_questions_and_concerns_survive_and_blanks_are_dropped(
    view: ProjectView, spec: dict
) -> None:
    result = proposal_from(
        {
            "fit": 0.8,
            "note": "Happy to take this on.",
            "questions": ["How many endpoints?", "  ", ""],
            "concerns": ["Thirty-nine freelancers have already bid."],
        },
        view=view,
        spec=spec,
    )
    assert result.questions == ("How many endpoints?",)
    assert len(result.concerns) == 1


# --------------------------------------------------------------------------
# the request
# --------------------------------------------------------------------------


def test_the_request_carries_the_versioned_prompt_and_its_hash(
    view: ProjectView, spec: dict
) -> None:
    """Prompts are files on disk, never inline strings, so the trace can say
    which version produced a draft."""
    prompt = load_prompt("freelance", "freelance_proposal", 1)
    request = build_request(view, family="data", spec=spec, today=TODAY)

    assert request.stage == "freelance_proposal"
    assert request.prompt_id == "freelance/freelance_proposal.v1"
    assert request.prompt_sha256 == prompt.sha256
    assert request.schema == proposal_mod.SCHEMA


def test_the_stage_is_routed_and_thinks() -> None:
    route = routing.resolve("freelance_proposal")
    assert "sonnet" in route.model.lower()
    assert route.thinking


def test_the_facts_the_flow_judges_on_all_reach_the_model(view: ProjectView, spec: dict) -> None:
    user = build_request(view, family="data", spec=spec, today=TODAY).user
    assert "7500" in user  # budget
    assert "2026-09-15" in user  # deadline
    assert "39 so far" in user  # crowding
    assert view.description[:200] in user  # scope


def test_an_unstated_budget_reaches_the_model_as_a_sentence(spec: dict) -> None:
    """An empty slot reads as a formatting error and invites the model to fill
    it in. The absence has to be said out loud."""
    user = build_request(view_of(row_for("215408")), family="none", spec=spec).user
    assert "the client stated no budget" in user


def test_the_prompt_forbids_a_price_and_forbids_contacting_anyone() -> None:
    content = load_prompt("freelance", "freelance_proposal", 1).content
    assert "Never state a price" in content
    assert "Never offer to contact anybody" in content
    assert "untrusted text" in content


def test_the_untrusted_text_framing_is_in_the_system_prompt() -> None:
    assert "untrusted" in proposal_mod.SYSTEM
    assert "never send" in proposal_mod.SYSTEM.lower() or "never sends" in proposal_mod.SYSTEM


def test_the_cv_is_background_and_is_not_the_deliverable(view: ProjectView, spec: dict) -> None:
    """The family router supplies vocabulary, not a document. No CV text is
    sent, and a proposal is a note about the work rather than a résumé."""
    user = build_request(view, family="data", spec=spec).user
    assert "background" in user.lower()
    assert proposal_mod.claims_for("data", spec=spec) in user
    assert "unknown-family" not in proposal_mod.claims_for("none", spec=spec)


# --------------------------------------------------------------------------
# structural — nothing here submits a bid or contacts anyone
# --------------------------------------------------------------------------


def test_the_stage_is_given_no_tools(view: ProjectView, spec: dict) -> None:
    """A property of the type rather than a sentence in a prompt: `LLMRequest`
    has no tool field, so there is no argument this module could pass that
    would put a callable in front of the model."""
    request = build_request(view, family="data", spec=spec)
    assert not hasattr(request, "tools")
    assert "tools" not in vars(request)


def test_the_one_tool_that_could_reach_a_client_is_denied_unconditionally() -> None:
    call = ToolCall(name="submit_application", args={}, tier="external")
    with pytest.raises(PolicyDenied, match="a human applies"):
        Policy(allow_external=True).check(call)


def test_the_package_contains_no_network_call_at_all() -> None:
    """Blunt on purpose. The docstring's promise rests on the absence of a code
    path, and an absence is only guaranteed by something that fails when it
    stops being absent."""
    forbidden = ("requests.", "httpx.", "urlopen", "urllib.request", "socket.", "smtplib")
    package = Path(command.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{source.name} reaches the network via {token}"


def test_the_package_never_dispatches_a_tool() -> None:
    package = Path(command.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "registry.dispatch" not in text
        assert "submit_application" not in text


def test_the_command_refuses_to_run_if_the_spec_stops_saying_never(monkeypatch) -> None:
    """One definition of "this system does not apply", shared with `desk
    digest`, rather than a second copy here that could drift out of step."""
    import argparse
    import copy

    # Deep-copied, because `load_spec` is lru_cached and hands every caller the
    # same mutable dict. Editing it in place here would leave "auto_apply:
    # always" behind for every test that ran afterwards, which is a failure
    # this suite has no reason to debug twice.
    edited = copy.deepcopy(load_spec())
    edited.setdefault("manager", {}).setdefault("delivery", {})["auto_apply"] = "always"
    monkeypatch.setattr(command, "load_spec", lambda *a, **k: edited)

    args = argparse.Namespace(
        fingerprint="fp-215433", engine="replay", budget=1.0, write=False
    )
    assert command.cmd_propose(args) == 1


# --------------------------------------------------------------------------
# the command, end to end
# --------------------------------------------------------------------------


def _wire(monkeypatch, tmp_path, answer: dict):
    """Point the command at a throwaway store and a stubbed model.

    The real store is never touched by a test. `build_context` already gives a
    deterministic run an in-memory store, but the command opens the on-disk one
    to read the posting, so `paths` is redirected too.
    """
    import argparse

    from desk.config import Paths
    from desk.runner import RunSettings, build_context
    from desk.sites.base import RawPosting

    monkeypatch.setattr(command, "paths", lambda *a, **k: Paths(tmp_path))

    project = projects()["215433"]
    raw = RawPosting(
        site=xplace.SITE,
        external_id=project.external_id,
        title=project.title,
        company="",
        url=project.url,
        body=xplace.render_body(project),
        posted_at=project.posted_at,
    )
    posting = raw.to_posting()
    store = command.Store(Paths(tmp_path).ensure().db)
    store.upsert_posting(posting, now="2026-08-19T00:00:00")
    store.close()

    # A project's fingerprint is its title alone: the client is behind a login
    # and the site states no location, so company and location are empty here
    # by design. The site module's docstring says why.
    fingerprint = make_fingerprint(posting.title, posting.company, posting.location)

    class _Gateway:
        def __init__(self) -> None:
            self.requests: list = []

        def complete(self, request, ctx=None):
            self.requests.append(request)
            return type("R", (), {"parsed": answer})()

    gateway = _Gateway()

    def _context(settings=None):
        ctx = build_context(RunSettings(root=tmp_path, deterministic=True, budget_usd=None))
        ctx.gateway = gateway
        return ctx

    monkeypatch.setattr("desk.runner.build_context", _context)
    args = argparse.Namespace(fingerprint=fingerprint, engine="replay", budget=1.0, write=False)
    return args, gateway


GOOD = {
    "fit": 0.85,
    "note": "I have built exactly this kind of short back-end job before.",
    "questions": ["How many endpoints does the API need?"],
    "concerns": ["Thirty-nine freelancers have already bid."],
}


def test_a_dry_run_prints_the_draft_and_writes_nothing(monkeypatch, tmp_path, capsys) -> None:
    args, _ = _wire(monkeypatch, tmp_path, GOOD)

    assert command.cmd_propose(args) == 0

    out = capsys.readouterr().out
    assert "dry run" in out
    assert GOOD["note"] in out
    assert "re-run with --write" in out
    assert not list(tmp_path.glob(f"runs/**/{command.FOLDER}/*.md"))


def test_write_saves_one_draft_and_still_sends_nothing(monkeypatch, tmp_path, capsys) -> None:
    args, _ = _wire(monkeypatch, tmp_path, GOOD)
    args.write = True

    assert command.cmd_propose(args) == 0

    written = list(tmp_path.glob(f"runs/**/{command.FOLDER}/*.md"))
    assert len(written) == 1
    assert "no bid was placed" in written[0].read_text(encoding="utf-8")
    assert "nothing was sent" in capsys.readouterr().out


def test_a_priced_draft_is_refused_before_it_reaches_the_screen(
    monkeypatch, tmp_path, capsys
) -> None:
    """The refusal happens before the draft is printed, because a price on
    screen has already done its damage — and `--write` does not save it either.

    The refusal line does name the offending sum, deliberately: the human has
    to know what was refused and why. What must not appear is the note itself,
    which is the thing that would have been copied into a message."""
    priced = "I'll do it for ₪3,000."
    args, _ = _wire(monkeypatch, tmp_path, {**GOOD, "note": priced})
    args.write = True

    assert command.cmd_propose(args) == 1

    out = capsys.readouterr().out
    assert "REFUSED" in out
    assert priced not in out
    assert not list(tmp_path.glob(f"runs/**/{command.FOLDER}/*.md"))


def test_the_family_router_costs_nothing(monkeypatch, tmp_path) -> None:
    """Exactly one model call in the whole flow. The router runs
    deterministically, and paying for vocabulary would be paying twice."""
    args, gateway = _wire(monkeypatch, tmp_path, GOOD)

    command.cmd_propose(args)

    assert len(gateway.requests) == 1
    assert gateway.requests[0].stage == "freelance_proposal"


def test_the_written_draft_says_nothing_was_sent(view: ProjectView, spec: dict) -> None:
    result = proposal_from(
        {
            "fit": 0.9,
            "note": "Happy to take this on.",
            "questions": ["How many endpoints?"],
            "concerns": [],
        },
        view=view,
        spec=spec,
    )
    document = command._document(view, result)

    assert "no bid was placed" in document
    assert "The number is yours to choose." in document
    assert view.url in document
    assert amounts(document) == (7500.0,)  # only the client's own budget
