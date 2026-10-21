"""Benchmark brands: how a brand name becomes a Meta advertiser page, and when the tool must NOT decide.

Every rule has its own test with exact expected answers. The page names, ids and follower counts in the
candidate lists below are shaped like the real Ad Library data (SHEIN, SHEIN CURVE, Madepants).
"""
import pytest

from contentintel import benchmarks as B

NOW = "2026-10-21T09:00:00Z"


def cand(page_id, name, likes=0, ads=1):
    return {"page_id": page_id, "name": name, "likes": likes, "ads": ads}


# ---- names ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Gymshark", "gymshark"), ("GYMSHARK", "gymshark"), ("The Oodie", "oodie"), ("the oodie", "oodie"),
    ("Rebel Babe®", "rebelbabe"), ("Rebel  Babe", "rebelbabe"), ("Chic Me", "chicme"), ("ChicMe", "chicme"),
    ("Superdw-DE", "superdwde"), ("Café Noir", "cafenoir"), ("Gymshark Official", "gymshark"),
    ("Gymshark Women", "gymsharkwomen"), ("Oodie Inc.", "oodie"), ("", ""), (None, ""), ("!!!", "")])
def test_names_are_compared_without_case_spacing_punctuation_or_filler_words(text, expected):
    assert B.norm_name(text) == expected


def test_a_name_made_only_of_filler_words_is_empty_so_it_can_never_match():
    assert B.norm_name("The Official Co") == ""


# ---- page ids ------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("380039845369159", "380039845369159"), (380039845369159, "380039845369159"), ("  108271304233408 ", "108271304233408"),
    ("https://www.facebook.com/ads/library/?active_status=all&view_all_page_id=380039845369159&country=US", "380039845369159"),
    ("https://www.facebook.com/ads/library/?view_all_page_id=abc", None), ("https://www.facebook.com/ads/library/?q=x", None),
    ("https://evil.example/?view_all_page_id=380039845369159", None), ("1234", None), ("12345678901234567890123", None),
    ("Gymshark", None), ("", None), (None, None), (True, None), (12.5, None), (["1"], None)])
def test_a_page_id_is_read_from_a_number_or_an_ad_library_link_and_nothing_else(value, expected):
    assert B.page_id_from(value) == expected


# ---- the manual override in research.yaml --------------------------------------------------------

def test_manual_choices_are_read_as_page_ids_or_skip():
    got = B.parse_manual({"Gymshark": 380039845369159, "The Oodie": "https://www.facebook.com/ads/library/?view_all_page_id=108271304233408",
                          "Some Brand": "skip", "Other": "None", "Third": "IGNORE"}, "x")
    assert got == {"Gymshark": "380039845369159", "The Oodie": "108271304233408", "Some Brand": "skip", "Other": "skip",
                   "Third": "skip"}


@pytest.mark.parametrize("empty", [None, {}])
def test_no_manual_choices_is_fine(empty):
    assert B.parse_manual(empty, "x") == {}


@pytest.mark.parametrize("bad,match", [
    (["Gymshark"], "list of name: page-id pairs"), ("Gymshark: 1", "list of name: page-id pairs"),
    ({"Gymshark": "not an id"}, "must be a Meta page id"), ({"Gymshark": 12}, "must be a Meta page id"),
    ({"Gymshark": None}, "must be a Meta page id"), ({"Gymshark": "https://www.facebook.com/ads/library/?q=x"}, "must be a Meta page id"),
    ({"": 123456}, "not a usable brand name"), ({"!!!": 123456}, "not a usable brand name"), ({12: 123456}, "not a usable brand name"),
    ({"Oodie": 123456, "The Oodie": 654321}, "same brand written twice")])
def test_a_bad_manual_choice_is_refused_with_a_message_that_names_it(bad, match):
    with pytest.raises(B.BenchmarkError, match=match):
        B.parse_manual(bad, "swoveralls/research.yaml ads.benchmark_pages")


def test_the_error_names_the_file_so_a_person_can_find_it():
    with pytest.raises(B.BenchmarkError, match="swoveralls/research.yaml ads.benchmark_pages"):
        B.parse_manual({"Gymshark": "nope"}, "swoveralls/research.yaml ads.benchmark_pages")


# ---- candidates from ads -------------------------------------------------------------------------

def item(item_id, page_id, name, likes, via):
    return {"item_id": item_id, "page_id": page_id, "handle": name, "page_likes": likes, "found_via": via}


def test_candidates_are_the_advertiser_pages_behind_the_ads_one_search_found():
    items = [item("a1", "1", "Gymshark", 6_000_000, "Gymshark"), item("a2", "1", "Gymshark", 6_100_000, "Gymshark | fleece overalls"),
             item("a3", "2", "Fitness 19 Palos Verdes", 900, "Gymshark"), item("a4", "3", "Madepants", 63224, "corduroy overalls")]
    got = B.candidates_from_items(items, "Gymshark")
    assert got == [cand("1", "Gymshark", 6_100_000, 2), cand("2", "Fitness 19 Palos Verdes", 900, 1)]


def test_the_search_label_is_matched_whole_and_ignoring_case_not_as_a_fragment():
    items = [item("a1", "1", "Gymshark", 5, "gymshark women | other"), item("a2", "2", "Other", 5, "Gymshark Women")]
    assert B.candidates_from_items(items, "Gymshark") == []
    assert [c["page_id"] for c in B.candidates_from_items(items, "GYMSHARK WOMEN")] == ["1", "2"]


def test_ads_without_a_page_id_are_ignored_and_the_same_ad_is_counted_once():
    items = [item("a1", "", "X", 1, "Gymshark"), item("a2", None, "X", 1, "Gymshark"),
             item("a3", "1", "Gymshark", 10, "Gymshark"), item("a3", "1", "Gymshark", 10, "Gymshark")]
    assert B.candidates_from_items(items, "Gymshark") == [cand("1", "Gymshark", 10, 1)]


def test_a_missing_follower_count_counts_as_unknown_not_as_a_huge_page():
    got = B.candidates_from_items([item("a1", "1", "A", None, "x"), item("a2", "2", "B", 7, "x")], "x")
    assert [(c["page_id"], c["likes"]) for c in got] == [("2", 7), ("1", 0)]


def test_the_most_common_spelling_of_a_page_name_is_used():
    items = [item("a1", "1", "Gymshark", 5, "x"), item("a2", "1", "Gymshark", 5, "x"), item("a3", "1", "GYMSHARK LTD", 5, "x")]
    assert B.candidates_from_items(items, "x")[0]["name"] == "Gymshark"


# ---- deciding ------------------------------------------------------------------------------------

def test_the_only_page_named_exactly_this_is_resolved():
    r = B.resolve("Gymshark", [cand("1", "Gymshark", 6_000_000, 4), cand("2", "Fitness 19 Palos Verdes", 900)])
    assert (r["status"], r["page_id"], r["page_name"], r["page_likes"]) == ("resolved", "1", "Gymshark", 6_000_000)
    assert r["reason"] == "the only advertiser page named exactly this"


def test_the_match_ignores_case_filler_words_and_spacing():
    assert B.resolve("oodie", [cand("9", "The Oodie", 1_000_000)])["page_id"] == "9"
    assert B.resolve("Chic Me", [cand("5", "ChicMe", 100)])["status"] == "resolved"


def test_several_pages_with_the_name_resolve_only_when_one_is_clearly_the_main_page():
    r = B.resolve("Gymshark", [cand("1", "Gymshark", 6_000_000, 3), cand("2", "Gymshark", 1_200_000, 1)])
    assert (r["status"], r["page_id"]) == ("resolved", "1")           # exactly five times as many followers
    assert "largest of 2 pages with this name" in r["reason"] and "6.0M followers against 1.2M" in r["reason"]


def test_several_pages_with_the_name_and_no_clear_winner_are_not_guessed():
    r = B.resolve("Gymshark", [cand("1", "Gymshark", 6_000_000), cand("2", "Gymshark", 1_300_000)])     # 4.6 times: not clear
    assert (r["status"], r["page_id"], r["page_name"]) == ("ambiguous", "", "")
    assert [c["page_id"] for c in r["candidates"]] == ["1", "2"] and "none is clearly the main one" in r["reason"]


def test_a_tie_on_followers_is_ambiguous_not_won_by_whichever_came_first():
    r = B.resolve("Gymshark", [cand("2", "Gymshark", 500), cand("1", "Gymshark", 500)])
    assert r["status"] == "ambiguous"


def test_unknown_follower_counts_never_make_a_page_the_clear_winner():
    assert B.resolve("Gymshark", [cand("1", "Gymshark", 0), cand("2", "Gymshark", 0)])["status"] == "ambiguous"
    assert B.resolve("Gymshark", [cand("1", "Gymshark", 5), cand("2", "Gymshark", 0)])["status"] == "resolved"
    #                                     5 is five times max(0, 1): the rule never divides by zero


def test_only_a_related_name_is_listed_for_a_person_never_accepted():
    r = B.resolve("Gymshark", [cand("1", "Gymshark Women", 3_000_000), cand("2", "Fitness 19 Palos Verdes", 900)])
    assert (r["status"], r["page_id"]) == ("ambiguous", "")
    assert [c["page_id"] for c in r["candidates"]] == ["1"] and "no page is named exactly this" in r["reason"]


def test_an_exact_name_beats_related_names_even_when_the_related_page_is_bigger():
    r = B.resolve("Gymshark", [cand("1", "Gymshark Women", 9_000_000, 9), cand("2", "Gymshark", 100, 1)])
    assert (r["status"], r["page_id"]) == ("resolved", "2")


def test_a_look_alike_name_that_merely_contains_the_brand_is_not_accepted_as_exact():
    r = B.resolve("Oodie", [cand("1", "Oodie Outlet Deals", 50_000)])
    assert (r["status"], r["page_id"]) == ("ambiguous", "")


def test_unrelated_advertisers_mean_unresolved():
    r = B.resolve("Gymshark", [cand("1", "SHEIN", 39_000_000), cand("2", "Rhoback", 200_000)])
    assert (r["status"], r["page_id"], r["candidates"][0]["page_id"]) == ("unresolved", "", "1")
    assert r["reason"] == "other advertisers turned up, but none has a name like this one"


def test_no_ads_at_all_means_unresolved_with_nothing_to_show():
    r = B.resolve("Gymshark", [])
    assert r["status"] == "unresolved" and r["candidates"] == [] and "no ads" in r["reason"]


def test_a_name_that_normalises_to_nothing_never_matches_anything():
    assert B.resolve("!!!", [cand("1", "!!!", 10), cand("2", "", 5)])["status"] == "unresolved"


def test_a_very_short_page_name_is_not_treated_as_related():
    assert B.resolve("Gymshark", [cand("1", "Gy", 10)])["status"] == "unresolved"


def test_at_most_five_candidates_are_kept_most_followed_first():
    many = [cand(str(i), f"Other {i}", i * 10) for i in range(1, 9)]
    r = B.resolve("Gymshark", many)
    assert [c["page_id"] for c in r["candidates"]] == ["8", "7", "6", "5", "4"]


def test_resolve_does_not_change_what_it_was_given():
    given = [cand("1", "Gymshark", 5), cand("2", "Gymshark", 1)]
    before = [dict(c) for c in given]
    B.resolve("Gymshark", given)
    assert given == before


# ---- what is remembered --------------------------------------------------------------------------

def resolved(page_id="1", name="Gymshark"):
    return {"status": "resolved", "page_id": page_id, "page_name": name, "page_likes": 5, "reason": "r", "candidates": []}


def test_a_decision_is_filed_under_the_normalised_name_without_changing_the_old_state():
    old = B.empty_state()
    new = B.record(old, "The Oodie", "benchmark", resolved("9", "The Oodie"), NOW)
    assert old == B.empty_state() and list(new["items"]) == ["oodie"]
    e = new["items"]["oodie"]
    assert (e["name"], e["kind"], e["status"], e["page_id"], e["checked_at"], e["source"]) == ("The Oodie", "benchmark", "resolved", "9", NOW, "auto")


def test_a_newer_decision_replaces_the_older_one_for_the_same_brand_only():
    s = B.record(B.empty_state(), "Oodie", "benchmark", resolved("9"), NOW)
    s = B.record(s, "Gymshark", "benchmark", resolved("1"), NOW)
    s = B.record(s, "oodie", "benchmark", {**resolved("7"), "status": "ambiguous", "page_id": ""}, "2026-11-01T00:00:00Z")
    assert sorted(s["items"]) == ["gymshark", "oodie"] and s["items"]["oodie"]["status"] == "ambiguous"
    assert s["items"]["gymshark"]["page_id"] == "1"


@pytest.mark.parametrize("raw", [None, [], "x", 5, {}, {"items": None}, {"items": []}, {"items": "x"}])
def test_unreadable_saved_state_means_nothing_decided_yet(raw):
    assert B.clean_state(raw) == B.empty_state()


def test_damaged_entries_are_dropped_and_good_ones_kept():
    raw = {"items": {"ok": {"name": "Ok", "status": "resolved", "page_id": "123456"},
                     "no_id": {"name": "X", "status": "resolved", "page_id": ""},
                     "bad_id": {"name": "X", "status": "resolved", "page_id": "abc"},
                     "bad_status": {"name": "X", "status": "maybe"}, "not_a_dict": 5, "no_name": {"status": "unresolved"},
                     "amb": {"name": "A", "status": "ambiguous"}}}
    assert sorted(B.clean_state(raw)["items"]) == ["amb", "ok"]


# ---- planning ------------------------------------------------------------------------------------

def plan(names=(), manual=None, state=None, own="Swoveralls", now=NOW, max_pages=6, per_run=3, retry=30):
    return B.plan_follow(own, list(names), manual or {}, state or B.empty_state(), now, max_pages=max_pages,
                         discovery_per_run=per_run, retry_days=retry)


def filed(*decisions):
    s = B.empty_state()
    for name, kind, res, when in decisions:
        s = B.record(s, name, kind, res, when)
    return s


def test_with_nothing_decided_the_brand_and_suggested_names_are_looked_up_own_brand_first():
    p = plan(["Gymshark", "Oodie"])
    assert [(d["name"], d["kind"]) for d in p.discover] == [("Swoveralls", "own"), ("Gymshark", "benchmark"), ("Oodie", "benchmark")]
    assert p.pages == [] and p.review == [] and p.waiting == []


def test_only_the_per_run_limit_of_names_are_looked_up_the_rest_wait():
    p = plan(["A1", "B2", "C3", "D4"], per_run=3)
    assert [d["name"] for d in p.discover] == ["Swoveralls", "A1", "B2"] and [w["name"] for w in p.waiting] == ["C3", "D4"]


def test_a_resolved_brand_is_followed_through_its_page_and_not_looked_up_again():
    p = plan(["Gymshark"], state=filed(("Gymshark", "benchmark", resolved("1"), NOW)))
    assert [(x["name"], x["page_id"], x["kind"], x["source"]) for x in p.pages] == [("Gymshark", "1", "benchmark", "auto")]
    assert [d["name"] for d in p.discover] == ["Swoveralls"]


def test_the_own_brand_page_does_not_count_against_the_benchmark_limit():
    s = filed(("Swoveralls", "own", resolved("5", "Swoveralls"), NOW), ("A1", "benchmark", resolved("1"), NOW),
              ("B2", "benchmark", resolved("2"), NOW))
    p = plan(["A1", "B2"], state=s, max_pages=2)
    assert [x["name"] for x in p.pages] == ["Swoveralls", "A1", "B2"] and p.held == []


def test_resolved_brands_beyond_the_limit_are_held_not_dropped_silently():
    s = filed(*[(n, "benchmark", resolved(str(i + 1)), NOW) for i, n in enumerate(["A1", "B2", "C3"])])
    p = plan(["A1", "B2", "C3"], state=s, own="", max_pages=2)
    assert [x["name"] for x in p.pages] == ["A1", "B2"] and p.held == ["C3"]


def test_an_ambiguous_brand_is_not_followed_not_searched_again_and_is_listed_for_review():
    amb = {**resolved("1"), "status": "ambiguous", "page_id": "", "page_name": "", "reason": "two pages", "candidates": [cand("1", "Oodie", 5)]}
    p = plan(["Oodie"], state=filed(("Oodie", "benchmark", amb, NOW)), own="")
    assert p.pages == [] and p.discover == []
    assert [(r["name"], r["status"], r["reason"]) for r in p.review] == [("Oodie", "ambiguous", "two pages")]
    assert p.review[0]["candidates"] == [cand("1", "Oodie", 5)]


def test_an_ambiguous_brand_stays_ambiguous_however_old_the_decision_is():
    amb = {**resolved("1"), "status": "ambiguous", "page_id": "", "reason": "two pages", "candidates": []}
    p = plan(["Oodie"], state=filed(("Oodie", "benchmark", amb, "2020-01-01T00:00:00Z")), own="")
    assert p.discover == [] and [r["status"] for r in p.review] == ["ambiguous"]


def test_a_brand_not_found_waits_the_retry_period_then_is_looked_up_again():
    nf = {"status": "unresolved", "page_id": "", "page_name": "", "page_likes": 0, "reason": "none", "candidates": []}
    s = filed(("Oodie", "benchmark", nf, "2026-10-01T09:00:00Z"))                 # 20 days before NOW
    soon = plan(["Oodie"], state=s, own="")
    assert soon.discover == [] and [r["status"] for r in soon.review] == ["unresolved"] and "tried again after 30 days" in soon.review[0]["retry_after"]
    later = plan(["Oodie"], state=s, own="", now="2026-11-05T09:00:00Z")          # 35 days
    assert [d["name"] for d in later.discover] == ["Oodie"] and later.review == []


def test_a_manual_page_is_always_followed_even_if_nobody_suggested_the_brand():
    p = plan([], manual={"Madepants": "108271304233408"})
    assert [(x["name"], x["page_id"], x["kind"], x["source"]) for x in p.pages] == [("Madepants", "108271304233408", "manual", "manual")]


def test_a_manual_choice_beats_any_automatic_decision_and_stops_the_look_up():
    s = filed(("Oodie", "benchmark", {**resolved("111111", "Wrong Oodie")}, NOW))
    p = plan(["Oodie", "oodie"], manual={"The Oodie": "222222"}, state=s, own="")
    assert [(x["page_id"], x["source"]) for x in p.pages] == [("222222", "manual")] and p.discover == [] and p.review == []


def test_skip_stops_a_brand_being_followed_or_looked_up_and_says_so():
    s = filed(("Oodie", "benchmark", resolved("111111"), NOW))
    p = plan(["Oodie", "Gymshark"], manual={"oodie": "skip"}, state=s, own="")
    assert p.pages == [] and p.skipped == ["Oodie"] and [d["name"] for d in p.discover] == ["Gymshark"]


def test_a_manual_page_for_the_own_brand_replaces_its_automatic_look_up():
    p = plan([], manual={"Swoveralls": "5555555"})
    assert [x["page_id"] for x in p.pages] == ["5555555"] and p.discover == []


def test_the_same_brand_suggested_twice_or_as_the_own_brand_is_handled_once():
    p = plan(["Gymshark", "GYMSHARK", "swoveralls", "The Gymshark"])
    assert [d["name"] for d in p.discover] == ["Swoveralls", "Gymshark"]


def test_no_own_brand_means_no_own_look_up():
    assert [d["name"] for d in plan(["Gymshark"], own="").discover] == ["Gymshark"]
    assert plan([], own="").discover == []


def test_blank_and_filler_only_names_are_ignored():
    assert [d["name"] for d in plan(["", "  ", "The Official Co", "Real Brand"], own="").discover] == ["Real Brand"]


def test_a_per_run_limit_of_zero_looks_nothing_up_and_everything_waits():
    p = plan(["Gymshark"], per_run=0)
    assert p.discover == [] and [w["name"] for w in p.waiting] == ["Swoveralls", "Gymshark"]


# ---- showing it ----------------------------------------------------------------------------------

def test_the_review_list_says_what_to_do_for_an_ambiguous_brand_and_names_the_candidates():
    amb = {**resolved("1"), "status": "ambiguous", "page_id": "", "reason": "2 pages have this name and none is clearly the main one",
           "candidates": [cand("111", "Oodie", 1_200_000, 4), cand("222", "Oodie", 1_000_000, 2)]}
    text = B.format_follow_plan(plan(["Oodie"], state=filed(("Oodie", "benchmark", amb, NOW)), own=""), "swoveralls", "Swoveralls")
    assert "NEEDS YOU   Oodie: 2 pages have this name and none is clearly the main one" in text
    assert '"Oodie" (id 111, 1.2M followers, 4 ads seen)' in text and '"Oodie" (id 222, 1.0M followers, 2 ads seen)' in text
    assert "brands/swoveralls/research.yaml" in text and "benchmark_pages: { Oodie: PAGE_ID }" in text and "Oodie: skip" in text


def test_the_list_shows_followed_pages_with_how_they_were_found():
    s = filed(("Swoveralls", "own", resolved("5", "Swoveralls"), NOW), ("Gymshark", "benchmark", resolved("1"), NOW))
    text = B.format_follow_plan(plan(["Gymshark"], manual={"Madepants": "108271304233408"}, state=s), "swoveralls", "Swoveralls")
    assert 'following   Swoveralls (your brand): page "Swoveralls", id 5' in text
    assert 'following   Gymshark: page "Gymshark", id 1' in text and "(found automatically)" in text
    assert "following   Madepants: id 108271304233408 (you chose)" in text


def test_the_list_reports_looking_up_waiting_held_skipped_and_not_found():
    nf = {"status": "unresolved", "page_id": "", "page_name": "", "page_likes": 0, "reason": "other advertisers turned up", "candidates": []}
    s = filed(("Gone", "benchmark", nf, NOW), ("A1", "benchmark", resolved("1"), NOW), ("B2", "benchmark", resolved("2"), NOW))
    text = B.format_follow_plan(plan(["Gone", "A1", "B2", "New1", "New2", "Zed"], manual={"Zed": "skip"}, state=s, own="", max_pages=1, per_run=1),
                                "s", "S")
    for needle in ("to look up  New1", "queued      New2", "held        B2", "skipped     Zed", "not found   Gone"):
        assert needle in text, needle


def test_an_empty_plan_says_so_instead_of_printing_nothing():
    assert "none yet" in B.format_follow_plan(plan([], own=""), "s", "S")


@pytest.mark.parametrize("n,expected", [(0, "0"), (999, "999"), (12_345, "12K"), (1_250_000, "1.2M"), (39_244_835, "39.2M"), (None, "0")])
def test_follower_counts_are_short_and_readable(n, expected):
    assert B._followers(n) == expected


def test_sheet_rows_give_one_row_per_brand_with_the_candidates_spelled_out():
    amb = {**resolved("1"), "status": "ambiguous", "page_id": "", "page_name": "", "page_likes": 0, "reason": "two",
           "candidates": [cand("111", "Oodie", 1_200_000, 4)]}
    rows = B.sheet_rows(filed(("Oodie", "benchmark", amb, NOW), ("Gymshark", "benchmark", resolved("1"), NOW)))
    assert sorted(rows) == ["gymshark", "oodie"]
    assert rows["gymshark"] == {"name": "Gymshark", "kind": "benchmark", "status": "resolved", "page_id": "1", "page_name": "Gymshark",
                                "page_followers": 5, "reason": "r", "candidates": "", "checked_at": NOW, "source": "auto"}
    assert rows["oodie"]["status"] == "ambiguous" and rows["oodie"]["candidates"] == '"Oodie" (id 111, 1.2M followers, 4 ads seen)'


def test_a_skip_for_a_brand_nobody_suggested_changes_nothing():
    p = plan(["Gymshark"], manual={"Zed": "skip"}, own="")
    assert p.skipped == [] and [d["name"] for d in p.discover] == ["Gymshark"]


def test_a_pages_follower_count_is_the_largest_seen_not_the_last_one_seen():
    items = [item("a1", "1", "Gymshark", 6_100_000, "x"), item("a2", "1", "Gymshark", 6_000_000, "x"), item("a3", "1", "Gymshark", None, "x")]
    assert B.candidates_from_items(items, "x") == [cand("1", "Gymshark", 6_100_000, 3)]
