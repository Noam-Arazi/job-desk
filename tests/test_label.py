"""What the gold set has to keep being true.

The one property worth testing here is negative: nothing the sampler shows can
carry the system's opinion. A number measured against labels formed while
looking at that opinion is not a measurement.
"""

from __future__ import annotations

import copy
from datetime import datetime

import pytest

from desk import label as gold
from desk.config import load_spec
from desk.store import Posting, Store

NOW = datetime(2026, 8, 18, 9, 0, 0)


@pytest.fixture
def spec() -> dict:
    return copy.deepcopy(load_spec())


def row(fingerprint: str, **overrides) -> dict:
    base = {
        "fingerprint": fingerprint,
        "site": "alljobs",
        "title": "אנליסט נתונים",
        "company": "חברה",
        "location": "חיפה",
        "body": "ניסיון של שנתיים בניתוח נתונים",
        "url": "https://example.com/1",
        "posted_at": NOW.isoformat(timespec="seconds"),
    }
    return {**base, **overrides}


def test_what_is_shown_carries_no_verdict_score_or_reason(spec) -> None:
    """The whole point of the file. If any of these leak, the labels are anchored
    to the system's answer and the agreement number stops meaning anything."""
    items = gold.sample([row("a")], spec=spec, now=NOW, size=1)
    shown = items[0].render()

    for leak in ("pass", "block", "unknown", "score", "gate", items[0].stratum):
        assert leak not in shown.lower()


def test_the_sample_includes_postings_the_gates_dropped(spec) -> None:
    """A sample drawn only from survivors can show what the gates wrongly let
    through and is structurally blind to what they wrongly dropped."""
    rows = [row(f"s{i}") for i in range(20)]
    rows += [row(f"b{i}", location="ירושלים") for i in range(20)]

    items = gold.sample(rows, spec=spec, now=NOW, size=30, blocked_share=10)

    assert sum(i.stratum == gold.BLOCKED for i in items) == 10
    assert sum(i.stratum == gold.SURVIVED for i in items) == 20


def test_the_same_seed_draws_the_same_thirty(spec) -> None:
    """A rerun after a crash has to resume the same set, or the number quietly
    changes what it was measured on."""
    rows = [row(f"s{i}") for i in range(50)]

    first = gold.sample(rows, spec=spec, now=NOW, size=30, seed=7)
    second = gold.sample(rows, spec=spec, now=NOW, size=30, seed=7)

    assert [i.fingerprint for i in first] == [i.fingerprint for i in second]


def test_already_labelled_postings_are_not_offered_again(spec) -> None:
    rows = [row(f"s{i}") for i in range(40)]

    items = gold.sample(rows, spec=spec, now=NOW, size=30, exclude=frozenset({"s0", "s1"}))

    assert {"s0", "s1"}.isdisjoint({i.fingerprint for i in items})


def test_a_short_stratum_is_made_up_by_the_other(spec) -> None:
    """Thirty asked for is thirty returned, when thirty exist at all."""
    rows = [row(f"s{i}") for i in range(40)] + [row("b0", location="ירושלים")]

    items = gold.sample(rows, spec=spec, now=NOW, size=30, blocked_share=10)

    assert len(items) == 30


def test_a_sample_smaller_than_the_blocked_share_is_still_that_size(spec) -> None:
    """A negative count is a slice from the end, not an error. `--count 3` once
    offered 976 postings this way."""
    rows = [row(f"s{i}") for i in range(40)] + [row(f"b{i}", location="ירושלים") for i in range(40)]

    items = gold.sample(rows, spec=spec, now=NOW, size=3, blocked_share=10)

    assert len(items) == 3


def test_agreement_separates_the_expensive_error_from_the_cheap_one(spec) -> None:
    """A posting the gates dropped and he wanted is invisible to him forever. A
    posting they passed and he does not want costs one model call."""
    rows = [
        row("kept_wanted"),
        row("dropped_wanted", location="ירושלים"),
        row("passed_unwanted"),
    ]
    labels = {
        "kept_wanted": {"label": gold.HIGH},
        "dropped_wanted": {"label": gold.HIGH},
        "passed_unwanted": {"label": gold.IRRELEVANT},
    }

    report = gold.agreement(rows, labels, spec=spec, now=NOW)

    assert report.gate_blocked_human_wanted == 1
    assert report.gate_passed_human_irrelevant == 1
    assert report.labelled == 3


def test_a_label_survives_between_runs(tmp_path) -> None:
    """It is state that outlives a run, which is why it is in the store."""
    with Store(tmp_path / "desk.sqlite") as store:
        posting = Posting(site="alljobs", external_id="1", title="אנליסט", company="חברה")
        store.upsert_posting(posting, now="2026-08-18T09:00:00")
        store.put_label(
            posting.fingerprint, gold.HIGH, stratum=gold.SURVIVED, now="2026-08-18T09:05:00"
        )

    with Store(tmp_path / "desk.sqlite") as reopened:
        assert reopened.labels()[posting.fingerprint]["label"] == gold.HIGH
        assert reopened.is_labelled(posting.fingerprint)
