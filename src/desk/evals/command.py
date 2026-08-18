"""`desk evals` — assemble the suites, render, and say what is not measured yet.

Two behaviours here are decisions rather than plumbing.

A suite with no data is not an error. `desk evals` on a clean clone runs to
completion and prints "not measured yet" against every row that needs the gold
set, because the gold set is thirty postings Noam judges by hand and cannot be
generated. Exiting non-zero on that would train whoever runs it to ignore the
exit code, which is the code that matters when a guardrail actually breaks.

A suite that finds the failure it was built to find IS an error. An uncaught
injection, a prompt whose fixture set is stale or whose cases regressed, a suite
that raised — those return 1, so the same command works in CI as it does by
hand. Nothing else changes the exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import load_spec, paths
from ..store import Store
from . import agreement as agreement_suite
from . import cost as cost_suite
from . import dedup as dedup_suite
from . import extraction as extraction_suite
from . import gates as gates_suite
from . import guardrails as guardrails_suite
from . import prompts as prompts_suite
from .report import FORMATS, render, render_diff
from .result import EvalRun, SuiteResult, failed

SUITES = (
    gates_suite.SUITE,
    agreement_suite.SUITE,
    extraction_suite.SUITE,
    dedup_suite.SUITE,
    guardrails_suite.SUITE,
    prompts_suite.SUITE,
    cost_suite.SUITE,
)
ALL = "all"


def _make_ctx_factory(root: Path | None):
    """Build run contexts for the adversarial suite, isolated from the real tree.

    Deterministic, so the store is in memory and the clock is frozen: the
    guardrail suite dispatches tools and must not touch the real store or leave
    a differently-timestamped trace behind on every invocation.

    The root is a throwaway directory rather than the repo, and that is the fix
    for a real bug rather than tidiness. Opening a run context opens a tracer,
    which writes `runs/<run_id>/trace.jsonl` — so `desk evals` was writing two
    traces of its own and the cost suite, which runs afterwards, was reading
    them back and adding the eval's own spans to the daily run's totals. A
    measurement that grows because you measured it is worse than no measurement.
    Writing outside the tree means those traces are never produced; `cost.py`
    additionally ignores any that a previous version left behind.

    Isolation also means a fixture that DID breach policy writes its marker into
    the temporary data dir, where `guardrails._check_dispatch` looks for it, and
    not into the real one.

    An explicit `root` is honoured — a caller who passes one is asking to look
    at what gets written, which is a legitimate thing to want.
    """
    from ..runner import RunSettings, build_context

    created: list[Any] = []
    scratch: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        scratch = tempfile.TemporaryDirectory(prefix="desk-evals-")
        root = Path(scratch.name)

    def make(approval_token: str | None):
        ctx = build_context(
            RunSettings(
                mode="evals" if approval_token else "evals-noauth",
                deterministic=True,
                budget_usd=None,
                approval_token=approval_token,
                root=root,
            )
        )
        created.append(ctx)
        return ctx

    return make, created, scratch


def run_suites(
    names: list[str],
    *,
    store: Store,
    spec: dict[str, Any],
    now: datetime,
    root: Path | None = None,
) -> list[SuiteResult]:
    """Run the named suites, each isolated so one failure cannot hide the rest."""
    results: list[SuiteResult] = []
    # The factory fills its list while the suite runs, so what is held here is
    # the list itself and not a copy of it — copying it at build time captured
    # an empty list and closed nothing.
    context_batches: list[list[Any]] = []
    scratches: list[tempfile.TemporaryDirectory[str]] = []

    for name in names:
        try:
            if name == gates_suite.SUITE:
                results.append(
                    gates_suite.run(store.all_postings(), store.labels(), spec=spec, now=now)
                )
            elif name == agreement_suite.SUITE:
                results.append(
                    agreement_suite.run(store.labels(), store.analyses(), spec=spec)
                )
            elif name == extraction_suite.SUITE:
                analyses = extraction_suite.from_rows(store.analyses())
                postings = {row["fingerprint"]: row for row in store.all_postings()}
                results.append(extraction_suite.run(analyses, postings, spec=spec))
            elif name == dedup_suite.SUITE:
                results.append(dedup_suite.run(store.links()))
            elif name == guardrails_suite.SUITE:
                make, created, scratch = _make_ctx_factory(root)
                context_batches.append(created)
                if scratch is not None:
                    scratches.append(scratch)
                results.append(guardrails_suite.run(make_ctx=make, spec=spec))
            elif name == prompts_suite.SUITE:
                results.append(prompts_suite.run())
            elif name == cost_suite.SUITE:
                results.append(cost_suite.run(runs_dir=paths(root).runs))
            else:  # pragma: no cover - guarded by the caller
                raise ValueError(f"unknown suite {name!r}")
        except Exception as exc:  # noqa: BLE001 - a raising suite is a reported failure
            results.append(failed(name, exc))

    for ctx in (c for batch in context_batches for c in batch):
        ctx.store.close()
    for scratch in scratches:
        scratch.cleanup()
    return results


def cmd_evals(args: argparse.Namespace) -> int:
    requested = str(getattr(args, "suite", ALL) or ALL)
    names = list(SUITES) if requested == ALL else [s.strip() for s in requested.split(",")]
    unknown = [n for n in names if n not in SUITES]
    if unknown:
        print(
            f"unknown suite {', '.join(unknown)}; have {ALL}, {', '.join(SUITES)}",
            file=sys.stderr,
        )
        return 1

    fmt = str(getattr(args, "format", "text") or "text")
    if fmt not in FORMATS:
        print(f"unknown format {fmt!r}; have {', '.join(FORMATS)}", file=sys.stderr)
        return 1

    spec = load_spec()
    where = paths()
    store = Store(where.ensure().db)
    try:
        results = run_suites(
            names,
            store=store,
            spec=spec,
            now=datetime.now(),
            root=None,
        )
    finally:
        store.close()

    evaluation = EvalRun(
        suites=tuple(results),
        generated_at=datetime.now().isoformat(timespec="seconds"),
        spec_version=int(spec.get("version", 0)),
        store_path=str(where.db),
    )

    # `document` is the result on its own; `printed` may also carry the baseline
    # diff. They are separated because --out and --baseline together used to
    # write a JSON document with a text diff stapled to the end, which no longer
    # parses — and the whole point of writing JSON is that the next run can pass
    # it back as --baseline.
    document = render(evaluation, fmt)
    printed = document

    baseline_path = getattr(args, "baseline", None)
    if baseline_path:
        loaded = _load_baseline(Path(baseline_path))
        if loaded is None:
            return 1
        printed = document.rstrip() + "\n\n" + render_diff(evaluation, loaded, fmt=fmt)

    out_path = getattr(args, "out", None)
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document, encoding="utf-8")
        print(f"written to {target}", file=sys.stderr)

    print(printed, end="" if printed.endswith("\n") else "\n")
    return 0 if evaluation.ok else 1


def _load_baseline(path: Path) -> EvalRun | None:
    if not path.exists():
        print(f"no baseline at {path}", file=sys.stderr)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(
            f"{path} is not a JSON eval result ({exc}); write one with "
            "`desk evals --format json --out <path>`",
            file=sys.stderr,
        )
        return None
    return EvalRun.from_dict(data)
