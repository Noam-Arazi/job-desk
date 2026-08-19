"""The spec is the source of truth, so the code must actually read it."""

from __future__ import annotations

from desk.config import enabled_sites, families, load_spec


def test_the_spec_loads_and_declares_a_version():
    assert load_spec()["version"] == 1


def test_the_four_families_are_the_ones_session_one_agreed():
    assert families() == ["ai_builder", "data_analyst", "product_project", "strategy_public"]


def test_every_family_names_a_cv_base_and_carries_both_languages():
    for name, family in load_spec()["families"].items():
        assert family["cv_base"], f"{name} has no CV base"
        assert family["terms_he"], f"{name} has no Hebrew search terms"
        assert family["terms_en"], f"{name} has no English search terms"


def test_linkedin_is_last_of_the_job_boards_and_uses_no_stealth():
    sites = load_spec()["sites"]
    linkedin = next(s for s in sites if s["id"] == "linkedin")
    # XPlace sits after it in the list but is the freelance pipeline, not a job
    # board — the ordering claim is about the boards LinkedIn competes with.
    boards = [s for s in sites if s.get("pipeline") != "freelance"]
    assert linkedin["order"] == max(s["order"] for s in boards)
    assert linkedin["stealth"] is False
    assert linkedin["fetch"] == "guest_api"


def test_linkedin_carries_its_own_disclosure_in_the_spec():
    """✅ 19.08.2026 — this site went from off-by-default behind a logged-in
    browser to on, against the logged-out guest endpoints. The reason it is
    the only site here read against its own the fetch rules has to travel with the
    entry itself: a reader who changes `enabled` should meet the disclosure
    before the request goes out, not after.
    """
    linkedin = next(s for s in load_spec()["sites"] if s["id"] == "linkedin")

    assert linkedin["enabled"] is True
    assert "the fetch rules" in linkedin["notes"]
    assert "no login" in linkedin["notes"]


def test_the_enabled_sites_are_ordered():
    assert enabled_sites() == [
        "alljobs",
        "drushim",
        "gotfriends",
        "jobify",
        "linkedin",
        "xplace",
    ]


def test_jobify_reads_through_the_browser_but_is_on_by_default():
    """It shares LinkedIn's access route and nothing else about its stance.

    The logged-in session is needed because the public path is a 2,199-shard
    sitemap with no titles, not because the site withholds anything: it
    invites reading, it forbids none, and no bypass is used. So unlike
    LinkedIn it ships enabled.
    """
    jobify = next(s for s in load_spec()["sites"] if s["id"] == "jobify")
    linkedin = next(s for s in load_spec()["sites"] if s["id"] == "linkedin")

    assert jobify["fetch"] == "attached_browser"
    assert jobify["stealth"] is False
    assert jobify["enabled"] is True
    assert jobify["order"] < linkedin["order"]


def test_the_scheduled_job_has_an_explicit_timeout():
    """An untimed launchd job hangs forever under Power Nap."""
    schedule = load_spec()["digest"]["schedule"]
    assert schedule["timeout_seconds"] > 0


def test_an_empty_day_is_representable():
    """A minimum score with no floor on item count means zero items is a valid digest."""
    digest = load_spec()["digest"]
    assert digest["min_score"] > 0
    assert "min_items" not in digest
