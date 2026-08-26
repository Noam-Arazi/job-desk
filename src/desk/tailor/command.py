"""`desk tailor` — a dry run that shows its work, unless told to write.

The default is deliberately the useless-looking one. Tailoring produces a file
in a folder Noam will open in Word and send to a stranger, and the failure mode
of a tool that writes by default is that a bad cut has already been saved by
the time anyone reads the diff. So the default run prints every change with its
evidence and its gaps and touches no disk, exactly as `desk fetch` and `desk
resolve` do, and `--write` is a separate decision made after looking.

What it prints is `review.digest_shows` from the contract: the before/after of
each changed line, the source behind it, and what the posting asked for that
the CV does not claim. The last of those is the part worth the screen space —
`auto_send: false` means the system never applies, so the only thing it owes
Noam is everything he needs to decide.

`--approved` is the scheduled form, added 24.08.2026, and the state it names is
the whole design. The morning delivers a shortlist and cuts nothing; Noam reads
it on his phone and marks the postings worth applying to; those, and only those,
get a document. Tailoring every ranked posting was the obvious build and it is
the wrong one — it spends a model call on four jobs out of five he would not
have applied to, and it puts a CV he never asked for in a folder he opens by
hand. `approved` already existed in the state machine as the word for "Noam
decided this one is worth it", so this reads the decision rather than inventing
a second place to keep it.

The loop shares one context, so `--budget` is a ceiling over the whole batch
rather than per posting, and an exhausted budget stops the loop the way it stops
the analyst. One posting that raises does not end the run: it is counted, named
and stepped over, because the three CVs that can be cut are worth more than a
clean exit code.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..config import paths
from ..llm.base import BudgetExceeded
from ..registry import registry
from ..store import Store
from . import render
from .bases import BaseNotFound
from .contract import ContractError, load_contract
from .tailor import Fabrication, NoFamily, load_analysis, tailor


def cmd_tailor(args: argparse.Namespace) -> int:
    from ..runner import RunSettings, build_context

    store = Store(paths().ensure().db)
    contract = load_contract()
    # The approval token is `--write` itself. A dry run is denied at the dispatch
    # point rather than by an `if args.write` here: the check that stops a write
    # should not be the one the caller could forget to make.
    ctx = build_context(
        RunSettings(
            engine=args.engine,
            budget_usd=args.budget,
            approval_token="local-run" if args.write else None,
        )
    )
    ctx.contract = contract

    if getattr(args, "approved", False):
        return _done(store, ctx, _tailor_approved(store, ctx, contract, args))
    return _done(store, ctx, _tailor_one(store, ctx, contract, args.fingerprint, args))


def approved_without_a_document(store: Store, *, force: bool = False) -> list[str]:
    """The postings Noam marked and that have no CV yet, oldest decision first.

    Oldest first because the order is a queue of his decisions and a budget
    that runs out should run out at the newest one, which is the one he is most
    likely to still be looking at. `force` is the answer to "re-cut something I
    have been editing in Word", so its absence means no.
    """
    from ..manager.states import APPROVED

    rows = list(reversed(store.in_state(APPROVED)))
    return [
        str(row["fingerprint"])
        for row in rows
        if force or not store.tailored(str(row["fingerprint"]))
    ]


def _tailor_approved(store: Store, ctx: object, contract: object, args) -> int:
    """Cut a CV for every posting Noam approved and that has none yet.

    Nothing here decides which jobs matter. That decision was made on a phone,
    recorded as a state transition with `source: human`, and this reads it.
    """
    wanted = approved_without_a_document(store, force=bool(args.force))
    if not wanted:
        print("approved  nothing is waiting for a document")
        return 0

    print(f"approved  {len(wanted)} waiting for a document")
    failed = 0
    for position, fingerprint in enumerate(wanted, start=1):
        print("")
        print(f"--- {position} of {len(wanted)}   {fingerprint[:12]}")
        try:
            if _tailor_one(store, ctx, contract, fingerprint, args) != 0:
                failed += 1
        except BudgetExceeded as exc:
            # The same halt the analyst takes, and for the same reason: the
            # documents already cut are on disk and the digest will attach them.
            print(f"halted   budget: {exc}")
            print(f"halted   {len(wanted) - position + 1} of {len(wanted)} left uncut")
            failed += 1
            break
        except Exception as exc:  # noqa: BLE001 — one bad posting is not a failed run
            print(f"failed   {fingerprint[:12]}  {type(exc).__name__}: {exc}")
            failed += 1

    print("")
    print(f"cut      {len(wanted) - failed} of {len(wanted)}")
    return 1 if failed else 0


def _tailor_one(
    store: Store,
    ctx: object,
    contract: object,
    fingerprint: str,
    args,
    report: Callable[[str], None] | None = None,
) -> int:
    """One posting, start to finish. Closes nothing: the caller owns the store.

    `report` is how a refusal escapes this function. Every failure below is
    printed, and print is enough for a person at a keyboard who is watching the
    command run. It was not enough for the one caller that matters: a button
    press on a phone, where the refusal went to `runs/inbox.log` and Noam, who
    had pressed ✅ and was waiting, saw nothing at all. A cut that produced no
    document is exactly as much of an answer as one that did, and it has to
    travel the same way.
    """
    def refuse(line: str) -> int:
        if report is not None:
            report(line)
        return 1

    analysis = load_analysis(store, fingerprint)
    if analysis is None:
        print(f"no analysis for {fingerprint}; run `desk analyze --write` first")
        return refuse("אין ניתוח למשרה הזאת עדיין")

    posting = store.get_posting(fingerprint) or {}

    try:
        result = tailor(
            analysis,
            ctx=ctx,
            contract=contract,
            language=args.language,
        )
    except NoFamily as exc:
        print(f"skipped   {exc}")
        return 0
    except BaseNotFound as exc:
        print(f"no base   {exc}")
        return refuse(f"לא נמצא בסיס קורות חיים מתאים\n{exc}")
    except ContractError as exc:
        print(f"REJECTED  {len(exc.violations)} contract violations, nothing was written")
        for violation in exc.violations:
            print(f"  {violation.rule:<26} {violation.where:<20} {violation.detail}")
        # The rule ids travel and the prose does not. They are the words the
        # contract file uses, so a message on a phone can be looked up in
        # `spec/change-contract.yaml` without a laptop, and they are short
        # enough that five of them still read as one line.
        return refuse(
            "חוזה קורות החיים חסם את העריכה ולא נכתב מסמך\n"
            + ", ".join(sorted({v.rule for v in exc.violations}))
        )
    except Fabrication as exc:
        print("REJECTED  the fabrication check refused the changeset")
        for claim in exc.unsupported:
            print(f"  unsupported  {claim}")
        return refuse(
            "בדיקת ההמצאה סירבה — שורה שאין לה מקור בבסיס או במלאי\n"
            + ", ".join(exc.unsupported)
        )

    base = result.base
    print(f"posting  {analysis.title[:60]}")
    print(f"family   {base.family} / {base.language}   {'WRITE' if args.write else 'dry run'}")
    print(f"base     {base.path.name}")
    print(f"sha256   {base.sha256[:16]}")
    print(f"changes  {len(result.changeset)} proposed, all of them sourced")
    for note in result.notes:
        print(f"note     {note}")
    print("")
    for row in render.diff(base, result.changeset):
        print(row)

    if result.gaps:
        print("")
        print("what the posting wanted and the CV does not claim:")
        for gap in result.gaps:
            print(f"  {gap}")

    if not args.write:
        print("")
        print("nothing written. re-run with --write to cut the document")
        return 0

    # The document is cut through the registry, not by calling the renderer here.
    # It is the one write-local act in the daily run, and routing it through the
    # single dispatch point is what puts it under the same policy, tracing and
    # redaction hooks as everything else. `--force` is the answer to "may this
    # overwrite the document you have been editing in Word", so its absence
    # means no, not "assume yes".
    outcome = registry.dispatch(
        "write_tailored_cv",
        {
            "fingerprint": analysis.fingerprint,
            "family": base.family,
            "language": base.language,
            "base_sha256": base.sha256,
            "changeset": result.changeset.as_json(),
            "company": str(posting.get("company") or analysis.company),
            "title": str(posting.get("title") or analysis.title),
            "force": bool(getattr(args, "force", False)),
        },
        ctx,
    )
    if not outcome.ok:
        print("")
        print(f"{'DENIED      ' if outcome.denied else 'NOT WRITTEN '} {outcome.error}")
        return 1

    written = outcome.content
    store.put_tailored(
        analysis.fingerprint,
        family=base.family,
        language=base.language,
        base_sha256=written["base_sha256"],
        path=written["path"],
        changes=result.changeset.as_json(),
        now=datetime.now().isoformat(timespec="seconds"),
    )
    print("")
    print(f"wrote    {written['path']}")
    print(
        f"applied  {written['changed']} edited, {written['removed']} removed, "
        f"{written['reordered']} reordered"
    )
    print("the page count is yours to check, in Word")
    return 0


def _done(store: Store, ctx: object, code: int) -> int:
    store.close()
    inner = getattr(ctx, "store", None)
    if inner is not None:
        inner.close()
    return code


def cut_one(
    store: Store,
    fingerprint: str,
    *,
    engine: str,
    budget: float,
    language: str | None = None,
    force: bool = False,
    report: Callable[[str], None] | None = None,
) -> Path | None:
    """Cut one CV for a caller that has a store and nothing else.

    This is what a button press calls. It exists so that `desk inbox` does not
    have to build a run context, know what a contract is, or reach into a
    private function — and so that the document a press produces is cut by
    exactly the code path `desk tailor` uses, rather than by a second one that
    would drift.

    Returns where the document was written, or None if nothing was. A caller
    that gets None has a posting Noam approved and no file to send him, which
    is a state the morning pass will find and retry.
    """
    import argparse

    from ..runner import RunSettings, build_context

    contract = load_contract()
    ctx = build_context(
        RunSettings(engine=engine, budget_usd=budget, approval_token="local-run")
    )
    ctx.contract = contract
    args = argparse.Namespace(
        fingerprint=fingerprint,
        engine=engine,
        budget=budget,
        write=True,
        force=force,
        language=language,
    )
    try:
        _tailor_one(store, ctx, contract, fingerprint, args, report=report)
    finally:
        inner = getattr(ctx, "store", None)
        if inner is not None:
            inner.close()
    written = store.tailored(fingerprint) or {}
    path = str(written.get("path") or "")
    return Path(path) if path else None
