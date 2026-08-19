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
"""

from __future__ import annotations

import argparse
from datetime import datetime

from ..config import paths
from ..registry import registry
from ..store import Store
from . import render
from .bases import BaseNotFound
from .contract import ContractError, load_contract
from .tailor import Fabrication, NoFamily, load_analysis, tailor


def cmd_tailor(args: argparse.Namespace) -> int:
    from ..runner import RunSettings, build_context

    store = Store(paths().ensure().db)
    analysis = load_analysis(store, args.fingerprint)
    if analysis is None:
        print(f"no analysis for {args.fingerprint}; run `desk analyze --write` first")
        store.close()
        return 1

    posting = store.get_posting(args.fingerprint) or {}
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

    try:
        result = tailor(
            analysis,
            ctx=ctx,
            contract=contract,
            language=args.language,
        )
    except NoFamily as exc:
        print(f"skipped   {exc}")
        return _done(store, ctx, 0)
    except BaseNotFound as exc:
        print(f"no base   {exc}")
        return _done(store, ctx, 1)
    except ContractError as exc:
        print(f"REJECTED  {len(exc.violations)} contract violations, nothing was written")
        for violation in exc.violations:
            print(f"  {violation.rule:<26} {violation.where:<20} {violation.detail}")
        return _done(store, ctx, 1)
    except Fabrication as exc:
        print("REJECTED  the fabrication check refused the changeset")
        for claim in exc.unsupported:
            print(f"  unsupported  {claim}")
        return _done(store, ctx, 1)

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
        return _done(store, ctx, 0)

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
        return _done(store, ctx, 1)

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
    return _done(store, ctx, 0)


def _done(store: Store, ctx: object, code: int) -> int:
    store.close()
    inner = getattr(ctx, "store", None)
    if inner is not None:
        inner.close()
    return code
