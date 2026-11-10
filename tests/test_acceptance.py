"""Live acceptance: one small real call per route, recorded whatever happens, and never a call to a route that is resting."""
import json

import pytest

from contentintel import acceptance as A
from contentintel import contract as K
from contentintel.browser import LoginWall
from contentintel.contract import Failure
from contentintel.health import HealthLedger
from contentintel.store import Store

import copy

import yaml

from contentintel import sources as S
from stage1_helpers import NOW, SHIPPED, at, registry, shipped


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        self.t += 0.5
        return self.t


@pytest.fixture
def world(tmp_path):
    store = Store(tmp_path / "data")
    reg = registry(tmp_path)
    return store, reg, HealthLedger({}, reg)


def go(probes, world, now=NOW, version="abc123", run_id="run1"):
    store, _, ledger = world
    return A.run_acceptance(probes, health=ledger, store=store, now=now, version=version, run_id=run_id, clock=Clock(), platform="github-actions")


def probe(source="tiktok", route="browser", fn=None, skip="", action="check"):
    return A.Probe(source, action, route, fn, skip)


# ---- a probe that passes ----------------------------------------------------------------------------------------------------

def test_a_pass_is_recorded_everywhere_and_earns_trust(world):
    store, reg, ledger = world
    [res] = go([probe(fn=lambda: (5, "5 items"))], world)
    assert res["status"] == "passed" and res["items"] == 5 and res["detail"] == "5 items" and res["elapsed_ms"] == 500
    [rec] = store.read_acceptance()
    assert rec == {"at": NOW, "run": "run1", "source": "tiktok", "action": "check", "route": "browser", "items": 5, "passed": True, "code": "",
                   "message": "5 items", "elapsed_ms": 500, "version": "abc123", "route_hash": ledger.route_hash("tiktok", "browser"), "platform": "github-actions"}
    assert ledger.status("tiktok", "browser", at(NOW, minutes=1)) == "healthy" and ledger.state["routes"]["tiktok|browser"]["acceptance"]["version"] == "abc123"


# ---- a probe that fails -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fn, code", [
    (lambda: (_ for _ in ()).throw(LoginWall("tiktok: not logged in")), K.NEED_LOGIN),
    (lambda: (_ for _ in ()).throw(Failure(K.PLATFORM_BLOCKED, "challenge")), K.PLATFORM_BLOCKED),
    (lambda: (_ for _ in ()).throw(TimeoutError("slow")), K.TIMEOUT),
    (lambda: (_ for _ in ()).throw(RuntimeError("odd")), K.UPSTREAM_BROKEN),
    (lambda: (0, "answered with nothing"), K.UPSTREAM_BROKEN),
])
def test_a_failure_is_the_finding_and_is_kept_with_its_code(world, fn, code):
    store, _, ledger = world
    [res] = go([probe(fn=fn)], world)
    assert res["status"] == "failed" and res["code"] == code
    [rec] = store.read_acceptance()
    assert rec["passed"] is False and rec["code"] == code and rec["items"] == 0
    assert ledger.status("tiktok", "browser", at(NOW, seconds=1)) in ("paused", "degraded")
    assert ledger.state["routes"]["tiktok|browser"]["acceptance"]["passed"] is False


def test_a_secret_in_a_failure_never_reaches_the_ledger(world):
    store, _, ledger = world
    go([probe(fn=lambda: (_ for _ in ()).throw(RuntimeError("bad key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV in APIFY_TOKEN=apify_api_SECRETSECRETSECRET1")))], world)
    text = json.dumps(store.read_acceptance()) + json.dumps(ledger.state)
    assert "sk-ant" not in text and "SECRETSECRET" not in text


def test_a_failed_pass_after_a_good_one_is_degraded_not_healthy(world):
    _, _, ledger = world
    go([probe(fn=lambda: (3, "ok"))], world, now=NOW)
    go([probe(fn=lambda: (_ for _ in ()).throw(Failure(K.UPSTREAM_BROKEN, "gone wrong")))], world, now=at(NOW, days=7))
    assert ledger.status("tiktok", "browser", at(NOW, days=7, hours=1)) == "degraded"


# ---- a probe that cannot run ------------------------------------------------------------------------------------------------

def test_a_missing_secret_is_skipped_not_failed_and_leaves_no_trace(world):
    store, _, ledger = world
    called = []
    [res] = go([probe(fn=lambda: called.append(1), skip="no login: set TIKTOK_COOKIES")], world)
    assert res["status"] == "skipped" and res["detail"] == "no login: set TIKTOK_COOKIES" and called == []
    assert store.read_acceptance() == [] and "tiktok|browser" not in ledger.state["routes"]
    assert ledger.status("tiktok", "browser", NOW) == "unverified"


def test_a_probe_may_decide_for_itself_that_it_cannot_run(world):
    store, _, ledger = world

    def cannot():
        raise A.Skipped("skipped: no items left under your monthly cap")
    [res] = go([probe(fn=cannot)], world)
    assert res["status"] == "skipped" and "monthly cap" in res["detail"] and store.read_acceptance() == [] and "tiktok|browser" not in ledger.state["routes"]


def test_a_resting_route_is_not_asked_again(world):
    store, _, ledger = world
    ledger.observe("tiktok", "browser", ok=False, code=K.PLATFORM_BLOCKED, message="challenge", now=NOW)
    called = []
    [res] = go([probe(fn=lambda: called.append(1) or (1, "x"))], world, now=at(NOW, hours=1))
    assert res["status"] == "skipped" and "resting until" in res["detail"] and called == [] and store.read_acceptance() == []
    [later] = go([probe(fn=lambda: (1, "x"))], world, now=at(NOW, hours=25))
    assert later["status"] == "passed"


def test_one_failing_probe_does_not_stop_the_others(world):
    store, _, ledger = world
    results = go([probe("meta_ads", fn=lambda: (_ for _ in ()).throw(RuntimeError("x"))), probe("tiktok", skip="no login"), probe("claude", "api", fn=lambda: (1, "ok"))], world)
    assert [r["status"] for r in results] == ["failed", "skipped", "passed"] and len(store.read_acceptance()) == 2


def test_the_platform_is_taken_from_the_environment_when_not_given(world, monkeypatch):
    store, _, ledger = world
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    A.run_acceptance([probe(fn=lambda: (1, "x"))], health=ledger, store=store, now=NOW, version="v", run_id="r", clock=Clock())
    monkeypatch.delenv("GITHUB_ACTIONS")
    A.run_acceptance([probe(fn=lambda: (1, "x"))], health=ledger, store=store, now=at(NOW, hours=1), version="v", run_id="r", clock=Clock())
    assert [r["platform"] for r in store.read_acceptance()] == ["github-actions", "other"]


# ---- turning the existing check rows into results ---------------------------------------------------------------------------

@pytest.mark.parametrize("row, expect", [
    ({"ok": True, "items": 3, "detail": "login ok; 3 items"}, (3, "login ok; 3 items")),
    ({"ok": True, "count": 2, "problem": ""}, (2, "")),
    ({"ok": True, "detail": "opened"}, (1, "opened")),                                      # a check that has no count (Deep Research is only opened) counts as one
    ({"ok": True, "count": 0, "problem": ""}, (0, "")),                                     # a search that answered with nothing found nothing
])
def test_a_check_row_that_passed_gives_its_item_count(row, expect):
    assert A.row_result(row) == expect


@pytest.mark.parametrize("row, code", [
    ({"ok": False, "detail": "x", "code": K.NEED_LOGIN}, K.NEED_LOGIN),
    ({"ok": False, "detail": "instagram: not logged in (cookies were not accepted)"}, K.NEED_LOGIN),
    ({"ok": False, "problem": "out of credits for this month"}, K.RATE_LIMITED),
    ({"ok": False, "detail": "nothing could be read"}, K.UPSTREAM_BROKEN),
    ({"ok": False}, K.UPSTREAM_BROKEN),
])
def test_a_check_row_that_failed_raises_the_failure_it_describes(row, code):
    with pytest.raises(Failure) as seen:
        A.row_result(row)
    assert seen.value.code == code


@pytest.mark.parametrize("detail", ["skipped: no items left under your monthly cap", "no login: set TIKTOK_COOKIES", "no key: set APIFY_TOKEN"])
def test_a_check_row_that_could_not_even_try_is_a_skip(detail):
    with pytest.raises(A.Skipped):
        A.row_result({"ok": False, "detail": detail})
    with pytest.raises(A.Skipped):
        A.row_result({"ok": False, "problem": detail})


# ---- choosing the probes ----------------------------------------------------------------------------------------------------

def ctx(reg, **kw):
    seen = []
    c = A.Context(registry=reg, **kw)
    c.collect_check = lambda source, backend: seen.append((source, backend)) or {"ok": True, "items": 2, "detail": "fine"}
    c.provider_check = lambda name, kind: {"ok": True, "count": {"web": 2, "news": 3}[kind], "problem": ""}
    c.catalogue_check = lambda: (10, "read 10 products")
    c.youtube_check = lambda: (1, "read a video")
    c.youtube_api_check = lambda: (1, "read a video through the API")
    c.claude_check = lambda: "key accepted"
    c.seen = seen
    return c


def keys(probes):
    return [(p.source, p.route, p.action) for p in probes]


def test_one_probe_per_enabled_route_that_has_something_to_check(tmp_path):
    c = ctx(shipped(), providers=("serper_dev", "tavily", "serpapi", "exa", "exa_mcp"))
    probes = A.build_probes(c)
    k = keys(probes)
    assert ("meta_ads", "browser", "check") in k and ("meta_ads", "apify", "check") in k and ("deep_research", "browser", "check") in k
    assert ("deep_research", "apify", "check") not in k                                          # Deep Research has no Apify route
    assert ("instagram", "agent_reach", "check") not in k                                         # a route that is off is never checked
    assert {kk for kk in k if kk[0] == "web_search"} == {("web_search", r, a) for r in ("serper_dev", "tavily", "serpapi", "exa", "exa_mcp", "agent_reach_reddit", "agent_reach_youtube") for a in ("search_web", "search_news")}
    skipped = {(p.route, p.skip) for p in probes if p.source == "web_search" and p.route.startswith("agent_reach")}
    assert {r for r, _ in skipped} == {"agent_reach_reddit", "agent_reach_youtube"} and all("AGENT_REACH_SEARCH=on" in why for _, why in skipped)
    assert ("shopify_catalogue", "public_json", "fetch_page") in k and ("youtube_public", "data_api", "video_details") in k and ("claude", "api", "check") in k
    assert not [kk for kk in k if kk[0] == "reddit_public"]                                                  # a planned source has nothing to check yet
    assert {kk[2] for kk in k if kk[0] in ("competitor_stores", "competitor_pages")} == {"catalogue_walk", "read_sitemap", "read_page", "read_feed", "read_via_reader"}
    assert not [kk for kk in k if kk[0].startswith("video_")]                                      # video downloads are judged on real items, not a probe
    assert all(p.skip == "" for p in probes if p.source not in ("competitor_stores", "competitor_pages", "reddit", "agent_reach_research") and not p.route.startswith("agent_reach"))      # only the routes that need a machine you control are skipped
    assert all("OBSERVATORY_CONTACT" in p.skip for p in probes if p.source in ("competitor_stores", "competitor_pages", "reddit", "agent_reach_research"))     # nothing to check them against in this test


def test_a_probe_runs_its_own_check_with_the_right_arguments(tmp_path):
    c = ctx(shipped(), providers=("tavily",))
    probes = {(p.source, p.route, p.action): p for p in A.build_probes(c)}
    assert probes[("meta_ads", "browser", "check")].run() == (2, "fine") and probes[("tiktok", "apify", "check")].run() == (2, "fine")
    assert c.seen == [("meta_ads", "browser"), ("tiktok", "apify")]
    assert probes[("claude", "api", "check")].run() == (1, "key accepted")
    assert probes[("web_search", "tavily", "search_news")].run() == (3, "") and probes[("web_search", "tavily", "search_web")].run() == (2, "")


def test_what_cannot_run_says_why(tmp_path):
    c = ctx(shipped(), providers=("tavily",), missing={("tiktok", "browser"): "no login: set TIKTOK_COOKIES", ("meta_ads", "apify"): "no key: set APIFY_TOKEN"})
    c.claude_check = c.catalogue_check = c.youtube_check = c.youtube_api_check = None
    by = {(p.source, p.route, p.action): p.skip for p in A.build_probes(c)}
    assert by[("tiktok", "browser", "check")] == "no login: set TIKTOK_COOKIES" and by[("meta_ads", "apify", "check")] == "no key: set APIFY_TOKEN"
    assert by[("web_search", "serper_dev", "search_web")] == "no API key for this service" and by[("web_search", "tavily", "search_web")] == ""
    assert by[("claude", "api", "check")] == "no ANTHROPIC_API_KEY" and by[("shopify_catalogue", "public_json", "fetch_page")] == "no brand catalogue to try"
    assert by[("youtube_public", "data_api", "video_details")] == "no key: set YOUTUBE_API_KEY"
    c2 = ctx(shipped())
    c2.collect_check = c2.provider_check = None
    assert all(p.skip for p in A.build_probes(c2) if p.source in ("meta_ads", "web_search"))


def test_a_probe_with_a_reason_to_skip_has_nothing_to_run(tmp_path):
    c = ctx(shipped(), missing={("tiktok", "browser"): "no login"})
    p = next(p for p in A.build_probes(c) if (p.source, p.route) == ("tiktok", "browser"))
    assert p.run is None and p.skip == "no login"


def test_the_choice_can_be_narrowed_to_some_sources(tmp_path):
    c = ctx(shipped(), providers=("tavily",))
    assert {p.source for p in A.build_probes(c, {"claude", "youtube_public"})} == {"claude", "youtube_public"}
    assert A.build_probes(c, {"reddit_public"}) == []


def test_the_report_counts_what_happened(world):
    results = go([probe("meta_ads", fn=lambda: (_ for _ in ()).throw(RuntimeError("boom"))), probe("tiktok", skip="no login"), probe("claude", "api", fn=lambda: (1, "ok"))], world)
    text = A.format_results(results)
    assert "FAILED" in text and "UPSTREAM_BROKEN: boom" in text and "SKIPPED" in text and "PASSED" in text
    assert "1 passed, 1 failed, 1 skipped" in text and "meta_ads / browser / check" in text


# ---- the ledger file --------------------------------------------------------------------------------------------------------

def test_the_ledger_keeps_every_result_and_skips_a_half_written_line(tmp_path):
    store = Store(tmp_path / "data")
    assert store.read_acceptance() == []
    store.append_acceptance({"at": NOW, "n": 1})
    store.append_acceptance({"at": NOW, "n": 2})
    with open(tmp_path / "data" / "_shared" / "acceptance.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"at": "half writt')
    assert [r["n"] for r in store.read_acceptance()] == [1, 2] and [r["n"] for r in store.read_acceptance(limit=1)] == [2]


def edited(tmp_path, edit):
    data = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    edit(data)
    path = tmp_path / "edited.yaml"
    path.write_text(yaml.safe_dump(copy.deepcopy(data)), encoding="utf-8")
    return S.load_sources(path)


def test_a_source_that_becomes_planned_or_a_route_that_is_switched_off_is_no_longer_probed(tmp_path):
    reg = edited(tmp_path, lambda d: (d["sources"]["meta_ads"].update(status="planned"), d["sources"]["tiktok"]["routes"]["apify"].update(enabled=False)))
    k = keys(A.build_probes(ctx(reg, providers=("tavily",))))
    assert not [x for x in k if x[0] == "meta_ads"] and ("tiktok", "apify", "check") not in k and ("tiktok", "browser", "check") in k
    k = keys(A.build_probes(ctx(shipped(), providers=("tavily",)), {"tiktok"}))
    assert set(k) == {("tiktok", "browser", "check"), ("tiktok", "apify", "check")}


def test_a_secret_in_what_a_passing_probe_says_never_reaches_the_ledger(world):
    store, _, ledger = world
    go([probe(fn=lambda: (1, "read it with key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV and APIFY_TOKEN=apify_api_SECRETSECRETSECRET1"))], world)
    text = json.dumps(store.read_acceptance()) + json.dumps(ledger.state)
    assert "sk-ant" not in text and "SECRETSECRET" not in text and store.read_acceptance()[0]["passed"] is True


# ---- the workflow hands the checks every secret they read ----------------------------------------------------------------------------------------

def _route_step_env():
    wf = yaml.safe_load(open(".github/workflows/acceptance.yml", encoding="utf-8"))
    step = next(s for s in wf["jobs"]["acceptance"]["steps"] if s.get("name") == "Check each route") if "acceptance" in wf["jobs"] else \
        next(s for job in wf["jobs"].values() for s in job["steps"] if s.get("name") == "Check each route")
    return step["env"]


def test_the_acceptance_workflow_passes_the_contact_address_and_every_documented_secret_the_checks_read():
    import re
    env = _route_step_env()
    assert env["OBSERVATORY_CONTACT"] == "${{ secrets.OBSERVATORY_CONTACT }}"                       # without it the competitor, Agent-Reach and Reddit checks all skip
    readme = open("README.md", encoding="utf-8").read()
    section = readme[readme.index("## Secrets"):]
    section = section[:section.index("\n## ", 5)] if "\n## " in section[5:] else section
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", section)) - {"GITHUB_TOKEN"}
    read = set()
    for path in ("contentintel/acceptance.py", "contentintel/observatory/acceptance_checks.py"):
        read |= set(re.findall(r'environ(?:\.get\(|\[)\s*"([A-Z][A-Z0-9_]{3,})"', open(path, encoding="utf-8").read()))
    must = (read & documented) | {"OBSERVATORY_CONTACT"}                                       # the contact address is read through the observatory settings, not by name here
    assert {"REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"} <= must
    missing = sorted(n for n in must if env.get(n) != "${{ secrets." + n + " }}")
    assert not missing, f"the Acceptance workflow's Check each route step does not pass: {missing}"
