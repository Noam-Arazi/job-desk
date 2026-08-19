"""Hand-labelling duplicate pairs — the sampling, not the typing.

The property under test is the same one that makes the gold set worth anything:
the person is not shown what the system decided. Merged and uncertain pairs are
drawn together, so a confirmation and a rejection cost the same and neither is
a nod at a verdict already on screen.
"""

from __future__ import annotations

from desk.resolve import review

ROWS = {
    "fp1": {"fingerprint": "fp1", "site": "alljobs", "title": "אנליסט/ית דאטה",
            "company": "סונול", "location": "נתניה", "body": "עבודה עם SQL", "url": "u1"},
    "fp2": {"fingerprint": "fp2", "site": "drushim", "title": "דרוש/ה אנליסט/ית דאטה",
            "company": "", "location": "נתניה", "body": "עבודה עם SQL", "url": "u2"},
    "fp3": {"fingerprint": "fp3", "site": "gotfriends", "title": "BI Analyst",
            "company": "", "location": "מרכז", "body": "dashboards", "url": "u3"},
}

LINKS = [
    {"left_fp": "fp1", "right_fp": "fp2", "band": "duplicate", "score": 0.9},
    {"left_fp": "fp1", "right_fp": "fp3", "band": "uncertain", "score": 0.6},
    {"left_fp": "fp2", "right_fp": "fp3", "band": "distinct", "score": 0.1},
]


def test_merged_and_uncertain_are_drawn_together() -> None:
    """A person shown only merges is being asked to agree, not to judge."""
    pairs = review.sample(LINKS, ROWS, size=10)
    bands = {(p.left, p.right) for p in pairs}
    assert ("fp1", "fp2") in bands
    assert ("fp1", "fp3") in bands
    assert ("fp2", "fp3") not in bands, "the arithmetic settled this one"


def test_nothing_on_screen_says_what_the_resolver_decided() -> None:
    pair = review.sample(LINKS, ROWS, size=10)[0]
    rendered = "\n".join(pair.as_lines())
    for word in ("duplicate", "uncertain", "merged", "score", "0.9"):
        assert word not in rendered


def test_nothing_this_screen_composes_mixes_two_directions_on_one_line() -> None:
    """A terminal reorders an English run inside a Hebrew line.

    The advert's own text is exempt and quoted as published — a person judging
    whether two postings are one seat has to see the real words. What is under
    test is everything this code puts around it: no field is joined to another
    by a separator and no line carries an English label in front of Hebrew.
    """
    pair = review.Pair(
        "fp1",
        "fp2",
        {"site": "alljobs", "title": "אנליסט/ית", "company": "Sonol", "location": "נתניה"},
        {"site": "drushim", "title": "אנליסט/ית", "company": "", "location": "נתניה"},
    )
    composed = [line for line in pair.as_lines() if line.strip() and "[" not in line]
    for line in composed:
        stripped = line.strip()
        has_hebrew = any("א" <= c <= "ת" for c in stripped)
        has_latin = any(c.isascii() and c.isalpha() for c in stripped)
        assert not (has_hebrew and has_latin), line


def test_the_same_seed_draws_the_same_pairs() -> None:
    first = review.sample(LINKS, ROWS, size=2, seed=7)
    second = review.sample(LINKS, ROWS, size=2, seed=7)
    assert [(p.left, p.right) for p in first] == [(p.left, p.right) for p in second]


def test_a_skip_writes_nothing() -> None:
    """An unjudged pair stays unjudged rather than becoming a guess in the
    denominator of a measurement whose value is that a person looked."""
    fixture = {"clusters": [], "distinct_pairs": []}
    pair = review.sample(LINKS, ROWS, size=1)[0]
    review.record(fixture, pair, review.SKIP)
    assert fixture == {"clusters": [], "distinct_pairs": []}


def test_a_confirmation_joins_an_existing_cluster() -> None:
    fixture = {"clusters": [{"members": ["fp1", "fp2"], "note": ""}], "distinct_pairs": []}
    pair = review.Pair("fp2", "fp3", ROWS["fp2"], ROWS["fp3"])
    review.record(fixture, pair, review.SAME)
    assert fixture["clusters"] == [{"members": ["fp1", "fp2", "fp3"], "note": ""}]


def test_a_pair_already_settled_is_not_asked_again() -> None:
    fixture = {"clusters": [{"members": ["fp1", "fp2"]}], "distinct_pairs": []}
    judged = review.already_judged(fixture)
    pairs = review.sample(LINKS, ROWS, size=10, exclude=judged)
    assert ("fp1", "fp2") not in {(p.left, p.right) for p in pairs}
