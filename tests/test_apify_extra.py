"""Apify as an EXTRA: both the browser and Apify run on every task and what they find is merged.

An ad or post that both find is kept once, the richer copy filling the other's blanks. If one route fails the other's
results are still kept; the failure is reported.
"""
from dataclasses import replace

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel.store import Store

from fake_browser import FakeBrowser
from test_apify_backend import AP_AD, ONE, apify_world, run as run_collect_with
from test_collectors import CREDS, site, statuses, used, world
from test_follow_plan_run import ad, feed

BOTH = {"track_own_brand": False, "ads": {"keywords": ["a"]}}
APIFY_SHARED = {**AP_AD, "adArchiveID": "555"}
APIFY_ONLY = {**AP_AD, "adArchiveID": "557", "snapshot": {**AP_AD["snapshot"], "body": {"text": "only Apify has this"}}}


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


def meta_world(cfg, *ads):
    f = world(cfg)
    f.browsers["meta_ads"] = FakeBrowser({C.AD_LIBRARY: {"json": [feed(*ads)]}}, clickable=set(site(cfg, "meta_ads")["consent_any"]))
    return f


def go(store, tmp_path, swov, cfg, settings, f, apify, **kw):
    kw.setdefault("sources", ("meta_ads",))
    kw.setdefault("data", BOTH)
    return run_collect_with(store, tmp_path, swov, cfg, settings, f, apify=apify, backend="extra", **kw)


# ---- the setting ------------------------------------------------------------------------------------------------------------

def test_extra_is_a_mode_and_a_backend_and_a_choice_in_the_workflow():
    assert "extra" in C.APIFY_MODES and "extra" in C.BACKENDS
    import yaml
    inputs = yaml.safe_load(open(".github/workflows/collect.yml", encoding="utf-8"))[True]["workflow_dispatch"]["inputs"]
    assert "extra" in inputs["backend"]["options"]


def test_research_yaml_can_make_extra_the_standing_choice(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text("collectors:\n  apify:\n    mode: extra\n")
    settings = C.load_collector_settings(path)
    assert settings.apify_mode == "extra" and C.effective_backend("auto", settings) == "extra"
    assert C.effective_backend("browser", settings) == "browser"                  # a run can still choose otherwise


def test_a_mode_that_does_not_exist_names_all_four(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text("collectors:\n  apify:\n    mode: sometimes\n")
    with pytest.raises(C.CollectError, match="must be one of off, fallback, always, extra"):
        C.load_collector_settings(path)


def test_the_explanation_in_research_yaml_describes_extra():
    text = open("research.yaml", encoding="utf-8").read()
    assert "mode: extra" in text and "IN ADDITION to the browser" in text


# ---- which routes ----------------------------------------------------------------------------------------------------------

def routes(backend, **kw):
    cfg = B.load_browser_settings("browser.yaml")
    args = dict(deep=False, backend=backend, apify=object(), apify_state={"disabled": None, "exhausted": False},
                site_cfg=cfg["sites"]["instagram"], creds=CREDS["instagram"], down=None)
    args.update(kw)
    return C._routes(args["deep"], args["backend"], args["apify"], args["apify_state"], args["site_cfg"], args["creds"], args["down"])


def test_extra_uses_both_routes_browser_first():
    assert routes("extra") == (["browser", "apify"], "")


def test_extra_with_no_token_uses_the_browser_alone_and_says_why():
    assert routes("extra", apify=None) == (["browser"], "no key: set APIFY_TOKEN")


def test_extra_with_the_browser_resting_uses_apify_alone():
    assert routes("extra", down="resting until 2026-10-29")[0] == ["apify"]


def test_extra_never_applies_to_deep_research():
    assert routes("extra", deep=True)[0] == ["browser"]


def test_other_backends_are_unchanged():
    assert routes("browser")[0] == ["browser"] and routes("apify")[0] == ["apify"] and routes("fallback")[0] == ["browser", "apify"]


# ---- merging two routes ------------------------------------------------------------------------------------------------------

def row(item_id, **kw):
    return C._row("meta_ads", item_id, **kw)


def test_an_item_both_routes_found_is_kept_once_with_blanks_filled_from_the_other():
    browser = row("1", handle="Acme", text="from the browser", found_via="kw one")
    apify = row("1", handle="Acme", text="from apify", media_url="https://img.example/a.jpg", reach=500, found_via="kw two")
    rows, stats = C.merge_routes([("browser", [browser], {"json": 1, "links": 0}), ("apify", [apify], {"json": 1, "links": 0})])
    (merged,) = rows
    assert merged["media_url"] == "https://img.example/a.jpg" and merged["reach"] == 500            # filled from the other route
    assert merged["found_via"] in ("kw one | kw two", "kw two | kw one") and merged["handle"] == "Acme"
    assert stats["common"] == 1 and stats["routes"] == {"browser": 1, "apify": 1}


def test_the_richer_copy_wins_and_a_filled_field_is_never_overwritten():
    rich = row("1", text="the long and detailed ad text", title="T", media_url="https://img.example/rich.jpg", views=10)
    poor = row("1", text="short", views=99)
    (merged,), _ = C.merge_routes([("browser", [poor], {}), ("apify", [rich], {})])
    assert merged["text"] == "the long and detailed ad text" and merged["media_url"] == "https://img.example/rich.jpg"
    assert merged["views"] == 10                                                                       # the richer copy's number stays


def test_different_items_from_each_route_are_all_kept_and_counted():
    rows, stats = C.merge_routes([("browser", [row("1"), row("2")], {"json": 2, "links": 0}),
                                  ("apify", [row("2"), row("3")], {"json": 2, "links": 0})])
    assert sorted(r["item_id"] for r in rows) == ["meta_ads:1", "meta_ads:2", "meta_ads:3"]
    assert stats["common"] == 1 and stats["json"] == 4 and stats["routes"] == {"browser": 2, "apify": 2}


def test_the_merge_reports_a_rate_limit_from_either_route_and_keeps_the_browsers_page_facts():
    _, stats = C.merge_routes([("browser", [row("1")], {"json": 1, "links": 0, "fallbacks": ["#a"], "fallback_how": ["typed"], "page_errors": 1}),
                               ("apify", [row("2")], {"json": 1, "links": 0, "rate_limited": True})])
    assert stats["rate_limited"] is True and stats["fallbacks"] == ["#a"] and stats["page_errors"] == 1


def test_the_merge_does_not_change_what_it_was_given():
    a, b = row("1", text="x"), row("1", text="x", media_url="https://i/x.jpg")
    C.merge_routes([("browser", [a], {}), ("apify", [b], {})])
    assert a["media_url"] == "" and b["media_url"] == "https://i/x.jpg"


# ---- a run -----------------------------------------------------------------------------------------------------------------

def test_both_routes_run_and_the_result_is_their_merge(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world(ads=[APIFY_SHARED, APIFY_ONLY])
    f = meta_world(cfg, ad("555", "100000000000500", "Acme", text="browser copy"), ad("556", "100000000000500", "Acme", text="browser only"),
                   ad("558", "100000000000500", "Acme", text="another browser one"))
    result = go(store, tmp_path, swov, cfg, settings, f, apify)
    assert statuses(result) == [("meta_ads", "ran")] and not result.problems
    detail = result.brands["swoveralls"][0].detail
    assert detail == "4 items: 3 from the browser, 2 via Apify, 1 found by both"          # unequal on purpose: a swap would show
    (record,) = store.read_collected("swoveralls")
    assert record["via"] == "browser+apify" and sorted(i["item_id"] for i in record["items"]) == ["meta_ads:555", "meta_ads:556", "meta_ads:557", "meta_ads:558"]
    shared = next(i for i in record["items"] if i["item_id"] == "meta_ads:555")
    assert shared["media_url"] == "https://img.example/a.jpg"                           # the browser's copy had none; Apify's filled it
    assert len(http.calls) == 1


def test_each_route_is_charged_to_its_own_budget(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world(ads=[APIFY_SHARED, APIFY_ONLY])
    f = meta_world(cfg, ad("555", "100000000000500", "Acme"), ad("556", "100000000000500", "Acme"), ad("558", "100000000000500", "Acme"))
    go(store, tmp_path, swov, cfg, settings, f, apify)
    assert used(store) == {"collected_items": 3, "apify_items": 2}


def test_the_browser_failing_still_keeps_apifys_results_and_flags_the_failure(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world(ads=[APIFY_SHARED])
    f = world(cfg)
    f.browsers["meta_ads"] = FakeBrowser({}, clickable=set(site(cfg, "meta_ads")["consent_any"]))     # opens, reads nothing
    result = go(store, tmp_path, swov, cfg, settings, f, apify)
    assert statuses(result) == [("meta_ads", "ran")]
    (record,) = store.read_collected("swoveralls")
    assert record["via"] == "apify" and [i["item_id"] for i in record["items"]] == ["meta_ads:555"]
    assert result.brands["swoveralls"][0].detail.startswith("1 items via Apify (the browser failed: the page opened but no ads or posts could be read")


def test_apify_failing_still_keeps_the_browsers_results_and_switches_apify_off(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    http.routes = [r for r in http.routes if "facebook-ads" not in r[1]]
    http.on("POST", "https://api.apify.com/v2/acts/apify~facebook-ads-scraper/run-sync-get-dataset-items", (401, {"error": {"message": "bad token"}}))
    f = meta_world(cfg, ad("556", "100000000000500", "Acme"))
    result = go(store, tmp_path, swov, cfg, settings, f, apify)
    assert statuses(result) == [("meta_ads", "ran")] and "apify" in result.problems_for
    (record,) = store.read_collected("swoveralls")
    assert record["via"] == "browser" and [i["item_id"] for i in record["items"]] == ["meta_ads:556"]


def test_both_routes_failing_is_a_failure_naming_both(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world(ads=[])
    f = world(cfg)
    f.browsers["meta_ads"] = FakeBrowser({}, clickable=set(site(cfg, "meta_ads")["consent_any"]))
    result = go(store, tmp_path, swov, cfg, settings, f, apify)
    (report,) = result.brands["swoveralls"]
    assert report.status == "failed" and "no ads or posts could be read" in report.detail and "Apify returned no readable items" in report.detail
    assert store.read_collected("swoveralls") == []


def test_without_a_token_extra_is_just_the_browser(tmp_path, store, swov, cfg, settings):
    f = meta_world(cfg, ad("556", "100000000000500", "Acme"))
    result = go(store, tmp_path, swov, cfg, settings, f, None)
    assert statuses(result) == [("meta_ads", "ran")]
    (record,) = store.read_collected("swoveralls")
    assert record["via"] == "browser" and used(store) == {"collected_items": 1}


def test_a_dry_run_charges_both_budgets_and_says_both_will_run(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), apify, dry_run=True)
    (report,) = result.brands["swoveralls"]
    assert report.status == "would_run"
    assert report.detail == "up to 30 items from meta_ads and up to 30 via Apify (apify/facebook-ads-scraper), merged"
    assert http.calls == [] and used(store) == {}


def test_the_item_cap_still_applies_to_each_route(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world(ads=[APIFY_SHARED, APIFY_ONLY])
    f = meta_world(cfg, *[ad(f"6{i}", "100000000000500", "Acme") for i in range(10)])
    go(store, tmp_path, swov, cfg, replace(settings, items_per_run=3, apify_items_per_run=1), f, apify)
    (record,) = store.read_collected("swoveralls")
    assert used(store) == {"collected_items": 3, "apify_items": 1} and len(record["items"]) == 4


def test_the_other_sources_follow_the_same_rule(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), apify, sources=("tiktok", "instagram"), data=ONE)
    assert statuses(result) == [("tiktok", "ran"), ("instagram", "ran")]
    assert {r["via"] for r in store.read_collected("swoveralls")} == {"browser+apify"}
    assert len(http.calls) == 2
