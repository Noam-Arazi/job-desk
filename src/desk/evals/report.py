"""Rendering — and the one property the markdown renderer has to keep.

The markdown output is not a convenience format. Session 9 requires the
measurements table in the README to match the output of the last run, and a
pre-commit hook checks it, so what this renderer emits is pasted into the README
as-is. Anything it prints that a reader would have to reword by hand — a
placeholder, an abbreviation only this codebase understands, a zero standing in
for an absent measurement — becomes a divergence between the README and the run
the moment somebody tidies it up. That is why an unmeasured row renders here as
"not measured yet" plus its reason, in both formats: it has to survive the trip
into the README without a human deciding what to do with it.

The baseline diff exists for the same reason a prompt is keyed by its hash. A
number without its predecessor is an impression. `--baseline` subtracts the
previous JSON result row by row, and states plainly when a row cannot be
subtracted: new, gone, or one of the two sides not measured. A row that was
unmeasured last time and is measured now is not an improvement of zero.
"""

from __future__ import annotations

from collections.abc import Mapping

from .result import NOT_MEASURED, EvalRun, Measurement, SuiteResult, Table

FORMATS = ("text", "markdown", "json")


def render(run: EvalRun, fmt: str = "text") -> str:
    if fmt == "markdown":
        return render_markdown(run)
    if fmt == "json":
        import json

        return json.dumps(run.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    return render_text(run)


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------


def render_text(run: EvalRun) -> str:
    lines: list[str] = []
    header = f"desk evals   spec version {run.spec_version}"
    if run.generated_at:
        header += f"   {run.generated_at}"
    lines.append(header)
    if run.store_path:
        lines.append(f"store        {run.store_path}")
    lines.append("")

    for suite in run.suites:
        lines.append(f"[{suite.suite}]" + ("" if suite.ok else "   FAILED"))
        width = max((len(m.name) for m in suite.measurements), default=0)
        for measurement in suite.measurements:
            lines.append(f"  {measurement.name:<{width}}  {_value(measurement)}")
        for table in suite.tables:
            lines.extend("  " + line for line in _text_table(table))
        for note in suite.notes:
            lines.extend("  " + line for line in _wrap(note, 92))
        lines.append("")

    if not run.suites:
        lines.append("no suite ran")
    return "\n".join(lines).rstrip() + "\n"


def _value(measurement: Measurement) -> str:
    if not measurement.measured:
        return f"{NOT_MEASURED} — {measurement.missing}"
    rendered = measurement.rendered()
    return f"{rendered}   {measurement.detail}" if measurement.detail else rendered


def _text_table(table: Table) -> list[str]:
    if not table.rows:
        return []
    grid = [list(table.columns)] + [list(r) for r in table.rows]
    widths = [max(len(row[i]) for row in grid) for i in range(len(table.columns))]
    out = ["", table.title]
    for index, row in enumerate(grid):
        out.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            out.append("  " + "  ".join("-" * w for w in widths))
    if table.note:
        out.extend(_wrap(table.note, 92))
    out.append("")
    return out


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


# --------------------------------------------------------------------------
# markdown — this output is pasted into the README verbatim
# --------------------------------------------------------------------------


def render_markdown(run: EvalRun) -> str:
    lines: list[str] = ["## measurements", ""]
    stamp = f"spec version {run.spec_version}"
    if run.generated_at:
        stamp += f", generated {run.generated_at}"
    lines.append(f"`desk evals --format markdown` — {stamp}.")
    lines.append("")
    lines.append("| suite | measurement | value | what it means |")
    lines.append("| --- | --- | --- | --- |")
    for suite in run.suites:
        for measurement in suite.measurements:
            value = measurement.rendered() if measurement.measured else f"_{NOT_MEASURED}_"
            note = measurement.detail if measurement.measured else measurement.missing
            lines.append(
                f"| {suite.suite} | {_cell(measurement.name)} | {value} | {_cell(note)} |"
            )
    lines.append("")

    for suite in run.suites:
        for table in suite.tables:
            lines.extend(_markdown_table(table))
        if suite.notes:
            lines.append(f"**{suite.suite}**")
            lines.append("")
            lines.extend(f"- {_cell(note)}" for note in suite.notes)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _markdown_table(table: Table) -> list[str]:
    if not table.rows:
        return []
    lines = [f"**{table.title}**", ""]
    lines.append("| " + " | ".join(_cell(c) for c in table.columns) + " |")
    lines.append("| " + " | ".join("---" for _ in table.columns) + " |")
    for row in table.rows:
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
    lines.append("")
    if table.note:
        lines.append(_cell(table.note))
        lines.append("")
    return lines


def _cell(text: str) -> str:
    """A pipe inside a cell would silently split the column."""
    return " ".join(str(text).split()).replace("|", "\\|")


# --------------------------------------------------------------------------
# baseline diff
# --------------------------------------------------------------------------

NEW = "new"
GONE = "gone"
NOW_MEASURED = "newly measured"
NO_LONGER = "no longer measured"
STILL_MISSING = "still not measured"


def diff_rows(current: EvalRun, baseline: EvalRun) -> list[tuple[str, str, str, str, str]]:
    """(suite, measurement, baseline, current, delta) for every row on either side."""
    before: dict[tuple[str, str], Measurement] = {
        (s.suite, m.name): m for s in baseline.suites for m in s.measurements
    }
    rows: list[tuple[str, str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for suite, measurement in current.rows():
        key = (suite, measurement.name)
        seen.add(key)
        old = before.get(key)
        if old is None:
            rows.append((suite, measurement.name, "—", _short(measurement), NEW))
            continue
        rows.append(
            (suite, measurement.name, _short(old), _short(measurement), _delta(old, measurement))
        )

    for key, old in before.items():
        if key in seen:
            continue
        rows.append((key[0], key[1], _short(old), "—", GONE))
    return rows


def _short(measurement: Measurement) -> str:
    return measurement.rendered() if measurement.measured else NOT_MEASURED


def _delta(old: Measurement, new: Measurement) -> str:
    if not old.measured and not new.measured:
        return STILL_MISSING
    if not old.measured:
        return NOW_MEASURED
    if not new.measured:
        return NO_LONGER
    change = float(new.value or 0) - float(old.value or 0)
    if change == 0:
        return "unchanged"
    sign = "+" if change > 0 else ""
    if new.unit == "share":
        return f"{sign}{change * 100:.1f} pts"
    if new.unit == "usd":
        return f"{sign}${change:.4f}"
    if abs(change) < 1 and isinstance(change, float):
        return f"{sign}{change:.2f}"
    return f"{sign}{change:,.0f}"


def render_diff(current: EvalRun, baseline: EvalRun, *, fmt: str = "text") -> str:
    rows = diff_rows(current, baseline)
    version_note = ""
    if baseline.spec_version and baseline.spec_version != current.spec_version:
        version_note = (
            f"baseline was measured against spec version {baseline.spec_version}, this "
            f"run against {current.spec_version} — the criteria changed, so some rows "
            "are not comparable"
        )

    if fmt == "markdown":
        lines = ["## delta against the baseline", ""]
        if version_note:
            lines += [f"> {version_note}", ""]
        lines.append("| suite | measurement | baseline | now | delta |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
        return "\n".join(lines) + "\n"

    widths = [max(len(row[i]) for row in rows) if rows else 0 for i in range(5)]
    out = ["delta against the baseline"]
    if version_note:
        out.extend(_wrap(version_note, 92))
    out.append("")
    for row in rows:
        out.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(out) + "\n"


def suite_summary(suites: Mapping[str, SuiteResult]) -> str:
    return ", ".join(f"{name}{'' if s.ok else ' FAILED'}" for name, s in sorted(suites.items()))
