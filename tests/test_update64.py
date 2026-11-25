"""Update 64 (Batch 1): audience voice (last30days), video facts (yt-dlp), pace (ffmpeg), the pool catalogue, Openverse, the motion prompts, the second Ad Library reader and page matching.

Fixtures with 'documented' in the name were written from the tool's documented fields (no network here); l30_research_mock_real.json is the real output of the real engine in its own mock mode.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from contentintel import adlibrary_secondary as AD
from contentintel import brief as BR
from contentintel import cli
from contentintel import collectors as C
from contentintel import connections as CONN
from contentintel import contract as K
from contentintel import ideabank as IB
from contentintel import main_page as MP
from contentintel import motion_prompts as MOTION
from contentintel import openverse as OV
from contentintel import pace as PACE
from contentintel import page_match as PM
from contentintel import poolcatalog as POOL
from contentintel import research_tools as RT
from contentintel import videometa as VM
from contentintel import voice as VOICE
from contentintel.store import Store

from test_collectors import NOW
from test_ideabank import bank_with, good, run_scoring
from fake_browser import FakeClaude

FIX = Path(__file__).parent / "fixtures" / "update64"
SETTINGS = {"services": {}}
needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="SKIPPED BY NAME: ffmpeg and ffprobe are not installed here (the workflows now install them)")


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _keep_health_out_of_the_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path / "health-data"))                  # connection health is written here, never into the repository's data/ folder


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


# ---- connections ---------------------------------------------------------------------------------------------------------------------------------

def test_the_five_new_connections_are_registered_with_only_the_secrets_they_declare():
    for cid in ("last30days", "yt_dlp", "omniroute_catalog", "opus_video_prompts", "fb_ads_library_mcp"):
        assert cid in CONN.CONNECTIONS and cid in CONN.IDS
        assert CONN.CONNECTIONS[cid]["fallback"] and CONN.CONNECTIONS[cid]["repo"].startswith("https://github.com/")
    assert CONN.CONNECTIONS["last30days"]["secrets"] == ("GITHUB_TOKEN", "SERPER_API_KEY", "SCRAPECREATORS_API_KEY")
    env = CONN.external_env(SETTINGS, "last30days", {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-x", "SERPER_API_KEY": "s", "GITHUB_TOKEN": "g", "GROQ_API_KEY": "q"})
    assert env["SERPER_API_KEY"] == "s" and env["GITHUB_TOKEN"] == "g" and "ANTHROPIC_API_KEY" not in env and "GROQ_API_KEY" not in env


# ---- audience voice -------------------------------------------------------------------------------------------------------------------------------

def test_the_engine_plan_has_the_documented_shape_and_the_topic_in_every_query():
    plan = VOICE.build_plan("bib overalls women")
    assert plan["intent"] == "product" and 1 <= len(plan["subqueries"]) <= 4
    assert [q["label"] for q in plan["subqueries"]] == ["primary", "problems", "choosing"]
    for q in plan["subqueries"]:
        assert "bib overalls women" in q["search_query"] and q["sources"] and 0 < q["weight"] <= 1 and q["ranking_query"].endswith("?")
    assert plan["subqueries"][0]["sources"] == list(VOICE.DEFAULT_SOURCES)


def test_the_real_engine_report_becomes_voice_items_with_their_engagement():
    items, status = VOICE.normalise(load("l30_research_mock_real.json"), "bib overalls women")
    assert status["reddit"] == "ok" and len(items) == 2
    top = max(items, key=lambda i: i["weight"])
    assert (top["source"], top["weight"], top["engagement"]["num_comments"], top["published"]) == ("reddit", 120, 48, "2026-11-09")
    assert top["cluster"] == "bib overalls women discussion thread" and "upvote_ratio" in top["engagement"]


def test_comparison_and_discovery_exports_are_read_and_a_failed_source_is_never_called_empty():
    items, status = VOICE.normalise(load("l30_comparison_documented.json"), "x")
    assert {i["entity"] for i in items} == {"Madepants", "The Oodie"} and all(i["mode"] == "competitors" for i in items)
    assert status["reddit"] == "rate-limited" and VOICE.failed_sources(status) == ["reddit"]            # 'no-results' is not a failure; 'rate-limited' is
    found, _ = VOICE.normalise(load("l30_discovery_documented.json"), "")
    assert [i["title"] for i in found] == ["weighted hoodie blankets", "matching family pyjamas"] and found[0]["weight"] == 88 and found[0]["mode"] == "discover"
    with pytest.raises(K.Failure):
        VOICE.normalise(["not a report"], "x")


class Runner:
    def __init__(self, stdout="", code=0, stderr=""):
        self.calls, self.out, self.code, self.err = [], stdout, code, stderr

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.code, self.out, self.err)


def checkout(tmp_path):
    root = tmp_path / "last30days"
    (root / VOICE.SCRIPT).parent.mkdir(parents=True)
    (root / VOICE.SCRIPT).write_text("# stand-in", encoding="utf-8")
    return root


def test_the_engine_is_run_as_itself_with_the_plan_json_and_a_clean_environment(tmp_path):
    runner = Runner((FIX / "l30_research_mock_real.json").read_text())
    ext = VOICE.ExternalLast30Days(SETTINGS, root=checkout(tmp_path), environ={"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-x", "SERPER_API_KEY": "s"}, runner=runner, python="py")
    items, status = ext.run("bib overalls women")
    cmd, kw = runner.calls[0]
    assert cmd[0] == "py" and cmd[1].endswith("last30days.py") and cmd[2] == "bib overalls women" and "--plan" in cmd and cmd[cmd.index("--emit") + 1] == "json" and "--quick" in cmd and "--no-browser-cookies" in cmd
    assert kw["env"]["LAST30DAYS_MEMORY_DIR"] == "" and "ANTHROPIC_API_KEY" not in kw["env"] and kw["env"]["SERPER_API_KEY"] == "s"
    assert len(items) == 2 and status["reddit"] == "ok"
    comp = ext.command("Madepants", "competitors", "plan.json", ("The Oodie", "Skims"))
    assert comp[2] == "Madepants vs The Oodie vs Skims" and comp[comp.index("--competitors-list") + 1] == "The Oodie,Skims"
    disc = ext.command("loungewear", "discover", "plan.json")
    assert disc[2:6] == ["--discover", "loungewear", "--discover-shallow", "--emit"] and "--plan" not in disc


@pytest.mark.parametrize("runner,code", [(Runner("", 1, "boom"), K.UPSTREAM_BROKEN), (Runner("no json here"), K.UPSTREAM_BROKEN),
                                         (Runner(json.dumps({"results": [], "clusters": [], "source_status": {"reddit": "unreachable", "hackernews": "timeout"}})), K.UPSTREAM_BROKEN)])
def test_a_broken_or_wholly_failed_engine_run_is_a_failure_not_an_empty_answer(tmp_path, runner, code):
    ext = VOICE.ExternalLast30Days(SETTINGS, root=checkout(tmp_path), environ={}, runner=runner)
    with pytest.raises(K.Failure) as exc:
        ext.run("x")
    assert exc.value.code == code
    if runner.code:
        assert "last30days stopped (1)" in exc.value.message and "boom" in exc.value.message
    elif "no json" in runner.out:
        assert "printed no JSON" in exc.value.message
    else:
        assert "every source last30days tried failed" in exc.value.message and "reddit unreachable" in exc.value.message


def test_a_missing_checkout_says_to_install_it(tmp_path):
    with pytest.raises(K.Failure) as exc:
        VOICE.ExternalLast30Days(SETTINGS, root=tmp_path / "none", environ={}).run("x")
    assert exc.value.code == K.NEED_SETUP and "install" in exc.value.message


class Get:
    def __init__(self, answers):
        self.answers, self.urls = answers, []

    def __call__(self, url, headers=None):
        self.urls.append(url)
        for key, (status, text) in self.answers.items():
            if key in url:
                return status, text
        return 404, ""


HN = json.dumps({"hits": [{"title": "Why my overalls ripped", "url": "https://x.example/a", "points": 55, "num_comments": 12, "created_at": "2026-11-10T01:00:00Z", "objectID": "1"}]})
RD = json.dumps({"data": {"children": [{"data": {"title": "Best bib overalls?", "selftext": "looking for strong straps", "score": 410, "num_comments": 77, "permalink": "/r/x/comments/1/", "subreddit_name_prefixed": "r/Overalls"}}]}})


def test_the_platforms_own_reader_gives_hacker_news_and_reddit_with_real_counts_and_reports_a_blocked_site():
    ok = VOICE.BuiltinVoice(Get({"algolia": (200, HN), "reddit.com": (200, RD)}), 1_790_000_000)
    items, status = ok.run("bib overalls")
    assert status == {"hackernews": "ok", "reddit": "ok"} and {(i["source"], i["weight"]) for i in items} == {("hackernews", 55), ("reddit", 410)}
    assert "numericFilters=created_at_i>" in ok.get.urls[0]
    partial, status = VOICE.BuiltinVoice(Get({"algolia": (200, HN), "reddit.com": (403, "")}), 1_790_000_000).run("x")
    assert status["reddit"] == "auth-failed" and len(partial) == 1
    limited, status = VOICE.BuiltinVoice(Get({"algolia": (200, HN), "reddit.com": (429, "")}), 1_790_000_000).run("x")
    assert status == {"hackernews": "ok", "reddit": "rate-limited"} and VOICE.failed_sources(status) == ["reddit"]
    with pytest.raises(K.Failure):
        VOICE.BuiltinVoice(Get({"algolia": (429, ""), "reddit.com": (429, "")}), 1).run("x")
    with pytest.raises(K.Failure):
        ok.run("x", mode="discover")


def test_the_owners_route_decides_which_reader_answers_and_a_failed_repository_falls_back(tmp_path):
    ext = VOICE.ExternalLast30Days(SETTINGS, root=checkout(tmp_path), environ={}, runner=Runner("", 1, "boom"))
    back = VOICE.BuiltinVoice(Get({"algolia": (200, HN), "reddit.com": (200, RD)}), 1_790_000_000)
    items, status, which = VOICE.gather(SETTINGS, "bib overalls", now=NOW, external=ext, builtin=back)
    assert which == "builtin" and len(items) == 2
    off = {"services": {"connections": {"enabled": True, "channels": {"last30days": False}}}}
    assert VOICE.gather(off, "x", now=NOW, external=ext, builtin=back)[2] == "builtin"
    only = {"services": {"connections": {"enabled": True, "channels": {"last30days": False}}, "connection_routes": {"routes": {"last30days": "external_only"}}}}
    with pytest.raises(K.Failure) as exc:
        VOICE.gather(only, "x", now=NOW, external=ext, builtin=back)
    assert exc.value.code == K.NEED_SETUP
    with pytest.raises(K.Failure) as bad:
        VOICE.gather(SETTINGS, "x", mode="sideways", now=NOW, external=ext, builtin=back)
    assert bad.value.code == K.BAD_INPUT


def test_kept_voice_is_deduplicated_by_address_and_only_a_real_overlap_counts_as_evidence():
    items, _ = VOICE.normalise(load("l30_research_mock_real.json"), "bib overalls women")
    kept, fresh = VOICE.merge({}, items, NOW)
    assert fresh == 2
    bigger = [dict(items[0], weight=500, engagement={"score": 500})]
    kept, fresh = VOICE.merge(kept, bigger + [dict(items[1])], "2026-11-16T00:00:00Z")
    assert fresh == 0 and len(kept["items"]) == 2 and kept["items"][0]["weight"] == 500 and kept["items"][0]["first_seen"] == NOW
    assert VOICE.evidence_for(kept, "bib overalls women discussion", 1)
    assert VOICE.evidence_for(kept, "summer sandals", 1) == [] and VOICE.evidence_for(kept, "overalls", 1) == []          # one shared word is not enough
    assert VOICE.prompt_lines(kept, 1).startswith("- (reddit, 500 engagement)") and VOICE.prompt_lines({}, 3) == ""
    assert VOICE.seed_topics(kept)[0] == "bib overalls women discussion thread"
    mixed, _ = VOICE.merge({}, [VOICE._item("reddit", "quiet topic", "https://r/a", "", {"score": 3}, "", "quiet", "t", "research"), VOICE._item("reddit", "loud topic", "https://r/b", "", {"score": 900}, "", "loud", "t", "research"),
                                VOICE._item("reddit", "middle topic", "https://r/c", "", {"score": 40}, "", "middle", "t", "research")], NOW)
    assert VOICE.seed_topics(mixed) == ["loud", "middle", "quiet"] and VOICE.seed_topics(mixed, 1) == ["loud"]


def test_competitor_talk_is_summed_per_brand():
    items, _ = VOICE.normalise(load("l30_comparison_documented.json"), "x")
    totals = VOICE.by_entity(VOICE.merge({}, items, NOW)[0])
    assert totals["Madepants"][1] > totals["The Oodie"][1] and totals["Madepants"][0] == 2


def test_voice_topics_come_from_the_brands_own_catalogue(store, swov):
    topics = VOICE.topics_for(swov, store, extra=("bib overalls",))
    assert topics[0] == "bib overalls" and topics[-1] == swov.name


# ---- video facts (yt-dlp) -------------------------------------------------------------------------------------------------------------------------

def test_the_facts_of_a_video_are_read_from_the_programs_own_answer_and_nothing_is_estimated():
    facts = VM.normalise(load("ytdlp_tiktok_documented_fields.json"))
    assert (facts["views"], facts["likes"], facts["comments"], facts["followers"], facts["posted"], facts["duration_s"]) == (1_200_000, 98_000, 2_100, 63_000, "2026-11-01", 21.4)
    assert facts["outlier"] == 19.0 and facts["music"] == "Madepants - original sound"
    assert facts["hashtags"] == ["pants", "comfy", "comfortfirst"] and facts["captions"] == ["en (automatic)"]
    assert [c["text"] for c in facts["top_comments"]] == ["need in navy", "where are these from??", "low"]          # best first, blank ones dropped
    bare = VM.normalise({"webpage_url": "u", "view_count": 10})
    assert "outlier" not in bare and "music" not in bare and VM.outlier(10, 0) is None and VM.outlier(None, 5) is None and VM.outlier(50, 10) == 5.0


def test_fetching_runs_yt_dlp_without_downloading_and_maps_refusals_to_the_platforms_codes():
    runner = Runner(json.dumps(load("ytdlp_tiktok_documented_fields.json")))
    facts = VM.fetch("https://www.tiktok.com/@m/video/1", run=runner, python="py", cookies="/c.txt", comments=True)
    cmd = runner.calls[0][0]
    assert cmd[:3] == ["py", "-m", "yt_dlp"] and "--skip-download" in cmd and "--dump-single-json" in cmd and "--no-playlist" in cmd and cmd[cmd.index("--cookies") + 1] == "/c.txt" and "--write-comments" in cmd
    assert facts["views"] == 1_200_000
    for stderr, code in (("ERROR: HTTP Error 429: Too Many Requests", K.RATE_LIMITED), ("ERROR: login required", K.PLATFORM_BLOCKED), ("ERROR: Video unavailable", K.NOT_FOUND), ("ERROR: odd", K.UPSTREAM_BROKEN)):
        with pytest.raises(K.Failure) as exc:
            VM.fetch("https://x.example/v", run=Runner("", 1, stderr))
        assert exc.value.code == code
    with pytest.raises(K.Failure):
        VM.fetch("https://x.example/v", run=Runner("nothing"))


def test_facts_are_wanted_only_for_real_video_pages_and_never_for_ad_cards():
    def item(**kw):
        return dict({"platform": "tiktok", "url": "https://www.tiktok.com/@m/video/1"}, **kw)
    assert VM.wants_facts(item()) and VM.wants_facts(item(platform="instagram", url="https://www.instagram.com/reel/abc/"))
    assert not VM.wants_facts(item(platform="meta_ads", url="https://www.facebook.com/ads/library/?id=1")) and not VM.wants_facts(item(url="https://blog.example/post"))
    assert VM.wants_facts(item(platform="facebook", url="https://www.facebook.com/page/videos/123")) and not VM.wants_facts(item(platform="meta_ads", url="https://www.facebook.com/page/videos/123"))
    assert not VM.wants_facts(item(video_meta={"views": 1})) and not VM.wants_facts(item(meta_tries=2))


def test_enriching_items_fills_missing_numbers_adds_the_outlier_from_the_profile_and_stops_a_site_that_refuses():
    items = {"IB-1": {"platform": "tiktok", "handle": "@Madepants", "url": "https://www.tiktok.com/@madepants/video/1", "metrics": {"likes": 5}},
             "IB-2": {"platform": "tiktok", "handle": "x", "url": "https://www.tiktok.com/@x/video/2", "metrics": {}},
             "IB-3": {"platform": "tiktok", "handle": "y", "url": "https://www.tiktok.com/@y/video/3", "metrics": {}}}
    info = dict(load("ytdlp_tiktok_documented_fields.json"))
    del info["channel_follower_count"]
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[-1])
        if cmd[-1].endswith("/2"):
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: login required")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(info), "")
    done, notes = VM.enrich_items(items, ["IB-1", "IB-2", "IB-3"], run=run, followers={("tiktok", "madepants"): 60_000}, now=NOW)
    assert done == 1 and calls == [items["IB-1"]["url"], items["IB-2"]["url"]]                  # the refusal on IB-2 stopped TikTok: IB-3 was not asked
    meta = items["IB-1"]["video_meta"]
    assert meta["followers"] == 60_000 and meta["outlier"] == 20.0 and meta["followers_from"] == "the account's public profile"
    assert items["IB-1"]["metrics"] == {"likes": 5, "views": 1_200_000, "comments": 2_100, "outlier": 20.0}          # an existing number is never overwritten
    assert any("login" in n or "bot check" in n for n in notes) and "video_meta" not in items["IB-2"]


# ---- pace (ffmpeg) --------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("v") / "sample.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=360x640:d=1.5:r=25", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1.5:r=25",
                    "-f", "lavfi", "-i", "color=c=green:s=360x640:d=2:r=25", "-f", "lavfi", "-i", "color=c=yellow:s=360x640:d=3:r=25", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=0.6",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=7.4", "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0[v];[4:a][5:a]concat=n=2:v=0:a=1[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


@needs_ffmpeg
def test_a_real_video_is_measured_cut_by_cut(video):
    p = PACE.analyse(video)
    assert p["vertical"] is True and (p["width"], p["height"]) == (360, 640) and abs(p["duration_s"] - 8.0) < 0.2
    assert p["cuts"] == 3 and p["shots"] == 4 and [round(t) for t in p["cut_times"]] == [2, 3, 5] and abs(p["cut_times"][0] - 1.5) < 0.1 and abs(p["cut_times"][2] - 5.0) < 0.1
    assert p["cuts_first_3s"] == 1 and p["cuts_first_10s"] == 3 and abs(p["avg_shot_s"] - 2.0) < 0.1 and p["pace"] == "fast"
    assert p["has_audio"] is True and abs(p["first_sound_s"] - 0.6) < 0.1 and p["loudness_lufs"] < 0
    text = PACE.describe(p)
    assert "fast pace" in text and "1 cut(s) in the first 3 s" in text and "first sound at 0.6" in text and "LUFS" in text


@needs_ffmpeg
def test_a_file_that_is_not_a_video_is_refused_with_a_reason(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_text("not a video")
    with pytest.raises(PACE.PaceError):
        PACE.analyse(bad)


def test_pace_failures_and_the_sentences_built_from_pace_need_no_ffmpeg():
    with pytest.raises(PACE.PaceError, match="not installed"):
        PACE.probe("x", runner=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert PACE.label(0.9) == "very fast" and PACE.label(1.8) == "fast" and PACE.label(3.0) == "medium" and PACE.label(5) == "slow"
    fast, slow = {"avg_shot_s": 1.2, "cuts_first_3s": 2}, {"avg_shot_s": 3.5, "cuts_first_3s": 0}
    assert PACE.rule_from([fast]) == "" and PACE.rule_from([None, fast]) == ""
    rule = PACE.rule_from([fast, fast, slow])
    assert "Across 3 measured winning videos" in rule and "1.2 s" in rule and "2 of them change shot within the first 3 seconds" in rule
    assert PACE.describe(None) == ""


# ---- the pool catalogue ---------------------------------------------------------------------------------------------------------------------------

def test_every_provider_the_gateway_serves_is_in_the_pool_and_never_offered_again():
    entries, where = POOL.read(None)
    assert len(entries) == 465 and "snapshot" in where
    providers = yaml.safe_load(Path("ai_providers.yaml").read_text(encoding="utf-8"))["providers"]
    groups = POOL.candidates(entries, providers)
    offered = {s["provider"] for k in ("recommended", "avoid") for s in groups[k]}
    norm = {POOL._norm(g) for g in POOL.GATEWAY_PLATFORMS}
    assert not [p for p in offered if POOL._norm(p) in norm]
    in_pool = {s["provider"] for s in groups["in_pool"]}
    assert {"glm", "glm-cn", "agnes", "kilo-gateway", "cloudflare-ai", "gemini", "nvidia", "mistral"} <= in_pool          # glm is Zhipu, which the gateway serves
    assert "vertex" not in in_pool and "ovhcloud" in in_pool


def test_the_gateways_own_providers_are_recognised_even_when_the_pool_file_names_none_of_them():
    groups = POOL.candidates(POOL.read(None)[0], {})
    in_pool = {s["provider"] for s in groups["in_pool"]}
    assert {"groq", "mistral", "cerebras", "glm", "glm-cn", "agnes", "kilo-gateway", "cloudflare-ai", "gemini", "nvidia", "openrouter", "reka"} <= in_pool
    assert not ({s["provider"] for k in ("recommended", "avoid") for s in groups[k]} & in_pool) and "glm" not in {s["provider"] for s in groups["recommended"]}


def test_the_catalogues_own_terms_verdict_sorts_providers_and_nothing_is_hidden():
    entries, _ = POOL.read(None)
    groups = POOL.candidates(entries, {})
    assert {"kiro", "t3-web", "duckduckgo-web"} <= {s["provider"] for s in groups["avoid"]} and all("avoid" in s["tos"] for s in groups["avoid"])
    assert not any("avoid" in s["tos"] for s in groups["recommended"]) and all(s["kinds"] for s in groups["recommended"])
    assert sum(len(v) for v in groups.values()) == len({e["provider"] for e in entries})          # every provider is accounted for in exactly one group
    text = "\n".join(POOL.render(groups, "x"))
    assert "Listed, not recommended" in text and "kiro:" in text and "(the owner decides)" in text


def test_the_catalogue_is_read_from_the_repositorys_own_file_when_it_is_there(tmp_path):
    f = tmp_path / POOL.DATA_FILE
    f.parent.mkdir(parents=True)
    f.write_text('export const X = [ { provider: "acme", modelId: "m1", displayName: "Acme One", monthlyTokens: 1_000, freeType: "recurring-monthly", tos: "ok", hardStopGuaranteed: true }, '
                 '{ provider: "acme", modelId: "m2", monthlyTokens: 2_000, freeType: "recurring-monthly", tos: "ok" } ];', encoding="utf-8")
    entries, where = POOL.read(tmp_path)
    assert where == "the OmniRoute checkout" and [(e["model"], e["monthly_tokens"], e["hard_stop"]) for e in entries] == [("m1", 1000, True), ("m2", 2000, False)]
    only = POOL.candidates(entries, {})
    assert only["recommended"][0]["monthly_tokens"] == 3000 and only["recommended"][0]["hard_stop"] is False


def test_the_gateways_provider_list_is_read_from_its_source_when_given(tmp_path):
    src = tmp_path / "server" / "src" / "providers"
    src.mkdir(parents=True)
    names = "\n".join(f"register(new OpenAICompatProvider({{ platform: 'p{i}' }}));" for i in range(25))
    (src / "index.ts").write_text(names, encoding="utf-8")
    assert {"p3", "p24"} <= set(POOL.gateway_platforms(tmp_path)) and "groq" in POOL.gateway_platforms(tmp_path) and POOL.gateway_platforms(None) == POOL.GATEWAY_PLATFORMS


# ---- motion prompts -------------------------------------------------------------------------------------------------------------------------------

def test_the_prompt_library_snapshot_is_whole_and_a_match_needs_two_shared_words_and_carries_its_author():
    entries, where = MOTION.read(None)
    assert len(entries) == 703 and "snapshot" in where and set(MOTION.categories(entries)) >= {"motion", "3d", "explainer", "interactive"}
    hit = MOTION.search(entries, "kinetic typography text reveal", 2)
    assert hit and all(e["author"] and e["post_url"].startswith("https://x.com/") for e in hit)
    assert all(len({w for w in __import__("re").findall(r"[a-z0-9]{4,}", "kinetic typography text reveal")} & MOTION._words(e["prompt"] + " " + " ".join(e["tags"]) + " " + e["category"])) >= 2 for e in hit)
    assert MOTION.search(entries, "kinetic zzzzqq", 3) == [] and MOTION.search(entries, "zzzz qqqq", 3) == [] and MOTION.search(entries, "kinetic typography", 5, "explainer") == [e for e in MOTION.search(entries, "kinetic typography", 5, "explainer") if e["category"] == "explainer"]
    assert hit[0]["author"] in MOTION.line(hit[0]) and hit[0]["post_url"] in MOTION.line(hit[0])


# ---- Openverse ------------------------------------------------------------------------------------------------------------------------------------

def test_openverse_results_carry_licence_creator_and_a_credit_line():
    get = Get({"api.openverse.org/v1/images": (200, (FIX / "openverse_images_documented_fields.json").read_text())})
    found = OV.search(get, "cotton texture", "images", limit=5)
    url = get.urls[0]
    assert "q=cotton+texture" in url and "license_type=commercial" in url and "mature=false" in url and "page_size=5" in url
    assert len(found) == 1 and found[0]["license"] == "by" and found[0]["creator"] == "Ana" and OV.credit_line(found[0]) == "\"Cotton texture\" by Ana is licensed under CC BY 2.0."
    assert OV.credit_line(dict(found[0], attribution="")).startswith('"Cotton texture" by Ana (BY 2.0)')


def test_openverse_failures_use_the_platforms_codes_and_the_backup_takes_over():
    for status, code in ((429, K.RATE_LIMITED), (403, K.PLATFORM_BLOCKED), (500, K.UPSTREAM_BROKEN)):
        with pytest.raises(K.Failure) as exc:
            OV.search(Get({"api.openverse": (status, "")}), "x")
        assert exc.value.code == code
    with pytest.raises(K.Failure):
        OV.search(Get({}), "x", "video")
    commons = json.dumps({"query": {"pages": {"1": {"title": "File:Cotton.jpg", "imageinfo": [{"url": "https://c.example/c.jpg", "thumburl": "https://c.example/t.jpg", "descriptionurl": "https://c.example/page", "width": 640, "height": 480,
                                                                                            "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": "<a href='x'>Bo</a>"}}}]}}}})
    get = Get({"api.openverse": (500, ""), "commons.wikimedia": (200, commons)})
    found, which = OV.find(get, "cotton", "images")
    assert which == "commons" and found[0]["license"] == "cc-by-sa-4.0" and found[0]["creator"] == "Bo" and found[0]["title"] == "Cotton.jpg"
    assert OV.find(get, "cotton", "images", use_openverse=False)[1] == "commons" and not any("api.openverse" in u for u in get.urls[2:])
    audio = json.dumps({"query": {"pages": {"1": {"title": "File:Song.ogg", "imageinfo": [{"url": "https://c.example/s.ogg", "descriptionurl": "https://c.example/sp", "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}}}]}}}})
    found, which = OV.find(Get({"commons.wikimedia": (200, audio)}), "song", "audio", use_openverse=False)
    assert which == "commons" and found[0]["kind"] == "audio" and found[0]["title"] == "Song.ogg"
    sent = Get({"commons.wikimedia": (200, audio)})
    OV.find(sent, "song", "audio", use_openverse=False)
    assert "filetype%3Aaudio" in sent.urls[0] and "bitmap" not in sent.urls[0]
    with pytest.raises(K.Failure):
        OV.find(Get({"api.openverse": (500, ""), "commons.wikimedia": (500, "")}), "song", "audio")


def test_a_token_is_asked_for_only_with_both_halves_of_the_registration_and_a_refusal_means_anonymous():
    calls = []

    def post(url, data):
        calls.append((url, data))
        return 200, json.dumps({"access_token": "tok"})
    assert OV.token(post, "", "") == "" and calls == [] and OV.token(post, "id", "") == ""
    assert OV.token(post, "id", "secret") == "tok" and calls[0][1]["grant_type"] == "client_credentials"
    assert OV.token(lambda u, d: (401, ""), "id", "secret") == "" and OV.token(lambda u, d: (200, "oops"), "id", "secret") == ""


# ---- the second Ad Library reader -----------------------------------------------------------------------------------------------------------------

def test_the_second_reader_finds_every_card_once_and_decodes_the_landing_page_and_button():
    ads = AD.parse((FIX / "adlib_cards.md").read_text())
    assert [a["library_id"] for a in ads] == ["1111111111", "2222222222"]                            # the card drawn twice is kept once
    first, second = ads
    assert (first["advertiser"], first["advertiser_handle"], first["ads_using_creative"], first["started_running"]) == ("Madepants", "madepants", 3, "Oct 3, 2026")
    assert first["landing_url"] == "https://madepants.com/collections/all?utm=fb" and first["landing_domain"] == "madepants.com" and first["cta"] == "Shop now"
    assert first["link_text"] == "Comfy pants for all" and first["platforms"] == ["Facebook", "Instagram"] and first["body"].startswith("Our best selling pair is back")
    assert second["cta"] is None and "landing_url" not in second and second["ads_using_creative"] == 1 and AD.advertisers(ads) == {"Madepants": 1, "The Oodie": 1}


def test_the_second_reader_reads_the_html_our_own_browser_fetched():
    html = ('<div>Library ID: 5555</div><div>Started running on Nov 1, 2026</div><div><strong>2 ads</strong> use this creative</div><a href="https://www.facebook.com/dudewipes/">Dude Wipes</a>'
            '<div><strong>Sponsored</strong></div><div>Wipes that work</div><a href="https://l.facebook.com/l.php?u=https%3A%2F%2Fdudewipes.com%2F&h=1"> Shop now </a><script>Library ID: 999</script>')
    ads = AD.read_page(html)
    assert len(ads) == 1 and (ads[0]["advertiser"], ads[0]["ads_using_creative"], ads[0]["cta"], ads[0]["landing_domain"]) == ("Dude Wipes", 2, "Shop now", "dudewipes.com")


def test_the_second_reader_adds_only_what_is_missing_and_supplies_ads_only_when_the_first_found_none():
    ad = AD.parse((FIX / "adlib_cards.md").read_text())[0]
    row = {"item_id": "meta_ads:1111111111", "details": "format: VIDEO; CTA: Learn more", "media_url": "", "variants": 4}
    filled = AD.fill_missing(row, ad)
    assert "CTA: Learn more" in row["details"] and "CTA: Shop now" not in row["details"]                  # the first reader's button is never replaced
    assert "headline: Comfy pants for all" in row["details"] and "landing: https://madepants.com/collections/all?utm=fb" in row["details"]
    assert row["media_url"].startswith("https://scontent") and row["variants"] == 4 and filled == ["details", "media_url"]
    html_cards = AD.html_to_markdown("<div>Library ID: 77</div><a href='https://www.facebook.com/x/'>Brand X</a>")
    assert "Library ID: 77" in html_cards
    page = "<div>Library ID: 77</div><div>Started running on Nov 1, 2026</div><a href='https://www.facebook.com/x/'>Brand X</a><div><strong>Sponsored</strong></div><div>Hello there</div>"
    existing = [{"item_id": "meta_ads:77", "details": "", "media_url": "", "variants": None}]
    new, facts = AD.apply(page, existing, row_factory=C._row)
    assert new == [] and facts["seen"] == 1 and facts["filled"] == 1 and existing[0]["variants"] == 1
    elsewhere, facts = AD.apply(page, [{"item_id": "meta_ads:999", "details": "", "media_url": "", "variants": 1}], row_factory=C._row)
    assert elsewhere == [] and facts["added"] == 0                                                                     # the first reader found other ads: this one is not added
    new, facts = AD.apply(page, [], row_factory=C._row)
    assert len(new) == 1 and new[0]["item_id"] == "meta_ads:77" and new[0]["handle"] == "Brand X" and new[0]["found_via"] == "secondary reader" and facts["added"] == 1
    assert AD.apply("<div>nothing here</div>", [], row_factory=C._row)[1]["seen"] == 0


def test_the_second_reader_is_off_when_the_owner_switches_the_connection_off_and_never_raises():
    off = {"services": {"connections": {"enabled": True, "channels": {"fb_ads_library_mcp": False}}}}
    assert AD.make_reader(off, C._row) is None
    reader = AD.make_reader(SETTINGS, C._row)
    assert reader("<div>Library ID: 1</div>", []) [1]["seen"] == 1 and reader(None, [])[1]["seen"] == 0
    assert C._page_notes({"secondary": {"seen": 3, "filled": 2, "added": 1}}).count("second reader") == 1 and "second reader" not in C._page_notes({"secondary": {"seen": 3, "filled": 0, "added": 0}})
    assert "second reader" not in C._page_notes({})


def _meta_run(secondary, json_rows, page_html):
    from contentintel import browser as B
    from fake_browser import FakeBrowser
    from test_collectors import META_JSON
    cfg = B.load_browser_settings("browser.yaml")
    site = cfg["sites"]["meta_ads"]
    browser = FakeBrowser({"https://www.facebook.com/ads/library": {"json": [META_JSON] if json_rows else []}}, clickable=set(site["consent_any"]))
    browser.page_html = page_html
    task = C.Task("meta_ads", "t", {"urls": ["https://www.facebook.com/ads/library/?q=acme"]}, "k", 5)
    return C.run_browser_task(browser, task, 5, site, cfg, {}, secondary=secondary)


PAGE = ("<div>Library ID: 111</div><div>Started running on Oct 3, 2026</div><a href='https://www.facebook.com/acme/'>Acme</a><div><strong>Sponsored</strong></div><div>Body one</div>"
        "<div>Library ID: 112</div><div>Started running on Oct 4, 2026</div><a href='https://www.facebook.com/beta/'>Beta</a><div><strong>Sponsored</strong></div><div>Body two</div>")


def test_the_collector_runs_the_second_reader_after_the_first_and_changes_nothing_without_it():
    plain_rows, plain_stats = _meta_run(None, True, PAGE)
    assert "secondary" not in plain_stats and [r["item_id"] for r in plain_rows] == ["meta_ads:111", "meta_ads:112"]
    rows, stats = _meta_run(AD.make_reader(SETTINGS, C._row), True, PAGE)
    assert [r["item_id"] for r in rows] == [r["item_id"] for r in plain_rows] and stats["secondary"]["seen"] == 2 and stats["secondary"]["added"] == 0           # the first reader found ads: none are added
    assert all(r["text"] == p["text"] and r["handle"] == p["handle"] for r, p in zip(rows, plain_rows))                                                     # nothing the first reader said was changed
    empty, stats = _meta_run(AD.make_reader(SETTINGS, C._row), False, PAGE)
    assert [r["item_id"] for r in empty] == ["meta_ads:111", "meta_ads:112"] and stats["secondary"]["added"] == 2 and "read by the second Ad Library reader" in empty[0]["details"]
    none, stats = _meta_run(AD.make_reader(SETTINGS, C._row), False, "<div>no ads here</div>")
    assert none == [] and stats["secondary"] == {"seen": 0, "filled": 0, "added": 0}
    assert _meta_run(None, False, PAGE)[0] == []                                                                                                              # without the reader the empty page stays empty


# ---- page matching and the main-page finder -------------------------------------------------------------------------------------------------------

def test_a_page_name_is_matched_exactly_by_a_shared_word_or_by_containment_and_decoration_is_ignored():
    assert PM.strength("Chobani", "Chobani | Facebook") == PM.EXACT and PM.strength("Chobani", "Chobani - Official Page") == PM.EXACT
    v = PM.judge("Chobani", ["Chobani | Facebook", "Chobani Australia", "Dairy Farmers"])
    assert [x["strength"] for x in v] == [PM.EXACT, PM.TOKEN, PM.NONE] and PM.clean_title("Chobani | Facebook") == "Chobani"
    assert PM.strength("Dude Wipes", "dudewipes") == PM.EXACT and PM.strength("Acme", "Acmetools") == PM.CONTAINED and PM.strength("", "x") == PM.NONE


def test_a_page_that_shares_only_a_category_word_is_never_the_brand():
    assert PM.category_only("Dude Wipes", "Dude Perfect") is True and PM.category_only("Dude Wipes", "Dude Wipes") is False
    assert PM.category_only("Chobani", "Chobani Australia") is False and PM.category_only("Acme Kitchen", "Kitchen World") is True and PM.category_only("Acme Kitchen", "Acme Kitchen Supplies") is False
    verdicts = PM.judge("Acme Kitchen", ["Kitchen World", "Acme Kitchen", "Kitchen Hub", "Kitchen Depot"])
    best = min(verdicts, key=PM.sort_key)
    assert best["name"] == "Acme Kitchen" and verdicts[0]["rarity"] > 1 and "category word" in PM.explain(verdicts[0])


def test_the_finder_ranks_the_pages_that_carry_the_brands_name_ahead_of_lookalikes_whatever_order_the_search_gave():
    cands = [{"url": "https://www.facebook.com/dudeperfect", "slug": "dudeperfect", "title": "Dude Perfect | Facebook", "snippet": ""},
             {"url": "https://www.facebook.com/dudewipes", "slug": "dudewipes", "title": "Dude Wipes | Facebook", "snippet": ""}]
    ranked = MP.rank("Dude Wipes", cands)
    assert ranked[0]["slug"] == "dudewipes" and ranked[0]["match"]["strength"] == PM.EXACT and ranked[1]["match"]["category_only"] is True
    cands2 = [{"url": "https://www.facebook.com/chobaniau", "slug": "chobaniau", "title": "Chobani Australia", "snippet": ""}, {"url": "https://www.facebook.com/Chobani", "slug": "Chobani", "title": "Chobani", "snippet": ""}]
    assert MP.rank("Chobani", cands2)[0]["slug"] == "Chobani"


def test_the_match_tiers_alone_decide_when_no_address_is_the_brands_own_name():
    cands = [{"url": "https://www.facebook.com/dudeperfect", "slug": "dudeperfect", "title": "Dude Perfect | Facebook", "snippet": ""},
             {"url": "https://www.facebook.com/dudewipesofficial", "slug": "dudewipesofficial", "title": "Dude Wipes | Facebook", "snippet": ""}]
    assert MP.rank("Dude Wipes", cands)[0]["slug"] == "dudewipesofficial"              # neither address is 'dudewipes': only the match decides, whatever the search order


def test_a_stronger_match_is_ahead_of_a_rarer_word_and_a_brand_page_is_ahead_of_a_lookalike():
    exact_common = {"strength": PM.EXACT, "rarity": 3, "category_only": False}
    token_rare = {"strength": PM.TOKEN, "rarity": 1, "category_only": False}
    assert PM.sort_key(exact_common) < PM.sort_key(token_rare) < PM.sort_key(dict(token_rare, strength=PM.CONTAINED))
    assert PM.sort_key(token_rare) < PM.sort_key(dict(token_rare, category_only=True)) and PM.sort_key(token_rare) < PM.sort_key(dict(token_rare, rarity=2))


def test_a_reviewer_choice_that_is_a_lookalike_while_the_brands_own_page_exists_is_left_to_a_person():
    results = [{"url": "https://www.facebook.com/dudeperfect", "title": "Dude Perfect | Facebook", "snippet": ""}, {"url": "https://www.facebook.com/dudewipes", "title": "Dude Wipes | Facebook", "snippet": ""}]
    out = MP.lookup_main_page("Dude Wipes", lambda q: results, lambda name, ranked: {"choice": "https://www.facebook.com/dudeperfect", "is_main": True, "regional": False, "reason": "bigger"}, lambda url: "123456789")
    assert out["status"] == "ambiguous" and "carries the brand's name" in out["reason"] and "dudewipes" in out["reason"] and not out["page_id"]
    good_pick = MP.lookup_main_page("Dude Wipes", lambda q: results, lambda name, ranked: {"choice": "https://www.facebook.com/dudewipes", "is_main": True, "regional": False, "reason": "official"}, lambda url: "229927120383343")
    assert good_pick["status"] == "resolved" and good_pick["page_id"] == "229927120383343"


# ---- the Idea Bank and the briefs -----------------------------------------------------------------------------------------------------------------

def _scored_item():
    bank = bank_with("overalls straps slip fix")
    item = bank["items"]["IB-0001"]
    item.update({"title": "straps that slip on overalls", "text": "the straps on bib overalls slip all day"})
    return bank, item


def test_an_item_without_evidence_is_described_exactly_as_before_and_one_with_evidence_says_what_was_measured():
    bank, item = _scored_item()
    plain = IB._item_line(item)
    assert "measured" not in plain and plain == IB._item_line(item, None)
    item["video_meta"] = {"outlier": 19.0, "music": "A - B", "duration_s": 21.4, "captions": ["en"]}
    item["pace"] = {"pace": "fast", "avg_shot_s": 1.4, "shots": 9, "cuts_first_3s": 2, "has_audio": True, "first_sound_s": 0.0, "loudness_lufs": -14.0}
    voice, _ = VOICE.merge({}, [VOICE._item("reddit", "straps that slip on overalls all day", "https://r/1", "", {"score": 410}, "", "", "t", "research")], NOW)
    line = IB._item_line(item, voice)
    assert line.startswith(plain) and "| measured:" in line and "19x the poster's followers in views" in line and "sound: A - B" in line
    assert "fast pace: average shot 1.4 s over 9 shot(s)" in line and "2 cut(s) in the first 3 s" in line and 'people say (reddit, 410 engagement): "straps that slip on overalls all day"' in line


def test_scoring_sends_what_audiences_say_and_the_measured_facts_and_notes_the_echo_on_the_item(store, swov):
    bank, item = _scored_item()
    item["pace"] = {"pace": "fast", "avg_shot_s": 1.4, "shots": 9, "cuts_first_3s": 2, "has_audio": False}
    voice, _ = VOICE.merge({}, [VOICE._item("reddit", "straps that slip on overalls all day", "https://r/1", "complaint", {"score": 410}, "", "", "t", "research")], NOW)
    store.write_extra(swov.slug, "voice", voice)
    claude = FakeClaude()
    claude.json_answers = [{"items": [good("IB-0001", 90)]}]
    (scored, _), _ = run_scoring(store, swov, bank, claude)
    prompt = claude.json_calls[0][1]
    assert scored == 1 and "What audiences say right now" in prompt and "410 engagement" in prompt and "| measured:" in prompt and "no audio track" in prompt
    assert item["people_say"] == "straps that slip on overalls all day (reddit, 410 engagement)"
    quiet = FakeClaude()
    quiet.json_answers = [{"items": [good("IB-0001", 90)]}]
    bank2, _ = _scored_item()
    run_scoring(Store(store.data_dir.parent / "other"), swov, bank2, quiet)
    assert "What audiences say" not in quiet.json_calls[0][1] and "measured" not in quiet.json_calls[0][1]                  # no evidence: the prompt is the old one


def test_the_sheet_has_the_new_columns_in_the_row_and_the_numeric_outlier(store, swov):
    for column in ("outlier", "sound", "pace", "people_say"):
        assert column in IB.IDEABANK_COLUMNS
    assert IB.IDEABANK_COLUMNS.index("outlier") == IB.IDEABANK_COLUMNS.index("look") + 1
    bank, item = _scored_item()
    item.update({"state": "kept", "score": 80, "metrics": {"outlier": 19.0}, "video_meta": {"music": "A - B"}, "pace": {"pace": "fast", "avg_shot_s": 1.4, "shots": 9, "cuts_first_3s": 0, "has_audio": False}, "people_say": "echo"})
    row = IB.sheet_rows(bank)["IB-0001"]
    assert set(IB.IDEABANK_COLUMNS) <= set(row) and (row["outlier"], row["sound"], row["people_say"]) == (19.0, "A - B", "echo") and row["pace"].startswith("fast pace")
    from contentintel import sheets
    assert "outlier" in sheets.NUMERIC


def test_the_brief_carries_evidence_only_when_there_is_some_and_keeps_it_apart_from_the_facts(store, swov):
    bank, item = _scored_item()
    item["pace"] = {"pace": "fast", "avg_shot_s": 1.2, "shots": 5, "cuts_first_3s": 2, "has_audio": True, "first_sound_s": 0.0}
    items = [item, dict(item, id="IB-0002")]
    voice, _ = VOICE.merge({}, [VOICE._item("reddit", "straps slip", "https://r/1", "", {"score": 410}, "", "", "t", "research")], NOW)
    store.write_extra(swov.slug, "voice", voice)
    motion = MOTION.search(MOTION.read(None)[0], "kinetic typography text reveal", 1)
    assets = [{"title": "Cotton", "url": "https://i/a.jpg", "page": "https://i/p", "license": "by", "license_version": "2.0", "creator": "Ana", "attribution": ""}]
    text = BR.extras_text(store, swov, items, motion=motion, assets=assets)
    assert "What audiences are saying right now" in text and "410 engagement" in text and "Across 2 measured winning videos" in text and "never copy" in text and "credit: " in text and "[BY]" in text
    prompt = BR.build_prompt(swov, "ask", items, {}, _ruleset(), 3, text)
    assert "Evidence for the concepts (not facts about our products)" in prompt and "Measured: fast pace" in prompt
    assert "Evidence for the concepts" not in BR.build_prompt(swov, "ask", items, {}, _ruleset(), 3, "")
    assert BR.extras_text(Store(store.data_dir.parent / "nothing"), swov, [dict(item, pace=None)]) == ""


def _ruleset():
    from contentintel import rules
    return rules.load_ruleset(Path("briefs") / "swoveralls-new-audience" / "rules.yaml")


# ---- hooks, commands, workflow --------------------------------------------------------------------------------------------------------------------

def test_the_idea_bank_hooks_follow_the_owners_yt_dlp_switch_and_what_is_installed(monkeypatch, tmp_path):
    class A:
        data_dir = str(tmp_path)
    off = {"services": {"connections": {"enabled": True, "channels": {"yt_dlp": False}}}}
    monkeypatch.setattr(RT, "_settings", lambda args: off)
    facts, pace, motion = RT.ideabank_hooks(A(), None, environ={})
    assert facts is None and motion
    monkeypatch.setattr(RT, "_settings", lambda args: SETTINGS)
    monkeypatch.setattr(RT.shutil, "which", lambda name: "/usr/bin/" + name)
    facts, pace, motion = RT.ideabank_hooks(A(), None, environ={})
    assert callable(facts) and callable(pace) and len(motion) == 703
    monkeypatch.setattr(RT.shutil, "which", lambda name: None)
    monkeypatch.setattr(RT, "_has_module", lambda name: False)
    off_motion = {"services": {"connections": {"enabled": True, "channels": {"opus_video_prompts": False}}}}
    monkeypatch.setattr(RT, "_settings", lambda args: off_motion)
    assert RT.ideabank_hooks(A(), None, environ={}) == (None, None, None)


def test_the_new_commands_parse_and_the_read_only_ones_run(capsys, tmp_path):
    parser = cli.build_parser()
    assert parser.parse_args(["voice", "--brand", "swoveralls", "--mode", "competitors", "--peers", "A,B", "--depth", "deep", "--dry-run"]).peers == "A,B"
    assert parser.parse_args(["assets", "--query", "q", "--kind", "audio"]).kind == "audio" and parser.parse_args(["analyze", "--url", "u", "--comments"]).comments is True
    assert cli.main(["motion", "--query", "kinetic typography text reveal", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "703 prompts" in out and "by " in out and "https://x.com/" in out
    assert cli.main(["pool", "--data-dir", str(tmp_path)]) == 0
    assert "Candidates (recurring or keyless free tier" in capsys.readouterr().out
    assert cli.main(["analyze", "--data-dir", str(tmp_path)]) == 1


def test_the_voice_command_keeps_what_it_finds_and_says_what_failed(tmp_path, capsys, monkeypatch):
    runner = Runner("", 1, "boom")
    monkeypatch.setattr(RT, "_settings", lambda args: SETTINGS)
    args = cli.build_parser().parse_args(["voice", "--brand", "swoveralls", "--topic", "bib overalls", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands"])
    code = RT.run_voice(args, Store(tmp_path / "data"), NOW, get=Get({"algolia": (200, HN), "reddit.com": (200, RD)}), runner=runner, environ={}, clock=lambda: 1_790_000_000)
    out = capsys.readouterr().out
    assert code == 0 and "2 item(s) from builtin (2 new)" in out and "audience voice now holds 2 item(s)" in out
    kept = Store(tmp_path / "data").read_extra("swoveralls", "voice")
    assert {i["source"] for i in kept["items"]} == {"hackernews", "reddit"} and kept["seed_topics"]
    dry = cli.build_parser().parse_args(["voice", "--brand", "swoveralls", "--topic", "x", "--dry-run", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands"])
    assert RT.run_voice(dry, Store(tmp_path / "data"), NOW, environ={}) == 0 and "would ask research about 'x'" in capsys.readouterr().out


def test_the_research_tools_workflow_offers_every_tool_installs_the_right_connection_and_documents_its_secrets():
    text = Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["tool"]["options"] == ["voice", "analyze", "pool", "assets", "motion", "pageid", "apify-budget", "analytics", "install"] and inputs["mode"]["options"] == list(VOICE.MODES)
    for tool, key in (("voice", "last30days"), ("analyze", "yt_dlp"), ("pool", "omniroute_catalog"), ("motion", "opus_video_prompts")):
        assert f"{tool}) keys=({key}) ;;" in text and key in CONN.CONNECTIONS
    assert "install) keys=(last30days yt_dlp omniroute_catalog opus_video_prompts fb_ads_library_mcp)" in text
    assert "git add -f data" in text and wf["concurrency"]["group"] == "catalogue-data" and wf["permissions"] == {"contents": "write"}
    assert text.count("secrets.OPENVERSE_CLIENT_ID") == 1 and text.count("secrets.OPENVERSE_CLIENT_SECRET") == 1 and text.count("secrets.SCRAPECREATORS_API_KEY") == 1
    assert "pool) args=(pool); if [ -d gateway-src ]; then args+=(--gateway-dir gateway-src); fi ;;" in text and "git clone --depth 1 --branch \"$tag\" https://github.com/tashfeenahmed/freellmapi gateway-src" in text
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "`OPENVERSE_CLIENT_ID`" in readme and "`OPENVERSE_CLIENT_SECRET`" in readme and "`SCRAPECREATORS_API_KEY`" in readme


# ---- the hooks reach the Idea Bank, the collector and the commands --------------------------------------------------------------------------------

@pytest.fixture
def drive():
    from fake_drive import EMAIL, ROOT, FakeDrive
    from contentintel.drive import DriveClient
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


def test_a_saved_video_is_measured_while_the_file_is_on_disk_and_a_failure_of_the_measurement_never_stops_the_saving(drive):
    from media_fixtures import tiny_mp4
    from test_media_integrity import kept_with_cover, runner_writing
    from test_ideabank import S
    client, _ = drive
    seen = []

    def measure(path):
        seen.append(Path(path).exists())
        return {"pace": "fast", "avg_shot_s": 1.4, "shots": 9, "cuts_first_3s": 2, "has_audio": False}
    bank = kept_with_cover("A")
    saved, _ = IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_writing(tiny_mp4(seconds=20)), pace_fn=measure)
    assert saved == 1 and seen == [True] and bank["items"]["IB-0001"]["pace"]["avg_shot_s"] == 1.4
    bank = kept_with_cover("A")
    saved, _ = IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_writing(tiny_mp4(seconds=20)), pace_fn=lambda p: 1 / 0)
    assert saved == 1 and "pace" not in bank["items"]["IB-0001"]
    bank = kept_with_cover("A")
    IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_writing(tiny_mp4(seconds=20)))
    assert "pace" not in bank["items"]["IB-0001"]


def test_video_facts_are_read_before_scoring_and_reach_the_prompt_the_item_and_the_sheet_row(store, swov, drive):
    from test_ideabank import S, full_run, scored, seeded
    seeded(store)
    asked = []

    def facts(items, ids):
        asked.append((list(ids), [items[i]["score"] for i in ids]))
        for i in ids:
            items[i]["video_meta"] = {"outlier": 19.0, "music": "A - B"}
            items[i]["metrics"]["outlier"] = 19.0
        return len(ids), ["one note"]
    claude = scored(("IB-0001", 80, "t"), ("IB-0002", 80, "t"))
    new, lines = full_run(store, swov, drive, claude, video_facts=facts)
    assert asked == [(["IB-0001", "IB-0002"], [None, None])]                                   # asked before any item was scored
    assert "19x the poster's followers in views" in claude.json_calls[0][1] and any("facts read for 2 video(s)" in x for x in lines) and "swoveralls: one note" in lines
    bank = store.read_ideabank("swoveralls")
    assert bank["items"]["IB-0001"]["video_meta"]["music"] == "A - B" and IB.sheet_rows(bank)["IB-0001"]["outlier"] == 19.0 and IB.sheet_rows(bank)["IB-0001"]["sound"] == "A - B"
    assert S is not None


def test_a_failing_facts_step_is_reported_and_the_rest_of_the_run_goes_on(store, swov, drive):
    from test_ideabank import full_run, scored, seeded
    seeded(store)
    claude = scored(("IB-0001", 80, "t"), ("IB-0002", 80, "t"))
    new, lines = full_run(store, swov, drive, claude, video_facts=lambda items, ids: 1 / 0)
    assert any("video facts skipped (ZeroDivisionError)" in x for x in lines) and any("item(s) scored" in x for x in lines)
    new, lines = full_run(store, swov, drive, None)
    assert not any("facts" in x for x in lines)


def test_the_command_line_hands_the_hooks_to_the_idea_bank_run(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli.tools_mod, "ideabank_hooks", lambda args, store: ("FACTS", "PACE", "MOTION"))
    monkeypatch.setattr(cli.ib_mod, "run_ideabank", lambda *a, **kw: (seen.update(kw) or ([], ["ok"])))
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = cli.main(["ideabank", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands", "--sources-config", "sources.yaml"])
    assert code == 0 and seen["video_facts"] == "FACTS" and seen["pace_fn"] == "PACE" and seen["motion_entries"] == "MOTION"


def test_the_second_reader_is_built_from_the_owners_switch_and_given_to_the_collector(monkeypatch, tmp_path):
    class A:
        data_dir = str(tmp_path)
    monkeypatch.setattr(cli, "_hub_settings", lambda args: SETTINGS)
    assert callable(cli._secondary_reader(A()))
    monkeypatch.setattr(cli, "_hub_settings", lambda args: {"services": {"connections": {"enabled": True, "channels": {"fb_ads_library_mcp": False}}}})
    assert cli._secondary_reader(A()) is None
    monkeypatch.setattr(cli, "_hub_settings", lambda args: None)
    assert cli._secondary_reader(A()) is None


def test_run_collect_hands_the_second_reader_to_every_meta_page_it_reads(tmp_path, store, swov, monkeypatch):
    from dataclasses import replace
    from contentintel import browser as B
    from contentintel import research as R
    from test_collectors import CREDS, FULL, Factory, META_JSON, write_research
    from fake_browser import FakeBrowser
    cfg = B.load_browser_settings("browser.yaml")
    settings = replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False)
    got = []
    real = C.run_browser_task

    def spy(*a, **kw):
        got.append(kw.get("secondary"))
        return real(*a, **kw)
    monkeypatch.setattr(C, "run_browser_task", spy)
    marker = lambda html, rows: ([], {"seen": 0, "filled": 0, "added": 0})                     # noqa: E731
    write_research(tmp_path, swov, {"track_own_brand": False, **FULL})
    factory = Factory({"meta_ads": FakeBrowser({"https://www.facebook.com/ads/library": {"json": [META_JSON]}}, clickable=set(cfg["sites"]["meta_ads"]["consent_any"]))})
    C.run_collect([swov], store, browser_factory=factory, creds=CREDS, settings=settings, browser_cfg=cfg, ledger_cls=R.Ledger, sources=["meta_ads"], now=NOW, run_id="r", root=str(tmp_path / "brands"), secondary=marker)
    assert got and all(s is marker for s in got)


def test_the_assets_command_follows_the_owners_openverse_switch_and_keeps_what_it_finds(tmp_path, capsys, monkeypatch):
    commons = json.dumps({"query": {"pages": {"1": {"title": "File:Cotton field.jpg", "imageinfo": [{"url": "https://c/a.jpg", "descriptionurl": "https://c/p", "extmetadata": {"LicenseShortName": {"value": "CC0"}}}]}}}})
    get = Get({"api.openverse": (200, (FIX / "openverse_images_documented_fields.json").read_text()), "commons.wikimedia": (200, commons)})
    args = cli.build_parser().parse_args(["assets", "--query", "cotton", "--brand", "swoveralls", "--data-dir", str(tmp_path)])
    store = Store(tmp_path)
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    assert RT.run_assets(args, store, NOW, get=get, post=lambda u, d: (500, ""), environ={}) == 0
    assert "from openverse" in capsys.readouterr().out and store.read_extra("swoveralls", "assets")["assets"][0]["license"] == "by"
    off = {"services": {"openverse": {"enabled": False, "use_for": {"brief": True}, "channels": {}}}}
    monkeypatch.setattr(RT, "_settings", lambda a: off)
    get.urls.clear()
    RT.run_assets(args, Store(tmp_path / "other"), NOW, get=get, post=lambda u, d: (500, ""), environ={})
    assert "from commons" in capsys.readouterr().out and not any("api.openverse" in u for u in get.urls)


def test_a_collect_run_from_the_command_line_hands_the_second_reader_to_the_collector(monkeypatch, tmp_path):
    seen = {}
    real = C.run_collect

    def spy(*a, **kw):
        seen["secondary"] = kw.get("secondary")
        return real(*a, **kw)
    monkeypatch.setattr(C, "run_collect", spy)
    monkeypatch.setattr(cli, "_hub_settings", lambda args: SETTINGS)
    cli.main(["collect", "--brand", "swoveralls", "--dry-run", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands", "--sources-config", "sources.yaml"])
    assert callable(seen["secondary"])


# ---- the review's fixes ---------------------------------------------------------------------------------------------------------------------------

def test_the_pool_report_says_what_the_catalogue_can_and_cannot_tell_and_puts_verdict_ok_first():
    assert len(POOL.GATEWAY_PLATFORMS) == 42 and "github" in POOL.GATEWAY_PLATFORMS and len(set(POOL.GATEWAY_PLATFORMS)) == 42
    entries, where = POOL.read(None)
    providers = yaml.safe_load(Path("ai_providers.yaml").read_text(encoding="utf-8"))["providers"]
    groups = POOL.candidates(entries, providers)
    text = "\n".join(POOL.render(groups, where))
    assert "no 'official provider' field" in text and "Check a provider's own terms before approving it" in text
    names = [s["provider"] for s in groups["recommended"]]
    ok = [s["provider"] for s in groups["recommended"] if s["tos"] == ["ok"]]
    assert ok == ["morph"] and text.index("  - morph:") < min(text.index(f"  - {n}:") for n in names if n not in ok)         # the one provider the catalogue calls ok comes before every caution, ambiguous and unknown one
    assert f"{len(ok)} with verdict ok first, then {len(names) - len(ok)} with caution, ambiguous or unknown" in text
    assert all(f"terms: {'/'.join(s['tos'])}" in text for s in groups["recommended"])


class PlanRunner:
    def __init__(self):
        self.plans, self.envs = [], []

    def __call__(self, cmd, **kw):
        self.plans.append(json.loads(Path(cmd[cmd.index("--plan") + 1]).read_text()))
        self.envs.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, (FIX / "l30_research_mock_real.json").read_text(), "")


def test_the_tiktok_instagram_and_meta_ads_lanes_are_added_only_when_the_owner_sets_the_key_and_allows_it(tmp_path):
    root = checkout(tmp_path)
    lanes = lambda plan: set(plan["subqueries"][0]["sources"])                      # noqa: E731
    without = PlanRunner()
    VOICE.ExternalLast30Days(SETTINGS, root=root, environ={"PATH": "/bin"}, runner=without).run("x")
    assert lanes(without.plans[0]) == set(VOICE.DEFAULT_SOURCES) and "SCRAPECREATORS_API_KEY" not in without.envs[0]
    keyed = PlanRunner()
    VOICE.ExternalLast30Days(SETTINGS, root=root, environ={"PATH": "/bin", "SCRAPECREATORS_API_KEY": "sc"}, runner=keyed).run("x")
    assert lanes(keyed.plans[0]) == set(VOICE.DEFAULT_SOURCES) | {"tiktok", "instagram", "meta_ads"} and keyed.envs[0]["SCRAPECREATORS_API_KEY"] == "sc"
    withheld = {"services": {"connection_secrets": {"channels": {"last30days": False}}}}
    held = PlanRunner()
    VOICE.ExternalLast30Days(withheld, root=root, environ={"PATH": "/bin", "SCRAPECREATORS_API_KEY": "sc"}, runner=held).run("x")
    assert lanes(held.plans[0]) == set(VOICE.DEFAULT_SOURCES) and "SCRAPECREATORS_API_KEY" not in held.envs[0]          # the owner's secrets switch withholds the key, so the lanes stay off


def test_the_scoring_prompt_carries_the_brand_wide_voice_block_only_when_an_item_in_the_batch_echoes_it(store, swov):
    voice, _ = VOICE.merge({}, [VOICE._item("reddit", "summer sandals comparison thread", "https://r/9", "", {"score": 900}, "", "", "t", "research")], NOW)
    store.write_extra(swov.slug, "voice", voice)
    unrelated, _ = _scored_item()
    with_voice = FakeClaude()
    with_voice.json_answers = [{"items": [good("IB-0001", 90)]}]
    run_scoring(store, swov, unrelated, with_voice)
    without_voice = FakeClaude()
    without_voice.json_answers = [{"items": [good("IB-0001", 90)]}]
    bank, _ = _scored_item()
    run_scoring(Store(store.data_dir.parent / "no-voice"), swov, bank, without_voice)
    assert with_voice.json_calls[0][1] == without_voice.json_calls[0][1]                  # voice is kept but nothing echoes it: the prompt is exactly the old one
    echoing = FakeClaude()
    echoing.json_answers = [{"items": [good("IB-0001", 90)]}]
    bank, item = _scored_item()
    item["title"] = "summer sandals comparison on overalls"
    run_scoring(store, swov, bank, echoing)
    assert "What audiences say right now" in echoing.json_calls[0][1] and "summer sandals comparison thread" in echoing.json_calls[0][1]


def test_openverse_can_be_lifted_to_any_licence_by_the_owner_and_says_so_in_the_address():
    get = Get({"api.openverse.org/v1/images": (200, (FIX / "openverse_images_documented_fields.json").read_text())})
    OV.find(get, "cotton", "images")
    OV.find(get, "cotton", "images", licence_ok_for_ads=False)
    assert "license_type=commercial" in get.urls[0] and "license_type" not in get.urls[1] and not hasattr(OV, "COMMERCIAL_OK")
    args = cli.build_parser().parse_args(["assets", "--query", "cotton", "--any-licence"])
    assert args.any_licence is True and cli.build_parser().parse_args(["assets", "--query", "x"]).any_licence is False


def test_the_assets_command_passes_the_owners_licence_choice_through(tmp_path, monkeypatch):
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    get = Get({"api.openverse": (200, (FIX / "openverse_images_documented_fields.json").read_text())})
    for flag, expected in (([], True), (["--any-licence"], False)):
        get.urls.clear()
        args = cli.build_parser().parse_args(["assets", "--query", "cotton", "--data-dir", str(tmp_path), *flag])
        RT.run_assets(args, Store(tmp_path), NOW, get=get, post=lambda u, d: (500, ""), environ={})
        assert ("license_type=commercial" in get.urls[0]) is expected


FAKE_PARSER = ("def _parse_ad_library_markdown(md):\n    return [{'library_id': 'EXT1', 'ad_details_url': 'https://www.facebook.com/ads/library/?id=EXT1', 'started_running': None, "
               "'ads_using_creative': 1, 'advertiser': 'FromRepo', 'cta': None, 'platforms': []}]\n")


def test_the_repositorys_own_parser_runs_first_and_the_platforms_copy_takes_over_when_it_is_missing_or_fails(tmp_path, monkeypatch):
    import sys
    ext = tmp_path / "ext"
    (ext / "fb_ads_library_mcp").mkdir(parents=True)
    (ext / "fb_ads_library_mcp" / AD.PARSER_FILE).write_text(FAKE_PARSER)
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    before = {m: sys.modules.get(m) for m in ("fastmcp", "crawl4ai")}
    assert callable(AD.checkout_parser()) and {m: sys.modules.get(m) for m in ("fastmcp", "crawl4ai")} == before          # the stand-ins are gone again
    page = "<div>Library ID: 5</div><a href='https://www.facebook.com/x/'>Brand X</a>"
    new, facts = AD.make_reader(SETTINGS, C._row)(page, [])
    assert [r["item_id"] for r in new] == ["meta_ads:EXT1"] and new[0]["handle"] == "FromRepo"                         # the repository's answer was used
    (ext / "fb_ads_library_mcp" / AD.PARSER_FILE).write_text("def _parse_ad_library_markdown(md):\n    raise RuntimeError('broken')\n")
    new, facts = AD.make_reader(SETTINGS, C._row)(page, [])
    assert [r["item_id"] for r in new] == ["meta_ads:5"] and facts["added"] == 1                                         # it failed, so the platform's copy answered
    (ext / "fb_ads_library_mcp" / AD.PARSER_FILE).write_text("def _parse_ad_library_markdown(md):\n    return 'not a list'\n")
    assert [r["item_id"] for r in AD.make_reader(SETTINGS, C._row)(page, [])[0]] == ["meta_ads:5"]
    (ext / "fb_ads_library_mcp" / AD.PARSER_FILE).write_text("x = 1\n")
    assert AD.checkout_parser() is None and [r["item_id"] for r in AD.make_reader(SETTINGS, C._row)(page, [])[0]] == ["meta_ads:5"]
    health = json.loads((tmp_path / "health-data" / "_shared" / "connections_health.json").read_text())
    assert "fb_ads_library_mcp" in json.dumps(health)                                                                    # the repository's attempts were recorded


def test_the_real_repositorys_parser_loads_and_agrees_with_the_platforms_copy_when_its_checkout_is_available():
    real = Path("/tmp/r35/RamsesAguirre777_facebook-ads-library-mcp")
    if not real.is_dir():
        pytest.skip("SKIPPED BY NAME: the repository's checkout is not present here (the Research tools install step clones it)")
    fn = AD.checkout_parser(real)
    md = (FIX / "adlib_cards.md").read_text()
    assert callable(fn) and fn(md) == AD.parse(md)


def test_discovered_topics_become_auto_seed_keywords_only_when_asked_and_only_as_short_phrases(store):
    assert "no auto seeds exist yet" in RT.seed_auto_seeds(store, "swoveralls", ["weighted hoodie blankets"])
    assert store.read_auto_seeds("swoveralls") is None
    store.write_auto_seeds("swoveralls", {"keywords": {"direct": ["overalls"], "adjacent": [], "cross_industry": ["pop up shop"]}, "context": "c"})
    said = RT.seed_auto_seeds(store, "swoveralls", ["Weighted Hoodie Blankets For Winter Nights", "#loungewear", "pop up shop", "matching family pyjamas"], limit=5)
    seeds = store.read_auto_seeds("swoveralls")
    assert seeds["keywords"]["cross_industry"] == ["pop up shop", "weighted hoodie blankets for", "loungewear", "matching family pyjamas"] and "weighted hoodie blankets for" in said
    assert seeds["keywords"]["direct"] == ["overalls"] and seeds["context"] == "c"
    assert "already a seed" in RT.seed_auto_seeds(store, "swoveralls", ["matching family pyjamas"])
    capped = RT.seed_auto_seeds(store, "swoveralls", [f"topic number {i}" for i in range(9)], limit=3)
    assert capped.count(";") == 2 and len(store.read_auto_seeds("swoveralls")["keywords"]["cross_industry"]) == 7


def test_the_voice_command_seeds_only_with_the_seed_flag(tmp_path, monkeypatch, capsys):
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    discovery = Runner((FIX / "l30_discovery_documented.json").read_text())
    store_ = Store(tmp_path / "data")
    store_.write_auto_seeds("swoveralls", {"keywords": {"direct": [], "adjacent": [], "cross_industry": []}})
    from contentintel import assets_find as AFX
    monkeypatch.setattr(AFX, "terms_from_files", lambda *a, **k: ["Swoveralls", "weighted hoodie loungewear", "family pyjamas"])           # the brand's own terms: both fixture topics share a word with them, so they reach the trending lane that feeds the seeds
    for flag in ([], ["--seed"]):
        args = cli.build_parser().parse_args(["voice", "--brand", "swoveralls", "--mode", "discover", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands", *flag])
        RT.run_voice(args, store_, NOW, runner=discovery, environ={})
        out = capsys.readouterr().out
        assert ("added to the auto seeds" in out) is bool(flag)
    assert store_.read_auto_seeds("swoveralls")["keywords"]["cross_industry"] == ["weighted hoodie blankets", "matching family pyjamas"]


def test_each_kept_item_gets_its_closest_motion_format_once_and_the_sheet_shows_it():
    entries = MOTION.read(None)[0]
    bank = bank_with("a", "b", "c")
    first, second, third = (bank["items"][i] for i in ("IB-0001", "IB-0002", "IB-0003"))
    for item in (first, second):
        item.update({"state": "kept", "score": 80})
    third.update({"state": "kept", "score": 80})
    first.update({"mechanism": "kinetic typography", "concept": "text reveal over the product"})
    second.update({"mechanism": "zzzz", "concept": "qqqq"})
    assert IB.note_motion(bank, entries) == 1
    assert first["motion_idea"] and "https://x.com/" in first["motion_idea"] and second["motion_idea"] == "" and "motion_idea" not in third          # nothing close: ''; no concept yet: left for later
    saved = first["motion_idea"]
    third.update({"mechanism": "text reveal", "concept": "kinetic typography reveal"})
    assert IB.note_motion(bank, entries) == 1 and first["motion_idea"] == saved                          # the first answer is kept, never redone
    assert IB.sheet_rows(bank)["IB-0001"]["motion_idea"] == saved and "motion_idea" in IB.IDEABANK_COLUMNS


def test_the_idea_bank_run_gives_motion_formats_to_scored_items(store, swov, drive):
    from test_ideabank import full_run, seeded
    seeded(store)
    claude = FakeClaude()
    claude.json_answers = [{"items": [dict(good("IB-0001", 80), mechanism="kinetic typography", concept="text reveal over the product"), good("IB-0002", 80)]}]
    new, lines = full_run(store, swov, drive, claude, motion_entries=MOTION.read(None)[0])
    bank = store.read_ideabank("swoveralls")
    assert bank["items"]["IB-0001"]["motion_idea"].startswith(("motion", "3d", "explainer", "interactive")) and bank["items"]["IB-0002"].get("motion_idea") is None
    assert "swoveralls: 1 item(s) got a close motion format from the prompt library (column motion_idea)" in lines
    off = full_run(store, swov, drive, None)
    assert not any("motion" in x for x in off[1])


def test_video_facts_also_cover_x_linkedin_and_vimeo_videos_and_never_an_ad_card():
    def item(platform, url, **kw):
        return dict({"platform": platform, "url": url}, **kw)
    assert VM.wants_facts(item("x", "https://x.com/brand/status/1")) and VM.wants_facts(item("linkedin", "https://www.linkedin.com/posts/brand-123")) and VM.wants_facts(item("web", "https://vimeo.com/123456"))
    assert not VM.wants_facts(item("meta_ads", "https://x.com/brand/status/1")) and not VM.wants_facts(item("web", "https://x.com/brand"))


def test_the_old_limits_are_options_or_larger(store, swov, tmp_path):
    p = cli.build_parser()
    args = p.parse_args(["voice", "--brand", "swoveralls"])
    assert (args.max_peers, args.max_topics, args.seed) == (6, 5, False) and p.parse_args(["voice", "--brand", "x", "--max-peers", "3", "--max-topics", "2"]).max_topics == 2
    bench = {"version": 1, "items": {f"k{i}": {"name": f"Brand {i}", "status": "resolved"} for i in range(8)}}
    store.write_benchmarks("swoveralls", bench)
    assert len(RT._peer_names(store, "swoveralls")) == 6 and len(RT._peer_names(store, "swoveralls", 3)) == 3
    assert len(VOICE.topics_for(swov, store, limit=2)) <= 2 and VOICE.topics_for(swov, store, limit=1)[-1] == swov.name and VOICE.KEEP == 2000
    many = [VOICE._item("reddit", f"t{i}", f"https://r/{i}", "", {"score": i}, "", "", "t", "research") for i in range(500)]
    assert len(VOICE.merge({}, many, NOW)[0]["items"]) == 500


def test_the_brief_shows_more_evidence_than_before(store, swov):
    bank, item = _scored_item()
    voice, _ = VOICE.merge({}, [VOICE._item("reddit", f"voice line {i}", f"https://r/{i}", "", {"score": 100 - i}, "", "", "t", "research") for i in range(12)], NOW)
    store.write_extra(swov.slug, "voice", voice)
    motion = MOTION.read(None)[0][:7]
    assets = [{"title": f"a{i}", "url": f"https://i/{i}", "page": "", "license": "by", "license_version": "", "creator": "c", "attribution": "x"} for i in range(9)]
    text = BR.extras_text(store, swov, [item], motion=motion, assets=assets)
    assert text.count("voice line") == 8 and text.count("\n- ") >= 8 + 5 + 6 - 1 and text.count("[BY]") == 6 and text.count("never copy") == 1
    assert sum(1 for line in text.split("\n") if line.startswith("- ") and "by " in line and "https://x.com/" in line) == 5


def test_the_readme_has_one_secrets_section_and_one_ads_section_and_documents_the_new_secrets():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert readme.count("\n## Secrets") == 1 and readme.count("\n## Ads, social posts and deep research (Actions > Collect)") == 1
    section = readme[readme.index("## Secrets"):]
    section = section[:section.index("\n## ", 5)]
    for name in ("OPENVERSE_CLIENT_ID", "OPENVERSE_CLIENT_SECRET", "SCRAPECREATORS_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "ANTHROPIC_API_KEY", "SERPER_API_KEY"):
        assert f"`{name}`" in section, name
    assert "Gemini's page text sometimes holds the report twice" in readme and "TikTok's security puzzle" in readme


def test_the_video_facts_limit_per_run_is_an_option_and_says_what_waits(monkeypatch, tmp_path):
    class A:
        data_dir = str(tmp_path)
    monkeypatch.setattr(RT, "_settings", lambda args: SETTINGS)
    monkeypatch.setattr(RT.shutil, "which", lambda name: "/usr/bin/" + name)
    info = json.dumps(load("ytdlp_tiktok_documented_fields.json"))
    runner = Runner(info)
    items = {f"IB-{n}": {"platform": "tiktok", "handle": "h", "url": f"https://www.tiktok.com/@h/video/{n}", "metrics": {}} for n in range(1, 4)}
    facts, _, _ = RT.ideabank_hooks(A(), None, environ={"CONTENTINTEL_VIDEO_FACTS_LIMIT": "2"}, runner=runner)
    done, notes = facts(items, list(items))
    assert done == 2 and len(runner.calls) == 2 and any("2 video(s) were read this run" in n for n in notes) and "video_meta" not in items["IB-3"]
    runner = Runner(info)
    facts, _, _ = RT.ideabank_hooks(A(), None, environ={}, runner=runner)
    assert facts(items, ["IB-3"])[0] == 1


def test_the_pool_command_reads_the_gateways_own_provider_list_from_the_directory_it_is_given(tmp_path, capsys):
    src = tmp_path / "gw" / "server" / "src" / "providers"
    src.mkdir(parents=True)
    (src / "index.ts").write_text("\n".join(f"register(new OpenAICompatProvider({{ platform: 'p{i}' }}));" for i in range(25)) + "\nregister(new OpenAICompatProvider({ platform: 'morph' }));\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["pool", "--gateway-dir", str(tmp_path / "gw")])
    assert RT.run_pool(args) == 0
    with_gateway = capsys.readouterr().out
    assert "  - morph:" not in with_gateway                                    # the gateway serves 'morph' in this directory, so it is not offered
    assert RT.run_pool(cli.build_parser().parse_args(["pool"])) == 0
    assert "  - morph:" in capsys.readouterr().out


def test_discovery_seeds_have_two_tiers_a_shared_word_is_priority_and_a_pool_judge_decides_the_rest_within_its_limits(tmp_path, monkeypatch, capsys):
    from contentintel import assets_find as AFX
    from contentintel import seed_judge as SJ
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    monkeypatch.setattr(AFX, "terms_from_files", lambda *a, **k: ["Swoveralls", "zzzz qqqq"])                  # the brand's own terms share a word with neither fixture topic: both are tier 2
    discovery = Runner((FIX / "l30_discovery_documented.json").read_text())
    store_ = Store(tmp_path / "data")
    store_.write_auto_seeds("swoveralls", {"keywords": {"direct": [], "adjacent": [], "cross_industry": []}})
    store_.write_extra("swoveralls", "voice", {"items": [], "trending": [{"title": "Unrelated headline from an old run", "url": "", "tier": ""}, {"title": "earlier judged topic", "url": "", "tier": "judged"}]})
    asked = []

    class Judge:
        def json_task(self, system, user, max_tokens=None):
            asked.append(user)
            return {"verdicts": [{"i": i, "could_help": "hoodie" in line.lower()} for i, line in enumerate(user.split("Topics:\n")[1].splitlines())]}
    args = cli.build_parser().parse_args(["voice", "--brand", "swoveralls", "--mode", "discover", "--seed", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands"])
    RT.run_voice(args, store_, NOW, runner=discovery, environ={}, judge=lambda: Judge())
    out = capsys.readouterr().out
    lane = store_.read_extra("swoveralls", "voice")["trending"]
    assert [x["title"] for x in lane] == ["weighted hoodie blankets", "earlier judged topic"] and lane[0]["tier"] == "judged"          # the old unrelated headline is gone, what was judged useful earlier stays                         # the judge said yes to one and no to the other
    assert "1 were judged useful to the brand by a pool model (0 from earlier judgments), 1 were judged not useful and dropped, 0 could not be judged this run" in out and "1 judge call(s)" in out
    assert len(asked) == 1 and "Brand: swoveralls. Its own terms" in asked[0] and "zzzz qqqq" in asked[0]
    assert store_.read_auto_seeds("swoveralls")["keywords"]["cross_industry"] == ["weighted hoodie blankets"]
    assert store_.load_usage()["months"][NOW[:7]][SJ.COUNTER] == 1 and len(store_.read_extra("swoveralls", "seed_judgments")["verdicts"]) == 2
    RT.run_voice(args, store_, NOW, runner=discovery, environ={}, judge=lambda: Judge())
    again = capsys.readouterr().out
    assert len(asked) == 1 and "(2 from earlier judgments)" in again and "0 judge call(s)" in again                         # asked once, remembered after
    class NoPool:
        def __call__(self):
            return None
    fresh = Store(tmp_path / "data2")
    fresh.write_auto_seeds("swoveralls", {"keywords": {"direct": [], "adjacent": [], "cross_industry": []}})
    RT.run_voice(cli.build_parser().parse_args(["voice", "--brand", "swoveralls", "--mode", "discover", "--data-dir", str(tmp_path / "data2"), "--brands-dir", "brands"]), fresh, NOW, runner=discovery, environ={}, judge=NoPool())
    nope = capsys.readouterr().out
    assert "2 could not be judged this run (no pool model is available to judge them)" in nope and not (fresh.read_extra("swoveralls", "voice") or {}).get("trending")
