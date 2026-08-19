"""`desk propose` — a dry run that shows the draft, unless told to write it.

The default prints and touches no disk, exactly as `desk fetch`, `desk resolve`
and `desk tailor` do, and for a sharper reason than any of them. A proposal is
a note addressed to a client, and the failure mode of a tool that writes by
default is a folder of plausible drafts that nobody has read — which is one
copy-paste away from being sent. Making the file a second decision keeps a human
between the model and the client, which is the only place that guarantee can
live.

The order of what happens here is the design, and it is cheapest-first for the
same reason the gate chain is:

    the spec is checked first        `check_auto_apply` refuses to run at all
                                     if the specification has been edited to say
                                     the system may apply. One definition of
                                     that promise, shared with `desk digest`,
                                     rather than a second copy that could drift.

    then the store, in Python        a posting with no freelance facts block, or
                                     one whose bidding has closed, or one
                                     already bid on, is refused before a token.

    then the family, for free        the router runs deterministically with no
                                     model attached. It is not deciding whether
                                     to draft — that has already been decided —
                                     only what may be claimed, and paying for
                                     that would be paying for vocabulary.

    then one model call              the only one in the flow.

    then the price check             before anything is printed. A refused draft
                                     is never shown, because a price on screen
                                     has already done its damage.

An unrouted project is drafted for rather than refused. The families are the
spec's four salaried tracks, and freelance work arrives in shapes none of them
name; refusing there would decline most of the site on the strength of a
vocabulary that was written for job boards. The model is told the background is
unmatched and asked to judge fit against that, which is the honest version of
the same information.
"""

from __future__ import annotations

import argparse
from datetime import date

from ..analyst import families
from ..config import load_spec, paths
from ..gates.chain import Candidate
from ..llm.base import BudgetExceeded, StructuredOutputError
from ..manager.delivery import NeverApplies, check_auto_apply
from ..store import Store
from .proposal import PriceProposed, Proposal, build_request, proposal_from
from .select import DRAFT, ProjectView, Refusal, screen, view_of

# Where a written draft lands. Inside the run directory rather than beside the
# CVs: a proposal is not a document to attach, and a folder that mixed the two
# would eventually see one sent as the other.
FOLDER = "proposals"


def cmd_propose(args: argparse.Namespace) -> int:
    from ..runner import RunSettings, build_context

    spec = load_spec()
    try:
        check_auto_apply(spec)
    except NeverApplies as exc:
        print(f"REFUSED  {exc}")
        return 1

    store = Store(paths().ensure().db)
    row = store.get_posting(args.fingerprint)
    if row is None:
        print(f"no posting for {args.fingerprint}; run `desk fetch --write` first")
        store.close()
        return 1

    try:
        view = view_of(row)
        screen(view, today=date.today(), has_bid=store.has_applied(args.fingerprint))
    except Refusal as exc:
        print(f"refused  {exc}")
        store.close()
        return 1

    # Deterministic and free: `ask` is left out, so no model is consulted about
    # vocabulary. An unmatched family is reported as such rather than hidden.
    routed = families.route(
        Candidate(
            title=view.title,
            body=view.description,
            fingerprint=view.fingerprint,
            site=view.site,
        ),
        spec=spec,
    )

    ctx = build_context(RunSettings(engine=args.engine, budget_usd=args.budget))
    try:
        response = ctx.gateway.complete(
            build_request(view, family=routed.family, spec=spec, today=date.today()),
            ctx=ctx,
        )
        proposal = proposal_from(response.parsed, view=view, spec=spec)
    except PriceProposed as exc:
        print(f"REFUSED  {exc}")
        return _done(store, ctx, 1)
    except BudgetExceeded as exc:
        print(f"stopped  {exc}")
        return _done(store, ctx, 1)
    except StructuredOutputError as exc:
        print(f"no draft  the model's answer did not validate: {exc}")
        return _done(store, ctx, 1)

    _show(view, proposal, routed_family=routed.family, write=bool(args.write))

    if not args.write:
        print("")
        print("nothing written. re-run with --write to save the draft")
        return _done(store, ctx, 0)

    target = ctx.run_dir / FOLDER / f"{view.fingerprint or 'unfingerprinted'}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_document(view, proposal), encoding="utf-8")
    print("")
    print(f"wrote    {target}")
    print("nothing was sent. sending is yours, from your own account")
    return _done(store, ctx, 0)


def _show(view: ProjectView, proposal: Proposal, *, routed_family: str, write: bool) -> None:
    budget = "not stated" if view.budget is None else f"{view.currency}{view.budget:g}"
    print(f"project  {view.title[:60]}")
    print(f"family   {routed_family}   {'WRITE' if write else 'dry run'}")
    print(f"budget   {budget}")
    print(f"deadline {view.due_date or 'not stated'}")
    print(f"bidding  {view.crowding()}")
    print(f"fit      {proposal.fit:.2f}   {proposal.verdict}")
    if proposal.verdict != DRAFT:
        print("         below the spec's floor: the draft is shown, the advice is not to bid")
    print("")
    print(proposal.note)

    if proposal.questions:
        print("")
        print("what has to be answered before you can name a price:")
        for question in proposal.questions:
            print(f"  {question}")

    if proposal.concerns:
        print("")
        print("what should give you pause:")
        for concern in proposal.concerns:
            print(f"  {concern}")


def _document(view: ProjectView, proposal: Proposal) -> str:
    """The written draft, with the facts it was judged on kept beside it.

    The note alone would be a file whose provenance is gone the moment the
    project scrolls off the site. Keeping the budget, the deadline and the
    crowding next to it means a draft opened a week later can still be checked
    against what was true when it was written.
    """
    budget = "not stated" if view.budget is None else f"{view.currency}{view.budget:g}"
    lines = [
        f"# {view.title}",
        "",
        f"- project: {view.url}",
        f"- budget: {budget}",
        f"- deadline: {view.due_date or 'not stated'}",
        f"- bidding: {view.crowding()}",
        f"- fit: {proposal.fit:.2f} ({proposal.verdict})",
        "",
        "## Draft",
        "",
        proposal.note,
    ]
    if proposal.questions:
        lines += ["", "## Ask before quoting", ""]
        lines += [f"- {q}" for q in proposal.questions]
    if proposal.concerns:
        lines += ["", "## Concerns", ""]
        lines += [f"- {c}" for c in proposal.concerns]
    lines += [
        "",
        "---",
        "",
        "This is a draft. Nothing was sent, no bid was placed, and no price was "
        "proposed on your behalf. The number is yours to choose.",
        "",
    ]
    return "\n".join(lines)


def _done(store: Store, ctx: object, code: int) -> int:
    store.close()
    inner = getattr(ctx, "store", None)
    if inner is not None:
        inner.close()
    return code
