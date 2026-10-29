"""Fixes from the first creative-team review (2026-10-28) and the run logs.

* The Google Doc archive failed with "The tab title cannot be longer than 50 characters" (the limit in the code was 60).
* A run that took in 98 items could score only 96 (the limit allowed 12 scoring calls; ads and web results together need 15).
* Several strong earlier ideas vanished after re-scoring, among them a Madepants ad that had run 437 days. A long-running ad is now kept
  as proven, however it scores (unless it is generic or an unrelated category).
* The plan now says how far away its occasion is (a Father's Day plan made in late October should say it is eight months off)."""
import re
from dataclasses import replace

import pytest

from contentintel import docs_archive as D
from contentintel import ideabank as IB
from contentintel import planner as PL
from contentintel.drive import DriveClient
from contentintel.store import Store

from fake_browser import FakeClaude
from fake_drive import EMAIL, ROOT, FakeDrive
from test_collectors import NOW
from test_ideabank import S, bank_with, full_run, good
from test_planner import make, raw_plan


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


# ---- Google Docs tab titles --------------------------------------------------------------------------------------------------------------

def test_google_allows_a_tab_name_of_50_characters_and_no_more():
    assert D.TITLE_LIMIT == 50


def test_the_title_from_the_failed_run_is_now_short_enough_and_says_what_it_is():
    title = D.tab_title("2026-10-28", "swoveralls", "How do gift buyers decide what to buy for Father's Day and why does it matter?")
    assert len(title) == 50 and title == "2026-10-28 swoveralls - How do gift buyers decide…"


@pytest.mark.parametrize("brand", ["a", "swoveralls", "a-very-long-brand-name-indeed-yes-it-is", "x" * 80])
@pytest.mark.parametrize("question", ["", None, "short", "word " * 40, "é" * 90, "**Task:** " + "long question " * 12])
def test_a_tab_title_is_never_longer_than_google_allows(brand, question):
    assert len(D.tab_title("2026-10-28", brand, question)) <= 50


def test_a_title_that_fits_is_left_exactly_as_it_is():
    assert D.tab_title("2026-10-28", "swoveralls", "How are overalls marketed?") == "2026-10-28 swoveralls - How are overalls marketed?"
    assert len(D.tab_title("2026-10-28", "swoveralls", "q" * 26)) == 50 and not D.tab_title("2026-10-28", "swoveralls", "q" * 26).endswith("…")
    assert D.tab_title("2026-10-28", "swoveralls", "q" * 27).endswith("…")


def test_a_cut_title_never_leaves_a_space_before_the_dots():
    title = D.tab_title("2026-10-28", "swoveralls", "word " * 6 + "more")                     # the cut falls right after a space
    assert title == "2026-10-28 swoveralls - word word word word…" or (title.endswith("word…") and " …" not in title and len(title) <= 50)
    assert not re.search(r"\s…$", title)


# ---- scoring everything a run takes in ----------------------------------------------------------------------------------------------------

def tools_of(*names):
    return {"tools": {n: ({"queries": ["q"]} if n == "web_search" else {"questions": ["q"]} if n == "deep_research" else {"keywords": ["k"], "hashtags": ["h"]}) for n in names}}


def test_the_estimate_counts_ads_and_web_results_together():
    both = PL.estimate(tools_of("meta_ads", "web_search"), S)
    assert both["new_items_at_most"] == 120 and both["claude_calls_at_most"] == 1 + 15
    assert PL.estimate(tools_of("meta_ads", "tiktok", "instagram"), S)["new_items_at_most"] == 60
    assert PL.estimate(tools_of("web_search"), S)["new_items_at_most"] == 60
    assert PL.estimate(tools_of("deep_research"), S)["new_items_at_most"] == 0


def test_the_estimate_never_promises_more_scoring_calls_than_the_limit_allows():
    assert PL.estimate(tools_of("meta_ads", "web_search"), replace(S, max_scoring_calls=5))["claude_calls_at_most"] == 1 + 5
    assert PL.estimate(tools_of("meta_ads", "web_search"), replace(S, score_batch=40))["claude_calls_at_most"] == 1 + 3


def test_the_default_limit_scores_a_full_run_of_ads_and_web_results(store, swov):
    assert S.max_scoring_calls >= 2 * -(-S.max_new_per_run // S.score_batch)
    ids = [f"N{n:03d}" for n in range(98)]                             # the size of the run that left 6 unscored
    store.write_ideabank("swoveralls", bank_with(*ids))
    claude = FakeClaude()
    claude.json_answers = [{"items": [good(f"IB-{n:04d}", 80, topic=f"t{n}") for n in range(start, min(start + 8, 99))]} for start in range(1, 99, 8)]
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    new, lines = full_run(store, swov, (drive, drive.session), claude, settings=replace(S, max_new_per_run=120))
    assert len(claude.json_calls) == 13 and not any("left unscored" in line or "wait to be scored" in line for line in lines)


# ---- proven winners ---------------------------------------------------------------------------------------------------------------------------

def item(days=437, **kw):
    base = {"metrics": {"days_running": days}, "generic": False, "category": "adjacent", "state": "removed", "score": 44}
    base.update(kw)
    return base


@pytest.mark.parametrize("kw,expected", [({}, True), ({"days": 150}, True), ({"days": 149}, False), ({"days": 0}, False), ({"generic": True}, False),
                                         ({"category": "other"}, False), ({"score": None}, False), ({"state": "new"}, True), ({"category": "same"}, True), ({"category": ""}, True)])
def test_what_counts_as_a_proven_ad(kw, expected):
    assert IB.is_proven(item(**kw)) is expected


def test_an_item_with_no_running_time_is_not_proven():
    assert IB.is_proven({"metrics": {}, "generic": False, "category": "same", "state": "kept"}) is False


def test_a_proven_ad_is_kept_however_low_it_scored_and_an_ordinary_one_is_not():
    bank = bank_with("A", "B", "C")
    for it, days in zip(bank["items"].values(), (437, 40, 437)):
        it.update({"score": 30, "scoring_version": IB.SCORING_VERSION, "state": "kept", "category": "adjacent", "metrics": {"days_running": days}})
    bank["items"]["IB-0003"]["generic"] = True
    IB.apply_relevance(bank, S)
    assert [it["state"] for it in bank["items"].values()] == ["kept", "removed", "removed"]            # a generic long-runner is still not kept


def test_a_proven_ad_that_was_removed_for_a_low_score_comes_back():
    bank = bank_with("A", "B")
    for it, days in zip(bank["items"].values(), (437, 20)):
        it.update({"score": 41, "scoring_version": IB.SCORING_VERSION, "state": "removed", "category": "adjacent", "metrics": {"days_running": days},
                   "removed_reason": "not relevant enough (scored 41, the minimum is 50)"})
    assert IB.revive_relevant(bank, S) == 1
    assert bank["items"]["IB-0001"]["state"] == "kept" and bank["items"]["IB-0002"]["state"] == "removed"


def test_a_proven_ad_is_never_brought_back_if_it_was_removed_as_a_copy_or_as_the_brands_own():
    bank = bank_with("A")
    bank["items"]["IB-0001"].update({"state": "removed", "removed_reason": "a copy of IB-0009", "category": "same", "metrics": {"days_running": 437}, "score": 80})
    assert IB.revive_relevant(bank, S) == 0


def test_a_run_gives_a_proven_ad_a_second_life_and_the_next_run_does_not_remove_it_again(store, swov):
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    bank = bank_with("A")
    bank["items"]["IB-0001"].update({"score": 44, "scoring_version": IB.SCORING_VERSION, "state": "removed", "category": "adjacent",
                                     "metrics": {"days_running": 437}, "removed_reason": "not relevant enough (scored 44, the minimum is 50)"})
    store.write_ideabank("swoveralls", bank)
    full_run(store, swov, (drive, drive.session))
    assert store.read_ideabank("swoveralls")["items"]["IB-0001"]["state"] == "kept"
    full_run(store, swov, (drive, drive.session))
    assert store.read_ideabank("swoveralls")["items"]["IB-0001"]["state"] == "kept"


def test_a_newly_scored_long_running_ad_is_kept_in_the_same_run(store, swov):
    drive = DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)
    bank = bank_with("A")
    bank["items"]["IB-0001"]["metrics"] = {"days_running": 300}
    store.write_ideabank("swoveralls", bank)
    claude = FakeClaude()
    claude.json_answers = [{"items": [{"id": "IB-0001", "fit": 55, "idea": 30, "generic": False, "category": "adjacent", "mechanism": "m", "concept": "c",
                                       "topic": "t", "summary": "s", "how_to_use": "h", "risk": "none"}]}]
    full_run(store, swov, (drive, drive.session), claude)
    got = store.read_ideabank("swoveralls")["items"]["IB-0001"]
    assert got["score"] == 40 and got["state"] == "kept"                  # the score is honest; the long run is why it stays


# ---- a plan that says when its occasion is ----------------------------------------------------------------------------------------------

def test_the_plan_is_given_todays_date_and_told_to_say_when_the_occasion_is_far_off_or_past(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan()]
    make(store, swov, claude, now="2026-10-28T09:07:13Z")
    asked, system = claude.json_calls[0][1], claude.json_calls[0][0]
    assert "Today: 2026-10-28" in asked
    assert "Today's date is given: if the occasion is more than 90 days away or has already passed, say so first in 'notes'" in system and "nearer occasions" in system


def test_a_changed_plan_is_given_the_date_too(store, swov):
    claude = FakeClaude()
    claude.json_answers = [raw_plan(), raw_plan()]
    req, budget = make(store, swov, claude)
    PL.revise_request(store, swov, req, claude, budget, S, changes="drop TikTok", now=NOW, context="x")
    assert f"Today: {NOW[:10]}" in claude.json_calls[1][1] or "Today: " in claude.json_calls[1][1]
