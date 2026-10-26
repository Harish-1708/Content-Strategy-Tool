import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel import providers as P
from contentintel import research as R
from contentintel.apify import Apify
from contentintel.store import Store

from conftest import BRANDS_DIR
from fake_browser import FakeBrowser, FakeClaude, GeminiFake
from fake_http import FakeHttp
from test_collectors import BROWSER_YAML, CREDS, FULL, NOW, site, statuses, used, world, write_research

ADS_URL = "https://api.apify.com/v2/acts/apify~facebook-ads-scraper/run-sync-get-dataset-items"
TT_URL = "https://api.apify.com/v2/acts/clockworks~tiktok-scraper/run-sync-get-dataset-items"
IG_URL = "https://api.apify.com/v2/acts/apify~instagram-hashtag-scraper/run-sync-get-dataset-items"
AP_AD = {"adArchiveID": "555", "pageName": "Acme", "startDate": 1760000000,
         "snapshot": {"body": {"text": "Apify ad text"}, "title": "T", "images": [{"originalImageUrl": "https://img.example/a.jpg"}]}}
AP_TT = {"id": "8001", "text": "apify tiktok", "webVideoUrl": "https://www.tiktok.com/@acme/video/8001", "createTimeISO": "2026-10-01T10:00:00.000Z",
         "authorMeta": {"name": "acme"}, "playCount": 1000, "diggCount": 100, "commentCount": 10, "shareCount": 5,
         "videoMeta": {"coverUrl": "https://img.example/c.jpg"}}
AP_IG = {"id": "9001", "shortCode": "abc", "url": "https://www.instagram.com/p/abc/?utm=x", "caption": "Weekend overalls", "ownerUsername": "acme",
         "likesCount": 50, "commentsCount": 3, "videoPlayCount": 99, "timestamp": "2026-10-01T10:00:00.000Z", "displayUrl": "https://img.example/i.jpg"}
ONE = {"track_own_brand": False, "ads": {"keywords": ["a"]}, "social": {"tiktok": {"hashtags": ["a"]}, "instagram": {"hashtags": ["a"]}}}
SOURCES = ("meta_ads", "tiktok", "instagram")


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


def apify_world(ads=None, tt=None, ig=None):
    http = FakeHttp()
    http.on("POST", ADS_URL, (200, [AP_AD] if ads is None else ads))
    http.on("POST", TT_URL, (200, [AP_TT] if tt is None else tt))
    http.on("POST", IG_URL, (200, [AP_IG] if ig is None else ig))
    return http, Apify("apify-secret-123", http=http, sleep=lambda s: None)


def run(store, tmp_path, brand, cfg, settings, factory, *, apify, backend, data=ONE, sources=SOURCES, creds=CREDS, dry_run=False):
    write_research(tmp_path, brand, {"track_own_brand": False, **data})
    return C.run_collect([brand], store, browser_factory=factory, creds=creds, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger,
                         sources=list(sources), now=NOW, run_id="run1", root=str(tmp_path / "brands"), dry_run=dry_run, apify=apify,
                         backend=backend)


def expired_instagram(cfg, f):
    f.browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["login_wall_any"][0]})


# --- the Apify client ------------------------------------------------------------------------------------------------------

def test_apify_request_shape():
    http, apify = apify_world(ads=[AP_AD, AP_AD, AP_AD])
    items = apify.run("apify/facebook-ads-scraper", {"startUrls": []}, max_items=2, max_cost_usd=1.0)
    (call,) = http.calls
    assert call["method"] == "POST" and call["url"] == ADS_URL and call["headers"]["Authorization"] == "Bearer apify-secret-123"
    assert call["params"] == {"timeout": 280, "maxItems": 2, "maxTotalChargeUsd": 1.0} and call["json"] == {"startUrls": []}
    assert call["timeout"] == 310 and len(items) == 2                                        # never more than the cap


@pytest.mark.parametrize("status,error", [(401, P.AuthError), (403, P.AuthError), (402, P.QuotaError), (408, P.ProviderError), (400, P.ProviderError)])
def test_apify_key_credit_and_run_problems(status, error):
    http, apify = apify_world()
    http.routes = [r for r in http.routes if r[1] != ADS_URL]
    http.on("POST", ADS_URL, (status, {"error": {"message": "nope"}}))
    with pytest.raises(error):
        apify.run("apify/facebook-ads-scraper", {}, max_items=1, max_cost_usd=1)


def test_apify_odd_answers_retries_and_the_token_never_leaks():
    http, apify = apify_world(ads={"not": "a list"})
    with pytest.raises(P.ProviderError, match="expected a list"):
        apify.run("apify/facebook-ads-scraper", {}, max_items=1, max_cost_usd=1)
    http.routes = [r for r in http.routes if r[1] != ADS_URL]
    http.on("POST", ADS_URL, (503, "busy"), (200, [AP_AD]))
    assert len(apify.run("apify/facebook-ads-scraper", {}, max_items=5, max_cost_usd=1)) == 1
    http.routes = [r for r in http.routes if r[1] != ADS_URL]
    http.on("POST", ADS_URL, (400, "bad input for apify-secret-123"))
    with pytest.raises(P.ProviderError) as info:
        apify.run("apify/facebook-ads-scraper", {}, max_items=1, max_cost_usd=1)
    assert "apify-secret-123" not in str(info.value)


# --- reading Apify's output ----------------------------------------------------------------------------------------------

def test_apify_items_are_read_into_the_same_rows_as_the_browser_ones():
    (ad,), _ = C.normalize_items("meta_ads", [AP_AD])
    assert (ad["item_id"], ad["handle"], ad["text"], ad["media_url"]) == ("meta_ads:555", "Acme", "Apify ad text", "https://img.example/a.jpg")
    (tt,), _ = C.normalize_items("tiktok", [AP_TT])
    assert (tt["item_id"], tt["handle"], tt["likes"], tt["comments"], tt["shares"], tt["views"], tt["media_url"]) == (
        "tiktok:8001", "acme", 100, 10, 5, 1000, "https://img.example/c.jpg")
    (ig,), _ = C.normalize_items("instagram", [AP_IG])
    assert (ig["item_id"], ig["handle"], ig["text"], ig["likes"], ig["comments"], ig["views"], ig["media_url"]) == (
        "instagram:abc", "acme", "Weekend overalls", 50, 3, 99, "https://img.example/i.jpg")
    assert ig["url"] == "https://www.instagram.com/p/abc/" and ig["posted_at"] == "2026-10-01T10:00:00.000Z"


def test_an_apify_reel_keeps_its_own_address_and_the_query_string_is_dropped():
    reel = {**AP_IG, "shortCode": "zzz", "url": "https://www.instagram.com/reel/zzz/?igsh=abc"}
    (row,), _ = C.normalize_items("instagram", [reel])
    assert row["url"] == "https://www.instagram.com/reel/zzz/"
    (built,), _ = C.normalize_items("instagram", [{k: v for k, v in AP_IG.items() if k != "url"}])
    assert built["url"] == "https://www.instagram.com/p/abc/"


def test_unreadable_apify_items_are_counted_not_guessed():
    rows, bad = C.normalize_items("tiktok", [AP_TT, {"error": "blocked"}, {"text": "no link"}, "junk", None])
    assert len(rows) == 1 and bad == 4


def test_each_scraper_is_asked_in_its_own_terms():
    ads = C.Task("meta_ads", "t", {"urls": ["u1", "u2"]}, "k", 2)
    assert C.apify_input(ads, 10) == {"startUrls": [{"url": "u1"}, {"url": "u2"}], "resultsLimit": 10}
    ig = C.Task("instagram", "t", {"hashtags": ["a", "b"]}, "k", 2)
    assert C.apify_input(ig, 10) == {"hashtags": ["a", "b"], "resultsType": "posts", "resultsLimit": 5}
    tt = C.Task("tiktok", "t", {"hashtags": ["a"], "searches": ["x y"]}, "k", 2)
    assert C.apify_input(tt, 10) == {"hashtags": ["a"], "searchQueries": ["x y"], "resultsPerPage": 5}
    assert C.apify_input(C.Task("tiktok", "t", {"profiles": ["p"]}, "k", 1), 1) == {"profiles": ["p"], "resultsPerPage": 1}


# --- the settings -------------------------------------------------------------------------------------------------------------

def test_apify_is_off_by_default_and_the_defaults_are_modest(settings):
    assert settings.apify_mode == "off" and (settings.apify_monthly_items, settings.apify_items_per_run, settings.apify_max_cost_per_run_usd) == (100, 30, 1.0)
    assert settings.actors == C.DEFAULT_APIFY_ACTORS
    assert (settings.claude_model, settings.claude_monthly_calls, settings.claude_max_rounds, settings.claude_max_tokens, settings.claude_prepare_prompt) == (
        "claude-sonnet-5-5", 60, 3, 700, True)


def test_an_unquoted_off_in_the_settings_file_means_off_and_on_is_explained(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text("collectors:\n  apify:\n    mode: off\n")
    assert C.load_collector_settings(path).apify_mode == "off"
    path.write_text("collectors:\n  apify:\n    mode: on\n")
    with pytest.raises(C.CollectError, match="'on' is not a mode; write fallback or always"):
        C.load_collector_settings(path)
    path.write_text("collectors:\n  apify:\n    mode: fallback\n")
    assert C.load_collector_settings(path).apify_mode == "fallback"


def test_apify_and_claude_settings_can_be_changed_and_one_actor_swapped(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": {"apify": {"mode": "always", "monthly_items": 20, "actors": {"tiktok": "someone~tiktok-thing"}},
                                                    "claude": {"model": "claude-haiku-5-5", "prepare_prompt": False, "max_rounds": 1}}}))
    s = C.load_collector_settings(path)
    assert (s.apify_mode, s.apify_monthly_items, s.actors["tiktok"], s.actors["meta_ads"]) == ("always", 20, "someone/tiktok-thing", "apify/facebook-ads-scraper")
    assert (s.claude_model, s.claude_prepare_prompt, s.claude_max_rounds, s.claude_monthly_calls) == ("claude-haiku-5-5", False, 1, 60)


@pytest.mark.parametrize("data,match", [
    ({"apify": {"mode": "sometimes"}}, "must be one of off, fallback, always"), ({"apify": {"colour": 1}}, "unknown 'colour'"),
    ({"apify": {"items_per_run": 0}}, "items_per_run"), ({"apify": {"monthly_items": 2.5}}, "whole number"),
    ({"apify": {"max_cost_per_run_usd": 0}}, "max_cost_per_run_usd"), ({"apify": {"actors": {"snapchat": "a/b"}}}, "unknown 'snapchat'"),
    ({"apify": {"actors": {"tiktok": "justaname"}}}, "username/actor-name"), ({"claude": {"model": ""}}, "model name"),
    ({"claude": {"prepare_prompt": "yes"}}, "true or false"), ({"claude": {"max_tokens": 10}}, "max_tokens"),
    ({"claude": {"monthly_calls": -1}}, "monthly_calls"), ({"claude": {"colour": 1}}, "unknown 'colour'")])
def test_bad_apify_and_claude_settings_are_rejected(tmp_path, data, match):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"collectors": data}))
    with pytest.raises(C.CollectError, match=match):
        C.load_collector_settings(path)


def test_auto_follows_the_settings_and_an_explicit_choice_wins(settings):
    for mode, expected in (("off", "browser"), ("fallback", "fallback"), ("always", "apify")):
        assert C.effective_backend("auto", replace(settings, apify_mode=mode)) == expected
        for explicit in ("browser", "apify", "fallback"):
            assert C.effective_backend(explicit, replace(settings, apify_mode=mode)) == explicit


# --- choosing the route -----------------------------------------------------------------------------------------------------

def test_the_browser_is_the_default_and_apify_is_never_touched(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    run(store, tmp_path, swov, cfg, settings, world(cfg), apify=apify, backend="browser")
    assert http.calls == [] and used(store) == {"collected_items": 6}


def test_in_browser_mode_a_failed_browser_run_is_never_quietly_rescued_by_apify(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    f = world(cfg)
    expired_instagram(cfg, f)
    result = run(store, tmp_path, swov, cfg, settings, f, apify=apify, backend="browser", sources=("instagram",))
    assert statuses(result) == [("instagram", "failed")] and http.calls == [] and used(store) == {}
    plan = run(store, tmp_path / "b", swov, cfg, settings, world(cfg), apify=apify, backend="browser", sources=("meta_ads",), dry_run=True)
    assert "Apify" not in plan.brands["swoveralls"][0].detail


def test_apify_only_collects_without_opening_a_browser(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    f = world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, apify=apify, backend="apify")
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "ran"), ("instagram", "ran")] and f.opened == [] and not result.problems
    assert {r["via"] for r in store.read_collected("swoveralls")} == {"apify"} and used(store) == {"apify_items": 3}
    assert all("via Apify" in r.detail for r in result.brands["swoveralls"])


def test_apify_obeys_its_own_per_run_and_monthly_caps(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world(ads=[AP_AD] * 5)
    run(store, tmp_path, swov, cfg, replace(settings, apify_items_per_run=3), world(cfg), apify=apify, backend="apify", sources=("meta_ads",))
    call = http.calls[0]
    assert call["params"]["maxItems"] == 3 and call["json"]["resultsLimit"] == 3 and call["params"]["maxTotalChargeUsd"] == 1.0
    http2, apify2 = apify_world(ads=[AP_AD] * 5)
    result = run(Store(tmp_path / "d2"), tmp_path / "t2", swov, cfg, replace(settings, apify_monthly_items=2), world(cfg), apify=apify2, backend="apify")
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "skipped"), ("instagram", "skipped")]
    assert "out of items under your monthly cap" in result.brands["swoveralls"][1].detail


def test_apify_without_a_token_is_explained_but_deep_research_still_uses_the_browser(tmp_path, store, swov, cfg, settings):
    result = run(store, tmp_path, swov, cfg, settings, world(cfg), apify=None, backend="apify", sources=("meta_ads", "deep_research"),
                 data={**ONE, **{"deep_research": {"questions": ["Q?"]}}})
    assert statuses(result) == [("meta_ads", "skipped"), ("deep_research", "ran")]
    assert result.brands["swoveralls"][0].detail == "no key: set APIFY_TOKEN" and not result.problems


def test_in_fallback_mode_a_working_browser_never_calls_apify(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    result = run(store, tmp_path, swov, cfg, settings, world(cfg), apify=apify, backend="fallback")
    assert http.calls == [] and not result.problems and {r["via"] for r in store.read_collected("swoveralls")} == {"browser"}


def test_in_fallback_mode_a_failed_login_is_rescued_by_apify_but_still_flagged(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    f = world(cfg)
    expired_instagram(cfg, f)
    result = run(store, tmp_path, swov, cfg, settings, f, apify=apify, backend="fallback")
    assert statuses(result) == [("meta_ads", "ran"), ("tiktok", "ran"), ("instagram", "ran")]
    assert "via Apify (the browser failed: instagram: not logged in" in result.brands["swoveralls"][2].detail
    assert result.problems and result.problems_for == ["instagram"]                         # the cookies still need refreshing
    assert {r["source"]: r["via"] for r in store.read_collected("swoveralls")} == {"meta_ads": "browser", "tiktok": "browser", "instagram": "apify"}
    assert used(store) == {"collected_items": 4, "apify_items": 1}


def test_in_fallback_mode_a_page_that_yields_nothing_is_also_rescued(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    result = run(store, tmp_path, swov, cfg, settings, world(cfg, ig={"nothing": "here"}), apify=apify, backend="fallback", sources=("instagram",))
    assert statuses(result) == [("instagram", "ran")] and "no ads or posts could be read" in result.brands["swoveralls"][0].detail


def test_in_fallback_mode_with_no_browser_login_apify_simply_takes_over(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    f = world(cfg)
    result = run(store, tmp_path, swov, cfg, settings, f, apify=apify, backend="fallback", sources=("instagram",),
                 creds={**CREDS, "instagram": {}})
    assert statuses(result) == [("instagram", "ran")] and f.opened == [] and not result.problems
    assert result.brands["swoveralls"][0].detail == "1 items via Apify"


def test_in_fallback_mode_without_a_token_a_browser_failure_is_just_a_failure(tmp_path, store, swov, cfg, settings):
    f = world(cfg)
    expired_instagram(cfg, f)
    result = run(store, tmp_path, swov, cfg, settings, f, apify=None, backend="fallback", sources=("instagram",))
    assert statuses(result) == [("instagram", "failed")] and "not logged in" in result.brands["swoveralls"][0].detail


def test_in_fallback_mode_a_used_up_apify_allowance_leaves_the_browser_failure_standing(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    f = world(cfg)
    expired_instagram(cfg, f)
    result = run(store, tmp_path, swov, cfg, replace(settings, apify_monthly_items=0), f, apify=apify, backend="fallback", sources=("instagram",))
    assert statuses(result) == [("instagram", "failed")] and http.calls == []


def test_a_rejected_apify_token_switches_apify_off_for_the_run(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    http.routes = [r for r in http.routes if r[1] != ADS_URL]
    http.on("POST", ADS_URL, (401, {"error": {"message": "bad token"}}))
    result = run(store, tmp_path, swov, cfg, settings, world(cfg), apify=apify, backend="apify")
    assert statuses(result) == [("meta_ads", "failed"), ("tiktok", "skipped"), ("instagram", "skipped")]
    assert result.problems_for == ["apify"] and used(store) == {} and len(http.calls) == 1


def test_running_out_of_apify_credit_is_not_a_failure(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    http.routes = [r for r in http.routes if r[1] != ADS_URL]
    http.on("POST", ADS_URL, (402, {"error": {"message": "limit"}}))
    result = run(store, tmp_path, swov, cfg, settings, world(cfg), apify=apify, backend="apify")
    assert statuses(result) == [("meta_ads", "skipped"), ("tiktok", "skipped"), ("instagram", "skipped")] and not result.problems
    assert any("apify is out of credit" in n for n in result.notes) and result.brands["swoveralls"][1].detail == "Apify is out of credit this run"


def test_apify_items_that_cannot_be_read_are_a_failure_and_are_still_charged(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world(tt=[{"error": "blocked"}])
    result = run(store, tmp_path, swov, cfg, settings, world(cfg), apify=apify, backend="apify", sources=("tiktok",))
    assert statuses(result) == [("tiktok", "failed")] and "Apify returned no readable items" in result.brands["swoveralls"][0].detail
    assert used(store) == {"apify_items": 1} and store.read_collected("swoveralls") == []


def test_a_dry_run_says_which_route_each_task_would_take(tmp_path, store, swov, cfg, settings):
    http, apify = apify_world()
    f = world(cfg)
    assert run(store, tmp_path, swov, cfg, settings, f, apify=apify, backend="apify", dry_run=True).brands["swoveralls"][0].detail == \
        "up to 30 items via Apify (apify/facebook-ads-scraper)"
    fallback = run(store, tmp_path / "b", swov, cfg, settings, f, apify=apify, backend="fallback", dry_run=True)
    assert fallback.brands["swoveralls"][0].detail == "up to 30 items from meta_ads (Apify if the browser fails)"
    assert http.calls == [] and f.opened == []


# --- the checks ------------------------------------------------------------------------------------------------------------------

def check(store, cfg, settings, f, *, apify=None, claude=None, backend="browser", sources=("meta_ads", "instagram", "tiktok", "deep_research"), creds=CREDS):
    return {r["source"]: r for r in C.check_collectors(f, creds, settings, cfg, store, R.Ledger, NOW, list(sources), apify=apify, claude=claude, backend=backend)}


def test_the_check_can_prove_the_apify_route_with_a_handful_of_items(store, cfg, settings):
    http, apify = apify_world(ads=[AP_AD] * 5)
    f = world(cfg)
    rows = check(store, cfg, settings, f, apify=apify, backend="apify", sources=SOURCES)
    assert all(r["ok"] for r in rows.values()) and rows["meta_ads"]["detail"] == "via Apify: 3 items, 3 readable" and f.opened == []
    for call in http.calls:
        assert call["params"]["maxItems"] == 3 and call["params"]["maxTotalChargeUsd"] == 0.25
    assert used(store) == {"apify_items": 5}


def test_the_apify_check_explains_each_way_it_can_fail(store, cfg, settings):
    assert check(store, cfg, settings, world(cfg), apify=None, backend="apify", sources=["tiktok"])["tiktok"]["detail"] == "no key: set APIFY_TOKEN"
    http, apify = apify_world(tt=[], ig=[{"foo": 1}])
    rows = check(store, cfg, settings, world(cfg), apify=apify, backend="apify", sources=["tiktok", "instagram"])
    assert not rows["tiktok"]["ok"] and "returned no items" in rows["tiktok"]["detail"]
    assert not rows["instagram"]["ok"] and "0 readable" in rows["instagram"]["detail"]
    capped = check(store, cfg, replace(settings, apify_monthly_items=2), world(cfg), apify=apify, backend="apify", sources=["tiktok"])
    assert capped["tiktok"]["detail"].startswith("skipped")
    http.routes = [r for r in http.routes if r[1] != TT_URL]
    http.on("POST", TT_URL, (401, {"error": {"message": "bad"}}))
    assert "rejected the API key" in check(store, cfg, settings, world(cfg), apify=apify, backend="apify", sources=["tiktok"])["tiktok"]["detail"]


def test_the_check_tests_claude_only_alongside_deep_research(store, cfg, settings):
    ok = check(store, cfg, settings, world(cfg), claude=FakeClaude())
    assert ok["claude"] == {"source": "claude", "ok": True, "detail": "key accepted; claude-sonnet-5-5 answered"}
    assert check(store, cfg, settings, world(cfg), claude=None)["claude"]["detail"].startswith("no key: set ANTHROPIC_API_KEY")
    broken = FakeClaude()
    broken.check = lambda: (_ for _ in ()).throw(P.AuthError("Claude rejected the API key (HTTP 401)"))
    assert "rejected the API key" in check(store, cfg, settings, world(cfg), claude=broken)["claude"]["detail"]
    assert "claude" not in check(store, cfg, settings, world(cfg), claude=FakeClaude(), sources=["tiktok"])


# --- the command line -----------------------------------------------------------------------------------------------------------------

MARKERS = {"INSTAGRAM_COOKIES": '[{"name": "s", "value": "COOKIEMARK1", "domain": ".instagram.com"}]',
           "TIKTOK_COOKIES": '[{"name": "s", "value": "COOKIEMARK2", "domain": ".tiktok.com"}]',
           "GEMINI_COOKIES": '[{"name": "s", "value": "COOKIEMARK3", "domain": ".google.com"}]',
           "ANTHROPIC_API_KEY": "sk-ant-KEYMARK4", "APIFY_TOKEN": "apify-KEYMARK5"}
ALL_SECRETS = ["INSTAGRAM_COOKIES", "INSTAGRAM_USERNAME", "INSTAGRAM_PASSWORD", "TIKTOK_COOKIES", "TIKTOK_USERNAME", "TIKTOK_PASSWORD",
               "GEMINI_COOKIES", "ANTHROPIC_API_KEY", "APIFY_TOKEN"]


@pytest.fixture
def cli2(tmp_path, monkeypatch):
    cfg = B.load_browser_settings("browser.yaml")
    state = {"factory": world(cfg)}
    http, _ = apify_world()
    http.on("POST", "https://api.anthropic.com/v1/messages", (200, {"content": [{"type": "text", "text": "A COMPLETE PROMPT"}]}))
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", lambda *_: state["factory"])
    monkeypatch.setattr(P, "default_http", http)
    monkeypatch.setattr(P.time, "sleep", lambda s: None)
    for name in ALL_SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    brands = tmp_path / "brands"
    shutil.copytree(BRANDS_DIR, brands)
    (brands / "swoveralls" / "research.yaml").write_text(yaml.safe_dump({**FULL, "deep_research": {"questions": ["Q?"]}}), encoding="utf-8")

    def go(*args, env=MARKERS, research="research.yaml"):
        for name in ALL_SECRETS:                      # each call sees exactly the secrets it is given
            monkeypatch.delenv(name, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return cli.main(["collect", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(brands), "--research-config", str(research),
                         "--browser-config", BROWSER_YAML, "--debug-dir", str(tmp_path / "debug"), *args])
    return state, go, http, cfg


def test_every_run_starts_by_listing_which_secrets_it_can_see_without_showing_any(cli2, capsys):
    state, go, http, cfg = cli2
    assert go("--brand", "swoveralls", "--dry-run", env={"INSTAGRAM_COOKIES": MARKERS["INSTAGRAM_COOKIES"]}) == 0
    out = capsys.readouterr().out
    for name in ALL_SECRETS:
        assert re.search(rf"^  {name}\s+(set|not set)$", out, re.M), name
    assert re.search(r"^  INSTAGRAM_COOKIES\s+set$", out, re.M) and re.search(r"^  APIFY_TOKEN\s+not set$", out, re.M)
    assert "COOKIEMARK" not in out and "KEYMARK" not in out


def test_the_check_can_test_the_apify_route(cli2, capsys):
    state, go, http, cfg = cli2
    assert go("--check", "--backend", "apify", "--sources", "meta_ads,tiktok,instagram") == 0
    out = capsys.readouterr().out
    assert out.count("via Apify: 1 items, 1 readable") == 3 and "KEYMARK" not in out and state["factory"].opened == []


def test_the_check_includes_claude_alongside_deep_research(cli2, capsys):
    state, go, http, cfg = cli2
    assert go("--check", "--sources", "deep_research") == 0
    assert "claude        OK: key accepted; claude-sonnet-5-5 answered" in capsys.readouterr().out


def test_a_missing_claude_key_is_reported_by_the_check_without_failing_it(cli2, capsys, monkeypatch):
    state, go, http, cfg = cli2
    assert go("--check", "--sources", "deep_research", env={"GEMINI_COOKIES": MARKERS["GEMINI_COOKIES"]}) == 0
    assert "claude        FAILED: no key: set ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_a_full_command_line_run_can_go_through_apify(cli2, capsys, tmp_path):
    state, go, http, cfg = cli2
    assert go("--brand", "swoveralls", "--backend", "apify", "--sources", "tiktok,instagram") == 0
    out = capsys.readouterr().out
    assert out.count("1 items via Apify") == 2 and state["factory"].opened == [] and "KEYMARK" not in out
    assert "Apify items 2/100" in out
    assert {json.loads(l)["via"] for l in (tmp_path / "data" / "swoveralls" / "research" / "collected.jsonl").read_text().splitlines()} == {"apify"}


def test_apify_only_without_a_token_skips_and_names_the_secret(cli2, capsys):
    state, go, http, cfg = cli2
    assert go("--all", "--backend", "apify", "--sources", "instagram,tiktok", env={}) == 0
    out = capsys.readouterr().out
    assert "Collection skipped" in out and "no key: set APIFY_TOKEN" in out and state["factory"].opened == []


def test_fallback_uses_apify_when_the_browser_login_fails_and_still_exits_red(cli2, capsys, tmp_path):
    state, go, http, cfg = cli2
    expired_instagram(cfg, state["factory"])
    assert go("--brand", "swoveralls", "--backend", "fallback", "--sources", "instagram") == 1
    out = capsys.readouterr().out
    assert "via Apify (the browser failed" in out and "::error::instagram: the login did not work" in out and "KEYMARK" not in out
    assert '"via": "apify"' in (tmp_path / "data" / "swoveralls" / "research" / "collected.jsonl").read_text()


def test_auto_follows_research_yaml_and_an_explicit_backend_overrides_it(cli2, capsys, tmp_path):
    state, go, http, cfg = cli2
    custom = tmp_path / "custom.yaml"
    custom.write_text("collectors:\n  apify:\n    mode: fallback\n")
    go("--brand", "swoveralls", "--dry-run", research=custom)
    assert "Backend for ads and social: fallback" in capsys.readouterr().out
    go("--brand", "swoveralls", "--dry-run", "--backend", "browser", research=custom)
    assert "Backend for ads and social: browser" in capsys.readouterr().out
    go("--brand", "swoveralls", "--dry-run")
    assert "Backend for ads and social: browser" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        go("--brand", "swoveralls", "--backend", "sometimes")


def test_the_deep_research_test_runs_one_report_and_prints_the_outcome(cli2, capsys, tmp_path):
    state, go, http, cfg = cli2
    assert go("--test-deep-research") == 0
    out = capsys.readouterr().out
    assert "Deep Research test: OK: a report of" in out and "Claude made 1 call(s)" in out and "KEYMARK" not in out
    text = (tmp_path / "debug" / "test-deep-research.md").read_text(encoding="utf-8")
    assert "A COMPLETE PROMPT" in text and (tmp_path / "data" / "swoveralls").exists() is False


def test_the_deep_research_test_fails_loudly_or_skips_politely(cli2, capsys):
    state, go, http, cfg = cli2
    state["factory"].browsers["gemini"] = GeminiFake(site(cfg, "gemini"), logged_in=False)
    assert go("--test-deep-research") == 1
    assert "::error::Deep Research test failed" in capsys.readouterr().out
    assert go("--test-deep-research", env={}) == 0 and "no login: set GEMINI_COOKIES" in capsys.readouterr().out


# --- the workflow and the secrets list ----------------------------------------------------------------------------------------

WORKFLOWS = sorted(Path(".github/workflows").glob("*.yml"))


def test_the_collect_workflow_has_one_mode_choice_and_one_backend_choice():
    text = open(".github/workflows/collect.yml", encoding="utf-8").read()
    data = yaml.safe_load(text)
    inputs = (data.get("on") or data.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["plan", "seeds", "check", "recon", "test_deep_research", "collect", "rest"] and inputs["mode"]["default"] == "plan"
    assert inputs["backend"]["options"] == ["auto", "browser", "apify", "fallback", "extra"] and inputs["backend"]["default"] == "auto"
    assert inputs["use_deep_research"]["default"] is False and set(inputs) >= {"brand", "use_meta_ads", "use_tiktok", "use_instagram"}
    for mode, flag in (("plan", "--dry-run"), ("seeds", "--seeds"), ("check", "--check"), ("recon", "--recon"), ("test_deep_research", "--test-deep-research")):
        assert re.search(rf"{mode}\)\s+.*{re.escape(flag)}", text), mode
    assert 'MODE="${MODE:-collect}"' in text and '--backend "${BACKEND:-auto}"' in text
    assert "inputs.mode != 'plan' && inputs.mode != 'seeds'" in text and "inputs.mode == 'collect' || inputs.mode == ''" in text
    assert data["jobs"]["collect"]["timeout-minutes"] >= 90 and "cron:" in text and "path: debug" in text


def workflow_secrets():
    return {n: set(re.findall(rf"(\w+): \$\{{\{{ secrets\.{n} \}}\}}", "".join(w.read_text() for w in WORKFLOWS))) for n in
            set(re.findall(r"secrets\.([A-Z][A-Z0-9_]+)", "".join(w.read_text() for w in WORKFLOWS)))}


def test_every_secret_is_passed_to_the_code_under_the_name_the_code_reads():
    for secret, env_names in workflow_secrets().items():
        assert env_names == {secret}, (secret, env_names)


def test_every_secret_the_code_can_read_is_given_to_it_by_a_workflow():
    cfg = B.load_browser_settings("browser.yaml")
    wanted = {n for s in cfg["sites"].values() for n in s.get("secrets", {}).values()} | set(cli.EXTRA_SECRETS)
    wanted |= {"SERPER_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON"}
    assert wanted <= set(workflow_secrets()), wanted - set(workflow_secrets())


def test_the_readme_documents_every_secret_and_lists_none_that_do_not_exist():
    readme = open("README.md", encoding="utf-8").read()
    section = readme[readme.index("## Secrets"):]
    section = section[:section.index("\n## ", 5)] if "\n## " in section[5:] else section
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", section)) - {"GITHUB_TOKEN"}
    used = set(workflow_secrets())
    assert used <= documented, f"not documented: {sorted(used - documented)}"
    assert documented <= used, f"documented but nothing uses them: {sorted(documented - used)}"
