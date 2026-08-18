"""What the duplicate resolver has to keep being true.

Every threshold in this module was set by running it over the 369 postings the
store held on 2026-08-18, and several of them were set by a false merge it made
first. The tests that pin those cases carry the pair that caused them, so a
later loosening fails loudly instead of quietly re-merging two different jobs.
"""

from __future__ import annotations

from desk.resolve import (
    DISTINCT,
    DUPLICATE,
    UNCERTAIN,
    candidate_pairs,
    cluster,
    core_tokens,
    resolve,
    role_core,
    score_pair,
    strip_gender,
)
from desk.resolve.similarity import (
    body_similarity,
    company_agrees,
    placeholder_to_blank,
    shingles,
)


def posting(fp: str, title: str, body: str = "", company: str = "", site: str = "alljobs"):
    return {
        "fingerprint": fp,
        "title": title,
        "body": body,
        "company": company,
        "site": site,
    }


# --------------------------------------------------------------------------
# role cores
# --------------------------------------------------------------------------


def test_dual_gender_spellings_collapse_onto_one_stem() -> None:
    assert strip_gender("מנהל /ת פרויקטים") == "מנהל פרויקטים"
    assert strip_gender("איש.ת צוות בית מרקחת") == "איש צוות בית מרקחת"
    assert strip_gender("נציגי /ות שירות") == "נציגי שירות"


def test_a_slash_between_two_words_is_not_a_gender_suffix() -> None:
    """Hebrew uses the same slash to separate alternatives. Without a right-hand
    boundary the leading letter of the second alternative is eaten and the two
    words weld into one that exists in no other posting: "מדעי המחשב/הנדסת
    תוכנה" became "המחשבנדסת תוכנה". Found by the gates session, 2026-08-18."""
    assert strip_gender("תואר ראשון במדעי המחשב/הנדסת תוכנה") == (
        "תואר ראשון במדעי המחשב/הנדסת תוכנה"
    )
    assert strip_gender("תואר בסטטיסטיקה/ מתמטיקה/ תואר טכנולוגי") == (
        "תואר בסטטיסטיקה/ מתמטיקה/ תואר טכנולוגי"
    )
    assert strip_gender("מנהל /תפעול") == "מנהל /תפעול"


def test_a_real_gender_suffix_still_goes() -> None:
    """The boundary must not cost the case the pattern exists for."""
    assert strip_gender("דרוש /ה אנליסט /ית") == "דרוש אנליסט"
    assert strip_gender("מנהל /ית פרויקטים") == "מנהל פרויקטים"


def test_an_agency_blurb_after_the_role_is_cut() -> None:
    """GotFriends shape: the role leads, the unnamed client follows."""
    assert role_core("Senior BI Developer בחברת סטארט-אפ בתחום ה-Analytics") == (
        "Senior BI Developer"
    )
    assert role_core("NLP Engineer לחברת סטארטאפ בתל אביב העוסקת בתחום המדיקל") == (
        "NLP Engineer"
    )


def test_an_employer_lead_in_before_the_role_is_cut() -> None:
    """AllJobs shape: the employer leads and the role follows a wanted-verb.

    The opposite order to the agency's, which is why one rule cannot serve both
    and why comparing raw titles scores these two shapes as unrelated.
    """
    assert role_core("לחברת הביטוח AIG דרושים /ות נציגים /ות למוקד שירות") == (
        "נציגים למוקד שירות"
    )
    assert role_core("חברת סטארטאפ לאחר Seed מגייסת NLP Researcher - תל אביב") == (
        "NLP Researcher"
    )


def test_an_unspaced_hyphen_is_part_of_a_word_and_never_a_separator() -> None:
    """Cutting at every hyphen truncated "Full-Stack Developer" to "Full"."""
    assert role_core("Full-Stack Developer בחברת הייטק") == "Full-Stack Developer"
    assert role_core("Senior Data Engineer בחברת סטארט-אפ בתחום ה-Mobility") == (
        "Senior Data Engineer"
    )
    assert role_core("R&D Engineer - תל אביב") == "R&D Engineer"


def test_a_comma_inside_a_number_is_not_a_separator() -> None:
    """It cut "משכורות של 16,000 בחודש" down to "16"."""
    assert role_core("משכורות של 16,000 בחודש, שיחות נכנסות") == "משכורות של 16,000 בחודש"


def test_an_unparseable_title_survives_whole() -> None:
    """A title we cannot take apart is still better evidence than nothing."""
    assert role_core("אנליסט נתונים") == "אנליסט נתונים"


def test_single_characters_are_not_tokens() -> None:
    """A one-letter leftover from a clause cut would match every posting."""
    assert all(len(t) > 1 for t in core_tokens("מנהל /ת ב פרויקטים"))


# --------------------------------------------------------------------------
# similarity
# --------------------------------------------------------------------------


def test_a_stated_non_name_is_treated_as_no_name() -> None:
    """27 of the 191 AllJobs rows say "חברה חסויה". Reading that as an employer
    would merge every confidential posting on the board into a single job."""
    assert placeholder_to_blank("חברה חסויה") == ""
    assert placeholder_to_blank("קבוצת אלקטרה") == "קבוצת אלקטרה"


def test_an_unknown_employer_is_not_a_disagreement() -> None:
    assert company_agrees("", "קבוצת אלקטרה") is None
    assert company_agrees("חברה חסויה", "קבוצת אלקטרה") is None
    assert company_agrees("קבוצת אלקטרה", "קבוצת אלקטרה") is True
    assert company_agrees("קבוצת אלקטרה", "סלקום") is False


def test_shingles_of_a_missing_body_are_empty_and_match_nothing() -> None:
    """Two postings with no body must not match each other on that absence."""
    assert shingles("") == frozenset()
    assert body_similarity("", "") == 0.0


def test_character_shingles_survive_a_hebrew_prefix() -> None:
    """"בחברה" and "לחברה" share no word token and four of five characters."""
    assert body_similarity("עבודה בחברה מובילה", "עבודה לחברה מובילה") > 0.5


# --------------------------------------------------------------------------
# blocking
# --------------------------------------------------------------------------


def test_postings_sharing_no_informative_token_are_never_compared() -> None:
    rows = [posting("a", "AI Engineer"), posting("b", "נהג משאית")]
    assert candidate_pairs(rows) == set()


def test_a_token_in_too_many_postings_stops_blocking_on_it() -> None:
    """Otherwise "מנהל" rebuilds the N-squared comparison the index avoids."""
    rows = [posting(str(i), f"מנהל תחום {i}") for i in range(20)]
    assert candidate_pairs(rows, max_doc_frequency=0.10) == set()


# --------------------------------------------------------------------------
# bands
# --------------------------------------------------------------------------

BLURB = "תיאור המשרה: עבודה מול צוות פיתוח, אחריות על ניתוח נתונים ובניית דשבורדים"


def test_the_same_role_across_two_sites_with_the_employers_own_text_merges() -> None:
    left = posting("a", "אנליסט נתונים", BLURB, "סלקום", site="alljobs")
    right = posting("b", "דרוש /ה אנליסט /ית נתונים", BLURB, "", site="drushim")
    assert score_pair(left, right).band == DUPLICATE


def test_shared_prose_inside_one_site_is_one_author_and_not_one_job() -> None:
    """The false merge that set this rule: an agency reuses a client blurb
    across every seat it is filling there, so within a site the role itself has
    to agree too."""
    left = posting("a", "Data Platform Engineer בחברת סטארט-אפ", BLURB, site="gotfriends")
    right = posting("b", "Product Analyst בחברת סטארט-אפ", BLURB, site="gotfriends")
    assert score_pair(left, right).band == UNCERTAIN


def test_two_ranks_of_one_role_never_merge_however_alike_the_prose() -> None:
    """AI Platform Engineer merged with AI Platform Team Lead at the same
    client. Two seniorities are two openings."""
    left = posting("a", "AI Platform Engineer בחברת סטארט-אפ בתחום ה-Cyber", BLURB)
    right = posting("b", "AI Platform Team Lead בחברת סטארט-אפ בתחום ה-Cyber", BLURB)
    assert score_pair(left, right).band != DUPLICATE


def test_one_large_employer_posts_many_different_jobs() -> None:
    """A Cellcom distribution-channel rep merged with a Cellcom warehouse hand
    on the employer's boilerplate alone. Agreement supports, it cannot carry."""
    left = posting("a", "נציג /ת תפעול ערוצי הפצה", BLURB, "סלקום")
    right = posting("b", "מחסנאי /ת מחסן הפצה", BLURB, "סלקום")
    assert score_pair(left, right).band != DUPLICATE


def test_two_named_employers_that_differ_need_verbatim_text_to_merge() -> None:
    same_role = "אנליסט נתונים"
    apart = posting("a", same_role, BLURB, "סלקום")
    other = posting("b", same_role, "טקסט אחר לגמרי על תפקיד אחר בארגון אחר", "פרטנר")
    assert score_pair(apart, other).band == DISTINCT


def test_matching_titles_with_unrelated_text_are_settled_not_escalated() -> None:
    """183 of 185 same-title pairs sat at a body similarity of 0.0 to 0.2.
    Paying a model for those is paying for an answer already in hand."""
    left = posting("a", "AI Engineer", "פיתוח מודלים ואימון רשתות נוירונים לזיהוי תמונה")
    right = posting("b", "AI Engineer", "ליווי לקוחות עסקיים והדרכות בשטח מול מנהלי מכירות")
    assert score_pair(left, right).band == DISTINCT


# --------------------------------------------------------------------------
# clustering and escalation
# --------------------------------------------------------------------------


def test_a_merge_is_transitive_across_pairs_never_compared_directly() -> None:
    groups = cluster(["a", "b", "c", "d"], [("a", "b"), ("b", "c")])
    assert groups == [["a", "b", "c"]]


def test_a_posting_that_matched_nothing_is_not_a_cluster() -> None:
    assert cluster(["a", "b"], []) == []


def test_without_a_judge_the_uncertain_band_does_not_merge() -> None:
    """The safe direction: a duplicate in the digest costs a line, a wrong
    collapse loses a job silently."""
    rows = [
        posting("a", "Data Platform Engineer בחברת סטארט-אפ", BLURB, site="gotfriends"),
        posting("b", "Data Platform Architect בחברת סטארט-אפ", BLURB, site="gotfriends"),
    ]
    result = resolve(rows)
    assert result.clusters == []
    assert result.judged == 0


def test_only_the_uncertain_band_reaches_the_judge() -> None:
    seen: list[tuple[str, str]] = []

    def judge(left, right, score) -> bool:
        seen.append((left["fingerprint"], right["fingerprint"]))
        return True

    rows = [
        posting("a", "Data Platform Engineer בחברת סטארט-אפ", BLURB, site="gotfriends"),
        posting("b", "Data Platform Architect בחברת סטארט-אפ", BLURB, site="gotfriends"),
        posting("c", "נהג משאית עד 12 טון", "הובלות בצפון", site="alljobs"),
    ]
    result = resolve(rows, judge=judge)

    assert seen == [("a", "b")]
    assert result.judged == 1
    assert result.clusters == [["a", "b"]]


def test_the_summary_reports_what_was_settled_and_what_was_paid_for() -> None:
    rows = [posting(str(i), f"תפקיד ייחודי {i}", f"טקסט ייחודי מספר {i}") for i in range(4)]
    summary = resolve(rows).summary()
    assert summary["judged"] == 0
    assert summary["collapsed"] == 0
    assert set(summary) == {
        "compared", "duplicate", "uncertain", "judged", "clusters", "collapsed",
    }


# --------------------------------------------------------------------------
# what the store exposes to the gates
# --------------------------------------------------------------------------


def test_a_merged_role_is_as_old_as_the_first_time_anyone_showed_it(ctx) -> None:
    """Freshness reads the cluster, never the single fingerprint. Otherwise a
    job that sat on a board for three weeks looks new the day an agency relists
    it, and it passes the seven-day window every time it moves between sites."""
    from desk.store import Posting

    old = Posting(site="alljobs", external_id="1", title="אנליסט נתונים", company="סלקום")
    new = Posting(site="gotfriends", external_id="2", title="BI Analyst", company="")
    ctx.store.upsert_posting(old, now="2026-07-20T09:00:00")
    ctx.store.upsert_posting(new, now="2026-08-18T09:00:00")
    ctx.store.record_link(
        old.fingerprint, new.fingerprint,
        score=0.9, band=DUPLICATE, method="deterministic", now="2026-08-18T09:00:00",
    )

    assert ctx.store.first_seen(new.fingerprint) == "2026-08-18T09:00:00"
    assert ctx.store.cluster_first_seen(new.fingerprint) == "2026-07-20T09:00:00"


def test_a_role_that_matched_nothing_needs_no_special_case(ctx) -> None:
    from desk.store import Posting

    alone = Posting(site="alljobs", external_id="9", title="נהג משאית", company="ע.נ.מ")
    ctx.store.upsert_posting(alone, now="2026-08-18T09:00:00")

    assert ctx.store.merged_with(alone.fingerprint) == [alone.fingerprint]
    assert ctx.store.cluster_first_seen(alone.fingerprint) == "2026-08-18T09:00:00"


def test_every_verdict_is_recorded_and_not_only_the_merges(ctx) -> None:
    """A pair the arithmetic called distinct is the evidence it was looked at."""
    ctx.store.record_link("a", "b", score=0.9, band=DUPLICATE, method="deterministic", now="t")
    ctx.store.record_link("c", "d", score=0.1, band=DISTINCT, method="deterministic", now="t")

    assert len(ctx.store.links()) == 2
    assert len(ctx.store.links(DUPLICATE)) == 1


def test_a_pair_is_stored_once_whichever_order_it_arrives_in(ctx) -> None:
    ctx.store.record_link("b", "a", score=0.9, band=DUPLICATE, method="deterministic", now="t")
    ctx.store.record_link("a", "b", score=0.9, band=DUPLICATE, method="judged", now="t")

    rows = ctx.store.links()
    assert len(rows) == 1
    assert rows[0]["method"] == "judged"
