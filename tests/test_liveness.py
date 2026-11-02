"""The open-check before delivery.

02.11.2026: the top two items in the digest were closed by the time Noam opened
them — Migdal on alljobs, Peak Innovation on jobify. Both boards still served
them in search; only the posting's own page said so.
"""

from __future__ import annotations

from dataclasses import dataclass

from desk.manager import liveness


@dataclass
class _Item:
    fingerprint: str
    site: str
    url: str


@dataclass
class _Digest:
    items: list[_Item]


def test_alljobs_says_the_employer_withdrew_it() -> None:
    page = '<div class="x">משרה זו סומנה ע"י המעסיק כלא אקטואלית יותר</div>'
    assert liveness.is_closed("alljobs", page) is True


def test_jobify_says_it_is_closed_now() -> None:
    page = "<h1>Business Analyst</h1><p>המשרה הזאת סגורה כעת.</p>"
    assert liveness.is_closed("jobify", page) is True


def test_an_open_page_is_open() -> None:
    page = "<h1>אנליסט/ית דאטה</h1><button>הגשת קורות חיים</button>"
    assert liveness.is_closed("jobify", page) is False


def test_a_site_with_no_known_marker_is_not_judged() -> None:
    assert liveness.is_closed("gotfriends", "anything at all") is None


def test_a_closed_item_is_closed_and_the_next_one_moves_up() -> None:
    a, b, c = _Item("a", "alljobs", "u1"), _Item("b", "jobify", "u2"), _Item("c", "jobify", "u3")
    board = [a, b, c]
    closed: list[str] = []

    def build() -> _Digest:
        return _Digest([i for i in board if i.fingerprint not in closed][:2])

    today, gone = liveness.drop_closed(
        build,
        check=lambda item: item.fingerprint == "a",
        close=lambda item: closed.append(item.fingerprint),
    )

    assert [i.fingerprint for i in today.items] == ["b", "c"]
    assert [i.fingerprint for i in gone] == ["a"]


def test_a_page_that_could_not_be_read_keeps_its_item() -> None:
    """Unknown is not closed. A board that is down costs the check, not the job."""
    item = _Item("a", "jobify", "u1")
    today, gone = liveness.drop_closed(
        lambda: _Digest([item]), check=lambda _: None, close=lambda _: None
    )

    assert today.items == [item]
    assert gone == []


def test_the_rounds_are_bounded() -> None:
    """Every refill can bring another closed job. The check stops after a few
    rounds rather than walking the whole ranking one page at a time."""
    calls = []

    def build() -> _Digest:
        calls.append(1)
        return _Digest([_Item(str(len(calls)), "alljobs", "u")])

    liveness.drop_closed(build, check=lambda _: True, close=lambda _: None, rounds=3)

    assert len(calls) == 4
