"""Update 61: what the live runs of Update 60 showed. A provider out of credits, a TikTok page that drew its text but not its counts, a way to collect fresh results, visible skips and the model-list tool."""
import re

import pytest
import yaml

from contentintel import cli
from contentintel import contract as K
from contentintel.ai import clients as C
from contentintel.ai import router as RT
from contentintel.health import add_seconds
from contentintel.observatory import social_profiles as SP

from conftest import ROOT
from test_update60 import GOOD, TIKTOK, go, page, reader, two_model_router
from ai_helpers import NOW

# ---- a provider that is out of credits --------------------------------------------------------------------------------------------------------------------

CREDITS_MESSAGE = "All models rate-limited after 1 attempt. Soonest cooldown reset ~13s. Attempt trail: ollama/gpt-oss:120b key1: out_of_credits. Last error: Ollama Cloud API error 402: Add usage credits or subscribe"


@pytest.mark.parametrize("message", ["out_of_credits", "Add usage credits or subscribe", "insufficient credits", "Insufficient_balance for this call", "402 Payment Required", "your credit balance is too low"])
def test_every_way_a_provider_says_it_is_out_of_credits_is_recognised(message):
    assert RT.CREDITS.search(message)


@pytest.mark.parametrize("message", ["rate limit reached", "Retry-After 45", "photo credits missing", "internal error", "the model was retired"])
def test_ordinary_failures_are_not_taken_for_a_lack_of_credits(message):
    assert not RT.CREDITS.search(message)


def clocked_router(tmp_path, client):
    """Two models of one provider, a fake clock for the pacing waits."""
    from contentintel.ai import lanes as L, pacing as P, registry as R, review as RV, usage as U
    from contentintel.health import HealthLedger
    from ai_helpers import PROVIDERS, LANES, edited, write
    from stage1_helpers import registry as sources_registry
    from test_ai_pacing import Clock
    data = edited(PROVIDERS, lambda d: next(m for m in d["providers"]["alpha"]["models"] if m["id"] == "a-2").update(approved=True))
    p, l = write(tmp_path, data, LANES)
    reg = R.load_registry(p)
    clock = Clock()
    pool = [{"provider": "alpha", "model": "a-2", "gateway_model": "a-2"}]
    router = RT.Router(reg, L.load_lanes(l, reg), U.UsageLedger(tmp_path / "u.json"), HealthLedger({}, sources_registry(tmp_path)), {"alpha": client}, RV.ReviewQueue(tmp_path / "r.json"), pool=pool,
                       pacer=P.Pacer(reg.pacing, clock=clock.now, sleep=clock.sleep))
    return router, clock


def test_a_model_that_is_out_of_credits_rests_for_a_day_and_the_providers_other_models_are_still_tried(tmp_path):
    client = C.MockClient([K.Failure(K.RATE_LIMITED, "Ollama Cloud API error 402: Add usage credits or subscribe (out_of_credits)"), GOOD])
    router = two_model_router(tmp_path, client)
    res = go(router)
    assert res["status"] == "ok" and res["route"] == {"provider": "alpha", "model": "a-2"} and len(client.calls) == 2         # a 402 on one model does not prove the others are out: the next one was tried and answered
    assert router.health.is_paused("ai:alpha", "gateway", NOW) is None                                              # the PROVIDER is not rested
    assert router.health.is_paused("ai:alpha", "model:a-1", add_seconds(NOW, 86399)) and not router.health.is_paused("ai:alpha", "model:a-1", add_seconds(NOW, 86401))
    again = go(router, add_seconds(NOW, 60))
    assert again["route"]["model"] == "a-2" and len(client.calls) == 3 and [s["provider"] for s in again["skipped"]].count("alpha") == 1      # a-1 is skipped for the day (named in the skipped list), a-2 keeps answering


def test_every_model_of_a_provider_is_tried_when_each_fails_and_each_failure_is_marked_with_its_own_error(tmp_path):
    client = C.MockClient([K.Failure(K.RATE_LIMITED, "402 out_of_credits for a-1"), K.Failure(K.RATE_LIMITED, "402 out_of_credits for a-2")])
    router = two_model_router(tmp_path, client)
    res = go(router)
    assert res["status"] == "deferred" and len(client.calls) == 2                                                    # both were tried; neither stopped the other
    assert [(a["model"], a["message"]) for a in res["attempts"]] == [("a-1", "402 out_of_credits for a-1"), ("a-2", "402 out_of_credits for a-2")]
    assert router.health.is_paused("ai:alpha", "model:a-1", NOW) and router.health.is_paused("ai:alpha", "model:a-2", NOW) and router.health.is_paused("ai:alpha", "gateway", NOW) is None


def test_the_gateways_short_cooldown_is_waited_for_so_the_next_model_is_really_tried(tmp_path):
    message = "RATE_LIMITED: All models rate-limited after 1 attempt. Soonest cooldown reset ~13s. Attempt trail: ollama/gpt-oss:120b key1: out_of_credits. Last error: Ollama Cloud API error 402: Add usage credits"
    client = C.MockClient([K.Failure(K.RATE_LIMITED, message), GOOD])
    router, clock = clocked_router(tmp_path, client)
    res = go(router)
    assert res["status"] == "ok" and res["route"]["model"] == "a-2" and clock.sleeps == [13.0]                        # it waited the gateway's 13 seconds, then asked the next model for real
    assert router.health.is_paused("ai:alpha", "model:a-1", NOW) and router.health.is_paused("ai:alpha", "gateway", add_seconds(NOW, 12)) is None      # a day's rest for that model; the 13 seconds were only waited, not stored


def test_a_long_wait_for_one_model_rests_that_model_only_and_a_short_one_waits_for_the_provider(tmp_path):
    (tmp_path / "a").mkdir()
    long_wait = C.MockClient([K.Failure(K.RATE_LIMITED, "429 slow down", retry_after=86400), GOOD])
    router, clock = clocked_router(tmp_path / "a", long_wait)
    res = go(router)
    assert res["route"]["model"] == "a-2" and clock.sleeps == [] and router.health.is_paused("ai:alpha", "gateway", NOW) is None
    assert router.health.is_paused("ai:alpha", "model:a-1", add_seconds(NOW, 86399)) and not router.health.is_paused("ai:alpha", "model:a-1", add_seconds(NOW, 86401))
    (tmp_path / "b").mkdir()
    short_wait = C.MockClient([K.Failure(K.RATE_LIMITED, "429 slow down", retry_after=45), GOOD])
    router, clock = clocked_router(tmp_path / "b", short_wait)
    res = go(router)
    assert res["status"] == "deferred" and router.health.is_paused("ai:alpha", "gateway", add_seconds(NOW, 44)) and clock.sleeps == []      # an ordinary run leaves a provider that said "wait 45 s" alone, as in Update 57
    route = {"provider": "alpha", "model": "a-2", "gateway_model": "a-2"}
    checked = router.run_route("pub", route, instructions="x", now=NOW, ignore_rests=True)
    assert checked["status"] == "ok" and clock.sleeps == [45.0]                                                      # a live check WAITS the 45 seconds and then really tries the provider's next model


@pytest.mark.parametrize("message, seconds", [("Soonest cooldown reset ~13s", 13), ("soonest reset ~2m", 120), ("Soonest reset ~24h", 86400), ("Soonest reset ~ 7 s", 7), ("no reset here", None), ("", None), (None, None)])
def test_the_gateways_own_reset_time_is_read(message, seconds):
    assert RT.reset_seconds(message) == seconds


def test_a_live_check_tries_a_route_that_an_earlier_run_left_resting_and_an_ordinary_run_does_not(tmp_path):
    client = C.MockClient([K.Failure(K.UPSTREAM_BROKEN, GONE_TEXT), GOOD, GOOD, GOOD])
    router = two_model_router(tmp_path, client)
    go(router)                                                                                                      # a-1 is reported gone and rests for a day; a-2 answers
    route = {"provider": "alpha", "model": "a-1", "gateway_model": "a-1"}
    skipped = router.run_route("pub", route, instructions="x", now=add_seconds(NOW, 60))
    assert skipped["status"] == "skipped" and "withdrawn" in skipped["reason"] and len(client.calls) == 2           # an ordinary run leaves it alone
    tried = router.run_route("pub", route, instructions="x", now=add_seconds(NOW, 60), ignore_rests=True)
    assert tried["status"] == "ok" and len(client.calls) == 3                                                       # the check asks it anyway: finding out is its purpose


def test_a_live_check_also_ignores_a_stored_rest_of_the_whole_provider(tmp_path):
    client = C.MockClient([GOOD, GOOD])
    router = two_model_router(tmp_path, client)
    router.health.observe("ai:alpha", "gateway", ok=False, code=K.UPSTREAM_BROKEN, message="boom", now=NOW)
    route = {"provider": "alpha", "model": "a-1", "gateway_model": "a-1"}
    assert router.run_route("pub", route, instructions="x", now=add_seconds(NOW, 30))["status"] == "skipped" and client.calls == []
    assert router.run_route("pub", route, instructions="x", now=add_seconds(NOW, 30), ignore_rests=True)["status"] == "ok" and len(client.calls) == 1


GONE_TEXT = "Every routed provider reports the model as not found or removed upstream (1 attempt(s)). Last error: 410 end of life"


# ---- TikTok counts ---------------------------------------------------------------------------------------------------------------------------------------

def test_a_count_written_after_its_label_is_read_only_when_none_is_written_the_usual_way():
    assert SP.parse_profile("tiktok", "Following 69 Followers 122.3K Likes 1.6M", "")["metrics"] == {"followers": 122300, "following": 69, "likes": 1600000}
    assert SP.parse_profile("instagram", "Followers: 976 Following: 122 Posts: 743", "")["metrics"] == {"followers": 976, "following": 122, "posts": 743}
    assert SP.parse_profile("tiktok", "69Following 122.3KFollowers 1.6MLikes", "")["metrics"] == {"followers": 122300, "following": 69, "likes": 1600000}      # the layout that worked live is read as before
    assert SP.parse_profile("tiktok", "Followers 5 and 20 posts", "")["metrics"] == {"posts": 20}                    # the two layouts are never mixed on one page
    assert SP.parse_profile("tiktok", "words about a thing", "")["metrics"] == {} and SP.label_metric("Likes soon", "likes") is None


UNDRAWN = "# Free the Sheep ## freethesheepco ### - TO FEEL SAFE AND SECURE FROM WITHIN - Luxury & Innovative Designs for Anxiety. linktr.ee/freethesheep Log in"      # real words, no counts: what monitoring #35 stored


def test_a_page_that_drew_its_text_but_not_its_counts_is_read_once_more_with_twice_the_wait(monkeypatch):
    read, calls = reader(monkeypatch, [page(UNDRAWN), page(TIKTOK)])
    got = read("tiktok", "https://www.tiktok.com/@freethesheepco")
    assert len(calls) == 2 and calls[1]["wait_ms"] == 6000 and "122.3KFollowers" in got["text"]
    assert SP.snapshot_profile("tiktok", "freethesheepco", read)["metrics"] == {"followers": 122300, "following": 69, "likes": 1600000}


def test_when_the_counts_never_appear_nothing_is_recorded_and_the_failure_shows_how_the_page_started(monkeypatch):
    read, calls = reader(monkeypatch, [page(UNDRAWN), page(UNDRAWN)])
    with pytest.raises(K.Failure) as seen:
        SP.snapshot_profile("tiktok", "freethesheepco", read)
    assert len(calls) == 2 and seen.value.code == K.NOT_FOUND and "shows no follower, like or post count" in str(seen.value) and "the page starts: '# Free the Sheep" in str(seen.value)


def test_linkedins_about_text_without_counts_is_kept_and_not_read_twice(monkeypatch):
    text = "Sign in # Swoveralls ## Apparel #### About us The comfiest overalls on earth, made for people who like to lounge in style all day long Follow"
    read, calls = reader(monkeypatch, [page(text)])
    snap = SP.snapshot_profile("linkedin", "swoveralls", read)
    assert len(calls) == 1 and snap["metrics"] == {} and snap["complete"] is False


def test_a_good_first_read_is_read_once(monkeypatch):
    read, calls = reader(monkeypatch, [page(TIKTOK)])
    assert SP.snapshot_profile("tiktok", "freethesheepco", read)["metrics"]["followers"] == 122300 and len(calls) == 1


# ---- a fresh collect -------------------------------------------------------------------------------------------------------------------------------------

def test_the_collect_command_has_a_fresh_option_that_is_off_by_default():
    parser = cli.build_parser()
    assert parser.parse_args(["collect"]).fresh is False and parser.parse_args(["collect", "--fresh"]).fresh is True


def test_the_collect_workflow_offers_fresh_and_only_a_real_collect_uses_it():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "collect.yml").read_text(encoding="utf-8"))
    assert wf[True]["workflow_dispatch"]["inputs"]["fresh"]["default"] is False and "3 days" in wf[True]["workflow_dispatch"]["inputs"]["fresh"]["description"]
    step = next(s for s in wf["jobs"]["collect"]["steps"] if "FRESH" in (s.get("env") or {}))
    assert step["env"]["FRESH"] == "${{ inputs.fresh }}" and '[ "$FRESH" = "true" ] && [ "$MODE" = "collect" ]' in step["run"] and "args+=(--fresh)" in step["run"]


# ---- skips are visible; the wheel test can run -----------------------------------------------------------------------------------------------------

def test_every_skipped_test_is_listed_with_its_reason_in_the_github_log_and_the_wheel_test_has_what_it_needs():
    tests = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert re.search(r"run: python -m pytest -q -rs\b", tests)
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert re.search(r"(?m)^setuptools>=", dev) and re.search(r"(?m)^wheel>=", dev)


# ---- the documents say it ----------------------------------------------------------------------------------------------------------------------------------

def test_the_documents_record_what_was_decided():
    pool = (ROOT / "docs" / "ai" / "shared-pool-and-pacing.md").read_text(encoding="utf-8")
    for phrase in ("## Update 61", "ai catalog", "out of credits", "rests for a day", "A live check tries every model", "laguna-m.1", "step-3.7-flash", "newer gateway pin", "Actions > AI gateway", "wait or replace"):
        assert phrase in pool, phrase
    guide = (ROOT / "docs" / "platform" / "session-capture-guide.md").read_text(encoding="utf-8")
    for phrase in ("## Update 61", "swoveralls_", "label first", "the page starts", "Fresh collect"):
        assert phrase in guide, phrase
    rules = (ROOT / "docs" / "standing-rules.md").read_text(encoding="utf-8")
    assert "A skipped test is named" in rules and "full SHA-256 hashes" in rules


def test_the_fresh_option_reaches_the_collection_run_and_is_announced(monkeypatch, capsys, tmp_path):
    from contentintel import collectors as C
    seen = {}

    def fake(brands, store, **kw):
        seen.update(kw)
        return C.CollectResult(run_id="r", now="2026-11-14T10:00:00Z", dry_run=kw.get("dry_run", False))
    monkeypatch.setattr(C, "run_collect", fake)
    (tmp_path / "s.yaml").write_text((ROOT / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    base = ["collect", "--brand", "swoveralls", "--sources", "meta_ads", "--data-dir", str(tmp_path / "data"), "--brands-dir", str(ROOT / "brands"), "--sources-config", str(tmp_path / "s.yaml")]
    cli.main(base + ["--fresh"])
    out = capsys.readouterr().out
    assert seen.get("fresh") is True and "Fresh run: what was collected in the last days is NOT reused" in out
    seen.clear()
    cli.main(base)
    out = capsys.readouterr().out
    assert seen.get("fresh") is False and "Fresh run:" not in out


def test_the_ai_check_asks_every_route_with_the_ignore_rests_request(monkeypatch, capsys, tmp_path):
    seen = []
    original = RT.Router.run_route

    def spy(self, lane, route, **kw):
        seen.append((route["provider"], route["model"], kw.get("ignore_rests")))
        return original(self, lane, route, **kw)
    monkeypatch.setattr(RT.Router, "run_route", spy)
    for name in ("ANTHROPIC_API_KEY", "FREEAPI_ENCRYPTION_KEY", "FREEAPI_ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "s.yaml").write_text((ROOT / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    code = cli.main(["ai", "check", "--data-dir", str(tmp_path / "data"), "--sources-config", str(tmp_path / "s.yaml"), "--browser-config", str(ROOT / "browser.yaml"),
                     "--providers-config", str(ROOT / "ai_providers.yaml"), "--lanes-config", str(ROOT / "ai_lanes.yaml"), "--tasks-config", str(ROOT / "ai_eval_tasks.yaml"), "--workdir", str(tmp_path / "w")])
    capsys.readouterr()
    assert code == 0 and len(seen) >= 29 and all(flag is True for _, _, flag in seen)                                  # every route of the check, every provider
    assert {("kilo", "kilo-auto/free"), ("kilo", "nvidia/nemotron-3.5-super-vl:free")} <= {(p, m) for p, m, _ in seen}
    assert len({m for p, m, _ in seen if p == "ollama"}) == 7                                                        # all seven Ollama models are on the list to try
