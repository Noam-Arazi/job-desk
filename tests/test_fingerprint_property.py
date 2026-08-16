"""Property tests: the fingerprint has to survive the mess real sites produce.

Cross-run dedup and the applied blocklist both key on it. If whitespace, casing
or an HTML wrapper changes the fingerprint, the same role resurfaces after it was
applied to — the one failure the blocklist exists to prevent.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from desk.store.fingerprint import fingerprint, normalize

# The alphabet is the one Israeli listings actually use: ASCII latin, Hebrew,
# digits, spaces and ordinary punctuation.
#
# It is deliberately not "all of Unicode". Several scripts have case mappings
# that do not round-trip through upper() — Turkish "ı" uppercases to "I" and
# folds back to "i"; Greek "ῒ" decomposes. Making the fingerprint survive those
# would mean a locale-aware fold, which buys nothing here and costs a dependency.
ALPHABET = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " .,-/()&+"
    "אבגדהוזחטיכךלמםנןסעפףצץקרשת"
)

text = st.text(alphabet=ALPHABET, min_size=1, max_size=40).filter(lambda s: normalize(s) != "")


@given(title=text, company=text)
@settings(max_examples=200, deadline=None)
def test_whitespace_does_not_change_the_fingerprint(title, company):
    padded_title = f"  {title}   \n\t"
    padded_company = f"\n {company}  "
    assert fingerprint(title, company) == fingerprint(padded_title, padded_company)


@given(title=text, company=text)
@settings(max_examples=200, deadline=None)
def test_casing_does_not_change_the_fingerprint(title, company):
    assert fingerprint(title, company) == fingerprint(title.upper(), company.upper())


@given(title=text, company=text)
@settings(max_examples=200, deadline=None)
def test_html_noise_does_not_change_the_fingerprint(title, company):
    wrapped = f"<p><strong>{title}</strong>&nbsp;</p>"
    assert fingerprint(title, company) == fingerprint(wrapped, company)


@given(title=text, company=text)
@settings(max_examples=200, deadline=None)
def test_the_fingerprint_is_a_stable_shape(title, company):
    fp = fingerprint(title, company)
    assert len(fp) == 16
    assert fp == fingerprint(title, company)


@given(company=text)
@settings(max_examples=100, deadline=None)
def test_different_titles_at_one_company_are_different_roles(company):
    assert fingerprint("Data Analyst", company) != fingerprint("AI Engineer", company)


def test_agency_reference_noise_is_stripped():
    assert fingerprint("Analyst (Ref: 4471)", "Bluewick") == fingerprint("Analyst", "Bluewick")


def test_location_participates():
    assert fingerprint("Analyst", "Bluewick", "Haifa") != fingerprint(
        "Analyst", "Bluewick", "Netanya"
    )
    assert fingerprint("Analyst", "Bluewick", "Tel Aviv") == fingerprint(
        "Analyst", "Bluewick", "tel-aviv"
    )


def test_normalize_never_raises_on_odd_input():
    for value in ["", "   ", "<<>>", "&nbsp;&amp;", "‏‎", "🙂"]:
        normalize(value)
