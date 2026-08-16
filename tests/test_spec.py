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


def test_linkedin_is_last_of_the_job_boards_disabled_and_uses_no_stealth():
    sites = load_spec()["sites"]
    linkedin = next(s for s in sites if s["id"] == "linkedin")
    # XPlace sits after it in the list but is the freelance pipeline, not a job
    # board — the ordering claim is about the boards LinkedIn competes with.
    boards = [s for s in sites if s.get("pipeline") != "freelance"]
    assert linkedin["order"] == max(s["order"] for s in boards)
    assert linkedin["enabled"] is False
    assert linkedin["stealth"] is False
    assert linkedin["fetch"] == "attached_browser"


def test_the_enabled_sites_are_ordered():
    assert enabled_sites() == ["alljobs", "drushim", "gotfriends", "xplace"]


def test_the_scheduled_job_has_an_explicit_timeout():
    """An untimed launchd job hangs forever under Power Nap."""
    schedule = load_spec()["digest"]["schedule"]
    assert schedule["timeout_seconds"] > 0


def test_an_empty_day_is_representable():
    """A minimum score with no floor on item count means zero items is a valid digest."""
    digest = load_spec()["digest"]
    assert digest["min_score"] > 0
    assert "min_items" not in digest
