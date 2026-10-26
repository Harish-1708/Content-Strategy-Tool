"""A whole Collect run with the default settings: the brand's own name, benchmark brands found by page, and every
way that can go wrong or be overridden by a person. The Ad Library is pretended with ads shaped like the real ones
(page id, page name and follower count on each ad).
"""
import json
from dataclasses import replace

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import research as R

from fake_browser import FakeBrowser
from test_autoseeds import SEEDS, claude_with, seeded
from test_collectors import CREDS, NOW, site, world, write_research
from test_meta_real_page import RATE_LIMIT

STATUS, COUNTRY = "active", "all"
P_SWOV, P_RESELLER, P_GYM, P_FITNESS = "100000000000500", "100000000000600", "100000000000001", "100000000000002"
P_OODIE_A, P_OODIE_B, P_RANDOM, P_MADE, P_OTHER = "100000000000011", "100000000000012", "100000000000900", "100000000000777", "100000000000999"
P_SHEIN = "100000000000009"


def url_of(name):
    return C.ad_library_url(name, COUNTRY, STATUS)


def page_url(page_id):
    return C.ad_library_page_url(page_id, COUNTRY, STATUS)


def ad(ad_id, page_id, page_name, likes=1000, text=None):
    return {"ad_archive_id": ad_id, "page_id": page_id, "page_name": page_name, "start_date": 1760000000, "end_date": 1760864000,
            "is_active": True, "snapshot": {"body": {"text": text or f"ad {ad_id} from {page_name}"}, "page_like_count": likes,
                                            "link_url": "https://example.com/"}}


def feed(*ads):
    return {"ads": list(ads)}


SWOV_HOME = feed(ad("s1", P_SWOV, "Swoveralls", 70000), ad("s2", P_SWOV, "Swoveralls", 70000), ad("r1", P_RESELLER, "Some Reseller", 30))
GYM_FEED = feed(ad("g1", P_GYM, "Gymshark", 6_000_000), ad("g2", P_GYM, "Gymshark", 6_000_000), ad("f1", P_FITNESS, "Fitness 19 Palos Verdes", 900))
OODIE_FEED = feed(ad("o1", P_OODIE_A, "Oodie", 1_200_000), ad("o2", P_OODIE_B, "Oodie", 1_000_000))
GYM_PAGE = feed(ad("g1", P_GYM, "Gymshark", 6_000_000), ad("g3", P_GYM, "Gymshark", 6_000_000), ad("g4", P_GYM, "Gymshark", 6_000_000))
SWOV_PAGE = feed(ad("s1", P_SWOV, "Swoveralls", 70000), ad("s3", P_SWOV, "Swoveralls", 70000))
OODIE_PAGE_11 = feed(ad("o1", P_OODIE_A, "Oodie", 1_200_000), ad("o3", P_OODIE_A, "Oodie", 1_200_000))
GENERIC = feed(ad("k1", P_RANDOM, "Random Brand", 50))


def meta_world(cfg, pages=None):
    spec = {C.AD_LIBRARY: {"json": [GENERIC]},
            url_of("Swoveralls"): {"json": [SWOV_HOME]}, url_of("Gymshark"): {"json": [GYM_FEED]},
            url_of("Oodie"): {"json": [OODIE_FEED]}, page_url(P_SWOV): {"json": [SWOV_PAGE]},
            page_url(P_GYM): {"json": [GYM_PAGE]}, page_url(P_OODIE_A): {"json": [OODIE_PAGE_11]}}
    spec.update(pages or {})
    f = world(cfg)
    f.browsers["meta_ads"] = FakeBrowser(spec, clickable=set(site(cfg, "meta_ads")["consent_any"]))
    return f


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def settings():
    return C.load_collector_settings("does-not-exist.yaml")


def run(store, tmp_path, swov, cfg, settings, factory, claude=None, data=None, dry_run=False, now=NOW, run_id="run1",
        sources=("meta_ads",)):
    write_research(tmp_path, swov, data or {})
    return C.run_collect([swov], store, browser_factory=factory, creds=CREDS, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                         sources=list(sources), now=now, run_id=run_id, root=str(tmp_path / "brands"), claude=claude, dry_run=dry_run)


def tasks(result):
    return [r.seeds for r in result.brands["swoveralls"]]


def state(store):
    return store.read_benchmarks("swoveralls")["items"]


# ---- the first run -------------------------------------------------------------------------------

def test_the_first_run_looks_up_the_brand_and_its_benchmarks_and_follows_what_it_found_in_the_same_run(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), None
    f = meta_world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS))
    assert tasks(result) == ["7 keywords", "look up the advertiser page of 3 brand(s): Swoveralls, Oodie, Gymshark",
                             "2 followed advertiser pages"]
    assert [r.status for r in result.brands["swoveralls"]] == ["ran", "ran", "ran"]
    opened = f.browsers["meta_ads"].opened
    assert url_of("Swoveralls") in opened and url_of("Gymshark") in opened and url_of("Oodie") in opened      # looked up by name
    assert page_url(P_SWOV) in opened and page_url(P_GYM) in opened                                            # then followed by page
    assert page_url(P_OODIE_A) not in opened and page_url(P_OODIE_B) not in opened                                    # never guessed
    assert opened.index(url_of("Gymshark")) < opened.index(page_url(P_GYM))


def test_what_was_decided_is_saved_exactly(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude_with(SEEDS))
    s = state(store)
    assert sorted(s) == ["gymshark", "oodie", "swoveralls"]
    assert (s["gymshark"]["status"], s["gymshark"]["page_id"], s["gymshark"]["page_name"], s["gymshark"]["page_likes"]) == (
        "resolved", P_GYM, "Gymshark", 6_000_000)
    assert (s["swoveralls"]["status"], s["swoveralls"]["page_id"], s["swoveralls"]["kind"]) == ("resolved", P_SWOV, "own")
    assert s["oodie"]["status"] == "ambiguous" and s["oodie"]["page_id"] == "" and s["oodie"]["page_name"] == ""
    assert sorted(c["page_id"] for c in s["oodie"]["candidates"]) == [P_OODIE_A, P_OODIE_B]
    assert all(e["checked_at"] == NOW and e["source"] == "auto" for e in s.values())


def test_an_ambiguous_brand_is_reported_for_a_decision_and_never_followed(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    result = run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude_with(SEEDS))
    text = C.format_collect_report(result, settings, R.Ledger(store.load_usage(), "2026-10"))
    assert "NEEDS YOU   Oodie: 2 pages have this name and none is clearly the main one" in text
    assert "ACTION: 1 benchmark brand(s) are ambiguous" in text and result.needs_review == 1
    assert f"following   Gymshark: page \"Gymshark\", id {P_GYM}" in text and "following   Swoveralls (your brand)" in text
    assert "benchmark_pages: { Oodie: PAGE_ID }" in text


def test_the_page_id_and_where_each_ad_was_found_are_saved_with_the_ads(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude_with(SEEDS))
    rows = C.collected_rows(store.read_collected("swoveralls"))
    gym = rows["meta_ads:g3"]
    assert gym["handle"] == "Gymshark" and f"page: {P_GYM} |" in gym["details"] and gym["queries"] == "Gymshark (page)"
    assert "Gymshark" in rows["meta_ads:g1"]["queries"] and "Gymshark (page)" in rows["meta_ads:g1"]["queries"]   # seen both ways
    assert rows["meta_ads:g1"]["times_seen"] == 2
    assert f"page: {P_SWOV} |" in rows["meta_ads:s3"]["details"] and rows["meta_ads:s3"]["queries"] == "Swoveralls (page)"


def test_the_own_brand_is_searched_first_whatever_claude_suggests(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    run(store, tmp_path, swov, cfg, settings, f, claude_with({**SEEDS, "keywords": {"direct": ["jogger overalls"], "adjacent": [], "cross_industry": []}}),
        data={"ads": {"keywords": ["sweatpant overalls"]}})
    keyword_searches = [u for u in f.browsers["meta_ads"].opened if "search_type=keyword_unordered" in u and "q=Swoveralls" in u]
    assert keyword_searches, "the own-brand keyword search was not made"
    first_keyword_task = [u for u in f.browsers["meta_ads"].opened if u.startswith(C.AD_LIBRARY)][:3]
    assert first_keyword_task == [url_of("Swoveralls"), url_of("sweatpant overalls"), url_of("jogger overalls")]


# ---- later runs ----------------------------------------------------------------------------------

def test_the_second_run_follows_the_saved_pages_and_looks_nothing_up_again(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude)
    f2 = meta_world(cfg)
    again = run(store, tmp_path / "again", swov, cfg, settings, f2, claude, now="2026-10-28T07:00:00Z", run_id="run2")
    assert tasks(again) == ["2 followed advertiser pages", "7 keywords"]
    opened = f2.browsers["meta_ads"].opened
    assert not any(u == url_of(n) for u in opened for n in ("Gymshark", "Oodie")) and page_url(P_GYM) in opened
    assert len(claude.complete_calls) == 1                                  # no second Claude call
    assert state(store)["gymshark"]["checked_at"] == NOW                    # the old decision was kept, not redone


def test_the_review_list_still_shows_the_ambiguous_brand_on_the_next_run(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude)
    again = run(store, tmp_path / "again", swov, cfg, settings, meta_world(cfg), claude, now="2026-10-28T07:00:00Z", run_id="run2")
    assert again.needs_review == 1 and "NEEDS YOU   Oodie" in C.format_collect_report(again, settings)


def test_an_unchanged_page_is_not_collected_again_inside_the_cache_window(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude)
    again = run(store, tmp_path / "again", swov, cfg, settings, meta_world(cfg), claude, now="2026-10-21T07:00:00Z", run_id="run2")
    assert [r.status for r in again.brands["swoveralls"]] == ["reused", "reused"]


# ---- the person's decisions ----------------------------------------------------------------------

def test_a_page_chosen_by_hand_is_followed_from_the_start_and_that_brand_is_never_looked_up(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS), data={"ads": {"benchmark_pages": {"Oodie": int(P_OODIE_A)}}})
    opened = f.browsers["meta_ads"].opened
    assert url_of("Oodie") not in opened and page_url(P_OODIE_A) in opened
    assert "oodie" not in state(store) and result.needs_review == 0
    assert f"following   Oodie: id {P_OODIE_A} (you chose)" in C.format_collect_report(result, settings)


def test_a_manual_choice_is_never_overwritten_by_an_automatic_one(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude)                    # Oodie is ambiguous now
    f2 = meta_world(cfg)
    run(store, tmp_path / "again", swov, cfg, settings, f2, claude, data={"ads": {"benchmark_pages": {"The Oodie": "https://www.facebook.com/ads/library/?view_all_page_id=" + P_OODIE_A}}},
        now="2026-10-28T07:00:00Z", run_id="run2")
    assert page_url(P_OODIE_A) in f2.browsers["meta_ads"].opened
    assert state(store)["oodie"]["status"] == "ambiguous"                              # the saved evidence is untouched


def test_skip_means_the_brand_is_neither_looked_up_nor_followed(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS), data={"ads": {"benchmark_pages": {"Gymshark": "skip", "oodie": "skip"}}})
    opened = f.browsers["meta_ads"].opened
    assert url_of("Gymshark") not in opened and url_of("Oodie") not in opened and page_url(P_GYM) not in opened
    assert sorted(state(store)) == ["swoveralls"] and result.needs_review == 0
    text = C.format_collect_report(result, settings)
    assert "skipped     Gymshark" in text and "skipped     Oodie" in text


def test_a_page_written_in_research_yaml_is_followed_alongside_the_automatic_ones(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    link = "https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=US&view_all_page_id=" + P_MADE
    f.browsers["meta_ads"].pages[link] = {"json": [feed(ad("m1", P_MADE, "Madepants", 63224))]}
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS), data={"ads": {"pages": [link]}})
    assert tasks(result)[0] == "1 followed advertiser pages" and link in f.browsers["meta_ads"].opened


def test_a_bad_manual_choice_stops_the_run_with_a_message_that_names_the_file(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    with pytest.raises(R.ResearchError, match="swoveralls/research.yaml ads.benchmark_pages.*must be a Meta page id"):
        run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude_with(SEEDS), data={"ads": {"benchmark_pages": {"Oodie": "soon"}}})
    assert store.read_collected("swoveralls") == []


# ---- the brand's own tracking --------------------------------------------------------------------

def test_not_tracking_the_brand_means_no_own_search_and_no_own_page_look_up(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS), data={"track_own_brand": False})
    assert "Swoveralls" not in " ".join(tasks(result)) and url_of("Swoveralls") not in f.browsers["meta_ads"].opened
    assert "swoveralls" not in state(store) and tasks(result)[0] == "6 keywords"


def test_the_own_brand_is_still_tracked_when_the_automatic_seeds_are_switched_off(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, None, data={"auto_seeds": False})
    assert tasks(result)[0] == "1 keywords" and url_of("Swoveralls") in f.browsers["meta_ads"].opened
    assert url_of("Gymshark") not in f.browsers["meta_ads"].opened


# ---- caps ----------------------------------------------------------------------------------------

def test_followed_pages_and_look_ups_get_their_own_item_budgets(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    big_gym = feed(*[ad(f"gp{i}", P_GYM, "Gymshark", 6_000_000) for i in range(20)])
    big_swov = feed(*[ad(f"sp{i}", P_SWOV, "Swoveralls", 70000) for i in range(20)])
    f = meta_world(cfg, pages={page_url(P_GYM): {"json": [big_gym]}, page_url(P_SWOV): {"json": [big_swov]}})
    result = run(store, tmp_path, swov, cfg, replace(settings, benchmark_items_per_page=3), f, claude_with(SEEDS))
    saved = {r["source"] + r["seeds"][:6]: len(r["items"]) for r in store.read_collected("swoveralls")}
    followed = [r for r in store.read_collected("swoveralls") if "followed" in r["seeds"]][0]
    assert len(followed["items"]) == 6                                       # 3 per page, 2 pages
    look_up = [r for r in store.read_collected("swoveralls") if r["seeds"].startswith("look up")][0]
    assert len(look_up["items"]) <= 10 * 3 and result.brands["swoveralls"][2].items == 6
    assert saved


def test_the_monthly_item_cap_still_applies_across_all_the_new_tasks(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    result = run(store, tmp_path, swov, cfg, replace(settings, monthly_items=5), meta_world(cfg), claude_with(SEEDS))
    used = store.load_usage()["months"]["2026-10"]["collected_items"]
    assert used <= 5 and any("out of items under your monthly cap" in r.detail for r in result.brands["swoveralls"])


def test_only_max_pages_benchmark_pages_are_followed_and_the_rest_are_held(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude)                    # Gymshark resolved, Swoveralls resolved
    result = run(store, tmp_path / "again", swov, cfg, replace(settings, benchmark_max_pages=0), meta_world(cfg), claude,
                 now="2026-10-28T07:00:00Z", run_id="run2")
    text = C.format_collect_report(result, settings)
    assert "held        Gymshark" in text and "following   Swoveralls (your brand)" in text


def test_look_ups_per_run_are_limited_and_the_rest_wait_for_the_next_run(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    one = replace(settings, benchmark_discovery_per_run=1)
    first = run(store, tmp_path, swov, cfg, one, meta_world(cfg), claude)
    assert sorted(state(store)) == ["swoveralls"] and "to look up  Oodie" in C.format_collect_report(first, settings)
    assert "queued      Gymshark" in C.format_collect_report(first, settings)
    run(store, tmp_path / "again", swov, cfg, one, meta_world(cfg), claude, now="2026-10-28T07:00:00Z", run_id="run2")
    assert sorted(state(store)) == ["oodie", "swoveralls"]                    # one more name per run, in the order Claude gave
    run(store, tmp_path / "third", swov, cfg, one, meta_world(cfg), claude, now="2026-11-04T07:00:00Z", run_id="run3")
    assert sorted(state(store)) == ["gymshark", "oodie", "swoveralls"]


# ---- when the site does not cooperate ------------------------------------------------------------

def test_a_rate_limited_search_leaves_that_brand_undecided_instead_of_calling_it_not_found(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    f = meta_world(cfg, pages={url_of("Gymshark"): {"json": [RATE_LIMIT]}})
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS))
    assert "gymshark" not in state(store) and sorted(state(store)) == ["oodie", "swoveralls"]
    assert any("Gymshark: the site rate-limited the search; it will be looked up again" in n for n in result.notes)
    again = run(store, tmp_path / "again", swov, cfg, settings, meta_world(cfg), claude_with(SEEDS), now="2026-10-28T07:00:00Z", run_id="run2")
    assert state(store)["gymshark"]["status"] == "resolved" and again.needs_review == 1


def test_a_search_that_really_finds_nothing_is_remembered_as_not_found_and_retried_later(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    f = meta_world(cfg, pages={url_of("Gymshark"): {"json": [feed(ad("x1", P_SHEIN, "SHEIN", 39_000_000))]}})
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS))
    assert state(store)["gymshark"]["status"] == "unresolved" and state(store)["gymshark"]["page_id"] == ""
    text = C.format_collect_report(result, settings)
    assert "not found   Gymshark: other advertisers turned up, but none has a name like this one" in text and result.needs_review == 1
    soon = run(store, tmp_path / "a", swov, cfg, settings, meta_world(cfg), claude_with(SEEDS), now="2026-11-02T07:00:00Z", run_id="run2")
    assert state(store)["gymshark"]["status"] == "unresolved"                                              # not retried yet
    run(store, tmp_path / "b", swov, cfg, settings, meta_world(cfg), claude_with(SEEDS), now="2026-11-25T07:00:00Z", run_id="run3")
    assert state(store)["gymshark"]["status"] == "resolved" and soon is not None                           # retried after 30 days


def test_a_followed_page_that_returns_nothing_from_that_page_is_flagged(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    f = meta_world(cfg, pages={page_url(P_GYM): {"json": [feed(ad("z1", P_OTHER, "Somebody Else", 5))]}})
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS))
    assert any(f"Gymshark (page {P_GYM}) returned no ads from that page; check the id" in n for n in result.notes)


def test_a_damaged_benchmark_file_is_treated_as_nothing_decided_and_never_crashes_the_run(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    path = store._research_file("swoveralls", "benchmarks.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not json", encoding="utf-8")
    result = run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude_with(SEEDS))
    assert [r.status for r in result.brands["swoveralls"]] == ["ran", "ran", "ran"] and state(store)["gymshark"]["status"] == "resolved"


# ---- dry run and switches ------------------------------------------------------------------------

def test_a_dry_run_plans_the_look_ups_and_writes_nothing(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    claude = claude_with(SEEDS)
    first = run(store, tmp_path, swov, cfg, settings, f, claude, dry_run=True)
    assert f.browsers["meta_ads"].opened == [] and store.read_collected("swoveralls") == [] and store.read_benchmarks("swoveralls") is None
    assert [r.status for r in first.brands["swoveralls"]] == ["would_run", "would_run"]
    assert claude.complete_calls == []


def test_a_dry_run_after_a_real_one_shows_the_saved_decisions_unchanged(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    run(store, tmp_path, swov, cfg, settings, meta_world(cfg), claude)
    before = json.dumps(store.read_benchmarks("swoveralls"), sort_keys=True)
    plan = run(store, tmp_path / "again", swov, cfg, settings, meta_world(cfg), claude, dry_run=True, now="2026-10-28T07:00:00Z")
    assert json.dumps(store.read_benchmarks("swoveralls"), sort_keys=True) == before and plan.needs_review == 1


def test_switching_benchmarks_off_goes_back_to_one_task_of_pages_and_keywords(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    result = run(store, tmp_path, swov, cfg, replace(settings, benchmarks_enabled=False), f, claude_with(SEEDS))
    assert tasks(result) == ["7 advertiser pages/keywords"] and store.read_benchmarks("swoveralls") is None and result.follow == {}


def test_benchmark_brands_never_become_keyword_searches(tmp_path, swov, cfg, settings):
    store, f = seeded(tmp_path, swov), meta_world(cfg)
    run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS))
    keyword_task = [r for r in store.read_collected("swoveralls") if r["seeds"] == "7 keywords"][0]
    assert "Gymshark" not in json.dumps(keyword_task["seeds"]) and all(i["found_via"] != "Gymshark" for i in keyword_task["items"])


def test_the_seeds_command_shows_the_benchmark_review_list(tmp_path, swov, cfg, settings, monkeypatch, capsys):
    import shutil
    from contentintel import cli
    from contentintel import providers as P
    from conftest import BRANDS_DIR
    from fake_http import FakeHttp
    from test_collectors import BROWSER_YAML
    http = FakeHttp()
    http.on("POST", "https://api.anthropic.com/v1/messages", (200, {"content": [{"type": "text", "text": json.dumps(SEEDS)}]}))
    monkeypatch.setattr(P, "default_http", http)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-KEYMARK")
    monkeypatch.chdir(tmp_path)
    shutil.copytree(BRANDS_DIR, tmp_path / "brands")
    seeded(tmp_path, swov)
    assert cli.main(["collect", "--seeds", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(tmp_path / "brands"),
                     "--research-config", "research.yaml", "--browser-config", BROWSER_YAML]) == 0
    out = capsys.readouterr().out
    assert "Benchmark brands (followed by advertiser page): Oodie; Gymshark" in out            # what Claude suggested
    assert "Benchmark brands (each is followed through its own Meta advertiser page" in out    # and what the tool will do with it
    assert "to look up  Swoveralls" in out and "to look up  Oodie" in out and "to look up  Gymshark" in out
    assert "KEYMARK" not in out


def test_a_look_up_reads_no_more_ads_per_brand_than_its_budget_allows(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    lots = {name: {"json": [feed(*[ad(f"{name[:2]}{i}", pid, name, 500000) for i in range(15)])]}
            for name, pid in (("Swoveralls", P_SWOV), ("Oodie", P_OODIE_A), ("Gymshark", P_GYM))}
    f = meta_world(cfg, pages={url_of(n): v for n, v in lots.items()})
    run(store, tmp_path, swov, cfg, replace(settings, benchmark_items_per_name=2), f, claude_with(SEEDS))
    look_up = [r for r in store.read_collected("swoveralls") if r["seeds"].startswith("look up")][0]
    assert len(look_up["items"]) == 6                                         # 2 per brand, 3 brands
    by_label = {}
    for i in look_up["items"]:
        by_label[i["found_via"]] = by_label.get(i["found_via"], 0) + 1
    assert by_label == {"Swoveralls": 2, "Oodie": 2, "Gymshark": 2}


def test_a_page_already_planned_is_not_planned_again_when_a_later_look_up_finds_another(tmp_path, swov, cfg, settings):
    store, claude = seeded(tmp_path, swov), claude_with(SEEDS)
    one = replace(settings, benchmark_discovery_per_run=1)
    run(store, tmp_path, swov, cfg, one, meta_world(cfg), claude)                               # only Swoveralls is resolved
    run(store, tmp_path / "b", swov, cfg, one, meta_world(cfg), claude, now="2026-10-28T07:00:00Z", run_id="run2")   # Oodie: ambiguous
    f = meta_world(cfg)
    third = run(store, tmp_path / "c", swov, cfg, one, f, claude, now="2026-11-04T07:00:00Z", run_id="run3")       # Gymshark resolves
    opened = f.browsers["meta_ads"].opened
    assert state(store)["gymshark"]["status"] == "resolved"
    assert tasks(third) == ["1 followed advertiser pages", "7 keywords", "look up the advertiser page of 1 brand(s): Gymshark",
                            "1 followed advertiser pages"]
    assert opened.count(page_url(P_SWOV)) == 1 and opened.count(page_url(P_GYM)) == 1


def test_a_run_of_other_sources_does_not_print_or_look_up_meta_benchmarks(tmp_path, swov, cfg, settings):
    store = seeded(tmp_path, swov)
    f = meta_world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, claude_with(SEEDS), sources=("instagram",))
    assert [(r.source, r.status) for r in result.brands["swoveralls"]] == [("instagram", "ran")]
    assert result.follow == {} and "benchmark" not in C.format_collect_report(result, settings).lower()
    assert f.browsers["meta_ads"].opened == [] and store.read_benchmarks("swoveralls") is None
    with_meta = run(store, tmp_path / "m", swov, cfg, settings, meta_world(cfg), claude_with(SEEDS), run_id="run2")
    assert "swoveralls" in with_meta.follow and "benchmark brands" in C.format_collect_report(with_meta, settings)
