"""Update 60: what the live runs of Updates 57 to 59 showed. Models the providers have withdrawn, Kilo's missing key row, a half-drawn TikTok page, counts that were in a reels-page result, and log lines that
mixed scopes. Also the standing rule of the owner: what already works must not be broken, so what the live runs PROVED works is locked here."""
import pytest

from contentintel import contract as K
from contentintel import session_live as SL
from contentintel.ai import clients as C
from contentintel.ai import lanes as L
from contentintel.ai import registry as R
from contentintel.ai import review as RV
from contentintel.ai import router as RT
from contentintel.ai import smoke as SM
from contentintel.ai import usage as U
from contentintel.health import HealthLedger, add_seconds
from contentintel.observatory import social_profiles as SP

from ai_helpers import NOW, PROVIDERS, LANES, edited, write
from conftest import ROOT
from stage1_helpers import registry as sources_registry

REG = R.load_registry(ROOT / "ai_providers.yaml")
LANE_MAP = L.load_lanes(ROOT / "ai_lanes.yaml", REG)
POOL = L.load_pool(ROOT / "ai_lanes.yaml", REG)
POOL_KEYS = {(r["provider"], r["model"]) for r in POOL}

# What the live check of 2026-11-14 (AI gateway #21) proved GONE, and the answer each gave.
DEAD = {
    ("nvidia", "deepseek-ai/deepseek-v4-pro"): "HTTP 410: end of life on 2026-08-07", ("nvidia", "mistralai/mistral-large-3-675b-instruct-2512"): "HTTP 410: end of life on 2026-07-23",
    ("nvidia", "moonshotai/kimi-k2.6"): "HTTP 404: not found for this account", ("nvidia", "meta/llama-3.3-70b-instruct"): "HTTP 410: end of life on 2026-08-26",
    ("nvidia", "meta/llama-4-maverick-17b-128e-instruct"): "HTTP 410: end of life on 2026-07-27", ("nvidia", "qwen/qwen3-coder-480b-a35b-instruct"): "HTTP 410: end of life on 2026-06-11",
    ("nvidia", "z-ai/glm-5.1"): "HTTP 410: end of life on 2026-07-02", ("nvidia", "deepseek-ai/deepseek-v4-flash"): "HTTP 410: end of life on 2026-08-07",
    ("nvidia", "nvidia/nemotron-3-super-120b-a12b"): "HTTP 404: not found for this account", ("nvidia", "meta/llama-3.1-70b-instruct"): "HTTP 410: end of life on 2026-08-26",
    ("nvidia", "minimaxai/minimax-m2.7"): "HTTP 410: end of life on 2026-07-27", ("nvidia", "nvidia/nemotron-3-nano-30b-a3b"): "HTTP 410: end of life on 2026-09-01",
    ("ollama", "kimi-k2-thinking"): "HTTP 410: retired by Ollama on 2026-06-16", ("ollama", "deepseek-v3.2"): "HTTP 410: retired by Ollama on 2026-07-15",
    ("ollama", "mistral-large-3:675b"): "HTTP 410: retired by Ollama on 2026-11-09", ("ollama", "cogito-2.1:671b"): "HTTP 410: retired by Ollama on 2026-06-16",
    ("google", "gemini-2.5-flash-lite"): "HTTP 404: no longer available to new users", ("kilo", "nvidia/nemotron-3-super-120b-a12b:free"): "404 model_not_found", ("kilo", "poolside/laguna-m.1:free"): "404 model_not_found", ("kilo", "stepfun/step-3.7-flash:free"): "404 model_not_found", ("kilo", "poolside/laguna-xs.2:free"): "404 model_not_found",
}
# What the same live check PROVED WORKING (answered). An update must not take any of these away.
PROVEN_LIVE = [("groq", "openai/gpt-oss-120b"), ("groq", "openai/gpt-oss-20b"), ("openrouter", "openrouter/free"), ("openrouter", "qwen/qwen3-coder:free"), ("openrouter", "z-ai/glm-4.5-air:free"),
               ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast"), ("google", "gemini-3.5-flash"), ("cohere", "command-a-03-2025"), ("cohere", "command-a-reasoning-08-2025"),
               ("cohere", "command-r-plus-08-2024"), ("cohere", "command-r-08-2024"), ("zhipu", "glm-4.5-flash"), ("zhipu", "glm-4.6v-flash"), ("nvidia", "google/gemma-4-31b-it")]
# Each lane's routes as they stood in Update 56 (what worked before the new providers): every one of them must still be there, in the same order.
PRE_UPDATE_57 = {'research_extraction': [('groq', 'openai/gpt-oss-120b'), ('mistral', 'mistral-small-latest'), ('openrouter', 'openrouter/free'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'competitor_intelligence': [('groq', 'openai/gpt-oss-120b'), ('mistral', 'mistral-small-latest'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'audience_voice': [('mistral', 'mistral-small-latest'), ('groq', 'openai/gpt-oss-120b'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'formula_analysis': [('groq', 'openai/gpt-oss-120b'), ('mistral', 'mistral-large-latest'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'trend_opportunity': [('mistral', 'mistral-large-latest'), ('groq', 'openai/gpt-oss-120b'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'content_ideation': [('mistral', 'mistral-large-latest'), ('groq', 'openai/gpt-oss-120b'), ('google', 'gemini-3.5-flash'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'production_support': [('groq', 'openai/gpt-oss-20b'), ('mistral', 'mistral-small-latest'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'general_text': [('groq', 'openai/gpt-oss-120b'), ('mistral', 'mistral-small-latest'), ('openrouter', 'openrouter/free'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')], 'general_json': [('groq', 'openai/gpt-oss-120b'), ('mistral', 'mistral-small-latest'), ('openrouter', 'openrouter/free'), ('cloudflare', '@cf/meta/llama-3.3-70b-instruct-fp8-fast')]}


def test_every_model_the_live_check_proved_gone_is_switched_off_with_the_reason_and_is_in_no_lane_and_not_in_the_pool():
    for (pid, model), reason in DEAD.items():
        m = REG.provider(pid).model(model)
        assert m is not None and m.approved is False and not REG.model_ok(pid, model), (pid, model)
        assert "switched off 2026-11-14" in m.note and reason in m.note and "Add it back only after a live check answers" in m.note, (pid, model)
        assert (pid, model) not in POOL_KEYS, (pid, model)
        assert not any((c["provider"], c["model"]) == (pid, model) for lane in LANE_MAP.values() for c in lane.chain), (pid, model)
    assert len(DEAD) == 21 and {p for p, _ in DEAD} == {"nvidia", "ollama", "google", "kilo"}


def test_the_providers_other_models_are_untouched_by_that_cleanup():
    assert [m.id for m in REG.provider("nvidia").models if m.approved] == ["google/gemma-4-31b-it"]
    assert sorted(m.id for m in REG.provider("ollama").models if m.approved) == sorted(["gpt-oss:120b", "glm-4.7", "devstral-2:123b", "qwen3-coder-next", "qwen3-coder:480b", "gpt-oss:20b", "gemma4:31b"])
    assert sorted(m.id for m in REG.provider("google").models if m.approved) == ["gemini-3.5-flash", "gemma-4-26b-a4b-it", "gemma-4-31b-it"] and len(POOL) == 32
    assert REG.provider("zhipu").model("glm-4.7-flash").approved is True                                       # one 429 (a passing limit), not a broken model


# ---- the owner's standing rule: what works stays working --------------------------------------------------------------------------------------

def test_every_model_the_live_check_proved_working_is_still_approved_and_in_the_shared_pool():
    for pid, model in PROVEN_LIVE:
        assert REG.model_ok(pid, model) and (pid, model) in POOL_KEYS, (pid, model)
    assert REG.model_ok("claude", "claude-sonnet-5-5") and REG.model_ok("claude", "claude-haiku-5-5")
    assert REG.daily_usd == 0.5 and REG.monthly_usd == 10                                                     # and the Claude spending cap is as it was


def test_every_lane_still_has_every_route_it_had_before_the_new_providers_in_the_same_order():
    assert len(PRE_UPDATE_57) == 9
    for lane, before in PRE_UPDATE_57.items():
        now = [(c["provider"], c["model"]) for c in LANE_MAP[lane].chain]
        kept = [r for r in now if r in before]
        assert kept == before, (lane, before, now)
    assert LANE_MAP["formula_analysis"].chain[0]["provider"] == "groq" and LANE_MAP["trend_opportunity"].chain[0]["provider"] == "mistral"      # the leads that worked before are the leads again


def test_the_live_tiktok_profile_that_returned_real_counts_still_parses_into_them():
    text = "# Free the Sheep ## freethesheepco ### 69Following 122.3KFollowers 1.6MLikes ## - TO FEEL SAFE AND SECURE FROM WITHIN - Luxury & Innovative Designs for Anxiety. linktr.ee/freethesheep Log in"
    got = SP.parse_profile("tiktok", text, "TikTok - Make Your Day")
    assert got["metrics"] == {"followers": 122300, "following": 69, "likes": 1600000} and got["name"] == "Free the Sheep"
    assert SL.judge_content(text)[0] == "real text"


def test_the_public_instagram_snippets_that_gave_counts_still_parse_into_them():
    assert SP.parse_profile("instagram", "SWOVERALLS (@swoveralls_) 51K followers · 1.9K+ following · 1325 posts", "x")["metrics"] == {"followers": 51000, "following": 1900, "posts": 1325}
    assert SP.parse_profile("instagram", "@madepants: “Family Owned Online Boutique … 183 posts 122K followers 194. Use the code INSCOLLABA to save 15%", "madepants")["metrics"] == {"followers": 122000, "posts": 183}


# ---- a withdrawn model rests ALONE ---------------------------------------------------------------------------------------------------------------

GONE_MESSAGE = "Every routed provider reports the model as not found or removed upstream (1 attempt(s)). Last error: NVIDIA NIM API error 410: The model has reached its end of life on 2026-08-07 and is no longer available."
GOOD = '{"answer": "fine"}'


def two_model_router(tmp_path, client):
    data = edited(PROVIDERS, lambda d: next(m for m in d["providers"]["alpha"]["models"] if m["id"] == "a-2").update(approved=True))      # alpha has a second model; approve it
    p, l = write(tmp_path, data, LANES)
    reg = R.load_registry(p)
    pool = [{"provider": "alpha", "model": "a-2", "gateway_model": "a-2"}]
    return RT.Router(reg, L.load_lanes(l, reg), U.UsageLedger(tmp_path / "u.json"), HealthLedger({}, sources_registry(tmp_path)), {"alpha": client}, RV.ReviewQueue(tmp_path / "r.json"), pool=pool)


def go(router, now=NOW):
    return router.run("pub", data_class="public", instructions="do it", now=now)


def test_a_model_the_provider_has_withdrawn_is_not_asked_again_for_a_day_and_its_provider_keeps_working(tmp_path):
    client = C.MockClient([K.Failure(K.UPSTREAM_BROKEN, GONE_MESSAGE), GOOD, GOOD, GOOD, GOOD])
    router = two_model_router(tmp_path, client)
    first = go(router)
    assert first["status"] == "ok" and first["route"] == {"provider": "alpha", "model": "a-2"}                  # the provider's other model answered in the same run
    assert router.health.is_paused("ai:alpha", "gateway", NOW) is None                                            # the PROVIDER is not rested
    assert router.health.is_paused("ai:alpha", "model:a-1", add_seconds(NOW, 86399)) and not router.health.is_paused("ai:alpha", "model:a-1", add_seconds(NOW, 86401))
    second = go(router, add_seconds(NOW, 60))
    assert second["route"]["model"] == "a-2" and len(client.calls) == 3 and client.calls[2]["route"]["model"] == "a-2"      # a-1 was not tried again: only a-2 was asked
    assert any("withdrawn by its provider" in s["reason"] for s in second["skipped"])
    third = go(router, add_seconds(NOW, 86401))
    assert third["status"] == "ok" and third["route"]["model"] == "a-1" and len(client.calls) == 4               # after a day it may be asked again (it answers here)


@pytest.mark.parametrize("message", ["boom", "the service is busy", "answered 500"])
def test_any_other_failure_still_rests_the_provider_as_before(tmp_path, message):
    client = C.MockClient([K.Failure(K.UPSTREAM_BROKEN, message), GOOD])
    router = two_model_router(tmp_path, client)
    assert go(router)["status"] == "deferred"
    assert router.health.is_paused("ai:alpha", "gateway", NOW) and router.health.is_paused("ai:alpha", "model:a-1", NOW) is None


@pytest.mark.parametrize("message", ["model_not_found", "the model was retired on June 16", "Function 'ac74': Not found for account 'x'", "has reached its end of life", "no longer available to new users"])
def test_every_way_a_provider_says_a_model_is_gone_is_recognised(message):
    assert RT.GONE.search(message)
    assert not RT.GONE.search("rate limit reached") and not RT.GONE.search("internal error")


def test_an_explicit_rest_sets_how_long_and_nonsense_is_ignored(tmp_path):
    health = HealthLedger({}, sources_registry(tmp_path))
    health.observe("ai:x", "model:m", ok=False, code=K.UPSTREAM_BROKEN, message="gone", now=NOW, rest_seconds=500)
    assert health.is_paused("ai:x", "model:m", add_seconds(NOW, 499)) and not health.is_paused("ai:x", "model:m", add_seconds(NOW, 501))
    for i, odd in enumerate((0, -5, True, "9", None)):
        health.observe(f"ai:y{i}", "gateway", ok=False, code=K.UPSTREAM_BROKEN, message="boom", now=NOW, rest_seconds=odd)
        assert health.is_paused(f"ai:y{i}", "gateway", add_seconds(NOW, 60)), odd                               # the standard rest for that failure applies (still resting a minute later), not the nonsense value
        assert not health.is_paused(f"ai:y{i}", "gateway", add_seconds(NOW, 4000)), odd


# ---- Kilo: the gateway's stored no-key row --------------------------------------------------------------------------------------------------------

def row(platform, model, key_count, key="modelId"):
    return {"platform": platform, key: model, "enabled": True, "keyCount": key_count}


def test_a_keyless_provider_needs_the_gateways_stored_row_and_the_check_names_the_one_that_lacks_it():
    kilo = "kilo-auto/free"
    ok, detail = SM.keyless_check([row("kilo", kilo, 1), row("groq", "openai/gpt-oss-120b", 0)], REG)
    assert ok and detail == "1 key-less model(s) have their stored row"                                         # a keyed provider's rows are not judged here
    ok, detail = SM.keyless_check([row("kilo", kilo, 0)], REG)
    assert not ok and "missing for kilo/" + kilo in detail                                                        # the live failure of AI gateway #21
    assert SM.keyless_check([row("kilo", kilo, None)], REG)[0] is False and SM.keyless_check([row("kilo", kilo, 1, key="model_id")], REG)[0] is True
    assert SM.keyless_check([], REG) == (True, "0 key-less model(s) have their stored row") and SM.keyless_check([row("custom", "smoke-model", 0)], REG)[0] is True


# ---- the profile reader ---------------------------------------------------------------------------------------------------------------------------

TIKTOK = "# Free the Sheep ## freethesheepco ### 69Following 122.3KFollowers 1.6MLikes ## - TO FEEL SAFE AND SECURE FROM WITHIN - Luxury & Innovative Designs for Anxiety. linktr.ee/freethesheep Log in"
HALF = "We're having trouble playing this video. Please refresh"                                                     # 55 characters: what a half-drawn TikTok page gave in monitoring #33


def page(text):
    return {"ok": True, "text": text, "title": "TikTok - Make Your Day", "final_url": "https://www.tiktok.com/@freethesheepco", "status": 200}


def reader(monkeypatch, answers):
    calls = []

    def fake(*a, **k):
        calls.append(k)
        return answers[min(len(calls), len(answers)) - 1]
    monkeypatch.setattr(SL, "_worker_page", fake)
    return SP.make_reader({"social": {}}, "me@example.com", "/py", {"X": "1"}), calls


def test_a_half_drawn_page_gets_one_more_try_with_twice_the_wait_and_the_counts_come_through(monkeypatch):
    read, calls = reader(monkeypatch, [page(HALF), page(TIKTOK)])
    got = read("tiktok", "https://www.tiktok.com/@freethesheepco")
    assert got["quality"] == "real text" and "122.3KFollowers" in got["text"]
    assert len(calls) == 2 and "wait_ms" not in calls[0] and calls[1]["wait_ms"] == 6000 and calls[1]["reading"] is True and calls[1]["with_session"] is False        # no login, ever
    assert SP.snapshot_profile("tiktok", "freethesheepco", read)["metrics"] == {"followers": 122300, "following": 69, "likes": 1600000}


def test_a_page_that_is_fine_the_first_time_is_read_once_and_a_bot_check_is_never_asked_for_again(monkeypatch):
    read, calls = reader(monkeypatch, [page(TIKTOK)])
    assert read("tiktok", "https://www.tiktok.com/@x")["quality"] == "real text" and len(calls) == 1
    read, calls = reader(monkeypatch, [page("Drag the puzzle piece into place Audio")])
    with pytest.raises(K.Failure) as seen:
        read("tiktok", "https://www.tiktok.com/@x")
    assert seen.value.code == K.PLATFORM_BLOCKED and len(calls) == 1


def test_when_the_second_try_is_also_half_drawn_it_fails_plainly_after_two_tries(monkeypatch):
    read, calls = reader(monkeypatch, [page(HALF), page(HALF + " Try")])
    with pytest.raises(K.Failure) as seen:
        read("tiktok", "https://www.tiktok.com/@x")
    assert seen.value.code == K.UPSTREAM_BROKEN and "too little to be a page" in str(seen.value) and len(calls) == 2


def test_the_wait_for_a_second_try_is_passed_to_the_worker_and_never_above_fifteen_seconds(monkeypatch):
    from contentintel.observatory import worker_client as wc
    seen = []

    def build(items, settings, contact):
        seen.append(items[0]["advanced"].get("wait"))
        raise K.Failure(K.NEED_SETUP, "stop here")
    monkeypatch.setattr(wc, "build_job", build)
    for wait_ms, expected in ((None, 3000), (6000, 6000), (99999, 15000)):
        with pytest.raises(K.Failure):
            SL._worker_page("tiktok", "https://www.tiktok.com/@x", {}, "me@example.com", "/py", {}, None, with_session=False, reading=True, wait_ms=wait_ms)
        assert seen[-1] == expected


def test_counts_are_taken_from_any_result_that_is_exactly_the_same_account_and_never_from_another(monkeypatch):
    rows = [{"url": "https://www.instagram.com/madepants/?hl=en", "title": "Madepants", "text": "Owned Online Boutique. Shop our collection. Built to last"},
            {"url": "https://www.instagram.com/madepants/reels/", "title": "madepants", "text": "@madepants: “Family Owned Online Boutique … 183 posts 122K followers 194. Use the code INSCOLLABA to save 15%"},
            {"url": "https://www.instagram.com/madepants_/", "title": "another", "text": "1M followers · 9 following · 5 posts"}]
    monkeypatch.setattr(SL, "discovery_rows", lambda url, environ, providers=None: rows)
    got = SP.make_discovery({}, [], limit=5)("instagram", "madepants")
    assert got["metrics"] == {"followers": 122000, "posts": 183} and got["handle"] == "madepants" and got["landed"].endswith("/madepants/reels/")
    rows[1] = {"url": "https://www.instagram.com/the_oodie/", "title": "x", "text": "Welcome to a world of comfort"}
    rows[2] = {"url": "https://www.instagram.com/the_oodie/reels/", "title": "x", "text": "Black Friday sale up to 50% off"}
    with pytest.raises(K.Failure) as seen:
        SP.make_discovery({}, [], limit=5)("instagram", "the_oodie")
    assert seen.value.code == K.NOT_FOUND and "shows no follower, like or post count" in str(seen.value)           # nothing anywhere: nothing is invented
    rows[:] = [{"url": "https://www.instagram.com/madepants_/", "title": "another", "text": "1M followers"}]
    with pytest.raises(K.Failure) as seen:
        SP.make_discovery({}, [], limit=5)("instagram", "madepants")
    assert seen.value.code == K.NOT_FOUND and "for exactly Instagram" in str(seen.value) and "other accounts are never used" in str(seen.value)


# ---- the documents say it ---------------------------------------------------------------------------------------------------------------------------

def test_the_documents_record_the_decisions_and_the_rule():
    pool = (ROOT / "docs" / "ai" / "shared-pool-and-pacing.md").read_text(encoding="utf-8")
    for phrase in ("## Update 60", "17 were gone", "The pool is now 25 models", "pinned (v0.16.17)", "rests alone", "no-key", "ninth check", "gpt-oss:120b"):
        assert phrase in pool, phrase
    guide = (ROOT / "docs" / "platform" / "session-capture-guide.md").read_text(encoding="utf-8")
    for phrase in ("## Update 60", "the route shown was **primary**", "are not wired into the snapshot collector", "twice as long", "183 posts, 122K followers", "from @swoveralls_"):
        assert phrase in guide, phrase
    rules = (ROOT / "docs" / "standing-rules.md").read_text(encoding="utf-8")
    for phrase in ("What works must keep working", "tests/test_update60.py", "PLACEMENT.txt", "deliberate-bug check", "a test that is skipped has not been run"):
        assert phrase in rules, phrase


def test_the_tally_counts_an_item_met_twice_in_one_run_as_new_once_and_a_known_one_not_at_all():
    from contentintel import collectors as C
    known, tally = {"b"}, {"items": 0, "new": 0}
    C.tally_new([{"item_id": "a"}, {"item_id": "b"}, {"item_id": "a"}, {"item_id": "c"}], known, tally)
    assert tally == {"items": 4, "new": 2} and known == {"a", "b", "c"}                                        # 4 collected, 2 new rows: the 119-versus-92 gap
    C.tally_new([{"item_id": "a"}, {"item_id": "d"}], known, tally)
    assert tally == {"items": 6, "new": 3} and C.tally_new([], known, tally) is None
