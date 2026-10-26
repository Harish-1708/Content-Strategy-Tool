"""Where to look: worldwide by default, one country when asked, for the search services and for the Meta Ad Library.

Google has no real "worldwide" market, so worldwide means no country is sent (and the Ad Library is asked for ALL countries);
a country code narrows it. Tavily is sent no country by this tool at all.
"""
from dataclasses import replace

import pytest
import yaml

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import providers as P
from contentintel import research as R
from contentintel.store import Store

from fake_http import FakeHttp, serper_web, serpapi_web

WORDS = ["worldwide", "Worldwide", "WORLDWIDE", "all", "any", "global", " all "]


# ---- the search services ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", *WORDS])
def test_worldwide_sends_no_country(value):
    assert P.search_country(value) is None


@pytest.mark.parametrize("value,expected", [("us", "us"), ("US", "us"), (" gb ", "gb"), ("In", "in")])
def test_a_country_is_sent_as_a_lower_case_code(value, expected):
    assert P.search_country(value) == expected


@pytest.mark.parametrize("country", [None, *WORDS])
def test_serper_dev_sends_no_gl_for_worldwide(country):
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (200, serper_web(1)))
    P.SerperDev("k", http=http, sleep=lambda s: None).search("overalls", country=country)
    assert "gl" not in http.calls[0]["json"]


def test_serper_dev_sends_the_country_when_one_is_chosen():
    http = FakeHttp().on("POST", "https://google.serper.dev/search", (200, serper_web(1)))
    P.SerperDev("k", http=http, sleep=lambda s: None).search("overalls", country="GB")
    assert http.calls[0]["json"]["gl"] == "gb"


@pytest.mark.parametrize("country,expected", [(None, None), ("worldwide", None), ("us", "us"), ("AU", "au")])
def test_serpapi_sends_gl_only_for_a_country(country, expected):
    http = FakeHttp().on("GET", "https://serpapi.com/search.json", (200, serpapi_web(1)))
    P.SerpApi("k", http=http, sleep=lambda s: None).search("overalls", country=country)
    assert http.calls[0]["params"].get("gl") == expected


# ---- the settings ----------------------------------------------------------------------------------------------------------

def test_worldwide_is_the_default_in_the_built_in_settings_and_the_shipped_file():
    assert R.load_settings("does-not-exist.yaml").country == "worldwide"
    assert R.load_settings("research.yaml").country == "worldwide"
    assert "country: worldwide" in open("research.yaml", encoding="utf-8").read()


@pytest.mark.parametrize("value", WORDS)
def test_the_worldwide_words_are_all_read_as_worldwide(value):
    assert R._country(value, "x") == "worldwide"


@pytest.mark.parametrize("value,expected", [("us", "us"), ("GB", "gb"), (None, None)])
def test_a_code_or_nothing_is_read_as_it_is(value, expected):
    assert R._country(value, "x") == expected


@pytest.mark.parametrize("bad", ["usa", "u", "u1", 5, ["us"], True])
def test_anything_else_is_refused_naming_where(bad):
    with pytest.raises(R.ResearchError, match=r"defaults.country must be a two-letter country code such as us, or worldwide"):
        R._country(bad, "defaults.country")


def plan(swov, tmp_path, **kw):
    folder = tmp_path / "swoveralls"
    folder.mkdir(exist_ok=True)
    data = {"competitors": ["Acme"], **{k: v for k, v in kw.items() if k != "settings"}}
    (folder / "research.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    config = R.load_brand_research(swov, str(tmp_path))
    return R.plan_queries(swov, config, replace(R.load_settings("does-not-exist.yaml"), **kw.get("settings", {})))


def test_queries_are_worldwide_unless_the_brand_names_a_country(swov, tmp_path):
    assert {q.country for q in plan(swov, tmp_path)} == {"worldwide"}
    assert {q.country for q in plan(swov, tmp_path, country="gb")} == {"gb"}


def test_a_run_override_beats_the_brands_country_and_the_default(swov, tmp_path):
    assert {q.country for q in plan(swov, tmp_path, country="gb", settings={"region_override": "au"})} == {"au"}
    assert {q.country for q in plan(swov, tmp_path, country="gb", settings={"region_override": "worldwide"})} == {"worldwide"}


def test_the_same_search_for_two_countries_is_two_searches_not_one_cached_one(swov, tmp_path):
    worldwide = {R.cache_key(q) for q in plan(swov, tmp_path)}
    gb = {R.cache_key(q) for q in plan(swov, tmp_path, settings={"region_override": "gb"})}
    assert worldwide and gb and not (worldwide & gb)


# ---- the Meta Ad Library -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", WORDS)
def test_worldwide_asks_the_ad_library_for_all_countries(value):
    assert C.ads_country(value, "x") == "ALL"


@pytest.mark.parametrize("value,expected", [("us", "US"), (" gb ", "GB"), ("AU", "AU")])
def test_a_country_is_upper_case_for_the_ad_library(value, expected):
    assert C.ads_country(value, "x") == expected


@pytest.mark.parametrize("bad", ["usa", "u", "u1", "", 5, None, ["us"]])
def test_a_bad_ad_country_names_where(bad):
    with pytest.raises(C.CollectError, match=r"swoveralls/research.yaml ads.country must be a two-letter code such as us, or worldwide"):
        C.ads_country(bad, "swoveralls/research.yaml ads.country")


def test_a_brand_with_no_country_searches_ads_worldwide():
    assert C.parse_collect_config({"ads": {"keywords": ["a"]}}, "f")["ads"]["country"] == "ALL"
    assert C.parse_collect_config({"ads": {"country": "gb"}}, "f")["ads"]["country"] == "GB"
    assert C.parse_collect_config({"ads": {"country": "worldwide"}}, "f")["ads"]["country"] == "ALL"


@pytest.mark.parametrize("country,expected", [("us", "country=US"), ("US", "country=US"), ("all", "country=ALL"), ("ALL", "country=ALL")])
def test_the_ad_library_address_always_has_an_upper_case_country(country, expected):
    assert expected + "&q=x" in C.ad_library_url("x", country, "active")
    assert expected + "&view_all_page_id=123456" in C.ad_library_page_url("123456", country, "active")


# ---- a run, and its override ------------------------------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def settings():
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


def collect_opened(tmp_path, swov, cfg, settings, **kw):
    from test_collectors import CREDS, NOW, world, write_research
    write_research(tmp_path, swov, {"track_own_brand": False, "ads": {"keywords": ["overalls"], **kw.pop("ads", {})}})
    f = world(cfg)
    C.run_collect([swov], Store(tmp_path / "data"), browser_factory=f, creds=CREDS, settings=settings, browser_cfg=cfg,
                  ledger_cls=R.Ledger, sources=["meta_ads"], now=NOW, run_id="r", root=str(tmp_path / "brands"), **kw)
    return f.browsers["meta_ads"].opened


def test_an_ad_search_is_worldwide_by_default(tmp_path, swov, cfg, settings):
    (url,) = collect_opened(tmp_path, swov, cfg, settings)
    assert "country=ALL&" in url


def test_the_brands_own_ad_country_is_used(tmp_path, swov, cfg, settings):
    (url,) = collect_opened(tmp_path, swov, cfg, settings, ads={"country": "gb"})
    assert "country=GB&" in url


def test_a_run_region_beats_the_brands_own_country(tmp_path, swov, cfg, settings):
    (url,) = collect_opened(tmp_path, swov, cfg, settings, ads={"country": "gb"}, region="au")
    assert "country=AU&" in url
    (url,) = collect_opened(tmp_path / "w", swov, cfg, settings, ads={"country": "gb"}, region="worldwide")
    assert "country=ALL&" in url


def test_a_bad_run_region_is_refused_before_anything_is_opened(tmp_path, swov, cfg, settings):
    with pytest.raises(C.CollectError, match="--region must be a two-letter code such as us, or worldwide"):
        collect_opened(tmp_path, swov, cfg, settings, region="usa")


# ---- the workflows -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flow,cmd", [("collect", "collect"), ("research", "research")])
def test_both_workflows_offer_a_region_and_pass_it_on(flow, cmd):
    wf = yaml.safe_load(open(f".github/workflows/{flow}.yml", encoding="utf-8"))
    region = wf[True]["workflow_dispatch"]["inputs"]["region"]
    assert region["required"] is False and region["default"] == "" and "worldwide" in region["description"]
    steps = wf["jobs"][flow]["steps"] if flow in wf["jobs"] else next(iter(wf["jobs"].values()))["steps"]
    step = next(s for s in steps if "REGION" in (s.get("env") or {}))
    assert step["env"]["REGION"] == "${{ inputs.region }}" and 'args+=(--region "$REGION")' in step["run"]
