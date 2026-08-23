"""What the submission manager has to keep being true.

Four of these are load-bearing rather than incidental, and they are the ones
worth reading first:

    an empty day is a valid answer. The digest is not allowed to reach for a
    fifth item by relaxing its floor, because a floor that moves teaches the
    reader that the score means nothing.

    an illegal transition raises. Coercing a backwards move would write a
    history that never happened, and the event log is the only record of what
    Noam actually did.

    an applied posting never comes back — through either door, the store's
    blocklist or the state machine, because a phone interview was never marked
    applied.

    the scheduled job carries an explicit timeout. On a Mac with Power Nap an
    untimed unsupervised job hangs indefinitely and KeepAlive does not rescue
    it, so the number is asserted against the spec rather than trusted.

No test here touches the network, and none of them reads the wall clock: every
window is exercised by moving `now`, which is a parameter everywhere in this
package for exactly that reason.
"""

from __future__ import annotations

import copy
import json
import plistlib
import re
import traceback
import urllib.request
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from desk.analyst.types import BUTTON, PERSON, Analysis, Family, Fit
from desk.config import REPO_ROOT, load_spec, paths
from desk.manager import delivery, render, states, timers
from desk.manager import digest as digest_module
from desk.store import Posting, Store

NOW = datetime(2026, 8, 18, 8, 0, 0)
PLIST = REPO_ROOT / "deploy" / "launchd" / "com.noamarazi.jobdesk.plist"

HEBREW = re.compile(r"[֐-׿]")
LATIN = re.compile(r"[A-Za-z]")


@pytest.fixture
def spec() -> dict:
    return copy.deepcopy(load_spec())


@pytest.fixture
def store():
    db = Store(":memory:")
    yield db
    db.close()


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def gate(name: str, verdict: str, **details) -> dict:
    result = {"gate": name, "verdict": verdict, "reason": f"{name} said {verdict}"}
    if details:
        result["details"] = details
    return result


PASSING_GATES = (
    gate("already_applied", "unknown"),
    gate("freshness", "pass"),
    gate("geography", "pass", accepted_regions=["haifa"], cities=["חיפה"]),
    gate("seniority", "pass"),
    gate("degree", "pass"),
)


def analysed(
    store: Store,
    fingerprint: str,
    *,
    score: float = 0.8,
    title: str = "אנליסט נתונים",
    company: str = "Bluewick",
    site: str = "alljobs",
    channel: str = BUTTON,
    gates: tuple[dict, ...] = PASSING_GATES,
    reason: str = "the Monday.com automation work maps onto this directly",
    now: datetime = NOW,
) -> Analysis:
    """Store one posting and the analysis of it, the way the analyst would."""
    posting = Posting(
        site=site,
        external_id=fingerprint,
        title=title,
        company=company,
        location="חיפה",
        fingerprint=fingerprint,
    )
    store.upsert_posting(posting, now=stamp(now))
    analysis = Analysis(
        fingerprint=fingerprint,
        site=site,
        title=title,
        company=company,
        url=f"https://example.test/{fingerprint}",
        gates=gates,
        family=Family("data_analyst", 0.9, "analyst titles"),
        fit=Fit(score=score, rationale=reason, channel=channel),
    )
    store.put_analysis(
        fingerprint,
        analysis.as_json(),
        family=analysis.family.family,
        score=score,
        channel=channel,
        rationale=reason,
        stopped_at="",
        now=stamp(now),
    )
    return analysis


def build(store: Store, spec: dict, **kwargs) -> digest_module.Digest:
    return digest_module.build(store, now=kwargs.pop("now", NOW), spec=spec, **kwargs)


# --------------------------------------------------------------------------
# the state machine — the rules, not the storage
# --------------------------------------------------------------------------


def test_the_pipeline_is_the_one_the_spec_lists(spec) -> None:
    assert states.states(spec) == (
        "discovered",
        "shortlisted",
        "approved",
        "applied",
        "ack",
        "interview",
        "offer",
        "closed",
    )


def test_the_transition_table_is_built_from_the_spec_order(spec) -> None:
    """Reordering the spec reorders the table. Nothing here is hand-written."""
    spec["manager"]["states"] = ["discovered", "applied", "closed"]
    table = states.transitions(spec)
    assert table["discovered"] == frozenset({"applied", "closed"})
    assert table["applied"] == frozenset({"closed"})
    assert table["closed"] == frozenset()


def test_moving_backwards_raises_rather_than_being_coerced(spec, store) -> None:
    """The required case: an illegal move is an error, never a quiet no-op."""
    states.move(store, "fp1", states.APPLIED, spec=spec, now=NOW)
    with pytest.raises(states.IllegalTransition):
        states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)

    # And the store still says what it said before the attempt.
    assert states.current(store, "fp1") == states.APPLIED
    assert [e["to_state"] for e in store.state_history("fp1")] == ["applied"]


def test_a_state_cannot_move_to_itself(spec, store) -> None:
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    with pytest.raises(states.IllegalTransition):
        states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)


def test_skipping_forward_is_legal_because_employers_skip(spec) -> None:
    """Interviews get arranged without an acknowledgement all the time."""
    assert states.allows(spec, "applied", "interview") is True
    assert states.allows(spec, "discovered", "offer") is True


def test_closed_is_reachable_from_everywhere_and_is_terminal(spec) -> None:
    for state in states.states(spec):
        if state != states.CLOSED:
            assert states.allows(spec, state, states.CLOSED) is True
    for state in states.states(spec):
        assert states.allows(spec, states.CLOSED, state) is False


def test_a_posting_can_enter_the_pipeline_anywhere(spec, store) -> None:
    """Noam applies to things by hand; the first we hear may be that he did."""
    moved = states.move(store, "fp1", states.APPLIED, spec=spec, now=NOW)
    assert moved.first is True
    assert moved.from_state is None


def test_a_posting_with_no_state_has_reached_nothing(spec, store) -> None:
    """Most postings in the store were never moved anywhere. That is not an error.

    `at_or_after` is what the digest asks to decide whether an item has gone
    past `applied`, so it is asked about every candidate, and the common answer
    has to be a quiet False rather than a raise.
    """
    assert states.at_or_after(spec, states.current(store, "fp1"), states.APPLIED) is False
    assert states.at_or_after(spec, "ghosted", states.APPLIED) is False


def test_a_state_the_spec_does_not_name_raises(spec, store) -> None:
    with pytest.raises(states.UnknownState):
        states.move(store, "fp1", "ghosted", spec=spec, now=NOW)


def test_a_spec_missing_a_required_state_raises(spec) -> None:
    spec["manager"]["states"] = ["discovered", "shortlisted"]
    with pytest.raises(states.UnknownState):
        states.states(spec)


def test_every_move_lands_in_the_append_only_log(spec, store) -> None:
    for target in ("shortlisted", "approved", "applied", "interview"):
        states.move(store, "fp1", target, spec=spec, now=NOW, note=f"to {target}")
    log = store.state_history("fp1")
    assert [e["to_state"] for e in log] == ["shortlisted", "approved", "applied", "interview"]
    assert [e["from_state"] for e in log] == [None, "shortlisted", "approved", "applied"]


# --------------------------------------------------------------------------
# timers — every window read from the spec, every clock passed in
# --------------------------------------------------------------------------


def test_applying_arms_a_follow_up_at_the_spec_s_window(spec, store) -> None:
    due_at = timers.due_at_for(states.APPLIED, now=NOW, spec=spec)
    states.move(store, "fp1", states.APPLIED, spec=spec, now=NOW, due_at=due_at)

    window = timedelta(days=spec["manager"]["follow_up_days"])
    assert store.state("fp1")["due_at"] == stamp(NOW + window)

    assert timers.due(store, now=NOW + window - timedelta(days=1)) == []
    nudges = timers.due(store, now=NOW + window)
    assert [n.fingerprint for n in nudges] == ["fp1"]
    assert nudges[0].state == states.APPLIED


def test_only_applied_carries_a_follow_up(spec) -> None:
    for state in states.states(spec):
        expected = state == states.APPLIED
        assert (timers.due_at_for(state, now=NOW, spec=spec) is not None) is expected


def test_answering_clears_the_nudge(spec, store) -> None:
    """An employer who answered must not go on being chased."""
    states.move(
        store,
        "fp1",
        states.APPLIED,
        spec=spec,
        now=NOW,
        due_at=timers.due_at_for(states.APPLIED, now=NOW, spec=spec),
    )
    later = NOW + timedelta(days=2)
    states.move(
        store,
        "fp1",
        "interview",
        spec=spec,
        now=later,
        due_at=timers.due_at_for("interview", now=later, spec=spec),
    )
    assert store.state("fp1")["due_at"] is None
    assert timers.due(store, now=NOW + timedelta(days=90)) == []


def test_the_sweep_never_closes_a_live_interview_or_an_offer(spec, store) -> None:
    """The sweep stops at `applied`, and the reason is that `closed` is terminal.

    An interview arranged on day 0 and not touched again was closed by the
    calendar on day 21 — and there was no way to say "still interviewing",
    because `interview -> interview` is an illegal move and nothing else
    refreshes `updated_at`. The offer that arrived on day 22 could then never
    be recorded at all: `closed` has an empty transition set.
    """
    for fingerprint, state in (
        ("fp_ack", "ack"),
        ("fp_interview", "interview"),
        ("fp_offer", "offer"),
        ("fp_shortlisted", "shortlisted"),
        ("fp_applied", states.APPLIED),
    ):
        states.move(store, fingerprint, state, spec=spec, now=NOW)

    later = NOW + timedelta(days=spec["manager"]["stale_days"] * 2)
    assert set(timers.sweep(store, now=later, spec=spec)) == {"fp_shortlisted", "fp_applied"}

    # Silence before an answer is an answer, so those two closed. A live thread
    # with a person on the other end is not the calendar's to end.
    assert states.current(store, "fp_ack") == "ack"
    assert states.current(store, "fp_interview") == "interview"
    assert states.current(store, "fp_offer") == "offer"

    # And the offer that arrives three weeks into an interview is recordable.
    states.move(store, "fp_interview", "offer", spec=spec, now=later)
    assert states.current(store, "fp_interview") == "offer"


def test_an_untouched_item_closes_itself_after_the_spec_s_window(spec, store) -> None:
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    stale_after = timedelta(days=spec["manager"]["stale_days"])

    assert timers.sweep(store, now=NOW + stale_after - timedelta(days=1), spec=spec) == ()
    assert states.current(store, "fp1") == "shortlisted"

    assert timers.sweep(store, now=NOW + stale_after, spec=spec) == ("fp1",)
    assert states.current(store, "fp1") == states.CLOSED


def test_the_calendar_closing_an_item_is_distinguishable_from_noam_closing_it(
    spec, store
) -> None:
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    timers.sweep(store, now=NOW + timedelta(days=spec["manager"]["stale_days"]), spec=spec)
    sources = [e["source"] for e in store.state_history("fp1")]
    assert sources == [states.HUMAN, states.SYSTEM]


def test_the_stale_close_says_why_it_closed(spec, store) -> None:
    """Read back off the row, so the log explains itself months later."""
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    timers.sweep(store, now=NOW + timedelta(days=spec["manager"]["stale_days"]), spec=spec)
    assert store.state("fp1")["note"] == timers.STALE_NOTE


def test_the_sweep_does_not_revisit_what_it_already_closed(spec, store) -> None:
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    far = NOW + timedelta(days=spec["manager"]["stale_days"] * 3)
    assert timers.sweep(store, now=far, spec=spec) == ("fp1",)
    assert timers.sweep(store, now=far, spec=spec) == ()


# --------------------------------------------------------------------------
# the digest
# --------------------------------------------------------------------------


def test_an_empty_day_is_a_valid_answer(spec, store) -> None:
    """The required case. Nothing cleared the floor, and it says so plainly."""
    analysed(store, "low1", score=0.2)
    analysed(store, "low2", score=0.4)

    today = build(store, spec)
    assert today.empty is True
    assert today.items == ()
    assert today.considered == 0

    text = render.as_text(today)
    assert "יום ריק הוא תשובה תקפה." in text
    assert "never padded" in text


def test_the_digest_does_not_pad_itself_to_the_ceiling(spec, store) -> None:
    """Two above the floor and three below it means two items, not five."""
    spec["digest"]["max_items"] = 5
    for index in range(2):
        analysed(store, f"good{index}", score=0.9 - index / 100)
    for index in range(3):
        analysed(store, f"weak{index}", score=0.3)

    today = build(store, spec)
    assert len(today.items) == 2
    assert all(item.score >= spec["digest"]["min_score"] for item in today.items)


def test_the_floor_and_the_ceiling_come_from_the_spec(spec, store) -> None:
    for index in range(6):
        analysed(store, f"fp{index}", score=0.61 + index / 100)

    assert len(build(store, spec).items) == spec["digest"]["max_items"]

    spec["digest"]["max_items"] = 2
    assert len(build(store, spec).items) == 2

    spec["digest"]["min_score"] = 0.65
    assert all(i.score >= 0.65 for i in build(store, spec).items)


def test_recording_an_application_writes_the_store_s_blocklist_too(spec, store) -> None:
    """Two doors, and one of them was walled up.

    Nothing in `src/desk` called `mark_applied`, so `has_applied` was False for
    every posting in production and the blocklist the gates and
    `unseen_postings` read was permanently empty. The state move is what opens
    it, and only the move to `applied` does — an interview arranged by phone is
    not an application anybody made, and that is why the second door exists.
    """
    states.move(store, "fp1", states.APPLIED, spec=spec, now=NOW)
    assert store.has_applied("fp1") is True

    states.move(store, "fp2", "interview", spec=spec, now=NOW)
    assert store.has_applied("fp2") is False


def test_an_already_applied_posting_never_reappears(spec, store) -> None:
    """The required case, through the store's blocklist.

    The application is recorded the way Noam records one — a state move — and
    not by the test writing the blocklist itself, which is what let this pass
    while the only production writer of that blocklist did not exist. The
    blocklist is what suppresses here: it is asked before the state is.
    """
    analysed(store, "fp1", score=0.9)
    analysed(store, "fp2", score=0.8)
    assert [i.fingerprint for i in build(store, spec).items] == ["fp1", "fp2"]

    states.move(store, "fp1", states.APPLIED, spec=spec, now=NOW)
    assert store.has_applied("fp1") is True

    today = build(store, spec)
    assert [i.fingerprint for i in today.items] == ["fp2"]
    assert today.suppressed["applied"] == 1


def test_applying_through_one_board_suppresses_the_merged_twin(spec, store) -> None:
    """One cluster is one job, so one member applied to is the job applied to.

    The cluster is now unioned into the dedupe set before any suppression can
    `continue` past it. Seeding it afterwards meant an applied posting never
    entered the set at all, and its twin on the other board came back the next
    morning as a fresh job.
    """
    analysed(store, "fpA", score=0.9, site="alljobs")
    analysed(store, "fpB", score=0.85, site="drushim")
    store.record_link(
        "fpA", "fpB", score=0.95, band="duplicate", method="arithmetic", now=stamp(NOW)
    )
    states.move(store, "fpA", states.APPLIED, spec=spec, now=NOW)

    today = build(store, spec)
    assert today.items == ()
    assert today.suppressed["applied"] == 1
    assert today.suppressed["duplicate"] == 1


def test_an_interview_on_one_board_suppresses_the_merged_twin(spec, store) -> None:
    """The state door, asked of the cluster and not of one fingerprint.

    The store answers `has_applied` for the whole cluster; the state row has no
    cluster-aware reader, so the digest is what has to ask. Without it, a job
    Noam is interviewing for came back under the other board's fingerprint.
    """
    analysed(store, "fpA", score=0.9, site="alljobs")
    analysed(store, "fpB", score=0.85, site="drushim")
    store.record_link(
        "fpA", "fpB", score=0.95, band="duplicate", method="arithmetic", now=stamp(NOW)
    )
    states.move(store, "fpA", "interview", spec=spec, now=NOW)

    today = build(store, spec)
    assert [i.fingerprint for i in today.items] == []
    assert today.suppressed["later_state"] == 1
    assert today.suppressed["duplicate"] == 1


def test_the_digest_reads_the_gate_report_s_per_gate_verdicts(spec, store) -> None:
    """`GateReport.as_dict()` grew a three-valued top-level verdict today.

    The digest reads the per-gate list underneath it and never the top-level
    field, which is what keeps `unknown` visible rather than folded into a
    pass. Fed straight from a real report, so the two cannot drift apart.
    """
    from desk.gates.result import GateReport, GateResult, Verdict

    report = GateReport(
        (
            GateResult("freshness", Verdict.UNKNOWN, "the board states no date"),
            GateResult("geography", Verdict.PASS, "haifa", details={"accepted_regions": ["haifa"]}),
        )
    )
    payload = report.as_dict()
    assert payload["verdict"] == "unknown"
    assert payload["blocked"] is False

    analysed(store, "fp1", score=0.9, gates=tuple(payload["gates"]))
    item = build(store, spec).items[0]
    assert item.unknown_gates == ("freshness",)
    assert item.region == "haifa"

    blocked = GateReport((GateResult("geography", Verdict.BLOCK, "outside every region"),))
    assert blocked.as_dict()["blocked"] is True
    analysed(store, "fp2", score=0.95, gates=tuple(blocked.as_dict()["gates"]))

    today = build(store, spec)
    assert [i.fingerprint for i in today.items] == ["fp1"]
    assert today.suppressed["blocked"] == 1


def test_a_posting_in_a_later_state_never_reappears(spec, store) -> None:
    """The second door: an interview arranged by phone was never marked applied."""
    analysed(store, "fp1", score=0.9)
    analysed(store, "fp2", score=0.8)
    states.move(store, "fp1", "interview", spec=spec, now=NOW)

    today = build(store, spec)
    assert [i.fingerprint for i in today.items] == ["fp2"]
    assert today.suppressed["later_state"] == 1


def test_an_earlier_state_still_travels(spec, store) -> None:
    """Shortlisted is not finished, and a shortlisted job still comes up."""
    analysed(store, "fp1", score=0.9)
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    assert [i.fingerprint for i in build(store, spec).items] == ["fp1"]


def test_a_blocked_posting_never_reaches_the_digest(spec, store) -> None:
    analysed(store, "fp1", score=0.9, gates=(gate("geography", "block"),))
    today = build(store, spec)
    assert today.items == ()
    assert today.suppressed["blocked"] == 1


def test_an_unknown_verdict_travels_and_is_shown(spec, store) -> None:
    """A board that states no date must not look like one that states a fresh one."""
    gates = (
        gate("freshness", "unknown"),
        gate("geography", "pass", accepted_regions=["haifa"]),
    )
    analysed(store, "fp1", score=0.9, gates=gates, site="gotfriends")

    item = build(store, spec).items[0]
    assert item.unknown_gates == ("freshness",)
    assert "freshness=unknown" in item.gate_line()
    assert "unstated freshness" in render.as_text(build(store, spec))


def test_one_cluster_is_one_item(spec, store) -> None:
    analysed(store, "fp1", score=0.9, site="alljobs")
    analysed(store, "fp2", score=0.85, site="drushim")
    store.record_link(
        "fp1", "fp2", score=0.95, band="duplicate", method="arithmetic", now=stamp(NOW)
    )

    today = build(store, spec)
    assert [i.fingerprint for i in today.items] == ["fp1"]
    assert today.items[0].also_on == ("drushim",)
    assert today.suppressed["duplicate"] == 1


def test_the_region_is_the_geography_gate_s_own_finding(spec, store) -> None:
    analysed(store, "fp1", gates=(gate("geography", "pass", accepted_regions=["sharon"]),))
    assert build(store, spec).items[0].region == "sharon"

    analysed(store, "fp2", gates=(gate("geography", "pass", remote=True),))
    regions = {i.fingerprint: i.region for i in build(store, spec).items}
    assert regions["fp2"] == "remote"


def test_distance_is_reported_as_unmeasured_rather_than_guessed(spec, store) -> None:
    analysed(store, "fp1")
    assert build(store, spec).items[0].distance == digest_module.DISTANCE_UNMEASURED


def test_the_tailored_cv_path_travels_when_there_is_one(spec, store) -> None:
    analysed(store, "fp1")
    assert build(store, spec).items[0].has_cv is False

    store.put_tailored(
        "fp1",
        family="data_analyst",
        language="he",
        base_sha256="abc",
        path="/tmp/cv.docx",
        changes="[]",
        now=stamp(NOW),
    )
    item = build(store, spec).items[0]
    assert item.cv_path == "/tmp/cv.docx"
    assert item.has_cv is True


def test_ties_break_deterministically(spec, store) -> None:
    for name in ("zzz", "aaa", "mmm"):
        analysed(store, name, score=0.8)
    once = [i.fingerprint for i in build(store, spec).items]
    assert once == sorted(once)
    assert once == [i.fingerprint for i in build(store, spec).items]


def test_an_order_the_spec_does_not_define_raises(spec, store) -> None:
    spec["digest"]["order_by"] = "vibes"
    with pytest.raises(ValueError):
        build(store, spec)


def test_the_digest_carries_the_follow_ups_that_are_due(spec, store) -> None:
    analysed(store, "fp1", score=0.9)
    states.move(
        store,
        "fp2",
        states.APPLIED,
        spec=spec,
        now=NOW,
        due_at=timers.due_at_for(states.APPLIED, now=NOW, spec=spec),
    )
    later = NOW + timedelta(days=spec["manager"]["follow_up_days"] + 3)
    today = build(store, spec, now=later)
    assert [n.fingerprint for n in today.follow_ups] == ["fp2"]
    assert today.follow_ups[0].days_late == 3


def test_a_swept_close_appears_in_the_digest_that_swept_it(spec, store) -> None:
    """`desk digest` writes, and the write is never silent.

    The daily run is the only heartbeat, so it is the only moment at which
    "untouched for three weeks" can become "closed". That makes it the one
    place a read-looking command mutates, and the price of that is that every
    close it performs is reported in the same output.
    """
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    later = NOW + timedelta(days=spec["manager"]["stale_days"])
    closed = timers.sweep(store, now=later, spec=spec)

    today = build(store, spec, now=later, closed=closed)
    assert today.closed == ("fp1",)

    text = render.as_text(today)
    assert "fp1" in text
    assert one_direction_per_line(text) == []


def test_an_item_being_closed_today_is_neither_offered_nor_chased(spec, store) -> None:
    """The close is found first, reported, and written only after delivery.

    Between the finding and the writing, the row still says what it said, so
    the digest has to be told what is closing — otherwise the same item is
    listed as auto-closed and offered as a candidate in the same output, or
    chased as a follow-up nobody is going to make.
    """
    analysed(store, "fp1", score=0.9)
    states.move(store, "fp1", "shortlisted", spec=spec, now=NOW)
    states.move(
        store,
        "fp2",
        states.APPLIED,
        spec=spec,
        now=NOW,
        due_at=timers.due_at_for(states.APPLIED, now=NOW, spec=spec),
    )

    later = NOW + timedelta(days=spec["manager"]["stale_days"])
    closing = timers.pending(store, now=later, spec=spec)
    assert set(closing) == {"fp1", "fp2"}

    # Found, not yet written: the pipeline still says what it said.
    assert states.current(store, "fp1") == "shortlisted"

    today = build(store, spec, now=later, closed=closing)
    assert today.closed == closing
    assert today.items == ()
    assert today.suppressed["closed"] == 1
    assert today.follow_ups == ()


# --------------------------------------------------------------------------
# rendering — three views of one object, one direction per line
# --------------------------------------------------------------------------


def one_direction_per_line(text: str) -> list[str]:
    """Lines carrying both scripts. Terminals reorder those and scramble them."""
    return [line for line in text.splitlines() if HEBREW.search(line) and LATIN.search(line)]


def test_no_rendered_line_mixes_hebrew_and_english(spec, store) -> None:
    analysed(store, "fp1", score=0.9, title="אנליסט נתונים בכיר", channel=PERSON)
    analysed(store, "fp2", score=0.7, title="Data Analyst", company="Bluewick")
    store.record_link("fp1", "fp2", score=0.4, band="distinct", method="arithmetic", now=stamp(NOW))
    states.move(
        store,
        "fp3",
        states.APPLIED,
        spec=spec,
        now=NOW,
        due_at=timers.due_at_for(states.APPLIED, now=NOW, spec=spec),
    )
    later = NOW + timedelta(days=spec["manager"]["follow_up_days"])
    today = build(store, spec, now=later)

    assert one_direction_per_line(render.as_text(today)) == []
    assert one_direction_per_line(render.as_telegram(today)) == []


def test_the_empty_day_renders_in_both_languages_and_neither_line_mixes(spec, store) -> None:
    today = build(store, spec)
    text = render.as_text(today)
    assert HEBREW.search(text) and LATIN.search(text)
    assert one_direction_per_line(text) == []


def test_the_three_renderings_describe_the_same_digest(spec, store) -> None:
    analysed(store, "fp1", score=0.9)
    today = build(store, spec)

    payload = render.as_json(today)
    assert '"fp1"' in payload
    assert "fp1"[:8] in render.as_text(today) or today.items[0].title in render.as_text(today)
    assert today.items[0].title in render.as_telegram(today)


def test_the_json_states_that_the_system_never_applies(spec, store) -> None:
    assert build(store, spec).as_dict()["auto_apply"] == "never"


def test_a_long_digest_is_capped_to_one_telegram_message(spec, store) -> None:
    spec["digest"]["max_items"] = 200
    for index in range(200):
        analysed(store, f"fp{index:03d}", score=0.9, reason="x" * 200)
    message = render.as_telegram(build(store, spec))
    assert len(message) <= render.TELEGRAM_LIMIT
    assert message.endswith("truncated to fit one Telegram message")


def test_an_unknown_format_raises(spec, store) -> None:
    with pytest.raises(ValueError):
        render.render(build(store, spec), "postcard")


# --------------------------------------------------------------------------
# delivery — off by default, loud when asked for and not configured
# --------------------------------------------------------------------------


def test_stdout_is_the_default_sink(spec, store) -> None:
    stream = StringIO()
    sink = delivery.sink_for(spec, send=False, stream=stream)
    assert isinstance(sink, delivery.StdoutSink)
    sink.send(render.as_text(build(store, spec)))
    assert "job-desk digest" in stream.getvalue()


def test_the_flag_in_the_spec_is_what_selects_the_channel() -> None:
    """The spec is the switch, and this reads it rather than asserting its value.

    It used to assert `is False`, which was true of the day it was written and
    stopped being true the day Noam turned delivery on — a test that fails when
    the configuration it is describing is configured. What is worth pinning is
    that `telegram_enabled` reports the spec and invents nothing, in both
    directions, which is what `sink_for` branches on.
    """
    live = load_spec()
    assert delivery.telegram_enabled(live) is bool(
        live["manager"]["delivery"]["telegram"]
    )
    assert delivery.telegram_enabled({"manager": {"delivery": {"telegram": True}}}) is True
    assert delivery.telegram_enabled({"manager": {"delivery": {"telegram": False}}}) is False
    assert delivery.telegram_enabled({}) is False


def test_asking_to_send_into_a_disabled_channel_fails_loudly(spec) -> None:
    """A silent print here would look like a day with no jobs, for a week."""
    spec["manager"]["delivery"]["telegram"] = False
    with pytest.raises(delivery.DeliveryError, match="telegram is false"):
        delivery.sink_for(spec, send=True, env={})


def test_asking_to_send_without_credentials_fails_loudly(spec) -> None:
    spec["manager"]["delivery"]["telegram"] = True
    with pytest.raises(delivery.DeliveryError) as error:
        delivery.sink_for(spec, send=True, env={})
    assert delivery.TOKEN_ENV in str(error.value)
    assert delivery.CHAT_ENV in str(error.value)


def test_the_credentials_are_read_from_the_environment_and_nowhere_else(spec) -> None:
    spec["manager"]["delivery"]["telegram"] = True
    sink = delivery.sink_for(
        spec,
        send=True,
        env={delivery.TOKEN_ENV: "secret-token", delivery.CHAT_ENV: "12345"},
    )
    assert isinstance(sink, delivery.TelegramSink)
    assert sink.chat_id == "12345"


def test_the_token_cannot_reach_a_log_line_or_a_traceback() -> None:
    sink = delivery.TelegramSink("secret-token", "12345")
    assert "secret-token" not in repr(sink)
    assert "secret-token" not in str(sink)
    assert "secret-token" not in f"{sink}"
    assert delivery.REDACTED in repr(sink)


def test_no_credential_is_written_anywhere_in_the_repo() -> None:
    """The env var names may be in the repo. A value never is."""
    source = (REPO_ROOT / "src" / "desk" / "manager" / "delivery.py").read_text(encoding="utf-8")
    assert "api.telegram.org/bot{self._token}" in source
    assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{30,}", source)


class FakeResponse:
    """Enough of an HTTP response for `send`. No socket is opened by any test."""

    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_a_sink_cannot_be_built_with_only_half_the_credentials() -> None:
    with pytest.raises(delivery.DeliveryError):
        delivery.TelegramSink("", "12345")
    with pytest.raises(delivery.DeliveryError):
        delivery.TelegramSink("secret-token", "")


def test_a_send_is_one_plain_post_to_the_bot_api(monkeypatch) -> None:
    """Plain text and no parse mode: a Hebrew title is full of Markdown syntax."""
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sink = delivery.TelegramSink("secret-token", "12345")
    sink.send("אנליסט נתונים")

    assert seen["url"] == "https://api.telegram.org/botsecret-token/sendMessage"
    assert seen["body"] == {"chat_id": "12345", "text": "אנליסט נתונים"}
    assert "parse_mode" not in seen["body"]
    assert sink.sent == 1


def test_a_refusal_from_telegram_is_an_error_and_not_a_shrug(monkeypatch) -> None:
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout=None: FakeResponse(500)
    )
    sink = delivery.TelegramSink("secret-token", "12345")
    with pytest.raises(delivery.DeliveryError, match="HTTP 500"):
        sink.send("hello")
    assert sink.sent == 0


def test_a_failed_send_carries_no_token_into_the_error_or_the_traceback(monkeypatch) -> None:
    """The token sits in the URL, so the transport's own error message holds it.

    This is why `send` re-raises with only the exception's type name and severs
    the chain: a urllib error printed by a scheduled run would otherwise write
    the bot token into a log file, verbatim, every morning.
    """

    def fake_urlopen(request, timeout=None):
        raise OSError(f"cannot reach {request.full_url}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sink = delivery.TelegramSink("secret-token", "12345")
    with pytest.raises(delivery.DeliveryError) as error:
        sink.send("hello")

    assert "secret-token" not in str(error.value)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__ is True
    assert "secret-token" not in "".join(traceback.format_exception(error.value))
    assert sink.sent == 0


def test_the_spec_says_the_system_never_applies() -> None:
    delivery.check_auto_apply(load_spec())


def test_a_spec_that_permitted_applying_stops_the_run(spec) -> None:
    """The guard rail is code, not prose. Editing the spec does not unlock it."""
    spec["manager"]["delivery"]["auto_apply"] = "always"
    with pytest.raises(delivery.NeverApplies):
        delivery.check_auto_apply(spec)


# --------------------------------------------------------------------------
# the commands
# --------------------------------------------------------------------------


class Args:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def test_the_digest_command_runs_over_an_empty_store(tmp_path, monkeypatch, capsys) -> None:
    from desk.manager.command import cmd_digest

    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    code = cmd_digest(Args(limit=None, min_score=None, send=False, format="text"))
    assert code == 0
    assert "job-desk digest" in capsys.readouterr().out


def test_the_digest_command_refuses_to_send_while_delivery_is_off(
    tmp_path, monkeypatch, capsys
) -> None:
    from desk.manager.command import cmd_digest

    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    code = cmd_digest(Args(limit=None, min_score=None, send=True, format="text"))
    assert code == 1
    assert capsys.readouterr().out == ""


def test_a_failed_delivery_does_not_lose_the_closes(tmp_path, monkeypatch) -> None:
    """A close nobody was told about is a close that never happened again.

    The sweep used to commit before the send. When the send failed the command
    returned 1 with the rows already `closed`, and tomorrow's sweep found
    nothing left to close — so that auto-close was never reported in any
    digest, on any day. The write now happens after a successful delivery, and
    a failure simply leaves the rows stale for tomorrow.

    Only the sink is substituted here. Which sink `desk digest` is allowed to
    build, and what it takes to build the Telegram one, is covered by the
    delivery tests above and is untouched by this.
    """
    from desk.manager.command import cmd_digest

    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    live_spec = load_spec()
    old = datetime.now() - timedelta(days=live_spec["manager"]["stale_days"] + 1)
    db = Store(paths().ensure().db)
    states.move(db, "fp1", "shortlisted", spec=live_spec, now=old)
    db.close()

    class FailingSink:
        def send(self, text: str) -> None:
            raise delivery.DeliveryError("the channel refused the message")

        def send_documents(self, documents) -> None:
            raise AssertionError("attachments must not be attempted after a failed send")

    sent: list[str] = []

    class CapturingSink:
        def send(self, text: str) -> None:
            sent.append(text)

        def send_documents(self, documents) -> None:
            pass

    args = Args(limit=None, min_score=None, send=False, format="text")

    monkeypatch.setattr(delivery, "sink_for", lambda *a, **k: FailingSink())
    assert cmd_digest(args) == 1

    db = Store(paths().db)
    assert db.state("fp1")["state"] == "shortlisted"
    db.close()

    monkeypatch.setattr(delivery, "sink_for", lambda *a, **k: CapturingSink())
    assert cmd_digest(args) == 0
    assert "fp1" in sent[0]

    db = Store(paths().db)
    assert db.state("fp1")["state"] == states.CLOSED
    assert db.state("fp1")["note"] == timers.STALE_NOTE
    db.close()


def test_the_state_command_is_the_door_that_writes_the_blocklist(tmp_path, monkeypatch) -> None:
    """`desk state --set applied` is the production path, and it now marks."""
    from desk.manager.command import cmd_state

    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    args = Args(fingerprint="fp1", new_state="applied", note="", list_state=None, due=False)
    assert cmd_state(args) == 0

    db = Store(paths().db)
    assert db.has_applied("fp1") is True
    db.close()


def test_the_state_command_reports_an_illegal_move_instead_of_doing_it(
    tmp_path, monkeypatch
) -> None:
    from desk.manager.command import cmd_state

    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    forward = Args(
        fingerprint="fp1", new_state="applied", note="", list_state=None, due=False
    )
    assert cmd_state(forward) == 0
    backward = Args(
        fingerprint="fp1", new_state="approved", note="", list_state=None, due=False
    )
    assert cmd_state(backward) == 1


def test_approving_says_out_loud_that_nothing_was_submitted(
    tmp_path, monkeypatch, capsys
) -> None:
    from desk.manager.command import cmd_state

    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    cmd_state(Args(fingerprint="fp9", new_state="approved", note="", list_state=None, due=False))
    assert "nothing was submitted" in capsys.readouterr().out


# --------------------------------------------------------------------------
# the scheduled job
# --------------------------------------------------------------------------


def plist() -> dict:
    return plistlib.loads(PLIST.read_bytes())


def test_the_scheduled_job_carries_an_explicit_timeout() -> None:
    """The required case. Power Nap plus no timeout is a job hung forever."""
    schedule = load_spec()["digest"]["schedule"]
    seconds = int(schedule["timeout_seconds"])
    job = plist()
    assert job["EnvironmentVariables"]["DESK_TIMEOUT_SECONDS"] == str(seconds)
    assert job["ExitTimeOut"] == seconds


def test_the_wrapper_refuses_to_run_without_the_timeout() -> None:
    """A default in the script would be a second copy of a spec number."""
    script = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    assert "DESK_TIMEOUT_SECONDS" in script
    assert "exit 78" in script
    assert "kill -KILL" in script


def test_the_schedule_is_the_one_the_spec_states() -> None:
    minute, hour, *_ = str(load_spec()["digest"]["schedule"]["cron"]).split()
    interval = plist()["StartCalendarInterval"]
    assert interval["Hour"] == int(hour)
    assert interval["Minute"] == int(minute)


def test_the_scheduled_job_is_a_job_and_not_a_daemon() -> None:
    job = plist()
    assert job["KeepAlive"] is False
    assert job["RunAtLoad"] is False
    assert job["Label"] == "com.noamarazi.jobdesk"


def test_nothing_installs_the_job_for_the_user() -> None:
    """Loading a launch agent is a deliberate human act. Nothing here does it."""
    for path in (PLIST, PLIST.parent / "run-digest.sh"):
        assert "launchctl load" not in path.read_text(encoding="utf-8")
    package = Path(REPO_ROOT / "src" / "desk" / "manager")
    for module in package.glob("*.py"):
        assert "launchctl" not in module.read_text(encoding="utf-8")


# --- attachments: the digest names a CV, and the CV travels with it ----------


def _document(tmp_path, name="cv.docx", content=b"PK\x03\x04 a word file"):
    path = tmp_path / name
    path.write_bytes(content)
    return delivery.Document(path=path, caption="1.  score 0.90")


def test_a_missing_attachment_is_named_and_never_skipped(tmp_path) -> None:
    """The digest promised a document. A quiet skip is a lie about a fact."""
    missing = delivery.Document(path=tmp_path / "gone.docx")
    with pytest.raises(delivery.DeliveryError, match="not on disk"):
        missing.checked()


def test_an_empty_attachment_is_refused(tmp_path) -> None:
    empty = tmp_path / "cv.docx"
    empty.write_bytes(b"")
    with pytest.raises(delivery.DeliveryError, match="empty"):
        delivery.Document(path=empty).checked()


def test_an_oversized_attachment_is_refused_before_the_upload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(delivery, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(delivery.DeliveryError, match="over Telegram's cap"):
        _document(tmp_path).checked()


def test_stdout_names_the_files_and_still_checks_them(tmp_path) -> None:
    stream = StringIO()
    sink = delivery.StdoutSink(stream=stream)
    sink.send_documents([_document(tmp_path)])
    assert "cv.docx" in stream.getvalue()
    assert sink.attached == 1

    with pytest.raises(delivery.DeliveryError):
        sink.send_documents([delivery.Document(path=tmp_path / "nope.docx")])


def test_every_attachment_is_checked_before_the_first_byte_is_sent(tmp_path) -> None:
    """Either the set is deliverable or nothing is half-sent."""
    posted: list[str] = []
    sink = delivery.TelegramSink("secret-token", "12345")
    sink._post = lambda method, body, ctype, *, what: posted.append(method)  # type: ignore[method-assign]

    good = _document(tmp_path)
    missing = delivery.Document(path=tmp_path / "gone.docx")
    with pytest.raises(delivery.DeliveryError, match="not on disk"):
        sink.send_documents([good, missing])
    assert posted == []
    assert sink.attached == 0


def test_the_upload_carries_the_file_the_chat_and_the_caption(tmp_path) -> None:
    captured: dict[str, object] = {}
    sink = delivery.TelegramSink("secret-token", "12345")

    def fake_post(method, body, content_type, *, what):
        captured.update(method=method, body=body, content_type=content_type)

    sink._post = fake_post  # type: ignore[method-assign]
    sink.send_documents([_document(tmp_path)])

    assert captured["method"] == "sendDocument"
    assert str(captured["content_type"]).startswith("multipart/form-data; boundary=")
    body = bytes(captured["body"])  # type: ignore[arg-type]
    assert b'name="chat_id"' in body and b"12345" in body
    assert b'filename="cv.docx"' in body
    assert b"PK\x03\x04 a word file" in body
    assert b"secret-token" not in body
    assert sink.attached == 1


def test_a_caption_over_telegrams_limit_is_cut_rather_than_rejected(tmp_path) -> None:
    captured: dict[str, bytes] = {}
    sink = delivery.TelegramSink("secret-token", "12345")
    sink._post = lambda m, body, c, *, what: captured.update(body=body)  # type: ignore[method-assign]
    long_caption = delivery.Document(path=_document(tmp_path).path, caption="x" * 5000)
    sink.send_documents([long_caption])
    assert b"x" * delivery.CAPTION_LIMIT in captured["body"]
    assert b"x" * (delivery.CAPTION_LIMIT + 1) not in captured["body"]


def test_the_multipart_boundary_never_appears_inside_the_file() -> None:
    """A boundary occurring in the payload would end the part early."""
    body, content_type = delivery._multipart(
        fields={"chat_id": "1"}, filename="cv.docx", content=b"\x00\x01\x02"
    )
    boundary = content_type.split("boundary=", 1)[1]
    assert body.count(boundary.encode()) == 3  # one field, one file, one closing


def test_a_failed_upload_does_not_carry_the_token_into_the_error(tmp_path) -> None:
    """`urllib` puts the URL in its error text, and the token is in the URL."""
    sink = delivery.TelegramSink("secret-token", "12345")

    def explode(request, timeout=0):
        raise OSError(f"failed opening {request.full_url}")

    original = urllib.request.urlopen
    urllib.request.urlopen = explode  # type: ignore[assignment]
    try:
        with pytest.raises(delivery.DeliveryError) as error:
            sink.send_documents([_document(tmp_path)])
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]

    assert "secret-token" not in str(error.value)
    assert "cv.docx" in str(error.value)


def test_the_attachments_are_exactly_what_the_digest_shows(tmp_path) -> None:
    """Not a second selection out of the store. What was ranked is what is sent."""
    from desk.manager.command import _attachments

    written = tmp_path / "one.docx"
    written.write_bytes(b"x")
    ranked = digest_module.Digest(
        date="2026-08-19",
        considered=3,
        min_score=0.5,
        max_items=5,
        items=(
            digest_module.Item(fingerprint="a", score=0.9, title="AI Builder", cv_path=""),
            digest_module.Item(
                fingerprint="b", score=0.8, title="Data Analyst", cv_path=str(written)
            ),
        ),
    )
    documents = _attachments(ranked)
    assert [document.path for document in documents] == [written]
    assert documents[0].caption.splitlines() == ["2.  score 0.80", "Data Analyst"]


# --- a follow-up says who it is about ----------------------------------------


def test_a_nudge_is_labelled_by_the_employer_and_not_by_a_hash() -> None:
    """Sixteen hex characters is unactionable on the phone this is read on."""
    named = timers.Nudge(
        fingerprint="200ef3e84d170290",
        state="applied",
        due_at="2026-07-20",
        days_late=30,
        company="שלמה חברה לביטוח",
        title="אנליסט/ית ניהול סיכונים",
    )
    assert named.label() == "שלמה חברה לביטוח"

    titled = timers.Nudge(
        fingerprint="abc", state="applied", due_at="2026-07-20", days_late=1, title="Data Analyst"
    )
    assert titled.label() == "Data Analyst"


def test_a_follow_up_whose_posting_is_gone_keeps_the_reminder(store) -> None:
    """A dropped posting removes the name on a nudge, never the nudge."""
    orphan = timers.Nudge(
        fingerprint="ffffffffffffffff0000", state="applied", due_at="2026-07-20", days_late=30
    )
    assert "unnamed" in orphan.label()
    assert "ffffffffffffffff" in orphan.label()


def test_the_name_and_the_metrics_never_share_a_line() -> None:
    """A Hebrew company and a Latin date on one line reorder into each other."""
    nudge = timers.Nudge(
        fingerprint="200ef3e84d170290",
        state="applied",
        due_at="2026-07-20",
        days_late=30,
        company="שלמה חברה לביטוח",
    )
    name_line, metric_line = render._nudge_lines(nudge)
    assert name_line.strip() == "שלמה חברה לביטוח"
    assert "days late" in metric_line
    assert "שלמה" not in metric_line


def test_the_scheduled_job_actually_delivers(): 
    """A daily run that prints into a log file nobody opens is not a delivery."""
    wrapper = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    command = [line for line in wrapper.splitlines() if "desk digest" in line and "$UV" in line]
    assert len(command) == 1
    assert "--send" in command[0]


def test_the_morning_pass_refreshes_before_it_ranks() -> None:
    """The bug this file exists to keep fixed.

    Until 23.08.2026 the scheduled job ran `desk digest` and nothing else. A
    digest is a view: it fetches nothing and judges nothing, so every morning
    re-ranked whatever the store happened to hold and delivered the same list.
    An empty day is a valid answer in this system; an unchanging one is not.
    """
    wrapper = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    commands = [line for line in wrapper.splitlines() if "$UV" in line and "desk " in line]
    order = [c for c in ("desk fetch", "desk analyze", "desk digest")
             if any(c in line for line in commands)]
    assert order == ["desk fetch", "desk analyze", "desk digest"]


def test_the_site_list_is_the_spec_and_the_registry_and_not_a_third_copy() -> None:
    """A hard-coded list in the wrapper is a list that goes stale in silence."""
    from desk.sites import MODULES

    wrapper = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    for site in MODULES:
        assert f'--site {site}' not in wrapper
        assert f'"{site}"' not in wrapper
    assert "MODULES" in wrapper


def test_a_site_enabled_with_no_module_is_named_and_not_skipped() -> None:
    """jobify is enabled in the spec and has no module. Silence would read as ran."""
    from desk.sites import MODULES

    enabled = [s["id"] for s in load_spec()["sites"] if s.get("enabled")]
    unbuilt = [s for s in enabled if s not in MODULES]
    assert unbuilt, "if every enabled site is built, this guarantee needs a new fixture"
    wrapper = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    assert "unbuilt" in wrapper


def test_one_failing_board_does_not_cost_the_morning_its_other_sources() -> None:
    """Five sources exist so that one being down is survivable."""
    wrapper = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    assert "continuing with the rest" in wrapper


def test_the_wrapper_refuses_every_value_it_should_not_default() -> None:
    """Same rule as the timeout, now over the engine and the two ceilings.

    `--engine replay` is the dangerous default: against a live store it returns
    recorded answers that are indistinguishable from real judgements.
    """
    wrapper = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    env = plist()["EnvironmentVariables"]
    for name in ("DESK_TIMEOUT_SECONDS", "DESK_ENGINE",
                 "DESK_ANALYZE_BUDGET_USD", "DESK_ANALYZE_LIMIT"):
        assert f"fail_unset {name}" in wrapper
        assert name in env
    assert env["DESK_ENGINE"] != "replay"


def test_the_watchdog_signals_the_whole_pass_and_not_just_the_shell() -> None:
    """The pass is a subshell now; TERM to it alone leaves the hung fetch running."""
    wrapper = (PLIST.parent / "run-digest.sh").read_text(encoding="utf-8")
    assert "set -m" in wrapper
    assert 'kill -TERM -"$job"' in wrapper
    assert 'kill -KILL -"$job"' in wrapper


def test_no_credential_is_written_into_the_scheduled_job() -> None:
    """The plist is not gitignored. A token in it is a token in git history."""
    for path in (PLIST, PLIST.parent / "run-digest.sh"):
        text = path.read_text(encoding="utf-8")
        assert delivery.TOKEN_ENV + "=" not in text
        assert "api.telegram.org" not in text
