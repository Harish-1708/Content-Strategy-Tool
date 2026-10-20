from dataclasses import replace

import pytest
import yaml

from contentintel import cli
from contentintel import providers as P
from contentintel import research as R
from contentintel.store import Store

from conftest import BRANDS_DIR as ROOT_BRANDS
from fake_http import FakeHttp, searches_only, serper_web, standard_routes

NOW = "2026-10-20T07:00:00Z"
KEYS = {"SERPER_API_KEY": "k-serper", "TAVILY_API_KEY": "k-tavily", "SERPAPI_API_KEY": "k-serpapi"}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return R.load_settings("does-not-exist.yaml")


def world(only=None, **route_kw):
    http = standard_routes(FakeHttp(), **route_kw)
    found = P.build_providers(KEYS, http=http, sleep=lambda s: None)
    if only:
        found = {n: p for n, p in found.items() if n in only}
    return http, found


def write_research(tmp_path, brand, data):
    root = tmp_path / "brands"
    (root / brand.slug).mkdir(parents=True, exist_ok=True)
    (root / brand.slug / "research.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


def go(store, brands, providers, settings, *, now=NOW, root="brands-that-do-not-exist", **kw):
    return R.run_research(brands, store, providers=providers, settings=settings, now=now,
                          root=root, **kw)


def with_caps(settings, **caps):
    return replace(settings, caps={**settings.caps, **{k: R.Caps(*v) for k, v in caps.items()}})


def explicit(*queries, kind="web"):
    return [{"q": q, "kind": kind} for q in queries]


def used_providers(http):
    out = []
    for call in searches_only(http):
        out.append("serper_dev" if "serper.dev" in call["url"] else
                   "tavily" if "tavily" in call["url"] else "serpapi")
    return out


# --- settings --------------------------------------------------------------------------------

def test_settings_default_when_there_is_no_file(settings):
    assert settings.caps["serper_dev"] == R.Caps(250, 2400, 0)
    assert settings.caps["tavily"] == R.Caps(600, None, 50)
    assert settings.caps["serpapi"] == R.Caps(100, None, 20)
    assert settings.routing == {"web": ["serper_dev", "tavily", "serpapi"],
                                "news": ["tavily", "serper_dev", "serpapi"]}
    assert (settings.results_per_query, settings.cache_days, settings.max_queries_per_run,
            settings.country, settings.recency) == (10, 7, 30, "us", None)


def test_a_settings_file_changes_only_what_it_names(tmp_path):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump({"providers": {"tavily": {"monthly_cap": 50}},
                                    "routing": {"web": ["tavily"]},
                                    "defaults": {"cache_days": 1, "country": "IN", "recency": "week"}}))
    s = R.load_settings(path)
    assert s.caps["tavily"] == R.Caps(50, None, 50) and s.caps["serpapi"] == R.Caps(100, None, 20)
    assert s.routing["web"] == ["tavily"] and s.routing["news"][0] == "tavily"
    assert (s.cache_days, s.country, s.recency) == (1, "in", "week")


@pytest.mark.parametrize("data,match", [
    ({"surprise": 1}, "unknown 'surprise'"),
    ({"providers": {"google": {}}}, "unknown 'google'"),
    ({"providers": {"tavily": {"monthly_cap": -1}}}, "monthly_cap"),
    ({"providers": {"tavily": {"monthly_cap": True}}}, "monthly_cap"),
    ({"providers": {"tavily": {"monthly_cap": "lots"}}}, "monthly_cap"),
    ({"providers": {"tavily": {"colour": 1}}}, "unknown 'colour'"),
    ({"routing": {"web": ["bing"]}}, "routing.web"),
    ({"routing": {"web": ["tavily", "tavily"]}}, "routing.web"),
    ({"routing": {"web": []}}, "routing.web"),
    ({"routing": {"images": ["tavily"]}}, "unknown 'images'"),
    ({"defaults": {"results_per_query": 11}}, "results_per_query"),
    ({"defaults": {"results_per_query": 2.5}}, "results_per_query"),
    ({"defaults": {"max_queries_per_run": 0}}, "max_queries_per_run"),
    ({"defaults": {"country": "usa"}}, "country"),
    ({"defaults": {"recency": "decade"}}, "recency"),
])
def test_bad_settings_are_rejected_with_a_clear_message(tmp_path, data, match):
    path = tmp_path / "research.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(R.ResearchError, match=match):
        R.load_settings(path)


def test_the_shipped_settings_file_matches_the_built_in_defaults():
    shipped = R.load_settings("research.yaml")
    built_in = R.load_settings("does-not-exist.yaml")
    assert shipped == built_in


# --- a brand's research file and the queries it makes ------------------------------------------

def test_with_no_file_a_brand_only_searches_for_its_own_name(swov, settings):
    config = R.load_brand_research(swov, "no-such-folder")
    plan = R.plan_queries(swov, config, settings)
    assert [q.text for q in plan] == ['"Swoveralls" reviews', '"Swoveralls" competitors',
                                      '"Swoveralls" TikTok']
    assert {q.kind for q in plan} == {"web"} and {q.tag for q in plan} == {"brand"}
    assert {q.brand for q in plan} == {"swoveralls"} and {q.country for q in plan} == {"us"}


def test_competitors_topics_and_explicit_queries_are_planned_in_order(tmp_path, swov, settings):
    root = write_research(tmp_path, swov, {
        "competitors": [{"name": "Acme Wear", "domain": "acme.example"}, "Bolt"],
        "topics": ["overalls outfits"],
        "queries": [{"q": "sweatpants overalls", "kind": "news", "recency": "week"}]})
    plan = R.plan_queries(swov, R.load_brand_research(swov, root), settings)
    got = [(q.text, q.kind, q.recency, q.tag) for q in plan]
    assert got[0] == ("sweatpants overalls", "news", "week", "config")
    assert got[1:4] == [('"Swoveralls" reviews', "web", None, "brand"),
                        ('"Swoveralls" competitors', "web", None, "brand"),
                        ('"Swoveralls" TikTok', "web", None, "brand")]
    assert ("Acme Wear reviews", "web", None, "competitor:Acme Wear") in got
    assert ("Acme Wear", "news", "month", "competitor:Acme Wear") in got
    assert ("Bolt reviews", "web", None, "competitor:Bolt") in got
    assert ("overalls outfits", "web", None, "topic:overalls outfits") in got
    assert ("overalls outfits trends", "news", "month", "topic:overalls outfits") in got
    assert len(got) == 1 + 3 + 4 + 2


def test_duplicate_queries_are_dropped_whatever_the_spacing_or_case(tmp_path, swov, settings):
    root = write_research(tmp_path, swov, {
        "queries": ["Overalls  Trends", "overalls trends", {"q": "overalls trends", "kind": "news"}],
        "topics": ["overalls trends"]})
    plan = R.plan_queries(swov, R.load_brand_research(swov, root), settings)
    texts = [(q.text, q.kind) for q in plan]
    assert texts.count(("Overalls Trends", "web")) == 1 and ("overalls trends", "web") not in texts
    assert ("overalls trends", "news") in texts and ("overalls trends trends", "news") in texts


def test_automatic_searches_can_be_switched_off(tmp_path, swov, settings):
    root = write_research(tmp_path, swov, {
        "competitors": ["Acme"], "topics": ["t"], "queries": ["only this"],
        "auto": {"brand": False, "competitors": False, "topics": False}})
    plan = R.plan_queries(swov, R.load_brand_research(swov, root), settings)
    assert [q.text for q in plan] == ["only this"]


def test_templates_can_be_replaced_and_use_the_brand_name_and_domain(tmp_path, swov, settings):
    root = write_research(tmp_path, swov, {
        "competitors": [{"name": "Acme", "domain": "acme.example"}],
        "templates": {"brand": ["{brand} outlet"], "competitor": ["{name} vs {brand} {domain}"]},
        "auto": {"topics": False}})
    plan = R.plan_queries(swov, R.load_brand_research(swov, root), settings)
    assert [q.text for q in plan] == ["Swoveralls outlet", "Acme vs Swoveralls acme.example"]


def test_a_brand_can_set_its_own_country_and_the_default_recency_applies(tmp_path, swov, settings):
    root = write_research(tmp_path, swov, {"country": "IN"})
    s = replace(settings, recency="month")
    plan = R.plan_queries(swov, R.load_brand_research(swov, root), s)
    assert {q.country for q in plan} == {"in"} and {q.recency for q in plan} == {"month"}


def test_an_unknown_placeholder_is_a_clear_error(tmp_path, swov, settings):
    root = write_research(tmp_path, swov, {"templates": {"brand": ["{colour} things"]}})
    with pytest.raises(R.ResearchError, match="placeholder"):
        R.plan_queries(swov, R.load_brand_research(swov, root), settings)


@pytest.mark.parametrize("data,match", [
    ({"competitor": ["Acme"]}, "unknown 'competitor'"),
    ({"competitors": [{"domain": "x.com"}]}, "needs a name"),
    ({"competitors": [{"name": "A", "colour": "red"}]}, "unknown 'colour'"),
    ({"topics": "overalls"}, "topics"),
    ({"topics": [""]}, "topics"),
    ({"queries": "nope"}, "queries"),
    ({"queries": [{"kind": "web"}]}, "needs a q"),
    ({"queries": [{"q": "x", "kind": "images"}]}, "kind"),
    ({"queries": [{"q": "x", "recency": "decade"}]}, "recency"),
    ({"queries": [{"q": "x", "depth": "deep"}]}, "depth"),
    ({"auto": {"brand": "yes"}}, "true or false"),
    ({"auto": {"colours": True}}, "unknown 'colours'"),
    ({"templates": {"brand": "x"}}, "must be a list"),
    ({"templates": {"colour": []}}, "unknown 'colour'"),
    ({"country": "usa"}, "country"),
])
def test_bad_brand_research_files_are_rejected(tmp_path, swov, data, match):
    root = write_research(tmp_path, swov, data)
    with pytest.raises(R.ResearchError, match=match):
        R.load_brand_research(swov, root)


def test_every_shipped_brand_research_file_loads_and_plans_the_brand_searches(brands, settings):
    for slug, brand in brands.items():
        plan = R.plan_queries(brand, R.load_brand_research(brand, "brands"), settings)
        assert len(plan) == 3 and all(brand.name in q.text for q in plan), slug


# --- routing and budgets ---------------------------------------------------------------------

def test_web_searches_go_to_serper_dev_and_news_to_tavily(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a web one") + explicit("a news one", kind="news"),
                                           "auto": {"brand": False}})
    http, found = world()
    result = go(store, [swov], found, settings, root=root)
    assert used_providers(http) == ["serper_dev", "tavily"]
    assert [(q.text, name) for q, name, _, _ in result.brands["swoveralls"].searched] == [
        ("a web one", "serper_dev"), ("a news one", "tavily")]
    tavily_call = next(c for c in http.calls if "tavily.com/search" in c["url"])
    assert tavily_call["json"]["topic"] == "news" and tavily_call["json"]["query"] == "a news one"


def test_searches_fall_back_to_the_next_provider_when_one_is_at_its_cap(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("q1", "q2", "q3"), "auto": {"brand": False}})
    http, found = world()
    result = go(store, [swov], found, with_caps(settings, serper_dev=(2, None, 0)), root=root)
    assert used_providers(http) == ["serper_dev", "serper_dev", "tavily"]
    assert not result.brands["swoveralls"].skipped


def test_when_every_provider_is_capped_the_search_is_skipped_not_forced(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("q1", "q2", "q3"), "auto": {"brand": False}})
    http, found = world(only=["serper_dev"])
    result = go(store, [swov], found, with_caps(settings, serper_dev=(2, None, 0)), root=root)
    rep = result.brands["swoveralls"]
    assert len(rep.searched) == 2 and [q.text for q, _ in rep.skipped] == ["q3"]
    assert len(searches_only(http)) == 2 and "out of credits" in rep.skipped[0][1]
    assert not result.problems                     # running out under a cap is not a failure


def test_a_provider_that_was_not_ticked_is_never_used(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a") + explicit("b", kind="news"),
                                           "auto": {"brand": False}})
    http, found = world(only=["serpapi"])
    go(store, [swov], found, settings, root=root)
    assert used_providers(http) == ["serpapi", "serpapi"]
    assert searches_only(http)[1]["params"]["tbm"] == "nws" and "tbm" not in searches_only(http)[0]["params"]


def test_with_no_provider_for_a_search_it_is_skipped_with_that_reason(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a"), "auto": {"brand": False}})
    s = replace(settings, routing={**settings.routing, "web": ["tavily"]})
    http, found = world(only=["serper_dev"])
    result = go(store, [swov], found, s, root=root)
    (q, reason), = result.brands["swoveralls"].skipped
    assert "no provider available" in reason and searches_only(http) == []


def test_the_lifetime_cap_of_a_one_time_pool_is_respected_across_months(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b", "c"), "auto": {"brand": False}})
    store.save_usage({"months": {"2026-07": {"serper_dev": 2398}}, "lifetime": {"serper_dev": 2398}})
    http, found = world(only=["serper_dev"])
    result = go(store, [swov], found, settings, root=root)
    assert len(result.brands["swoveralls"].searched) == 2          # 2400 - 2398
    assert store.load_usage()["lifetime"]["serper_dev"] == 2400


def test_a_new_month_gets_a_fresh_monthly_allowance(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b"), "auto": {"brand": False}})
    store.save_usage({"months": {"2026-09": {"serper_dev": 250}}, "lifetime": {"serper_dev": 250}})
    http, found = world(only=["serper_dev"])
    result = go(store, [swov], found, settings, root=root)
    assert len(result.brands["swoveralls"].searched) == 2
    usage = store.load_usage()
    assert usage["months"]["2026-10"]["serper_dev"] == 2 and usage["months"]["2026-09"]["serper_dev"] == 250


def test_an_advanced_tavily_search_costs_two_credits(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": [{"q": "deep one", "kind": "news", "depth": "advanced"}],
                                           "auto": {"brand": False}})
    http, found = world(only=["tavily"])
    result = go(store, [swov], found, settings, root=root)
    assert store.load_usage()["months"]["2026-10"]["tavily"] == 2
    assert result.brands["swoveralls"].searched[0][2] == 2
    assert http.calls[-1]["json"]["search_depth"] == "advanced"


def test_an_advanced_search_is_skipped_when_only_one_credit_is_left(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": [{"q": "deep", "kind": "news", "depth": "advanced"}],
                                           "auto": {"brand": False}})
    http, found = world(only=["tavily"])
    result = go(store, [swov], found, with_caps(settings, tavily=(1, None, 0)), root=root)
    assert len(result.brands["swoveralls"].skipped) == 1 and searches_only(http) == []


def test_the_balance_a_provider_reports_limits_spending_too(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("n1", "n2", "n3", "n4", kind="news"),
                                           "auto": {"brand": False}})
    http, found = world(tavily_used=948, tavily_limit=1000)      # 52 left, keep_free 50 -> 2 spendable
    result = go(store, [swov], found, settings, root=root)
    assert used_providers(http) == ["tavily", "tavily", "serper_dev", "serper_dev"]
    assert result.reported["tavily"] == 50


def test_when_the_balance_check_fails_the_local_count_is_used(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("n1", kind="news"), "auto": {"brand": False}})
    http, found = world(only=["tavily"])
    http.routes = [r for r in http.routes if "usage" not in r[1]]
    http.on("GET", "https://api.tavily.com/usage", (500, "down"))
    result = go(store, [swov], found, settings, root=root)
    assert any("could not read tavily's own balance" in n for n in result.notes)
    assert len(result.brands["swoveralls"].searched) == 1 and not result.problems


def test_a_rejected_key_found_at_the_balance_check_switches_the_provider_off(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("n1", kind="news"), "auto": {"brand": False}})
    http, found = world()
    http.routes = [r for r in http.routes if "usage" not in r[1]]
    http.on("GET", "https://api.tavily.com/usage", (401, {"detail": {"error": "Unauthorized"}}))
    http.on("GET", "https://serpapi.com/account", (200, {"total_searches_left": 250}))
    result = go(store, [swov], found, settings, root=root)
    assert result.problems and result.notes_with_problems == ["tavily"]
    assert used_providers(http) == ["serper_dev"]                       # fell back
    assert any("tavily switched off" in n for n in result.notes)


# --- remembering what was searched -----------------------------------------------------------

def test_a_repeat_within_the_cache_window_costs_nothing(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b"), "auto": {"brand": False}})
    http, found = world()
    go(store, [swov], found, settings, root=root)
    before = len(searches_only(http))
    result = go(store, [swov], found, settings, root=root, now="2026-10-25T07:00:00Z")
    rep = result.brands["swoveralls"]
    assert len(rep.cached) == 2 and not rep.searched and len(searches_only(http)) == before
    assert store.load_usage()["months"]["2026-10"]["serper_dev"] == 2


def test_after_the_window_the_search_is_paid_for_again(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a"), "auto": {"brand": False}})
    http, found = world()
    go(store, [swov], found, settings, root=root)
    result = go(store, [swov], found, settings, root=root, now="2026-10-27T07:00:01Z")
    assert len(result.brands["swoveralls"].searched) == 1 and len(searches_only(http)) == 2


def test_a_different_kind_recency_or_country_is_a_different_search(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": [
        {"q": "robes"}, {"q": "robes", "kind": "news"}, {"q": "robes", "recency": "week"}],
        "auto": {"brand": False}})
    http, found = world()
    result = go(store, [swov], found, settings, root=root)
    assert len(result.brands["swoveralls"].searched) == 3 and not result.brands["swoveralls"].cached
    other = write_research(tmp_path / "x", swov, {"queries": ["robes"], "country": "gb", "auto": {"brand": False}})
    again = go(store, [swov], found, settings, root=other)
    assert len(again.brands["swoveralls"].searched) == 1


def test_a_news_search_is_never_answered_from_an_earlier_web_search(tmp_path, store, swov, settings):
    http, found = world()
    web = write_research(tmp_path / "w", swov, {"queries": ["robes"], "auto": {"brand": False}})
    news = write_research(tmp_path / "n", swov, {"queries": explicit("robes", kind="news"),
                                                 "auto": {"brand": False}})
    go(store, [swov], found, settings, root=web)
    result = go(store, [swov], found, settings, root=news, now="2026-10-21T07:00:00Z")
    rep = result.brands["swoveralls"]
    assert len(rep.searched) == 1 and not rep.cached
    assert [r["kind"] for r in store.read_searches("swoveralls")] == ["web", "news"]
    back = go(store, [swov], found, settings, root=web, now="2026-10-22T07:00:00Z")
    assert len(back.brands["swoveralls"].cached) == 1


def test_a_cache_window_of_zero_always_searches(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a"), "auto": {"brand": False}})
    http, found = world()
    s = replace(settings, cache_days=0)
    go(store, [swov], found, s, root=root)
    result = go(store, [swov], found, s, root=root)
    assert len(result.brands["swoveralls"].searched) == 1


def test_reused_searches_do_not_count_against_the_run_limit(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b", "c"), "auto": {"brand": False}})
    http, found = world()
    go(store, [swov], found, settings, root=root, max_queries=1)
    result = go(store, [swov], found, settings, root=root, max_queries=1, now="2026-10-21T07:00:00Z")
    rep = result.brands["swoveralls"]
    assert len(rep.cached) == 1 and len(rep.searched) == 1 and len(rep.deferred) == 1


def test_failed_searches_are_not_remembered_so_they_are_retried(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a"), "auto": {"brand": False}})
    broken = FakeHttp().on("POST", "https://google.serper.dev/search", (500, "down"))
    found = P.build_providers(KEYS, http=broken, sleep=lambda s: None)
    result = go(store, [swov], {"serper_dev": found["serper_dev"]}, settings, root=root)
    assert result.problems and store.read_searches("swoveralls") == []
    http, ok = world()
    again = go(store, [swov], ok, settings, root=root)
    assert len(again.brands["swoveralls"].searched) == 1


# --- limits, failures, and what gets saved ---------------------------------------------------

def test_the_run_limit_defers_the_rest_to_the_next_run(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b", "c", "d"), "auto": {"brand": False}})
    http, found = world()
    result = go(store, [swov], found, replace(settings, max_queries_per_run=2), root=root)
    rep = result.brands["swoveralls"]
    assert len(rep.searched) == 2 and [q.text for q in rep.deferred] == ["c", "d"]
    again = go(store, [swov], found, replace(settings, max_queries_per_run=2), root=root)
    assert [q.text for q, *_ in again.brands["swoveralls"].searched] == ["c", "d"]
    assert len(again.brands["swoveralls"].cached) == 2


def test_the_run_limit_is_shared_across_brands(tmp_path, store, swov, dude, settings):
    http, found = world()
    result = go(store, [swov, dude], found, settings, max_queries=4)
    assert len(result.brands["swoveralls"].searched) == 3
    assert len(result.brands["duderobe"].searched) == 1 and len(result.brands["duderobe"].deferred) == 2


def test_a_rejected_key_switches_that_provider_off_and_the_next_one_answers(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b"), "auto": {"brand": False}})
    http, found = world()
    http.routes = [r for r in http.routes if "serper.dev/search" not in r[1]]
    http.on("POST", "https://google.serper.dev/search", (403, {"message": "Unauthorized."}))
    result = go(store, [swov], found, settings, root=root)
    assert used_providers(http) == ["serper_dev", "tavily", "tavily"]   # one failed try, then never again
    assert result.problems and result.notes_with_problems == ["serper_dev"]
    assert store.load_usage()["months"]["2026-10"] == {"tavily": 2}


def test_running_out_of_credits_mid_run_moves_on_without_trying_again(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b", "c"), "auto": {"brand": False}})
    http, found = world()
    http.routes = [r for r in http.routes if "serper.dev/search" not in r[1]]
    http.on("POST", "https://google.serper.dev/search",
            (200, serper_web(2)), (400, {"message": "Not enough credits"}))
    result = go(store, [swov], found, settings, root=root)
    assert used_providers(http) == ["serper_dev", "serper_dev", "tavily", "tavily"]
    assert any("serper_dev is out of credits" in n for n in result.notes)
    assert not result.problems                       # out of credits is not a broken key
    assert store.load_usage()["months"]["2026-10"] == {"serper_dev": 1, "tavily": 2}


def test_a_temporary_failure_falls_through_to_the_next_provider(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a"), "auto": {"brand": False}})
    http, found = world()
    http.routes = [r for r in http.routes if "serper.dev/search" not in r[1]]
    http.on("POST", "https://google.serper.dev/search", (503, "down"))
    result = go(store, [swov], found, settings, root=root)
    (q, name, cost, count), = result.brands["swoveralls"].searched
    assert name == "tavily" and not result.problems
    assert store.load_usage()["months"]["2026-10"] == {"tavily": 1}      # the failed try was free


def test_when_every_provider_fails_the_search_is_reported_failed(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a"), "auto": {"brand": False}})
    http = FakeHttp()
    for url in ("https://google.serper.dev/search", "https://api.tavily.com/search"):
        http.on("POST", url, (503, "down"))
    http.on("GET", "https://api.tavily.com/usage", (200, {"key": {"usage": 0, "limit": 1000}}))
    http.on("GET", "https://serpapi.com/search.json", (503, "down"))
    http.on("GET", "https://serpapi.com/account", (200, {"total_searches_left": 250}))
    found = P.build_providers(KEYS, http=http, sleep=lambda s: None)
    result = go(store, [swov], found, settings, root=root)
    (q, reason), = result.brands["swoveralls"].failed
    assert "serper_dev" in reason and "tavily" in reason and "serpapi" in reason and result.problems
    assert store.load_usage()["months"].get("2026-10", {}) == {}


def test_earlier_work_is_saved_even_if_a_later_search_blows_up(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b"), "auto": {"brand": False}})
    http, found = world(only=["serper_dev"])
    calls = {"n": 0}
    real = found["serper_dev"].search

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("unexpected")
        return real(*args, **kwargs)
    found["serper_dev"].search = flaky
    with pytest.raises(RuntimeError):
        go(store, [swov], found, settings, root=root)
    assert [r["query"] for r in store.read_searches("swoveralls")] == ["a"]
    assert store.load_usage()["months"]["2026-10"] == {"serper_dev": 1}


def test_each_search_is_saved_with_what_is_needed_to_reuse_and_audit_it(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": [{"q": "robes", "kind": "news", "recency": "week"}],
                                           "auto": {"brand": False}})
    http, found = world()
    result = go(store, [swov], found, settings, root=root)
    (rec,) = store.read_searches("swoveralls")
    assert rec["at"] == NOW and rec["run"] == result.run_id and rec["query"] == "robes"
    assert (rec["kind"], rec["recency"], rec["country"], rec["depth"], rec["tag"]) == (
        "news", "week", "us", "basic", "config")
    assert rec["provider"] == "tavily" and rec["credits"] == 1 and len(rec["key"]) == 16
    assert rec["results"][0] == {"rank": 1, "title": "t page 1", "url": "https://t1.example.com/page",
                                 "snippet": "t content 1", "date": "2026-10-18"}
    (run_rec,) = store.read_research_runs("swoveralls")
    assert run_rec["planned"] == 1 and run_rec["searched"] == 1 and run_rec["credits"] == {"tavily": 1}


def test_a_search_that_returns_nothing_is_still_saved_and_reused(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("zzzz"), "auto": {"brand": False}})
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (200, {"organic": []}))
    found = P.build_providers(KEYS, http=http, sleep=lambda s: None)
    go(store, [swov], {"serper_dev": found["serper_dev"]}, settings, root=root)
    (rec,) = store.read_searches("swoveralls")
    assert rec["results"] == [] and rec["credits"] == 1
    again = go(store, [swov], {"serper_dev": found["serper_dev"]}, settings, root=root)
    assert len(again.brands["swoveralls"].cached) == 1


# --- dry run ---------------------------------------------------------------------------------

def test_a_dry_run_searches_nothing_and_saves_nothing(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b"), "auto": {"brand": False}})
    http, found = world()
    result = go(store, [swov], found, settings, root=root, dry_run=True)
    assert searches_only(http) == []
    assert not (tmp_path / "data").exists()
    assert [(q.text, name) for q, name, _, count in result.brands["swoveralls"].searched] == [
        ("a", "serper_dev"), ("b", "serper_dev")]
    assert all(count is None for *_, count in result.brands["swoveralls"].searched)
    assert "would be searched" in R.format_research_report(result) and "dry run" in R.format_research_report(result)


def test_a_dry_run_applies_the_caps_and_fallbacks_exactly_like_a_real_run(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b", "c"), "auto": {"brand": False}})
    http, found = world()
    s = with_caps(settings, serper_dev=(2, None, 0))
    preview = go(store, [swov], found, s, root=root, dry_run=True)
    real = go(store, [swov], found, s, root=root)
    assert [(q.text, n) for q, n, *_ in preview.brands["swoveralls"].searched] == [
        (q.text, n) for q, n, *_ in real.brands["swoveralls"].searched]


def test_a_dry_run_does_not_change_the_credit_ledger(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a"), "auto": {"brand": False}})
    store.save_usage({"months": {"2026-10": {"serper_dev": 5}}, "lifetime": {"serper_dev": 5}})
    http, found = world()
    go(store, [swov], found, settings, root=root, dry_run=True)
    assert store.load_usage() == {"months": {"2026-10": {"serper_dev": 5}}, "lifetime": {"serper_dev": 5}}


# --- brands stay separate, credits are shared ----------------------------------------------

def test_each_brands_findings_stay_in_its_own_folder_but_credits_are_shared(store, swov, dude, settings):
    http, found = world()
    go(store, [swov, dude], found, settings)
    assert {r["query"] for r in store.read_searches("swoveralls")} == {
        '"Swoveralls" reviews', '"Swoveralls" competitors', '"Swoveralls" TikTok'}
    assert {r["query"] for r in store.read_searches("duderobe")} == {
        '"DudeRobe" reviews', '"DudeRobe" competitors', '"DudeRobe" TikTok'}
    assert store.load_usage()["months"]["2026-10"]["serper_dev"] == 6


def test_the_same_words_searched_for_two_brands_are_searched_twice(tmp_path, store, swov, dude, settings):
    http, found = world()
    configs = {b.slug: {"competitors": [], "topics": [], "queries": explicit("overalls trend"),
                        "templates": R.DEFAULT_TEMPLATES,
                        "auto": {"brand": False, "competitors": False, "topics": False}, "country": None}
               for b in (swov, dude)}
    go(store, [swov, dude], found, settings, configs=configs)
    assert len(searches_only(http)) == 2


# --- the report ------------------------------------------------------------------------------

def test_the_report_says_what_ran_what_it_cost_and_what_is_left(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("alpha") + explicit("beta", kind="news"),
                                           "auto": {"brand": False}})
    http, found = world(tavily_used=100, tavily_limit=1000)
    text = R.format_research_report(go(store, [swov], found, settings, root=root))
    assert "Providers in use: serpapi, serper_dev, tavily" in text
    assert '[web] "alpha" -> serper_dev (1 credit, 3 results)' in text
    assert '[news] "beta" -> tavily (1 credit, 3 results)' in text
    assert "serper_dev: 1/250; lifetime 1/2400" in text and "tavily: 1/600" in text and "serpapi: 0/100" in text
    assert "Provider-reported credits left: serpapi 250, tavily 899" in text


def test_the_report_lists_skips_failures_and_deferrals(tmp_path, store, swov, settings):
    root = write_research(tmp_path, swov, {"queries": explicit("a", "b", "c"), "auto": {"brand": False}})
    http, found = world(only=["serper_dev"])
    s = replace(with_caps(settings, serper_dev=(1, None, 0)), max_queries_per_run=2)
    text = R.format_research_report(go(store, [swov], found, s, root=root))
    assert 'skipped [web] "b": out of credits under your caps' in text
    assert "left for next run: 1" in text


# --- sources: one row per distinct page --------------------------------------------------------

def rec(query, urls, at, provider="serper_dev", tag="brand", titles=None, snippets=None):
    return {"at": at, "run": "r", "key": query, "query": query, "kind": "web", "recency": None,
            "country": "us", "depth": "basic", "tag": tag, "provider": provider, "credits": 1,
            "results": [{"rank": i, "title": (titles or {}).get(u, f"title of {u}"), "url": u,
                         "snippet": (snippets or {}).get(u, "snip"), "date": None}
                        for i, u in enumerate(urls, start=1)]}


def test_sources_are_one_row_per_page_with_when_and_how_often_it_was_seen():
    rows = R.research_sources([
        rec("q1", ["https://b.example.com/y", "https://www.a.example.com/x"], "2026-10-01T00:00:00Z"),
        rec("q2", ["https://zzz.example.com/z", "https://b.example.com/y"], "2026-10-08T00:00:00Z",
            provider="tavily", tag="topic:t")])
    a, b = rows[R.source_id("https://www.a.example.com/x")], rows[R.source_id("https://b.example.com/y")]
    assert len(rows) == 3 and a["domain"] == "a.example.com" and a["times_seen"] == 1
    assert (b["first_seen"], b["last_seen"], b["times_seen"]) == ("2026-10-01T00:00:00Z", "2026-10-08T00:00:00Z", 2)
    assert b["providers"] == "serper_dev; tavily" and b["purposes"] == "brand; topic:t"
    assert b["queries"] == "q1 | q2" and b["best_rank"] == 1 and a["best_rank"] == 2


def test_the_same_page_with_tracking_noise_is_one_source():
    urls = ["https://x.example.com/p/", "https://X.example.com/p?utm_source=a&utm_medium=b",
            "https://x.example.com/p#section", "https://x.example.com/p?fbclid=zzz"]
    rows = R.research_sources([rec("q", urls[:2], "2026-10-01T00:00:00Z"), rec("q2", urls[2:], "2026-10-02T00:00:00Z")])
    assert len(rows) == 1 and next(iter(rows.values()))["times_seen"] == 2
    assert len({R.source_id(u) for u in urls}) == 1
    assert R.source_id("https://x.example.com/p?id=1") != R.source_id("https://x.example.com/p?id=2")


def test_a_page_listed_twice_in_one_search_counts_once():
    rows = R.research_sources([rec("q", ["https://a.example.com/x", "https://a.example.com/x/"],
                                   "2026-10-01T00:00:00Z")])
    assert next(iter(rows.values()))["times_seen"] == 1


def test_the_newest_title_and_snippet_win_and_snippets_are_trimmed():
    url = "https://a.example.com/x"
    rows = R.research_sources([
        rec("q", [url], "2026-10-01T00:00:00Z", titles={url: "Old"}, snippets={url: "old"}),
        rec("q", [url], "2026-10-08T00:00:00Z", titles={url: "New"}, snippets={url: "n" * 500})])
    row = rows[R.source_id(url)]
    assert row["title"] == "New" and row["snippet"] == "n" * 300


def test_source_ids_are_text_that_a_spreadsheet_cannot_turn_into_a_number():
    for n in range(200):
        sid = R.source_id(f"https://example.com/{n}")
        assert sid.startswith("s_") and len(sid) == 14


def test_sources_ignore_records_that_are_not_searches():
    assert R.research_sources([{"at": "x", "query": "q", "results": [{"url": "https://a.com", "rank": 1}]}]) == {}


# --- the command line ------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    http = standard_routes(FakeHttp())
    monkeypatch.setattr(P, "default_http", http)
    monkeypatch.setattr(P.time, "sleep", lambda seconds: None)
    for k in ("TAVILY_API_KEY", "SERPER_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)

    def go_cli(*args, keys=KEYS):
        for k, v in keys.items():
            monkeypatch.setenv(k, v)
        return cli.main(["research", "--data-dir", str(tmp_path / "data"),
                         "--brands-dir", str(ROOT_BRANDS), "--research-config", "research.yaml", *args])
    return http, go_cli



def test_cli_without_any_key_skips_politely(cli_env, capsys):
    http, run = cli_env
    assert run("--all", keys={}) == 0
    out = capsys.readouterr().out
    assert "Research skipped" in out and "::warning::" in out and http.calls == []


def test_cli_runs_every_brand_and_prints_the_report(cli_env, capsys, tmp_path):
    http, run = cli_env
    assert run("--all") == 0
    out = capsys.readouterr().out
    assert "=== swoveralls ===" in out and "=== duderobe ===" in out and "=== defi_snacks ===" in out
    assert len(searches_only(http)) == 9
    assert (tmp_path / "data" / "swoveralls" / "research" / "searches.jsonl").is_file()
    assert (tmp_path / "data" / "_shared" / "research_usage.json").is_file()


def test_cli_dry_run_writes_nothing(cli_env, capsys, tmp_path):
    http, run = cli_env
    assert run("--brand", "swoveralls", "--dry-run") == 0
    assert "dry run" in capsys.readouterr().out
    assert searches_only(http) == [] and not (tmp_path / "data").exists()


def test_cli_provider_choice_is_respected(cli_env, capsys):
    http, run = cli_env
    assert run("--brand", "swoveralls", "--providers", "tavily") == 0
    assert used_providers(http) == ["tavily"] * 3


def test_cli_warns_when_a_chosen_provider_has_no_key(cli_env, capsys):
    http, run = cli_env
    assert run("--brand", "swoveralls", "--providers", "tavily,serpapi", keys={"SERPAPI_API_KEY": "k"}) == 0
    out = capsys.readouterr().out
    assert "tavily was asked for but its API key is not set" in out
    assert used_providers(http) == ["serpapi"] * 3


def test_cli_rejects_an_unknown_provider_name(cli_env, capsys):
    http, run = cli_env
    assert run("--brand", "swoveralls", "--providers", "bing") == 1
    assert "unknown provider bing" in capsys.readouterr().out and http.calls == []


def test_cli_max_queries_limits_the_run_and_warns_about_the_rest(cli_env, capsys):
    http, run = cli_env
    assert run("--brand", "swoveralls", "--max-queries", "1") == 0
    out = capsys.readouterr().out
    assert len(searches_only(http)) == 1 and "::warning::swoveralls: 0 searches skipped for credits, 2 left" in out


def test_cli_exits_with_an_error_when_a_key_is_rejected_and_never_prints_it(cli_env, capsys):
    http, run = cli_env
    http.routes = [r for r in http.routes if "serper.dev/search" not in r[1]]
    http.on("POST", "https://google.serper.dev/search", (403, {"message": "Unauthorized."}))
    assert run("--brand", "swoveralls") == 1
    out = capsys.readouterr().out
    assert "::error::serper_dev: the API key was rejected" in out and "k-serper" not in out


def test_cli_exits_with_an_error_when_searches_fail(cli_env, capsys):
    http, run = cli_env
    http.routes = [r for r in http.routes if "search" not in r[1] or "usage" in r[1]]
    for url in ("https://google.serper.dev/search", "https://api.tavily.com/search"):
        http.on("POST", url, (503, "down"))
    http.on("GET", "https://serpapi.com/search.json", (503, "down"))
    assert run("--brand", "swoveralls") == 1
    assert "::error::swoveralls: 3 searches failed" in capsys.readouterr().out


def test_cli_reports_a_broken_research_file_plainly(cli_env, capsys, monkeypatch, tmp_path):
    http, run = cli_env
    bad = tmp_path / "bad_research.yaml"
    bad.write_text("defaults:\n  results_per_query: 99\n")
    monkeypatch.setenv("TAVILY_API_KEY", "k")
    assert cli.main(["research", "--brand", "swoveralls", "--data-dir", str(tmp_path / "d"),
                     "--brands-dir", str(ROOT_BRANDS), "--research-config", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "Error:" in out and "results_per_query" in out and http.calls == []


def test_no_api_key_ever_reaches_a_saved_file_or_the_report(cli_env, capsys, tmp_path):
    http, run = cli_env
    http.routes = [r for r in http.routes if "serpapi.com/search" not in r[1]]
    http.on("GET", "https://serpapi.com/search.json", (200, {"organic_results": [
        {"position": 1, "title": "t", "link": "https://z.example.com", "snippet": "s"}]}))
    run("--brand", "swoveralls", "--providers", "serpapi")
    saved = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "data").rglob("*") if p.is_file())
    assert "k-serpapi" not in saved and "k-serpapi" not in capsys.readouterr().out


def test_the_workflow_uses_the_secret_names_and_options_the_command_understands():
    text = open(".github/workflows/research.yml", encoding="utf-8").read()
    for secret in ("TAVILY_API_KEY", "SERPER_API_KEY", "SERPAPI_API_KEY"):
        assert f"secrets.{secret}" in text
    for name in ("serper_dev", "tavily", "serpapi"):
        assert name in text
    args = cli.build_parser().parse_args([
        "research", "--all", "--providers", "serper_dev,tavily,serpapi", "--max-queries", "5", "--dry-run"])
    assert args.dry_run and args.max_queries == 5 and args.providers == "serper_dev,tavily,serpapi"
    assert "cron:" in text and "workflow_dispatch" in text
