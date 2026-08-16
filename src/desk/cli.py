"""desk — the command line.

desk demo      run the offline skeleton end to end (no key, no network)
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
from .config import load_spec
from .llm.routing import MODELS, TABLE
from .orchestrator import Status, run
from .pipeline import AGENTS, demo_plan
from .registry import registry
from .runner import ENGINES, build_context, settings_from_env


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
    denied = [e for e in ctx.tracer.events if e["kind"] == "error" and "denied" in str(e)]
    print(f"denied   {len(denied)} external-tier attempts blocked")
    ctx.store.close()
    return 0 if report.ok else 1


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="desk", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the offline skeleton end to end")
    demo.add_argument("--engine", choices=ENGINES, default="replay")
    demo.add_argument("--budget", type=float, default=1.00, help="cost ceiling in USD")
    demo.add_argument("--wall-clock", action="store_true", help="disable the deterministic clock")
    demo.add_argument("--root", default=None, help="where data/ and runs/ live")
    demo.set_defaults(func=cmd_demo)

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
