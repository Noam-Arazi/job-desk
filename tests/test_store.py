from __future__ import annotations

from desk.store import Posting, Store

NOW = "2026-01-01T00:00:00+00:00"


def make_store() -> Store:
    return Store(":memory:")


def test_the_same_role_from_two_sites_is_one_fingerprint():
    store = make_store()
    a = Posting(site="alljobs", external_id="1", title="Data Analyst", company="Bluewick")
    b = Posting(site="drushim", external_id="9", title="  data   analyst ", company="bluewick")
    assert a.fingerprint == b.fingerprint

    assert store.upsert_posting(a, now=NOW) is True
    assert store.upsert_posting(b, now=NOW) is False  # not new; the role was already known
    assert len(store.duplicates_of(a.fingerprint)) == 2
    assert store.counts()["fingerprints"] == 1


def test_rerunning_a_day_is_idempotent():
    store = make_store()
    posting = Posting(site="alljobs", external_id="1", title="Analyst", company="Bluewick")
    for _ in range(3):
        store.upsert_posting(posting, now=NOW)
    assert store.counts()["postings"] == 1
    assert store.counts()["fingerprints"] == 1


def test_the_applied_blocklist_suppresses_a_role():
    store = make_store()
    posting = Posting(site="alljobs", external_id="1", title="Analyst", company="Bluewick")
    store.upsert_posting(posting, now=NOW)
    assert [p["fingerprint"] for p in store.unseen_postings()] == [posting.fingerprint]

    store.mark_applied(posting.fingerprint, now=NOW)
    assert store.has_applied(posting.fingerprint) is True
    assert store.unseen_postings() == []


def test_the_blocklist_suppresses_the_role_not_the_listing():
    """Applied through AllJobs means the Drushim copy is suppressed too."""
    store = make_store()
    a = Posting(site="alljobs", external_id="1", title="Analyst", company="Bluewick")
    b = Posting(site="drushim", external_id="2", title="Analyst", company="Bluewick")
    store.upsert_posting(a, now=NOW)
    store.upsert_posting(b, now=NOW)
    store.mark_applied(a.fingerprint, now=NOW)
    assert store.unseen_postings() == []


def test_decisions_accumulate_per_role():
    store = make_store()
    store.record_decision(
        run_id="r1", fingerprint="fp", stage="gates", verdict="pass", now=NOW, score=0.8
    )
    store.record_decision(
        run_id="r1", fingerprint="fp", stage="fit_score", verdict="hold", now=NOW, score=0.55
    )
    decisions = store.decisions_for("fp")
    assert [d["stage"] for d in decisions] == ["gates", "fit_score"]
    assert decisions[1]["score"] == 0.55


def test_cv_bases_are_hash_pinned():
    store = make_store()
    store.put_cv_base("ai_builder", "he", "/x/base.md", "deadbeef", NOW)
    assert store.cv_base("ai_builder", "he")["sha256"] == "deadbeef"
    assert store.cv_base("ai_builder", "en") is None
