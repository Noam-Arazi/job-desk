"""`desk analyze` — the analyst over the postings already in the store.

A dry run by default, exactly like `desk fetch` and `desk resolve`. It gates,
routes, extracts, reflects and scores, prints what it concluded, and stores
nothing. `--write` is what puts a verdict in the store. The asymmetry is
deliberate: a prompt edit or a spec change should be observable before it is
recorded, because a stored analysis is what the digest and the tailoring agent
read next.

The summary line is the point of this command as much as the table is. It
reports where each posting stopped and how many model calls the whole pass made,
so the cost claim this project makes is a number printed by the tool rather than
a paragraph in a README.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from ..config import load_spec, paths
from ..gates.chain import store_first_seen
from ..llm.base import BudgetExceeded
from ..runner import RunSettings, build_context
from ..store import Store
from .analyst import Analyst, analyse_row
from .types import (
    STOPPED_EXTRACT,
    STOPPED_FAMILY,
    STOPPED_GATES,
    STOPPED_REFLECT,
    Analysis,
)

# In the order a posting meets them, so the summary reads as the funnel it is.
STAGES = (STOPPED_GATES, STOPPED_FAMILY, STOPPED_EXTRACT, STOPPED_REFLECT)


def _rows(store: Store, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Which postings this pass considers.

    Named fingerprints override everything, including `--all`: re-running one
    posting after a prompt change is the most common reason to touch this
    command at all, and it must not depend on whether that posting happens to
    have been analysed already.
    """
    if args.fingerprint:
        found = [store.get_posting(fp) for fp in args.fingerprint]
        return [row for row in found if row]
    if args.all:
        return store.all_postings()[: args.limit]
    return store.unanalysed_postings(args.limit)


def _line(analysis: Analysis) -> str:
    fit = analysis.fit
    verdict = f"{fit.score:.2f} {fit.channel:<6}" if analysis.scored else f"—    {'':<6}"
    family = analysis.family.family
    stop = analysis.stopped_at or "scored"
    return (
        f"  {analysis.fingerprint[:8]}  {analysis.site[:10]:<10}  {verdict}  "
        f"{family[:16]:<16}  {stop:<8}  {analysis.title[:40]}"
    )


def _summary(analyst: Analyst) -> str:
    counted = " · ".join(
        f"{name or 'scored'} {analyst.stops.get(name, 0)}" for name in (*STAGES, "")
    )
    return counted


def _mark_discovered(
    store: Store, analysis: Analysis, *, spec: Mapping[str, Any], now: datetime
) -> bool:
    """Put a newly analysed posting at the entry point of the pipeline.

    Nothing else in the system was moving postings into `discovered`, so the
    state machine sat empty and every later state had no legal predecessor to
    come from. This is bookkeeping and not a judgment: the analyst having read a
    posting is exactly what "discovered" means, and a posting that already has a
    state is left where it is — re-analysing an item Noam already approved must
    not walk it backwards.
    """
    from ..manager.states import DISCOVERED, SYSTEM, current, move

    if current(store, analysis.fingerprint) is not None:
        return False
    move(store, analysis.fingerprint, DISCOVERED, spec=spec, now=now, source=SYSTEM)
    return True


def _store_analysis(store: Store, analysis: Analysis, *, now: str) -> None:
    store.put_analysis(
        analysis.fingerprint,
        analysis.as_json(),
        family=analysis.family.family,
        score=analysis.fit.score if analysis.scored else None,
        channel=analysis.fit.channel,
        rationale=analysis.fit.rationale,
        stopped_at=analysis.stopped_at,
        now=now,
        run_id=analysis.run_id,
    )


def cmd_analyze(args: argparse.Namespace) -> int:
    spec: Mapping[str, Any] = load_spec()
    store = Store(paths().ensure().db)
    rows = _rows(store, args)
    if not rows:
        print("nothing to analyse; run `desk fetch --site <id> --write` first")
        print("(everything in the store may already be analysed — try --all)")
        store.close()
        return 1

    ctx = build_context(RunSettings(engine=args.engine, budget_usd=args.budget, mode="analyze"))
    now = datetime.now()
    analyst = Analyst(
        spec=spec,
        gateway=ctx.gateway,
        ctx=ctx,
        now=now,
        first_seen=store_first_seen(store),
        has_applied=store.has_applied,
        run_id=ctx.run_id,
        # A posting already applied to is suppressed by the gates, and the store
        # is what knows. Passing it in is what makes `already_applied` a verdict
        # rather than the permanent `unknown` it reports without a history.
    )

    analyses: list[Analysis] = []
    errors: list[str] = []
    halted = ""
    for row in rows:
        try:
            analyses.append(analyse_row(analyst, row))
        except BudgetExceeded as exc:
            halted = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 — one bad posting is not a failed run
            errors.append(f"{(row.get('fingerprint') or '')[:8]}  {type(exc).__name__}: {exc}")

    print(f"store    {len(rows)} postings considered   {'WRITE' if args.write else 'dry run'}")
    print(f"run      {ctx.run_id}   engine={args.engine}")
    print(f"stopped  {_summary(analyst)}")
    calls = " · ".join(f"{stage} {count}" for stage, count in sorted(analyst.calls.items()))
    print(f"calls    {analyst.total_calls} model calls" + (f"   ({calls})" if calls else ""))
    total = ctx.tracer.total
    print(f"tokens   in={total.input_tokens} out={total.output_tokens} cost=${total.cost_usd:.6f}")
    if halted:
        print(f"halted   budget: {halted}")
    if errors:
        # Stated as a count as well as a list, so the funnel above adds up. A
        # posting that raised produced no analysis and is in none of the stage
        # counters, and without this line the difference reads as a miscount.
        print(f"failed   {len(errors)} postings raised and produced no analysis")
        for error in errors[:8]:
            print(f"error    {error}")

    print("")
    scored = sorted((a for a in analyses if a.scored), key=lambda a: a.fit.score, reverse=True)
    rest = [a for a in analyses if not a.scored]
    for analysis in (scored + rest)[: args.show]:
        print(_line(analysis))
        if analysis.scored and analysis.fit.rationale:
            print(f"  {'':10}  {analysis.fit.rationale[:88]}")
        if analysis.fit.gaps:
            print(f"  {'':10}  gaps: {', '.join(analysis.fit.gaps)[:80]}")

    if not args.write:
        print("")
        print("nothing stored. re-run with --write to store the verdicts")
        store.close()
        ctx.store.close()
        return 0 if not errors and not halted else 1

    stamp = now.isoformat(timespec="seconds")
    discovered = 0
    for analysis in analyses:
        _store_analysis(store, analysis, now=stamp)
        before = _mark_discovered(store, analysis, spec=spec, now=now)
        discovered += int(before)
    store.close()
    ctx.store.close()
    print("")
    print(f"stored   {len(analyses)} analyses, {discovered} entered the pipeline as discovered")
    return 0 if not errors and not halted else 1
