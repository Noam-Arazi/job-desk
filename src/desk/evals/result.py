"""The shape a measurement is reported in, and the one rule the shape enforces.

A measurement here can be absent, and absent is a first-class value rather than
a zero. That distinction is the reason this module exists at all.

The failure mode it prevents is specific and it is common. A suite that has no
data to work on returns 0, the report prints `0`, and `0` reads as a result —
"zero false blocks" is a sentence somebody puts in a README. It is not a
finding, it is the absence of one, and by the time it reaches a reader the
difference is invisible. So `value=None` is never rendered as a number in any
format: the text and markdown writers print `not measured yet` followed by the
reason, and the JSON writer emits `null` alongside a `missing` string. Every
suite in this package is required to reach for `missing()` rather than to
default.

The second rule is that two numbers with opposite consequences are never
averaged into one. There is no `overall_score` field here on purpose. A suite
returns the measurements it actually made, each named for what it counts, and
the report prints them side by side.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# Units. They control formatting only — the stored value is always the raw
# number, so a baseline diff subtracts comparable things.
COUNT = "count"
SHARE = "share"  # 0..1, rendered as a percentage
USD = "usd"
TOKENS = "tokens"
SECONDS = "seconds"
RATIO = "ratio"

NOT_MEASURED = "not measured yet"


@dataclass(frozen=True)
class Measurement:
    """One number, or the honest statement that there is no number.

    `detail` qualifies a value that exists (what it is out of, what it assumes).
    `missing` explains a value that does not. Exactly one of them is normally
    set, and `missing` is only meaningful when `value is None`.
    """

    name: str
    value: float | int | None = None
    unit: str = COUNT
    detail: str = ""
    missing: str = ""

    @property
    def measured(self) -> bool:
        return self.value is not None

    def rendered(self) -> str:
        if self.value is None:
            return NOT_MEASURED
        if self.unit == SHARE:
            return f"{self.value:.0%}"
        if self.unit == USD:
            return f"${self.value:.4f}"
        if self.unit == RATIO:
            return f"{self.value:.2f}x"
        if self.unit == SECONDS:
            return f"{self.value:.2f}s"
        if isinstance(self.value, float) and not self.value.is_integer():
            return f"{self.value:.2f}"
        return f"{int(self.value):,}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "detail": self.detail,
            "missing": self.missing,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Measurement:
        raw = data.get("value")
        return cls(
            name=str(data.get("name", "")),
            value=None if raw is None else float(raw) if isinstance(raw, float) else raw,
            unit=str(data.get("unit", COUNT)),
            detail=str(data.get("detail", "")),
            missing=str(data.get("missing", "")),
        )


def missing(name: str, why: str, *, unit: str = COUNT) -> Measurement:
    """A measurement that could not be made, and why.

    Always preferred over returning zero. `why` is printed next to the row, so
    a reader can tell "nobody has labelled anything yet" from "the gates got it
    right every time".
    """
    return Measurement(name=name, value=None, unit=unit, missing=why)


@dataclass(frozen=True)
class Table:
    """A small matrix a single number would have hidden.

    The confusion matrix is the motivating case. An accuracy percentage cannot
    say whether a system is optimistic or pessimistic, and those two have
    opposite costs, so the matrix travels with the number rather than instead
    of it.
    """

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = ()
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Table:
        return cls(
            title=str(data.get("title", "")),
            columns=tuple(str(c) for c in data.get("columns", ())),
            rows=tuple(tuple(str(c) for c in row) for row in data.get("rows", ())),
            note=str(data.get("note", "")),
        )


@dataclass(frozen=True)
class SuiteResult:
    """Everything one suite concluded.

    `ok` is False only when the suite found the specific failure it was built to
    catch — an uncaught injection, a prompt case that regressed, a suite that
    raised. Having no data is not a failure; it is a measurement that is
    missing, and `ok` stays True.
    """

    suite: str
    measurements: tuple[Measurement, ...] = ()
    notes: tuple[str, ...] = ()
    tables: tuple[Table, ...] = ()
    ok: bool = True
    extra: Mapping[str, Any] = field(default_factory=dict)

    def get(self, name: str) -> Measurement | None:
        return next((m for m in self.measurements if m.name == name), None)

    @property
    def measured(self) -> tuple[Measurement, ...]:
        return tuple(m for m in self.measurements if m.measured)

    @property
    def unmeasured(self) -> tuple[Measurement, ...]:
        return tuple(m for m in self.measurements if not m.measured)

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "ok": self.ok,
            "measurements": [m.as_dict() for m in self.measurements],
            "notes": list(self.notes),
            "tables": [t.as_dict() for t in self.tables],
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SuiteResult:
        return cls(
            suite=str(data.get("suite", "")),
            measurements=tuple(Measurement.from_dict(m) for m in data.get("measurements", ())),
            notes=tuple(str(n) for n in data.get("notes", ())),
            tables=tuple(Table.from_dict(t) for t in data.get("tables", ())),
            ok=bool(data.get("ok", True)),
            extra=dict(data.get("extra", {}) or {}),
        )


@dataclass(frozen=True)
class EvalRun:
    """Every suite that ran, plus what it ran against.

    `spec_version` is recorded because every threshold in this package is read
    from spec/search.yaml. A baseline diff across two spec versions is comparing
    two different questions, and the report says so rather than subtracting
    quietly.
    """

    suites: tuple[SuiteResult, ...] = ()
    generated_at: str = ""
    spec_version: int = 0
    store_path: str = ""

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.suites)

    def suite(self, name: str) -> SuiteResult | None:
        return next((s for s in self.suites if s.suite == name), None)

    def rows(self) -> list[tuple[str, Measurement]]:
        return [(s.suite, m) for s in self.suites for m in s.measurements]

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "spec_version": self.spec_version,
            "store_path": self.store_path,
            "ok": self.ok,
            "suites": [s.as_dict() for s in self.suites],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EvalRun:
        return cls(
            suites=tuple(SuiteResult.from_dict(s) for s in data.get("suites", ())),
            generated_at=str(data.get("generated_at", "")),
            spec_version=int(data.get("spec_version", 0) or 0),
            store_path=str(data.get("store_path", "")),
        )


def failed(suite: str, error: BaseException) -> SuiteResult:
    """A suite that raised. Reported as a failure, never as an empty result.

    An exception swallowed into "no measurements" is indistinguishable in the
    report from a suite that had nothing to measure, and those are opposite
    situations.
    """
    return SuiteResult(
        suite=suite,
        notes=(f"suite raised {type(error).__name__}: {error}",),
        ok=False,
    )
