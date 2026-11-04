"""Stage 1 in the real flows: collection, search, video, catalogue, the Idea Bank's file fetch, the sheet and the command line."""
import json
import shutil
from dataclasses import replace

import pytest

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel import contract as K
from contentintel import evidence as E
from contentintel import health as H
from contentintel import ideabank as IB
from contentintel import research as R
from contentintel import sheets
from contentintel import sync as sync_mod
from contentintel.drive import DriveClient
from contentintel.shopify_fetch import FetchError, fetch_products
from contentintel.store import Store

from conftest import BRANDS_DIR, raw_products
from fake_browser import FakeBrowser
from fake_drive import EMAIL, ROOT, FakeDrive
from fake_http import searches_only
from fake_sheets import FakeSpreadsheet, read
from media_fixtures import tiny_mp4
from stage1_helpers import SHIPPED as ROOT_SOURCES, NOW as NOW_HEALTH, at, shipped
from test_apify_backend import ADS_URL, ONE, apify_world
from test_collectors import BROWSER_YAML, CREDS, NOW, site, world, write_research
from test_ideabank import S, getter
from test_research import world as search_world
from test_video_outcomes import TIKTOK_REFUSED, posts, runner_saying

REG = shipped()


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


@pytest.fixture
def settings():
    return replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)


@pytest.fixture
def ledger():
    return H.HealthLedger({}, REG)


@pytest.fixture
def drive():
    return DriveClient(FakeDrive(), ROOT, EMAIL, sleep=lambda s: None)


def go(store, tmp_path, swov, cfg, settings, factory, *, health=None, registry=REG, now=NOW, sources=("meta_ads", "tiktok", "instagram"), backend="browser",
       apify=None, dry_run=False, data=ONE):
    write_research(tmp_path, swov, {"track_own_brand": False, **data})
    return C.run_collect([swov], store, browser_factory=factory, creds=CREDS, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger, sources=list(sources),
                         now=now, run_id="r", root=str(tmp_path / "brands"), dry_run=dry_run, apify=apify, backend=backend, health=health, registry=registry)


def entry(ledger, source, route):
    return ledger.state["routes"][f"{source}|{route}"]


def detail(result, source):
    return next(r for r in result.brands["swoveralls"] if r.source == source)


# ---- collection -------------------------------------------------------------------------------------------------------------

def test_each_route_that_runs_is_recorded_and_each_saved_batch_carries_its_provenance(store, tmp_path, swov, cfg, settings, ledger):
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), health=ledger)
    assert not result.problems
    for source in ("meta_ads", "tiktok", "instagram"):
        e = entry(ledger, source, "browser")
        assert e["history"][-1]["ok"] is True and e["history"][-1]["items"] >= 1 and e["last_success"] == NOW and e["consecutive_failures"] == 0
    records = store.read_collected("swoveralls")
    assert {r["source"] for r in records} == {"meta_ads", "tiktok", "instagram"}
    tiktok = next(r for r in records if r["source"] == "tiktok")
    assert tiktok["provenance"] == {"schema": 1, "route": "browser", "access_tier": "session_based", "access_type": "site_data", "collector": "tiktok.browser",
                                    "captured_at": NOW, "run": "r", "source_health": "unverified"}
    # and the real, normalised items of a real run become valid evidence
    evidence = E.evidence_from_collected(records, REG, NOW)
    assert len(evidence) >= 3 and all(E.validate_evidence(r, REG, NOW) == [] for r in evidence)
    assert {r["origin"]["source"] for r in evidence} == {"meta_ads", "tiktok", "instagram"}


def test_without_a_ledger_or_a_registry_collection_is_exactly_as_before(store, tmp_path, swov, cfg, settings):
    go(store, tmp_path, swov, cfg, settings, world(cfg), health=None, registry=None)
    assert all("provenance" not in r for r in store.read_collected("swoveralls"))
    assert not (tmp_path / "data" / "_shared" / "collector_health.json").exists()


def test_a_registry_alone_stamps_provenance_with_no_health_status(store, tmp_path, swov, cfg, settings):
    go(store, tmp_path, swov, cfg, settings, world(cfg), health=None, registry=REG)
    assert {r["provenance"]["source_health"] for r in store.read_collected("swoveralls")} == {""}


def test_a_dry_run_records_nothing(store, tmp_path, swov, cfg, settings, ledger):
    go(store, tmp_path, swov, cfg, settings, world(cfg), health=ledger, dry_run=True)
    assert ledger.state["routes"] == {} and store.read_collected("swoveralls") == []


def test_a_browser_that_will_not_start_is_recorded_as_broken_and_rests_briefly(store, tmp_path, swov, cfg, settings, ledger):
    factory = world(cfg, fail={"instagram": "chromium did not start"})
    result = go(store, tmp_path, swov, cfg, settings, factory, health=ledger)
    assert detail(result, "instagram").status == "failed"
    e = entry(ledger, "instagram", "browser")
    assert e["history"][-1]["code"] == K.UPSTREAM_BROKEN and "chromium did not start" in e["last_error"]["message"]
    assert ledger.is_paused("instagram", "browser", at(NOW, seconds=119)) and not ledger.is_paused("instagram", "browser", at(NOW, seconds=120))
    assert entry(ledger, "meta_ads", "browser")["history"][-1]["ok"] is True                      # the other sources are untouched


def test_a_login_wall_rests_the_route_and_the_next_run_does_not_open_the_browser(store, tmp_path, swov, cfg, settings, ledger):
    ig = site(cfg, "instagram")
    factory = world(cfg)
    factory.browsers["instagram"] = FakeBrowser({}, present_always={ig["login_wall_any"][0]})
    result = go(store, tmp_path, swov, cfg, settings, factory, health=ledger, sources=("instagram",))
    assert detail(result, "instagram").status == "failed"
    assert entry(ledger, "instagram", "browser")["history"][-1]["code"] == K.NEED_LOGIN
    assert ledger.is_paused("instagram", "browser", at(NOW, hours=11)) and not ledger.is_paused("instagram", "browser", at(NOW, hours=12))
    again = world(cfg)
    result = go(store, tmp_path, swov, cfg, settings, again, health=ledger, sources=("instagram",), now=at(NOW, hours=1))
    d = detail(result, "instagram")
    assert d.status == "skipped" and "resting until" in d.detail and "NEED_LOGIN" in d.detail and again.opened == []
    result = go(store, tmp_path, swov, cfg, settings, again, health=ledger, sources=("instagram",), now=at(NOW, hours=13))
    assert detail(result, "instagram").status == "ran" and again.opened == ["instagram"]                # the rest is over; the route works and is used


def test_an_account_warning_is_recorded_as_a_block_with_a_day_of_rest(store, tmp_path, swov, cfg, settings, ledger):
    ig = site(cfg, "instagram")
    factory = world(cfg)
    factory.browsers["instagram"] = FakeBrowser({"https://www.instagram.com/explore/tags/": {"present": [ig["warning_any"][0]], "text": "We suspect automated behavior"}},
                                                present_always={ig["logged_in_any"][0]})
    go(store, tmp_path, swov, cfg, settings, factory, health=ledger, sources=("instagram",))
    e = entry(ledger, "instagram", "browser")
    assert e["history"][-1]["code"] == K.PLATFORM_BLOCKED and ledger.is_paused("instagram", "browser", at(NOW, hours=23))
    assert not ledger.is_paused("instagram", "browser", at(NOW, hours=24))


def test_a_page_that_opens_but_gives_nothing_is_a_failure_not_a_success(store, tmp_path, swov, cfg, settings, ledger):
    factory = world(cfg)
    factory.browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["logged_in_any"][0]})
    go(store, tmp_path, swov, cfg, settings, factory, health=ledger, sources=("instagram",))
    h = entry(ledger, "instagram", "browser")["history"][-1]
    assert h["ok"] is False and h["code"] == K.UPSTREAM_BROKEN and "no ads or posts could be read" in entry(ledger, "instagram", "browser")["last_error"]["message"]


def test_running_out_of_apify_credit_rests_only_the_apify_route(store, tmp_path, swov, cfg, settings, ledger):
    http, apify = apify_world()
    http.routes = [r for r in http.routes if r[1] != ADS_URL]
    http.on("POST", ADS_URL, (402, {"error": {"message": "limit"}}))
    go(store, tmp_path, swov, cfg, settings, world(cfg), health=ledger, apify=apify, backend="apify", sources=("meta_ads",))
    e = entry(ledger, "meta_ads", "apify")
    assert e["history"][-1]["code"] == K.RATE_LIMITED and ledger.is_paused("meta_ads", "apify", at(NOW, minutes=59)) and not ledger.is_paused("meta_ads", "apify", at(NOW, hours=1))
    assert "meta_ads|browser" not in ledger.state["routes"]


def test_with_both_routes_a_resting_apify_does_not_stop_the_browser(store, tmp_path, swov, cfg, settings, ledger):
    http, apify = apify_world()
    ledger.observe("meta_ads", "apify", ok=False, code=K.RATE_LIMITED, message="429", now=NOW)
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), health=ledger, apify=apify, backend="fallback", sources=("meta_ads",), now=at(NOW, minutes=5))
    assert detail(result, "meta_ads").status == "ran" and not [c for c in http.calls if "apify" in c["url"]]


def test_a_check_row_carries_an_item_count_and_the_error_code(store, cfg, settings):
    rows = C.check_collectors(world(cfg), CREDS, settings, cfg, store, R.Ledger, NOW, ["instagram"])
    assert rows[0]["ok"] and rows[0]["items"] >= 1
    factory = world(cfg)
    factory.browsers["instagram"] = FakeBrowser({}, present_always={site(cfg, "instagram")["login_wall_any"][0]})
    bad = C.check_collectors(factory, CREDS, settings, cfg, store, R.Ledger, NOW, ["instagram"])[0]
    assert bad["ok"] is False and bad["code"] == K.NEED_LOGIN


def test_invisible_characters_never_enter_collected_text():
    row = C._row("meta_ads", "1", title="Big\u200b sale\ufeff", text="Nobody\u202e tells\u200d you\U000e0041", details="a\u200bb")
    assert row["title"] == "Big sale" and row["text"] == "Nobody tells you" and row["details"] == "ab"


# ---- search -----------------------------------------------------------------------------------------------------------------

def run_search(store, swov, providers, settings, ledger, **kw):
    return R.run_research([swov], store, providers=providers, settings=settings, now=NOW, root="brands-that-do-not-exist", health=ledger, **kw)


def test_search_services_are_recorded_by_what_they_answered(store, swov, ledger):
    http, providers = search_world()
    result = run_search(store, swov, providers, R.load_settings("does-not-exist.yaml"), ledger, max_queries=2)
    assert result.brands["swoveralls"].searched
    seen = {k: v for k, v in ledger.state["routes"].items() if k.startswith("web_search|")}
    assert seen and all(e["history"][-1]["ok"] is True and e["history"][-1]["items"] == 3 for e in seen.values())
    assert all(REG.route("web_search", e["route"]) for e in seen.values())                          # every observed name is a declared route


def test_a_service_that_errors_is_recorded_and_the_next_one_answers(store, swov, ledger):
    http, providers = search_world()
    for url in ("https://google.serper.dev/search", "https://api.tavily.com/search"):
        http.routes = [r for r in http.routes if r[1] != url]
        http.on("POST", url, (500, {"error": "boom"}))
    run_search(store, swov, providers, R.load_settings("does-not-exist.yaml"), ledger, max_queries=1)
    for name in ("serper_dev", "tavily"):
        if f"web_search|{name}" in ledger.state["routes"]:
            assert entry(ledger, "web_search", name)["history"][-1]["code"] == K.UPSTREAM_BROKEN
    assert any(e["history"][-1]["ok"] for k, e in ledger.state["routes"].items())


def test_a_service_that_is_resting_is_not_called(store, swov, ledger):
    http, providers = search_world()
    ledger.observe("web_search", "tavily", ok=False, code=K.RATE_LIMITED, message="429", now=NOW)
    run_search(store, swov, providers, R.load_settings("does-not-exist.yaml"), ledger, max_queries=6)
    assert not [c for c in searches_only(http) if "tavily" in c["url"]] and searches_only(http)


def test_when_every_service_is_resting_the_searches_wait_and_say_why(store, swov, ledger):
    http, providers = search_world()
    for name in providers:
        ledger.observe("web_search", name, ok=False, code=K.RATE_LIMITED, message="429", now=NOW)
    result = run_search(store, swov, providers, R.load_settings("does-not-exist.yaml"), ledger, max_queries=2)
    rep = result.brands["swoveralls"]
    assert searches_only(http) == [] and rep.skipped and all("resting after a failure" in why for _, why in rep.skipped)


def test_search_works_exactly_as_before_without_a_ledger(store, swov):
    http, providers = search_world()
    result = R.run_research([swov], store, providers=providers, settings=R.load_settings("does-not-exist.yaml"), now=NOW, root="brands-that-do-not-exist", max_queries=2)
    assert result.brands["swoveralls"].searched and not (store.data_dir / "_shared" / "collector_health.json").exists()


def test_the_provider_check_rows_carry_the_error_code(store):
    http, providers = search_world(only=["tavily"])
    http.routes = [r for r in http.routes if r[1] != "https://api.tavily.com/search"]
    http.on("POST", "https://api.tavily.com/search", (401, {"detail": "bad key"}))
    rows = R.check_providers(providers, store, R.load_settings("does-not-exist.yaml"), NOW)
    assert len(rows) == 2 and all(r["ok"] is False and r["code"] == K.NEED_SETUP for r in rows)         # the second kind is skipped as dead, and says why


# ---- video downloads --------------------------------------------------------------------------------------------------------

def one_video(drive, runner, ledger, now=NOW, platform="tiktok", ident="A"):
    bank = posts(ident, platform=platform)
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", drive, getter(), S)
    saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", drive, S, runner, health=ledger, now=now)
    return bank["items"]["IB-0001"], saved, lines


def test_a_refused_download_is_a_block_and_rests_the_platform_not_the_post(drive, ledger):
    runner = runner_saying(TIKTOK_REFUSED)
    item, saved, _ = one_video(drive, runner, ledger)
    assert item["video_status"] == "blocked" and saved == 0
    e = entry(ledger, "video_tiktok", "layered")
    assert e["history"][-1]["code"] == K.PLATFORM_BLOCKED and ledger.is_paused("video_tiktok", "layered", at(NOW, hours=23))
    assert "video_instagram|layered" not in ledger.state["routes"]
    _, _, lines = one_video(drive, runner, ledger, now=at(NOW, hours=2))                      # an hour later nothing is asked of TikTok again
    assert len(runner.calls) == 1 and any("video_tiktok (layered) is resting" in ln for ln in lines)
    one_video(drive, runner, ledger, now=at(NOW, hours=25))
    assert len(runner.calls) == 2


def test_a_saved_video_is_a_success_and_a_post_without_video_says_nothing_about_the_platform(drive, ledger):
    item, saved, _ = one_video(drive, runner_saying(tiny_mp4()), ledger)
    assert saved == 1 and item["video_status"] == "saved"
    e = entry(ledger, "video_tiktok", "layered")
    assert e["history"][-1]["ok"] is True and e["consecutive_successes"] == 1
    other = H.HealthLedger({}, REG)
    one_video(drive, runner_saying("There is no video in this post"), other, platform="instagram")
    h = entry(other, "video_instagram", "layered")
    assert h["history"][-1]["ok"] is None and h["pause"] is None and h["consecutive_failures"] == 0


def test_a_video_that_is_not_a_video_or_is_missing_is_recorded_correctly(drive, ledger):
    item, _, _ = one_video(drive, runner_saying(b"this is not a video at all, just bytes " * 50), ledger)
    assert item["video_status"] == "failed" and entry(ledger, "video_tiktok", "layered")["history"][-1]["code"] == K.UPSTREAM_BROKEN
    gone = H.HealthLedger({}, REG)
    one_video(drive, runner_saying("Video unavailable. This video is private"), gone)
    assert entry(gone, "video_tiktok", "layered")["history"][-1]["ok"] is None


def test_videos_are_fetched_exactly_as_before_without_a_ledger(drive):
    bank = posts("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", drive, getter(), S)
    runner = runner_saying(TIKTOK_REFUSED)
    for _ in range(2):
        IB.save_videos(bank, ["IB-0001"], "Swoveralls", drive, S, runner)
    assert len(runner.calls) == 2


# ---- files from the internet ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["https://127.0.0.1/x.jpg", "https://localhost/x.jpg", "https://10.0.0.5/x.jpg", "https://169.254.169.254/latest/meta-data/",
                                 "https://user:pw@cdn.example.com/x.jpg", "https://intranet/x.jpg", "https://metadata.google.internal/x"])
def test_a_media_address_that_is_not_public_is_never_opened(url):
    get = getter()
    with pytest.raises(IB.AssetSkip, match="not a public web address"):
        IB.fetch_media_checked(get, url, S)
    assert get.calls == []


def test_an_ordinary_public_media_address_is_still_fetched():
    get = getter()
    data, check = IB.fetch_media_checked(get, "https://scontent.cdninstagram.com/v/t51/pic.jpg?oe=6900ABCD", S)
    assert get.calls == ["https://scontent.cdninstagram.com/v/t51/pic.jpg?oe=6900ABCD"] and check.kind == "image" and data


# ---- the catalogue ----------------------------------------------------------------------------------------------------------

def fetched(swov):
    products = raw_products("swoveralls_full_72.json")
    pages = {1: products, 2: []}
    return fetch_products(swov.domain, getter=lambda url, params: {"products": pages.get(params["page"], [])}, delay=0, sleep=lambda s: None)


def test_a_catalogue_fetch_is_recorded(swov, store, ledger):
    result = sync_mod.run_sync(swov, store, fetcher=lambda domain: fetched(swov), now=NOW, health=ledger)
    assert result.count == 72
    e = entry(ledger, "shopify_catalogue", "public_json")
    assert e["history"][-1]["ok"] is True and e["history"][-1]["items"] == 72 and e["last_success"] == NOW


def test_a_catalogue_that_cannot_be_fetched_is_recorded_with_its_real_reason_and_still_blocks_the_sync(swov, store, ledger):
    def refuse(domain):
        raise FetchError("HTTP 429 for https://swoveralls.com/products.json")
    result = sync_mod.run_sync(swov, store, fetcher=refuse, now=NOW, health=ledger)
    assert result.state == "blocked"                                                                # the safeguard is unchanged
    assert entry(ledger, "shopify_catalogue", "public_json")["history"][-1]["code"] == K.RATE_LIMITED
    gone = H.HealthLedger({}, REG)
    sync_mod.run_sync(swov, store, fetcher=lambda d: (_ for _ in ()).throw(FetchError("404 Not Found for https://x/products.json")), now=NOW, health=gone)
    assert entry(gone, "shopify_catalogue", "public_json")["history"][-1]["ok"] is None             # a wrong domain is about the request, not the source


# ---- the sheet --------------------------------------------------------------------------------------------------------------

def test_the_sheet_gets_a_collector_health_tab_that_updates_in_place(swov, store, ledger):
    ledger.record_acceptance({"at": NOW, "run": "r", "source": "meta_ads", "route": "browser", "items": 4, "passed": True, "code": "", "message": "ok", "version": "abc",
                              "route_hash": ledger.route_hash("meta_ads", "browser")})
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(swov, store, sp, NOW, health_rows=ledger.rows(NOW))
    header, rows = read(sp.worksheet("Collector Health"))
    assert header[:len(H.SHEET_COLUMNS)] == H.SHEET_COLUMNS and len(rows) == 25
    by = {r["route_key"]: r for r in rows}
    assert by["meta_ads|browser"]["status"] == "healthy" and by["meta_ads|browser"]["acceptance_items"] == 4 and by["meta_ads|browser"]["acceptance_result"] == "pass"
    assert by["meta_ads|browser"]["success_rate_7d"] == 100 and by["meta_ads|browser"]["attempts_7d"] == 1
    assert by["tiktok|browser"]["status"] == "unverified" and by["instagram|agent_reach"]["status"] == "disabled"
    ledger.observe("tiktok", "browser", ok=False, code=K.RATE_LIMITED, message="429", now=at(NOW, hours=1))
    sheets.update_brand_sheet(swov, store, sp, at(NOW, hours=1), health_rows=ledger.rows(at(NOW, hours=1)))
    _, again = read(sp.worksheet("Collector Health"))
    assert len(again) == 25 and {r["route_key"]: r for r in again}["tiktok|browser"]["status"] == "paused"
    assert {r["route_key"]: r for r in again}["tiktok|browser"]["error_code"] == "RATE_LIMITED"


def test_no_health_rows_means_no_tab(swov, store):
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(swov, store, sp, NOW)
    assert "Collector Health" not in [ws.title for ws in sp.worksheets()]


def test_a_person_reordering_or_adding_columns_to_the_health_tab_is_respected(swov, store, ledger):
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(swov, store, sp, NOW, health_rows=ledger.rows(NOW))
    ws = sp.worksheet("Collector Health")
    from fake_sheets import insert_column
    insert_column(ws, 1, "My notes", {row: "note" for row in range(2, 27)})
    sheets.update_brand_sheet(swov, store, sp, at(NOW, hours=1), health_rows=ledger.rows(at(NOW, hours=1)))
    header, rows = read(ws)
    assert header[0] == "My notes" and len(rows) == 25 and all(r["My notes"] == "note" for r in rows) and "route_key" in header


# ---- the command line -------------------------------------------------------------------------------------------------------

@pytest.fixture
def run_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    shutil.copy(BROWSER_YAML, tmp_path / "browser.yaml")
    shutil.copy(BROWSER_YAML.replace("browser.yaml", "sources.yaml"), tmp_path / "sources.yaml")
    shutil.copy(BROWSER_YAML.replace("browser.yaml", "research.yaml"), tmp_path / "research.yaml")
    shutil.copytree(BRANDS_DIR, tmp_path / "brands")
    for name in ("INSTAGRAM_COOKIES", "TIKTOK_COOKIES", "GEMINI_COOKIES", "APIFY_TOKEN", "ANTHROPIC_API_KEY", "SERPER_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def run(*args):
        return cli.main([args[0], "--data-dir", str(tmp_path / "data"), "--brands-dir", str(tmp_path / "brands"), *args[1:]])
    run.root = tmp_path
    return run


def test_health_prints_every_route_and_json_matches(run_cli, capsys):
    assert run_cli("health") == 0
    out = capsys.readouterr().out
    assert "meta_ads / browser" in out and "unverified" in out and "instagram / agent_reach" in out and "disabled" in out and "planned" in out
    assert run_cli("health", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 25 and rows[0]["route_key"] == "meta_ads|browser"
    assert run_cli("health", "--matrix") == 0
    assert "OFF EXPERIMENTAL" in capsys.readouterr().out


def test_health_clear_removes_a_rest_and_refuses_an_unknown_source(run_cli, capsys, tmp_path):
    reg = shipped()
    led = H.HealthLedger({}, reg)
    led.observe("tiktok", "browser", ok=False, code=K.PLATFORM_BLOCKED, message="warned", now="2099-01-01T00:00:00Z")
    led.save(Store(tmp_path / "data"))
    assert run_cli("health", "--clear", "tiktok|browser") == 0 and "1 route(s) of tiktok" in capsys.readouterr().out
    assert H.HealthLedger.load(Store(tmp_path / "data"), reg).is_paused("tiktok", "browser", "2099-01-01T00:00:01Z") is None
    assert run_cli("health", "--clear", "myspace") == 1 and "not a declared source" in capsys.readouterr().out


def test_a_missing_sources_file_never_stops_a_collection(run_cli, capsys, tmp_path):
    (tmp_path / "sources.yaml").unlink()
    assert run_cli("collect", "--brand", "swoveralls", "--dry-run", "--sources", "meta_ads", "--research-config", "research.yaml", "--browser-config", "browser.yaml") == 0
    out = capsys.readouterr().out
    assert "::warning::sources.yaml was not found" in out
    assert run_cli("health") == 1


def test_a_broken_sources_file_is_reported_by_name(run_cli, tmp_path):
    (tmp_path / "sources.yaml").write_text("version: 1\nsources: {}\n", encoding="utf-8")
    with pytest.raises(Exception, match="at least one source"):
        run_cli("health")


def test_acceptance_plan_names_what_can_and_cannot_run_and_calls_nothing(run_cli, capsys, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a plan must not call anything")
    monkeypatch.setattr(cli, "_YTDLP_RUNNER", refuse)
    assert run_cli("acceptance", "--plan") == 0
    out = capsys.readouterr().out
    assert "meta_ads / browser / check: will run" in out and "tiktok / browser / check: cannot run: no login: set TIKTOK_COOKIES" in out
    assert "claude / api / check: cannot run: no ANTHROPIC_API_KEY" in out and "instagram / agent_reach" not in out


class YT:
    """A stand-in for a requests response from YouTube's Data API."""

    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


OK_VIDEO = {"items": [{"snippet": {"title": "Me at the zoo"}, "statistics": {"viewCount": "1234567"}}]}


class Done:
    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_a_passing_youtube_acceptance_run_earns_trust_and_is_remembered(run_cli, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0")
    monkeypatch.setattr(cli, "_YT_API_GET", lambda url, **kw: YT(200, OK_VIDEO))
    monkeypatch.delenv("GITHUB_SHA", raising=False)                       # GitHub always sets it; the test must not depend on where it runs
    assert run_cli("acceptance", "--only", "youtube_public") == 0
    out = capsys.readouterr().out
    assert "PASSED" in out and "video_details" in out and "1 item(s)" in out and "AIzaFAKE" not in out
    [rec] = Store(tmp_path / "data").read_acceptance()
    assert rec["source"] == "youtube_public" and rec["route"] == "data_api" and rec["passed"] is True and rec["items"] == 1 and rec["version"] == "dev"
    assert run_cli("health", "--json") == 0
    rows = {r["route_key"]: r for r in json.loads(capsys.readouterr().out)}
    assert rows["youtube_public|data_api"]["status"] == "healthy" and rows["youtube_public|data_api"]["acceptance_items"] == 1
    assert rows["youtube_public|yt_dlp"]["status"] == "disabled"           # the blocked route is off and is never checked


def test_a_refused_youtube_acceptance_run_fails_loudly_with_the_real_code(run_cli, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0")
    monkeypatch.setattr(cli, "_YT_API_GET", lambda url, **kw: YT(403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}))
    assert run_cli("acceptance", "--only", "youtube_public") == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "RATE_LIMITED" in out and "::error::youtube_public / data_api failed acceptance (RATE_LIMITED)" in out and "AIzaFAKE" not in out
    [rec] = Store(tmp_path / "data").read_acceptance()
    assert rec["passed"] is False and rec["code"] == K.RATE_LIMITED
    assert run_cli("health", "--json") == 0
    row = next(r for r in json.loads(capsys.readouterr().out) if r["route_key"] == "youtube_public|data_api")
    assert row["status"] == "paused" and row["error_code"] == K.RATE_LIMITED and row["acceptance_result"] == "fail" and row["verified"] == "no"
    assert run_cli("acceptance", "--only", "youtube_public", "--plan") == 0


@pytest.mark.parametrize("answer, code", [
    (Done(0, "not json at all"), K.UPSTREAM_BROKEN), (Done(0, json.dumps({"title": ""})), K.UPSTREAM_BROKEN),
    (Done(1, "", "ERROR: Video unavailable"), K.NOT_FOUND), (Done(1, "", ""), K.UPSTREAM_BROKEN)])
def test_every_way_the_youtube_probe_can_go_wrong_has_a_code(run_cli, monkeypatch, answer, code):
    monkeypatch.setattr(cli, "_YTDLP_RUNNER", lambda cmd, **kw: answer)
    with pytest.raises(K.Failure) as seen:
        cli._youtube_probe()
    assert seen.value.code == code


def test_the_youtube_probe_reports_a_missing_tool_and_a_timeout(monkeypatch):
    import subprocess

    def missing(cmd, **kw):
        raise FileNotFoundError("yt-dlp")

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 120)
    monkeypatch.setattr(cli, "_YTDLP_RUNNER", missing)
    with pytest.raises(K.Failure) as a:
        cli._youtube_probe()
    monkeypatch.setattr(cli, "_YTDLP_RUNNER", slow)
    with pytest.raises(K.Failure) as b:
        cli._youtube_probe()
    assert (a.value.code, b.value.code) == (K.NEED_SETUP, K.TIMEOUT)


def test_acceptance_refuses_an_unknown_source(run_cli, capsys):
    assert run_cli("acceptance", "--only", "myspace") == 1 and "unknown source myspace" in capsys.readouterr().out


def test_evidence_summarises_checks_and_exports(run_cli, capsys, tmp_path):
    store = Store(tmp_path / "data")
    store.append_collected("swoveralls", {"at": "2026-10-30T07:41:00Z", "run": "r", "source": "tiktok", "key": "k", "seeds": "#a", "via": "browser", "from_site_data": 1,
                                          "from_links": 0, "items": [{"item_id": "tiktok:1", "source": "tiktok", "handle": "@a", "text": "A caption long enough to hash properly",
                                                                      "url": "https://www.tiktok.com/@a/video/1", "likes": 3, "views": 30, "posted_at": "2026-10-01", "media_url": "https://c.example.com/a.jpg"}]})
    assert run_cli("evidence", "--brand", "swoveralls", "--check", "--export", str(tmp_path / "out.jsonl")) in (0, 1)
    out = capsys.readouterr().out
    assert "=== swoveralls: 1 evidence record(s) ===" in out and "by access type: site_data 1" in out and "Wrote 1 record(s)" in out
    assert json.loads((tmp_path / "out.jsonl").read_text(encoding="utf-8"))["origin"]["source"] == "tiktok"
    assert "by source: none" not in out


def test_evidence_check_fails_when_a_record_breaks_a_rule(run_cli, capsys, tmp_path):
    store = Store(tmp_path / "data")
    store.append_collected("swoveralls", {"at": "2026-10-30T07:41:00Z", "run": "r", "source": "tiktok", "key": "k", "seeds": "#a", "via": "browser", "from_site_data": 1,
                                          "from_links": 0, "provenance": {"schema": 1, "route": "carrier_pigeon", "access_tier": "unknown", "access_type": "unknown",
                                                                          "captured_at": "2026-10-30T07:41:00Z", "run": "r", "source_health": ""},
                                          "items": [{"item_id": "tiktok:1", "source": "tiktok", "text": "x" * 40, "url": "https://www.tiktok.com/@a/video/1"}]})
    assert run_cli("evidence", "--brand", "swoveralls", "--check") == 1
    out = capsys.readouterr().out
    assert "PROBLEM tiktok:1" in out and "not a known tier" in out and "0 of 1 record(s) keep the contract" in out


# ---- more of the real flows -------------------------------------------------------------------------------------------------

def test_an_apify_batch_is_stamped_as_apify(store, tmp_path, swov, cfg, settings, ledger):
    http, apify = apify_world()
    go(store, tmp_path, swov, cfg, settings, world(cfg), health=ledger, apify=apify, backend="apify", sources=("tiktok",))
    [rec] = store.read_collected("swoveralls")
    assert rec["via"] == "apify" and rec["provenance"]["route"] == "apify" and rec["provenance"]["access_tier"] == "third_party_service"
    assert entry(ledger, "tiktok", "apify")["history"][-1]["ok"] is True and "tiktok|browser" not in ledger.state["routes"]
    assert E.evidence_from_collected([rec], REG, NOW)[0]["origin"]["access_tier"] == "third_party_service"


def test_a_routes_hash_follows_the_site_settings_for_browser_routes_only(cfg):
    extra = cli._route_extra(cfg)
    assert extra("meta_ads", "browser") == cfg["sites"]["meta_ads"] and extra("deep_research", "browser") == cfg["sites"]["gemini"]
    assert extra("tiktok", "apify") is None and extra("claude", "api") is None and extra("web_search", "tavily") is None
    assert cli._route_extra(None)("tiktok", "browser") is None
    changed = {**cfg, "sites": {**cfg["sites"], "tiktok": {**cfg["sites"]["tiktok"], "logged_in_any": ["text=different"]}}}
    assert cli._route_extra(changed)("tiktok", "browser") != extra("tiktok", "browser")


def test_a_changed_selector_means_the_route_must_pass_acceptance_again(tmp_path, cfg):
    from test_health import passing
    store = Store(tmp_path / "data")

    class Args:
        sources_config = str(ROOT_SOURCES)
        browser_config = BROWSER_YAML
    _, ledger = cli._health(Args, store)
    passing(ledger, source="tiktok", route="browser")
    assert ledger.status("tiktok", "browser", NOW_HEALTH) == "healthy"
    ledger.save(store)
    edited = yaml_text_with_changed_tiktok_selector(tmp_path)

    class Args2:
        sources_config = str(ROOT_SOURCES)
        browser_config = str(edited)
    _, again = cli._health(Args2, store)
    assert again.status("tiktok", "browser", NOW_HEALTH) == "unverified" and again.status("meta_ads", "browser", NOW_HEALTH) == "unverified"
    _, same = cli._health(Args, store)
    assert same.status("tiktok", "browser", NOW_HEALTH) == "healthy"


def test_a_dry_run_from_the_command_line_saves_no_health_file(run_cli, tmp_path):
    assert run_cli("collect", "--brand", "swoveralls", "--dry-run", "--sources", "meta_ads", "--research-config", "research.yaml", "--browser-config", "browser.yaml") == 0
    assert not (tmp_path / "data" / "_shared" / "collector_health.json").exists()


def test_a_real_collect_run_from_the_command_line_saves_the_ledger_and_the_provenance(run_cli, tmp_path, monkeypatch, cfg):
    from test_collectors import COOKIES, FULL
    (tmp_path / "brands" / "swoveralls" / "research.yaml").write_text(__import__("yaml").safe_dump({**FULL, "track_own_brand": False}), encoding="utf-8")
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", lambda browser_cfg, creds, debug: world(cfg))
    for name in ("INSTAGRAM_COOKIES", "TIKTOK_COOKIES", "GEMINI_COOKIES"):
        monkeypatch.setenv(name, COOKIES)
    assert run_cli("collect", "--brand", "swoveralls", "--sources", "meta_ads,tiktok", "--research-config", "research.yaml", "--browser-config", "browser.yaml") == 0
    led = H.HealthLedger.load(Store(tmp_path / "data"), REG)
    assert led.state["routes"]["meta_ads|browser"]["history"][-1]["ok"] and led.state["routes"]["tiktok|browser"]["history"][-1]["ok"]
    assert all(r["provenance"]["route"] == "browser" for r in Store(tmp_path / "data").read_collected("swoveralls"))


def test_the_sheets_command_puts_the_health_tab_on_the_sheet(tmp_path, monkeypatch, swov):
    from test_sheets import BOT, EMAIL, baselined
    from fake_sheets import FakeClient
    data = tmp_path / "data"
    baselined(swov, Store(data), "swoveralls_full_72.json", 72)
    client = FakeClient()
    monkeypatch.setattr(cli.sheets_mod, "connect", lambda key: (client, BOT))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("SHARE_EMAIL", EMAIL)
    monkeypatch.setenv("SHEET_SHARE_EMAIL", EMAIL)
    assert cli.main(["sheets", "--brand", "swoveralls", "--data-dir", str(data), "--sheets-config", str(tmp_path / "none.yaml")]) == 0
    header, rows = read(client.created[0].worksheet("Collector Health"))
    assert header[:len(H.SHEET_COLUMNS)] == H.SHEET_COLUMNS and len(rows) == 25 and {r["status"] for r in rows} == {"unverified", "disabled", "planned"}


def test_without_sources_yaml_the_sheets_command_still_works_and_adds_no_health_tab(tmp_path, monkeypatch, swov):
    from test_sheets import BOT, EMAIL, baselined
    from fake_sheets import FakeClient
    data = tmp_path / "data"
    baselined(swov, Store(data), "swoveralls_full_72.json", 72)
    client = FakeClient()
    monkeypatch.setattr(cli.sheets_mod, "connect", lambda key: (client, BOT))
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    monkeypatch.setenv("SHEET_SHARE_EMAIL", EMAIL)
    assert cli.main(["sheets", "--brand", "swoveralls", "--data-dir", str(data), "--sheets-config", str(tmp_path / "none.yaml"),
                     "--sources-config", str(tmp_path / "missing.yaml")]) == 0
    assert "Collector Health" not in [ws.title for ws in client.created[0].worksheets()]


def yaml_text_with_changed_tiktok_selector(tmp_path):
    import yaml as _yaml
    data = _yaml.safe_load(open(BROWSER_YAML, encoding="utf-8"))
    data["sites"]["tiktok"]["logged_in_any"] = ["text=a different marker"]
    path = tmp_path / "edited-browser.yaml"
    path.write_text(_yaml.safe_dump(data), encoding="utf-8")
    return path


def test_a_deep_research_report_and_a_failure_to_open_gemini_are_both_recorded(store, tmp_path, swov, cfg, settings, ledger):
    deep = {"deep_research": {"questions": ["Who are the competitors?"]}}
    result = go(store, tmp_path, swov, cfg, settings, world(cfg), health=ledger, sources=("deep_research",), data=deep)
    assert detail(result, "deep_research").status == "ran"
    e = entry(ledger, "deep_research", "browser")
    assert e["history"][-1]["ok"] is True and e["history"][-1]["items"] == 1
    assert store.read_collected("swoveralls")[0]["provenance"]["access_type"] == "report_text"
    broken = H.HealthLedger({}, REG)
    go(Store(tmp_path / "other"), tmp_path, swov, cfg, settings, world(cfg, fail={"gemini": "chromium did not start"}), health=broken, sources=("deep_research",), data=deep)
    h = entry(broken, "deep_research", "browser")["history"][-1]
    assert h["ok"] is False and h["code"] == K.UPSTREAM_BROKEN


# ---- search: every kind of answer, in both modes -----------------------------------------------------------------------------

TAVILY = "https://api.tavily.com/search"
SERPER = "https://google.serper.dev/search"


def answering(http, url, status):
    http.routes = [r for r in http.routes if r[1] != url]
    http.on("POST", url, (status, {"detail": "x"}))


@pytest.mark.parametrize("status, code", [(400, K.UPSTREAM_BROKEN), (402, K.RATE_LIMITED), (401, K.NEED_SETUP)])
def test_each_kind_of_search_failure_is_recorded_with_its_own_code(store, swov, ledger, status, code):
    http, providers = search_world(only=["tavily"])
    answering(http, TAVILY, status)
    run_search(store, swov, providers, R.load_settings("does-not-exist.yaml"), ledger, max_queries=1)
    assert entry(ledger, "web_search", "tavily")["history"][-1]["code"] == code


@pytest.mark.parametrize("status, code", [(400, K.UPSTREAM_BROKEN), (402, K.RATE_LIMITED), (401, K.NEED_SETUP)])
def test_compare_mode_records_failures_and_successes_for_every_service(store, swov, ledger, status, code):
    http, providers = search_world()
    answering(http, TAVILY, status)
    run_search(store, swov, providers, R.load_settings("does-not-exist.yaml"), ledger, max_queries=1, compare=True, compare_limit=1)
    assert entry(ledger, "web_search", "tavily")["history"][-1]["code"] == code
    for name in ("serper_dev", "serpapi"):
        h = entry(ledger, "web_search", name)["history"][-1]
        assert h["ok"] is True and h["items"] == 3


def test_compare_mode_does_not_call_a_resting_service(store, swov, ledger):
    http, providers = search_world()
    ledger.observe("web_search", "serper_dev", ok=False, code=K.RATE_LIMITED, message="429", now=NOW)
    result = run_search(store, swov, providers, R.load_settings("does-not-exist.yaml"), ledger, max_queries=1, compare=True, compare_limit=1)
    assert not [c for c in searches_only(http) if "serper" in c["url"]] and [c for c in searches_only(http) if "tavily" in c["url"]]
    assert any("resting until" in why for entry_ in result.compare for why in entry_["failures"].values())


def test_a_provider_check_that_fails_for_any_reason_carries_a_code(store):
    http, providers = search_world(only=["tavily"])
    answering(http, TAVILY, 400)
    rows = R.check_providers(providers, store, R.load_settings("does-not-exist.yaml"), NOW)
    assert rows[0]["ok"] is False and rows[0]["code"] == K.UPSTREAM_BROKEN
    http2, providers2 = search_world(only=["tavily"])
    answering(http2, TAVILY, 402)
    rows = R.check_providers(providers2, store, R.load_settings("does-not-exist.yaml"), NOW)
    assert [r["code"] for r in rows] == [K.RATE_LIMITED, K.RATE_LIMITED]


def test_clearing_one_route_leaves_the_others_resting(run_cli, capsys, tmp_path):
    led = H.HealthLedger({}, REG)
    for route in ("browser", "apify"):
        led.observe("tiktok", route, ok=False, code=K.PLATFORM_BLOCKED, message="warned", now="2099-01-01T00:00:00Z")
    led.save(Store(tmp_path / "data"))
    assert run_cli("health", "--clear", "tiktok|browser") == 0 and "1 route(s) of tiktok" in capsys.readouterr().out
    after = H.HealthLedger.load(Store(tmp_path / "data"), REG)
    assert after.is_paused("tiktok", "browser", "2099-01-01T00:00:01Z") is None and after.is_paused("tiktok", "apify", "2099-01-01T00:00:01Z")


def test_the_catalogue_acceptance_probe_asks_for_one_page_only(run_cli, monkeypatch, tmp_path):
    seen = {}

    def fake(domain, **kw):
        seen.update(kw, domain=domain)
        return type("R", (), {"products": [{"id": 1}] * 4})()
    monkeypatch.setattr("contentintel.shopify_fetch.fetch_products", fake)

    class Args:
        brand, brands_dir, research_config, browser_config, debug_dir = "swoveralls", str(tmp_path / "brands"), "research.yaml", "browser.yaml", "debug"
    ctx = cli._acceptance_context(Args, Store(tmp_path / "data"), REG, NOW)
    assert ctx.catalogue_check() == (4, "swoveralls: read 4 products from the first page of the public feed")
    assert seen["max_pages"] == 1 and seen["domain"] == "swoveralls.com"


def test_search_checks_are_paid_for_once_per_service_not_once_per_kind(run_cli, monkeypatch, tmp_path):
    calls = []
    http, providers = search_world(only=["tavily"])
    real = R.check_providers
    monkeypatch.setattr(R, "check_providers", lambda *a, **k: calls.append(1) or real(*a, **k))
    monkeypatch.setattr(cli.providers_mod, "build_providers", lambda environ: providers)

    class Args:
        brand, brands_dir, research_config, browser_config, debug_dir = "swoveralls", str(tmp_path / "brands"), "research.yaml", "browser.yaml", "debug"
    ctx = cli._acceptance_context(Args, Store(tmp_path / "data"), REG, NOW)
    assert ctx.provider_check("tavily", "web")["ok"] and ctx.provider_check("tavily", "news")["ok"] and len(calls) == 1


def test_the_acceptance_record_names_the_commit_it_ran_on(run_cli, monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0")
    monkeypatch.setattr(cli, "_YT_API_GET", lambda url, **kw: YT(200, OK_VIDEO))
    monkeypatch.setenv("GITHUB_SHA", "4b6aceca6445f0e1d2c3b4a5968778695a4b3c2d")
    assert run_cli("acceptance", "--only", "youtube_public") == 0
    assert Store(tmp_path / "data").read_acceptance()[0]["version"] == "4b6aceca6445"          # the first 12 characters of the commit id
