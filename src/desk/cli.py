"""desk — the command line.

desk demo      run the offline skeleton end to end (no key, no network)
desk fetch     scrape one site into the store; a dry run unless --write
desk analyze   gates, family, requirements and a fit score over the store
desk tailor    cut a CV from its approved base for one posting
desk digest    the daily ranked digest; it never applies for you
desk state     show or move where a posting stands
desk propose   draft a short proposal for one freelance project
desk evals     score the system against the gold set
desk spec      show what the search specification currently says
desk tools     show the registered tools and their permission tiers
desk routes    show the stage routing table
desk trace     replay the last run's trace
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import prompts
from .config import load_spec, paths
from .llm.routing import MODELS, TABLE
from .orchestrator import Status, run
from .pipeline import AGENTS, demo_plan
from .registry import registry
from .runner import ENGINES, build_context, settings_from_env
from .store import Store


def cmd_demo(args: argparse.Namespace) -> int:
    settings = settings_from_env(
        engine=args.engine,
        deterministic=not args.wall_clock,
        budget_usd=args.budget,
        root=Path(args.root) if args.root else None,
    )
    ctx = build_context(settings)
    plan = demo_plan()
    report = run(plan, AGENTS, ctx)

    print(f"run      {ctx.run_id}   engine={settings.engine}")
    print(f"trace    {ctx.tracer.path}")
    print("")
    for result in report.results:
        mark = {Status.OK: "ok  ", Status.FAILED: "FAIL", Status.SKIPPED: "skip"}[result.status]
        detail = result.error or json.dumps(result.value, ensure_ascii=False)
        print(f"  {mark}  {result.id:<10} {detail[:96]}")

    total = ctx.tracer.total
    print("")
    print(f"tokens   in={total.input_tokens} out={total.output_tokens} cost=${total.cost_usd:.6f}")
    denied = [e for e in ctx.tracer.events if e["kind"] == "error" and "denied:" in str(e)]
    # Zero is the expected number here. The demo does not stage a jailbreak — one
    # of the sample postings carries an injection payload and is normalized as
    # ordinary data, which is the point. The adversarial proof, where a
    # compromised model does make the call, is tests/test_injection.py.
    print(f"denied   {len(denied)} policy denials (adversarial proof: tests/test_injection.py)")
    ctx.store.close()
    return 0 if report.ok else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetching spends no tokens. It is the cut that happens before the models.

    A dry run by default: it fetches, parses and reports, and writes nothing.
    Storing is an explicit `--write`, so the first look at a new site or a
    changed layout can never quietly fill the store with garbage.
    """
    from datetime import datetime

    from .sites import MODULES, HttpFetcher, Throttle, ThrottledFetcher, rate_limit

    spec = load_spec()
    if args.site not in MODULES:
        print(f"unknown site {args.site!r}; have {', '.join(sorted(MODULES))}", file=sys.stderr)
        return 1

    throttle = Throttle(rate_limit(spec, args.site))
    fetcher = ThrottledFetcher(HttpFetcher(), throttle)
    now = datetime.now()

    result = MODULES[args.site](
        fetcher,
        spec=spec,
        now=now,
        max_pages=args.pages,
        max_age_days=args.max_age,
        terms=args.term or None,
        regions=args.region or None,
    )

    print(f"site     {result.site}   {'WRITE' if args.write else 'dry run'}")
    print(f"pages    {result.pages_fetched} fetched at {throttle.interval:.1f}s apart")
    print(f"kept     {len(result.postings)} postings")
    for reason, count in sorted(result.skipped.items()):
        print(f"dropped  {count:>4}  {reason}")
    for error in result.errors:
        print(f"error    {error}")
    if result.stopped_because:
        print(f"stopped  {result.stopped_because}")
    for note in result.notes:
        print(f"note     {note}")

    undated = [p for p in result.postings if not p.posted_at]
    if undated:
        print(f"undated  {len(undated)} postings carrying no date")

    print("")
    for posting in result.postings[: args.show]:
        stamp = posting.posted_at[:16] or posting.posted_raw or "?"
        terms = ", ".join(result.matched_terms.get(posting.external_id, []))
        print(f"  {stamp}  {posting.location[:14]:<14}  {posting.title[:48]}")
        if terms:
            print(f"  {'':16}  found by: {terms[:70]}")

    if not args.write:
        print("")
        print("nothing stored. re-run with --write to store")
        return 0 if result.ok else 1

    store = Store(paths().ensure().db)
    stored = new = 0
    for posting in result.postings:
        stored += 1
        if store.upsert_posting(posting.to_posting(), now=now.isoformat(timespec="seconds")):
            new += 1
    counts = store.counts()
    store.close()
    print("")
    print(f"stored   {stored} rows, {new} roles new to the store")
    print(f"store    {counts['postings']} postings, {counts['fingerprints']} distinct roles")
    return 0 if result.ok else 1


def cmd_resolve(args: argparse.Namespace) -> int:
    """Find the postings in the store that are one job seen twice.

    A dry run by default, like fetch: it scores, bands and reports, and records
    nothing. `--write` stores the verdicts. `--judge` is what spends tokens, and
    it only ever reaches the pairs the arithmetic left uncertain.
    """
    from datetime import datetime

    from .resolve import DUPLICATE, UNCERTAIN
    from .resolve import resolve as resolve_duplicates

    store = Store(paths().ensure().db)
    rows = store.all_postings()
    if not rows:
        print("store is empty; run `desk fetch --site <id> --write` first")
        store.close()
        return 1

    judge = None
    ctx = None
    if args.judge:
        from .resolve.judge import gateway_judge
        from .runner import RunSettings, build_context

        ctx = build_context(RunSettings(budget_usd=args.budget))
        judge = gateway_judge(ctx.gateway, ctx)

    result = resolve_duplicates(rows, judge=judge)
    summary = result.summary()
    by_fp = {r["fingerprint"]: r for r in rows}

    print(f"store    {len(rows)} postings   {'WRITE' if args.write else 'dry run'}")
    possible = len(rows) * (len(rows) - 1) // 2
    print(f"compared {summary['compared']} pairs, out of {possible} possible")
    print(f"merged   {summary['duplicate']} pairs into {summary['clusters']} clusters")
    print(f"collapse {summary['collapsed']} postings would leave the digest")
    if judge is None:
        print(f"escalate {summary['uncertain']} pairs uncertain, no judge attached")
    else:
        print(f"judged   {summary['judged']} pairs sent to a model")

    print("")
    for group in result.clusters[: args.show]:
        head = by_fp[group[0]]
        print(f"  cluster of {len(group)}   {head['title'][:56]}")
        for member in group:
            row = by_fp[member]
            print(f"    {row['site']:<11} {row['external_id']:<14} {row['title'][:44]}")

    if args.uncertain:
        print("")
        for pair in result.pairs:
            if pair.band != UNCERTAIN:
                continue
            left, right = by_fp[pair.left], by_fp[pair.right]
            print(f"  uncertain {pair.score:.2f}  core {pair.core:.2f}  body {pair.body:.2f}")
            print(f"    {left['site']:<11} {left['title'][:56]}")
            print(f"    {right['site']:<11} {right['title'][:56]}")

    if not args.write:
        print("")
        print("nothing recorded. re-run with --write to record the verdicts")
        store.close()
        return 0

    now = datetime.now().isoformat(timespec="seconds")
    for pair in result.pairs:
        store.record_link(
            pair.left,
            pair.right,
            score=pair.score,
            band=pair.band,
            method=pair.method,
            now=now,
        )
    merged = len(store.links(DUPLICATE))
    store.close()
    print("")
    print(f"recorded {len(result.pairs)} verdicts, {merged} of them merges")
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    """Build the gold set: thirty postings, judged before anything is revealed.

    Nothing on screen says what the gates concluded or what any model would
    score. That is deliberate and it is the only reason the resulting number
    means anything — see the module docstring in label.py.
    """
    from datetime import datetime

    from . import label as gold

    store = Store(paths().ensure().db)
    rows = store.all_postings()
    if not rows:
        print("store is empty; run `desk fetch --site <id> --write` first")
        store.close()
        return 1

    spec = load_spec()
    now = datetime.now()
    existing = store.labels()

    if args.review:
        report = gold.agreement(rows, existing, spec=spec, now=now)
        if not report.labelled:
            print("nothing labelled yet; run `desk label` first")
            store.close()
            return 1
        print(f"labelled  {report.labelled}")
        print(f"agreed    {report.agreed}  ({report.rate:.0%}) gates against you")
        print(f"dropped   {report.gate_blocked_human_wanted} you would have wanted")
        print(f"passed    {report.gate_passed_human_irrelevant} you called irrelevant")
        print("")
        print("The first number is the expensive one: you never see those.")
        store.close()
        return 0

    items = gold.sample(
        rows,
        spec=spec,
        now=now,
        size=args.count,
        seed=args.seed,
        exclude=frozenset(existing),
    )
    if not items:
        print(f"nothing left to label; {len(existing)} already recorded")
        store.close()
        return 0

    print(f"{len(items)} postings.")
    print("1 = a good fit · 2 = maybe · 3 = not for me · s = skip · q = stop")
    print("Nothing here tells you what the system thought. That is on purpose.")
    print("")
    done = 0
    for index, item in enumerate(items, start=1):
        print("=" * 72)
        print(f"[{index}/{len(items)}]")
        print(item.render())
        print("")
        try:
            answer = input("1/2/3/s/q > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("")
            break
        if answer == "q":
            break
        if answer == "s" or answer not in {"1", "2", "3"}:
            continue
        store.put_label(
            item.fingerprint,
            {"1": gold.HIGH, "2": gold.MEDIUM, "3": gold.IRRELEVANT}[answer],
            stratum=item.stratum,
            now=now.isoformat(timespec="seconds"),
        )
        done += 1

    total = len(store.labels())
    print("")
    print(f"recorded {done} this pass, {total} in the store")
    print("`desk label --review` compares them against the gates.")
    store.close()
    return 0


def cmd_spec(args: argparse.Namespace) -> int:
    spec = load_spec()
    gates = spec["gates"]
    print(f"spec version {spec['version']}")
    print("")
    print("regions   " + ", ".join(spec["geography"]["regions"]))
    print("excluded  " + ", ".join(spec["geography"]["exclude_regions"]))
    print(
        "seniority block above "
        f"{gates['seniority']['max_required_years']} years, "
        f"unstated={gates['seniority']['unstated']}, "
        f"range={gates['seniority']['range_rule']}"
    )
    print(f"freshness {gates['freshness']['max_age_days']} days")
    digest = spec["digest"]
    print(f"digest    max {digest['max_items']} items, min score {digest['min_score']}")
    print("")
    print("families  " + ", ".join(sorted(spec["families"])))
    print("sites     " + ", ".join(s["id"] for s in spec["sites"] if s.get("enabled")))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    width = max(len(t.name) for t in registry)
    for tool in sorted(registry, key=lambda t: (t.tier.value, t.name)):
        print(f"{tool.tier.value:<12} {tool.name:<{width}}  {tool.description[:70]}")
    return 0


def cmd_routes(args: argparse.Namespace) -> int:
    width = max(len(s) for s in TABLE)
    for stage, route in TABLE.items():
        spec = MODELS[route.model]
        effort = route.effort if spec.supports_effort else f"{route.effort} (not sent)"
        print(f"{stage:<{width}}  {route.model:<20} effort={effort}")
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    for prompt in prompts.all_prompts():
        print(f"{prompt.id:<40} {prompt.sha256[:16]}")
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"no trace at {path}", file=sys.stderr)
        return 1
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        print(
            f"{event['seq']:>3}  {event['kind']:<14} {json.dumps(event, ensure_ascii=False)[:120]}"
        )
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    """Gates, family, requirements, score — over the postings in the store.

    Imported inside the function on purpose. The analyst pulls in the model
    layer and the gates; `desk spec` and `desk trace` should keep working in a
    clone where nothing but the standard library is importable.
    """
    from .analyst.command import cmd_analyze as run_analyze

    return run_analyze(args)


def cmd_tailor(args: argparse.Namespace) -> int:
    from .tailor.command import cmd_tailor as run_tailor

    return run_tailor(args)


def cmd_digest(args: argparse.Namespace) -> int:
    from .manager.command import cmd_digest as run_digest

    return run_digest(args)


def cmd_propose(args: argparse.Namespace) -> int:
    from .freelance.command import cmd_propose as run_propose

    return run_propose(args)


def cmd_evals(args: argparse.Namespace) -> int:
    from .evals.command import cmd_evals as run_evals

    return run_evals(args)


def cmd_state(args: argparse.Namespace) -> int:
    from .manager.command import cmd_state as run_state

    return run_state(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="desk", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the offline skeleton end to end")
    demo.add_argument("--engine", choices=ENGINES, default="replay")
    demo.add_argument("--budget", type=float, default=1.00, help="cost ceiling in USD")
    demo.add_argument("--wall-clock", action="store_true", help="disable the deterministic clock")
    demo.add_argument("--root", default=None, help="where data/ and runs/ live")
    demo.set_defaults(func=cmd_demo)

    fetch = sub.add_parser("fetch", help="scrape one site into the store")
    fetch.add_argument("--site", default="alljobs")
    fetch.add_argument("--pages", type=int, default=6, help="page ceiling per query")
    fetch.add_argument("--term", action="append", help="search term; default is the whole spec")
    fetch.add_argument("--region", type=int, action="append", help="board region code")
    fetch.add_argument("--max-age", type=int, default=None, help="override the freshness window")
    fetch.add_argument("--show", type=int, default=12, help="how many postings to print")
    fetch.add_argument("--write", action="store_true", help="store instead of dry-running")
    fetch.set_defaults(func=cmd_fetch)

    resolve_cmd = sub.add_parser("resolve", help="find duplicate postings in the store")
    resolve_cmd.add_argument("--write", action="store_true", help="record the verdicts")
    resolve_cmd.add_argument("--judge", action="store_true", help="send uncertain pairs to a model")
    resolve_cmd.add_argument("--budget", type=float, default=None, help="usd ceiling for --judge")
    resolve_cmd.add_argument("--show", type=int, default=10, help="clusters to print")
    resolve_cmd.add_argument("--uncertain", action="store_true", help="list the uncertain pairs")
    resolve_cmd.set_defaults(func=cmd_resolve)

    label_cmd = sub.add_parser("label", help="build the gold set by hand, unprompted")
    label_cmd.add_argument("--count", type=int, default=30)
    label_cmd.add_argument("--seed", type=int, default=0, help="same seed, same sample")
    label_cmd.add_argument(
        "--review", action="store_true", help="compare recorded labels against the gates"
    )
    label_cmd.set_defaults(func=cmd_label)

    analyze = sub.add_parser("analyze", help="run the analyst over stored postings")
    analyze.add_argument("--limit", type=int, default=20, help="postings to consider")
    analyze.add_argument("--fingerprint", action="append", help="analyse these and nothing else")
    analyze.add_argument("--engine", choices=ENGINES, default="replay")
    analyze.add_argument("--budget", type=float, default=1.00, help="cost ceiling in USD")
    analyze.add_argument("--write", action="store_true", help="store the verdicts")
    analyze.add_argument("--show", type=int, default=12, help="how many to print")
    analyze.add_argument("--all", action="store_true", help="include already-analysed postings")
    analyze.set_defaults(func=cmd_analyze)

    tailor = sub.add_parser("tailor", help="cut a CV from its approved base for one posting")
    tailor.add_argument("--fingerprint", required=True)
    tailor.add_argument("--engine", choices=ENGINES, default="replay")
    tailor.add_argument("--budget", type=float, default=1.00, help="cost ceiling in USD")
    tailor.add_argument("--write", action="store_true", help="write the document to disk")
    tailor.add_argument("--language", choices=("he", "en"), default=None)
    tailor.set_defaults(func=cmd_tailor)

    digest = sub.add_parser("digest", help="build the daily ranked digest")
    digest.add_argument("--limit", type=int, default=None, help="override the spec's max_items")
    digest.add_argument("--min-score", type=float, default=None, help="override the spec's floor")
    digest.add_argument("--send", action="store_true", help="deliver it; off by default")
    digest.add_argument("--format", choices=("text", "telegram", "json"), default="text")
    digest.set_defaults(func=cmd_digest)

    propose = sub.add_parser("propose", help="draft a proposal for one freelance project")
    propose.add_argument("--fingerprint", required=True)
    propose.add_argument("--engine", choices=ENGINES, default="replay")
    propose.add_argument("--budget", type=float, default=1.00, help="cost ceiling in USD")
    propose.add_argument("--write", action="store_true", help="write the proposal to disk")
    propose.set_defaults(func=cmd_propose)

    evals = sub.add_parser("evals", help="score the system against the gold set")
    evals.add_argument("--suite", default="all", help="which suite to run")
    evals.add_argument("--format", choices=("text", "markdown", "json"), default="text")
    evals.add_argument("--baseline", default=None, help="a previous result to diff against")
    evals.add_argument("--out", default=None, help="write the result table here")
    evals.set_defaults(func=cmd_evals)

    state = sub.add_parser("state", help="show or move where a posting stands")
    state.add_argument("--fingerprint", default=None)
    state.add_argument("--set", dest="new_state", default=None, help="move it to this state")
    state.add_argument("--note", default="", help="why")
    state.add_argument("--list", dest="list_state", default=None, help="list one state")
    state.add_argument("--due", action="store_true", help="list what a follow-up is due on")
    state.set_defaults(func=cmd_state)

    sub.add_parser("spec", help="show the search specification").set_defaults(func=cmd_spec)
    sub.add_parser("tools", help="show registered tools and tiers").set_defaults(func=cmd_tools)
    sub.add_parser("routes", help="show the stage routing table").set_defaults(func=cmd_routes)
    sub.add_parser("prompts", help="show prompt versions and hashes").set_defaults(func=cmd_prompts)

    trace = sub.add_parser("trace", help="print a run trace")
    trace.add_argument("path", help="path to trace.jsonl")
    trace.set_defaults(func=cmd_trace)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
