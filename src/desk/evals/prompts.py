"""Prompt regression — every version scored on its own fixture set.

A prompt is the only part of this system that can be improved by feel and get
worse at the same time. Nobody edits a gate and reports "it seems sharper now",
but that is the normal way prompts are changed, and it is why a prompt edit
usually ships with an impression instead of a delta.

So each prompt gets a fixture set of its own, in `fixtures/prompt_cases.json`,
and a change is reported as a difference against the previous run rather than
as a feeling. The cases here run offline and spend no tokens: a case supplies
the fields the prompt takes, and asserts what must and must not survive
rendering. That catches the two failures that actually happen in practice — a
placeholder was renamed and every caller now raises, or an edit quietly deleted
a load-bearing instruction, such as the sentence telling the model that the
posting is untrusted text. A model-scored A/B belongs on top of this layer, not
instead of it.

The part that matters most is the key. Results are stored under the prompt's id
AND its sha256, and the fixture set records the sha it was written against. If
the file has been edited since, the prompt is reported `stale` and its old score
is not carried forward — an edited prompt inheriting a passing score is exactly
the silent regression this whole design exists to prevent. Re-blessing is
deliberate work: look at what changed, then write the new hash in.

A prompt with no fixture set is reported as unmeasured, never as passing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..prompts import Prompt, all_prompts
from .result import SHARE, Measurement, SuiteResult, Table, missing

SUITE = "prompts"

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CASES_PATH = FIXTURES_DIR / "prompt_cases.json"

SCORED = "scored"
STALE = "stale"
UNMEASURED = "no fixtures"


def load_cases(path: Path | None = None) -> dict[str, Any]:
    target = Path(path or CASES_PATH)
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    return dict(data.get("prompts", {}))


def score_prompt(prompt: Prompt, fixture: Mapping[str, Any]) -> tuple[int, int, list[str]]:
    """Run one prompt's cases. Returns (passed, total, failures)."""
    cases = list(fixture.get("cases", []))
    failures: list[str] = []
    passed = 0
    for case in cases:
        name = str(case.get("id") or "?")
        try:
            rendered = prompt.render(**dict(case.get("fields") or {}))
        except KeyError as exc:
            failures.append(f"{name}: the prompt needs a field the case does not supply — {exc}")
            continue
        problems = [
            f"missing {needle!r}"
            for needle in case.get("must_contain", [])
            if str(needle) not in rendered
        ]
        problems += [
            f"leaked {needle!r}"
            for needle in case.get("must_not_contain", [])
            if str(needle) in rendered
        ]
        if problems:
            failures.append(f"{name}: " + "; ".join(problems))
        else:
            passed += 1
    return passed, len(cases), failures


def run(
    *,
    prompts_dir: Path | None = None,
    cases_path: Path | None = None,
    loaded: Sequence[Prompt] | None = None,
) -> SuiteResult:
    """Score every prompt on disk against its recorded fixture set."""
    prompts = list(loaded if loaded is not None else all_prompts(prompts_dir))
    fixtures = load_cases(cases_path)

    rows: list[tuple[str, ...]] = []
    by_prompt: dict[str, dict[str, Any]] = {}
    cases_run = 0
    cases_passed = 0
    stale: list[str] = []
    failed: list[str] = []
    with_fixtures = 0

    for prompt in sorted(prompts, key=lambda p: p.id):
        fixture = fixtures.get(prompt.id)
        short = prompt.sha256[:12]
        if not fixture:
            rows.append((prompt.id, short, "—", UNMEASURED))
            by_prompt[prompt.id] = {"sha256": prompt.sha256, "state": UNMEASURED}
            continue
        with_fixtures += 1
        recorded = str(fixture.get("sha256") or "")
        if recorded != prompt.sha256:
            stale.append(prompt.id)
            rows.append(
                (
                    prompt.id,
                    short,
                    "—",
                    f"{STALE}: fixtures were written against {recorded[:12] or 'nothing'}",
                )
            )
            by_prompt[prompt.id] = {
                "sha256": prompt.sha256,
                "state": STALE,
                "fixture_sha256": recorded,
            }
            continue
        passed, total, failures = score_prompt(prompt, fixture)
        cases_run += total
        cases_passed += passed
        if failures:
            failed.extend(f"{prompt.id} {f}" for f in failures)
        rows.append(
            (
                prompt.id,
                short,
                f"{passed}/{total}",
                SCORED if not failures else "; ".join(failures)[:70],
            )
        )
        by_prompt[prompt.id] = {
            "sha256": prompt.sha256,
            "state": SCORED,
            "cases": total,
            "passed": passed,
        }

    measurements = [
        Measurement("prompt versions on disk", len(prompts)),
        Measurement(
            "with a fixture set",
            with_fixtures,
            detail="a prompt with no fixture set is unmeasured, never passing",
        ),
        Measurement(
            "stale fixture sets",
            len(stale),
            detail="the prompt was edited after its cases were written; the old score "
            "is not carried forward",
        ),
        Measurement("cases run", cases_run),
        (
            Measurement(
                "cases passed",
                cases_passed / cases_run,
                unit=SHARE,
                detail=f"{cases_passed} of {cases_run}, offline, no tokens spent",
            )
            if cases_run
            else missing(
                "cases passed",
                "no fresh fixture set ran" if with_fixtures else "no prompt has a fixture set",
                unit=SHARE,
            )
        ),
    ]

    notes = [
        "Results are keyed by prompt id AND sha256. An edited prompt cannot "
        "inherit the score of the version its cases were written against.",
        "These cases are offline structural checks. They catch a renamed "
        "placeholder and a deleted instruction; they do not judge output quality, "
        "which is what a model-scored A/B on top of this layer is for.",
    ]
    if stale:
        notes.append(
            "STALE: "
            + ", ".join(stale)
            + ". Read the diff, then write the new sha256 into "
            + CASES_PATH.name
            + " — re-blessing is meant to be deliberate."
        )
    if failed:
        notes.append("FAILED: " + " | ".join(failed))

    return SuiteResult(
        suite=SUITE,
        measurements=tuple(measurements),
        notes=tuple(notes),
        tables=(
            Table(
                title="prompt versions",
                columns=("prompt", "sha256", "cases", "state"),
                rows=tuple(rows),
            ),
        ),
        ok=not stale and not failed,
        extra={"by_prompt": by_prompt},
    )
