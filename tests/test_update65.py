"""Update 65: what the live round of Update 64 showed. Why the repository's engine failed is now said in words, topics are what an audience searches for, competitors are the followed brands (never the skipped ones),
viewer comments become audience voice, pace can be measured from a URL on a runner that has ffmpeg, the competitor cap is gone, the page id has a fourth place to be read, and a lane can say what its job needs."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from contentintel import cli
from contentintel import connections as CONN
from contentintel import contract as K
from contentintel import main_page as MP
from contentintel import research_tools as RT
from contentintel import videometa as VM
from contentintel import voice as VOICE
from contentintel.ai import clients as AC
from contentintel.ai import lanes as L
from contentintel.observatory import config as OC
from contentintel.providers import Result
from contentintel.store import Store

from ai_helpers import NOW as AI_NOW, LANES, PROVIDERS, edited, stack
from test_collectors import NOW
from test_update64 import FIX, Get, HN, RD, Runner, SETTINGS, checkout, load


@pytest.fixture(autouse=True)
def _health_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path / "health-data"))


# ---- why the repository's own engine failed is said in words --------------------------------------------------------------------------------------

ALL_FAILED = json.dumps({"results": [], "clusters": [], "source_status": {"reddit": "unreachable", "youtube": "unreachable", "grounding": "unreachable"}})
ENGINE_STDERR = "[YouTube] YouTube search failed (rc=1): ERROR: Sign in to confirm you're not a bot\n[RedditSearch] global page 1 failed: HTTP Error 403: Blocked\n"


def test_a_wholly_failed_engine_run_carries_what_the_engine_itself_said(tmp_path):
    root = checkout(tmp_path)
    runner = Runner(ALL_FAILED, 0, ENGINE_STDERR)
    ext = VOICE.ExternalLast30Days(SETTINGS, root=root, environ={}, runner=runner)
    with pytest.raises(K.Failure) as exc:
        ext.run("x")
    assert "every source last30days tried failed: grounding unreachable, reddit unreachable, youtube unreachable" in exc.value.message
    assert "The engine said:" in exc.value.message and "Sign in to confirm" in exc.value.message and "HTTP Error 403" in exc.value.message
    assert ext.last_error.startswith("UPSTREAM_BROKEN: every source")
    ok = Runner((FIX / "l30_research_mock_real.json").read_text())
    ext2 = VOICE.ExternalLast30Days(SETTINGS, root=root, environ={}, runner=ok)
    ext2.last_error = "old"
    ext2.run("x")
    assert ext2.last_error == ""                                                    # a good run clears it


def _args(tmp_path, *extra):
    return cli.build_parser().parse_args(["voice", "--brand", "swoveralls", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands", *extra])


def test_the_voice_run_says_which_keys_the_engine_got_and_why_the_backup_answered(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    store = Store(tmp_path / "data")
    get = Get({"algolia": (200, HN), "reddit.com": (200, RD)})
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, NOW, get=get, runner=Runner(ALL_FAILED, 0, ENGINE_STDERR), environ={"SERPER_API_KEY": "s"}, clock=lambda: 1_790_000_000)
    out = capsys.readouterr().out
    assert "Keys the engine is given (names only, never values): SERPER_API_KEY. SCRAPECREATORS_API_KEY is NOT set in this run; those three lanes are off." in out
    assert "the last30days engine could not answer (UPSTREAM_BROKEN: every source last30days tried failed" in out and "Sign in to confirm" in out and "answered instead" in out and "from builtin" in out
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, NOW, get=get, runner=Runner((FIX / "l30_research_mock_real.json").read_text()), environ={"SERPER_API_KEY": "s", "SCRAPECREATORS_API_KEY": "sc"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "SERPER_API_KEY, SCRAPECREATORS_API_KEY. SCRAPECREATORS_API_KEY is set in this run and is handed to the engine: when the engine answers (research and competitors modes), TikTok, Instagram and Meta Ads are asked too. Each keyed query spends ScrapeCreators credits (100 free, once): try one topic first." in out
    assert "from last30days" in out or "from external" in out and "could not answer" not in out


# ---- topics are what an audience searches for -----------------------------------------------------------------------------------------------------

def test_topics_come_from_the_brands_own_search_phrases_and_never_from_the_shops_category_labels(tmp_path, swov):
    store = Store(tmp_path / "data")
    store.save_latest = None
    latest = {"products": {f"p{i}": {"category": cat, "excluded": False} for i, cat in enumerate(["core"] * 5 + ["merch"] * 3 + ["kids"] * 2 + ["overalls"])}}
    store.load_latest = lambda slug: latest
    assert VOICE.topics_for(swov, store, seeds=["sweatpant overalls", "loungewear"]) == ["sweatpant overalls", "loungewear", "overalls", swov.name]
    assert VOICE.topics_for(swov, store) == ["overalls", swov.name]                       # 'core', 'merch' and 'kids' say nothing to an audience
    assert VOICE.topics_for(swov, store, seeds=["a", "b", "c", "d", "e", "f"], limit=3) == ["a", "b", swov.name]


def test_a_brands_search_phrases_are_its_own_keywords_then_the_auto_seeds_direct_and_adjacent(tmp_path):
    store = Store(tmp_path / "data")
    args = cli.build_parser().parse_args(["voice", "--brand", "swoveralls", "--brands-dir", "brands"])
    assert RT._seed_keywords(args, store, "swoveralls") == ["sweatpant overalls"]
    store.write_auto_seeds("swoveralls", {"keywords": {"direct": ["bib overalls", "sweatpant overalls"], "adjacent": ["loungewear"], "cross_industry": ["pop up shop"]}})
    assert RT._seed_keywords(args, store, "swoveralls") == ["sweatpant overalls", "bib overalls", "loungewear"]          # the cross-industry ideas are not the audience's words about this product


# ---- competitors ----------------------------------------------------------------------------------------------------------------------------------

def test_competitors_mode_asks_about_the_followed_brands_not_the_skipped_ones_and_reports_every_one(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    store = Store(tmp_path / "data")
    store.write_benchmarks("swoveralls", {"version": 1, "items": {k: {"name": n, "status": "resolved", "page_id": "1"} for k, n in enumerate(["Swoveralls", "The Oodie", "Madepants", "Allbirds", "Liquid Death", "SKIMS"])}})
    assert sorted(RT._skipped_names(_args(tmp_path), "swoveralls").values()) == ["Allbirds", "Cotopaxi", "Dollar Shave Club", "Liquid Death"]
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    hn_one = json.dumps({"hits": [{"title": "Madepants thread", "url": "https://x/1", "points": 9, "num_comments": 1, "created_at": "2026-11-10T00:00:00Z", "objectID": "1"}]})
    seen = []

    def get(url, headers=None):
        seen.append(url)
        return (200, hn_one if "algolia" in url and "Madepants" in url else json.dumps({"hits": []})) if "algolia" in url else (403, "")
    RT.run_voice(_args(tmp_path, "--mode", "competitors"), store, NOW, get=get, runner=Runner("", 1, "boom"), environ={}, clock=lambda: 1_790_000_000)
    out = capsys.readouterr().out
    assert "competitors asked about: The Oodie, Madepants, SKIMS (left out because you set them to skip: Allbirds, Cotopaxi, Dollar Shave Club, Liquid Death)" in out
    assert "this run, items per competitor: The Oodie: 0; Madepants: 1; SKIMS: 0; the brand itself: 0" in out
    assert not any("Allbirds" in u or "Liquid" in u for u in seen)


# ---- viewer comments are audience voice -----------------------------------------------------------------------------------------------------------

def _bank_with_comments():
    meta = VM.normalise(load("ytdlp_tiktok_documented_fields.json"))
    return {"items": {"IB-1": {"platform": "tiktok", "url": "https://www.tiktok.com/@m/video/1", "title": "POV: pants that move", "topic": "pants", "video_meta": meta},
                      "IB-2": {"platform": "instagram", "url": "https://www.instagram.com/reel/a/", "title": "no facts"}}}


def test_the_comments_under_videos_already_read_become_voice_items_one_each_with_their_likes():
    items = VOICE.comment_items(_bank_with_comments()["items"])
    assert [i["title"] for i in items] == ["need in navy", "where are these from??"] or {i["title"] for i in items} == {"need in navy", "where are these from??"}
    best = max(items, key=lambda i: i["weight"])
    assert best["title"] == "need in navy" and best["weight"] == 900 and best["source"] == "tiktok comment" and best["mode"] == "comments"
    assert best["url"].startswith("https://www.tiktok.com/@m/video/1#c-") and len({i["url"] for i in items}) == len(items) and best["summary"] == "POV: pants that move"
    assert len(VOICE.merge({}, items, NOW)[0]["items"]) == len(items)                          # two comments under one video are two items, not one
    assert VOICE.comment_items({}) == [] and VOICE.comment_items(None) == []


def test_the_voice_command_adds_the_viewer_comments_it_finds_in_the_idea_bank(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    store = Store(tmp_path / "data")
    store.write_ideabank("swoveralls", _bank_with_comments())
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, NOW, get=Get({"algolia": (200, HN), "reddit.com": (200, RD)}), runner=Runner("", 1, "boom"), environ={}, clock=lambda: 1_790_000_000)
    out = capsys.readouterr().out
    assert "2 viewer comment(s) from videos already read were added to the voice (2 new)" in out
    kept = store.read_extra("swoveralls", "voice")
    assert {i["source"] for i in kept["items"]} >= {"tiktok comment", "hackernews", "reddit"} and any(i["title"] == "need in navy" for i in kept["items"])


# ---- pace from a URL, and ffmpeg on the runners ---------------------------------------------------------------------------------------------------

def test_a_video_is_downloaded_small_and_measured_when_asked(tmp_path, capsys):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if "-o" in cmd:
            shutil.copy(FIX.parent.parent / "fixtures" / "update64" / "adlib_cards.md", cmd[cmd.index("-o") + 1].replace("%(ext)s", "bin"))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    path = VM.fetch_file("https://www.tiktok.com/@a/video/1", tmp_path, run=fake, python="py", cookies="/c.txt")
    cmd = calls[0]
    assert path.endswith("video.bin") and cmd[:3] == ["py", "-m", "yt_dlp"] and "b[height<=720]/b" in cmd and cmd[cmd.index("--max-filesize") + 1] == "60M" and cmd[cmd.index("--cookies") + 1] == "/c.txt"
    for stderr, code in (("ERROR: login required", K.PLATFORM_BLOCKED), ("ERROR: HTTP Error 429", K.RATE_LIMITED)):
        with pytest.raises(K.Failure) as exc:
            VM.fetch_file("https://x/v", tmp_path, run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", stderr))
        assert exc.value.code == code
    with pytest.raises(K.Failure, match="saved no file"):
        VM.fetch_file("https://x/v", tmp_path / "empty", run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", "")) if (tmp_path / "empty").mkdir() is None else None


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="SKIPPED BY NAME: ffmpeg and ffprobe are not installed here (the workflows now install them)")
def test_analyze_with_pace_downloads_the_video_and_prints_how_it_moves(tmp_path, capsys):
    sample = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=360x640:d=1.5:r=25", "-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1.5:r=25",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(sample)], check=True)

    def fake(cmd, **kw):
        if "yt_dlp" in " ".join(cmd):
            if "-o" not in cmd:                                                      # the facts call
                return subprocess.CompletedProcess(cmd, 0, json.dumps(load("ytdlp_tiktok_documented_fields.json")), "")
            shutil.copy(sample, cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4"))
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.run(cmd, **kw)
    args = cli.build_parser().parse_args(["analyze", "--url", "https://www.tiktok.com/@a/video/1", "--pace"])
    assert RT.run_analyze(args, runner=fake) == 0
    out = capsys.readouterr().out
    assert "Video facts:" in out and "Pace: fast pace: average shot 1.52 s over 2 shot(s); 1 cut(s) in the first 3 s; no audio track" in out


def test_every_workflow_that_measures_or_tests_pace_installs_ffmpeg_and_analyze_asks_for_it():
    for name in ("collect.yml", "tests.yml", "research-tools.yml"):
        text = Path(".github/workflows") / name
        body = text.read_text(encoding="utf-8")
        assert "sudo apt-get install -y -qq ffmpeg" in body, name
    tools = Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")
    assert "analyze) args=(analyze); if [ -n \"$VIDEO_URL\" ]; then args+=(--url \"$VIDEO_URL\" --comments --pace); fi ;;" in tools and "if: inputs.tool == 'analyze'" in tools
    assert cli.build_parser().parse_args(["analyze", "--url", "u", "--pace"]).pace is True and cli.build_parser().parse_args(["analyze", "--url", "u"]).pace is False


# ---- the competitor cap is gone ---------------------------------------------------------------------------------------------------------------------

def test_the_competitor_caps_are_the_settings_own_maximums_in_the_defaults_and_in_the_owners_file():
    for s in (OC.load_settings("missing.yaml"), OC.load_settings("observatory.yaml")):
        assert s["discovery"]["auto_monitor_max"] == 50 and s["discovery"]["max_monitored"] == 100
    assert OC._RANGES[("discovery", "auto_monitor_max")] == (0, 50) and OC._RANGES[("discovery", "max_monitored")] == (1, 100)


# ---- the ScrapeCreators allowance is stated as it is ------------------------------------------------------------------------------------------------

def test_the_scrapecreators_allowance_is_100_free_credits_once_everywhere_it_is_stated():
    for p in ("contentintel/voice.py", "contentintel/connections.py", "README.md"):
        text = Path(p).read_text(encoding="utf-8")
        assert "10,000 free" not in text, p
    assert "100 free credits once" in Path("README.md").read_text(encoding="utf-8") and "100 free credits once" in CONN.CONNECTIONS["last30days"]["needs"]


# ---- the page id has a fourth place to be read ------------------------------------------------------------------------------------------------------

def test_the_public_page_plugin_is_the_fourth_place_a_page_id_is_read_when_the_other_three_show_a_login_wall():
    asked = []

    def get_html(url):
        asked.append(url)
        return '{"pageID":"229927120383343"}' if "/plugins/page.php" in url else "<html>log in to continue</html>"

    class Serper:
        def search(self, query, num=10):
            return [Result(rank=1, title="DUDE Wipes | Facebook", url="https://www.facebook.com/dudewipes", snippet="Chicago, Shark Tank")]

    class Claude:
        def json_task(self, system, user, max_tokens=0):
            return {"choice": "https://www.facebook.com/dudewipes", "is_main": True, "regional": False, "reason": "the brand's own page"}
    out = MP.make_finder(Serper(), Claude(), get_html)("Dude Wipes")
    assert out["status"] == "resolved" and out["page_id"] == "229927120383343" and len(asked) == 4
    assert asked[3].startswith("https://www.facebook.com/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2Fdudewipes&tabs") and asked[:3] == ["https://www.facebook.com/dudewipes", "https://m.facebook.com/dudewipes", "https://mbasic.facebook.com/dudewipes"]


# ---- a lane says what its job needs ------------------------------------------------------------------------------------------------------------------

def _vision_stack(tmp_path, clients, lane_extra=None):
    providers = edited(PROVIDERS, lambda d: d["providers"]["alpha"]["models"][0].update(capabilities=["text", "vision"]))
    lanes = edited(LANES, lambda d: d["lanes"].update(see={"max_data_class": "public", "chain": [{"provider": "beta", "model": "b-1"}, {"provider": "alpha", "model": "a-1"}], "needs": ["vision"], "output": {"format": "text", "min_chars": 3},
                                                       "max_tokens": 100, **(lane_extra or {})}))
    providers["providers"]["beta"]["approved_classes"] = ["public", "internal"]
    return stack(tmp_path, clients, providers, lanes)


def test_a_model_that_cannot_do_the_job_is_never_tried_and_the_next_capable_one_answers(tmp_path):
    beta, alpha = AC.MockClient(["a text answer from beta"]), AC.MockClient(["a seeing answer from alpha"])
    router, *_ = _vision_stack(tmp_path, {"alpha": alpha, "beta": beta})
    res = router.run("see", data_class="public", instructions="look", now=AI_NOW)
    assert res["status"] == "ok" and res["route"]["provider"] == "alpha" and beta.calls == []
    # the scored ranking puts the provider that can do the job first (its capability dimension), so the one that cannot is never even reached


def test_when_no_provider_can_do_the_job_the_run_says_so_plainly_and_guesses_nothing(tmp_path):
    beta = AC.MockClient(["a text answer from beta"])
    router, *_ = _vision_stack(tmp_path, {"beta": beta})
    res = router.run("see", data_class="public", instructions="look", now=AI_NOW)
    assert res["status"] == "deferred" and res["reason"].startswith("no provider that can do this job (vision) was available") and beta.calls == [] and res["attempts"] == []
    assert "nothing was tried and nothing was guessed" in res["reason"]


def test_claude_still_answers_a_job_no_free_model_can_do_where_the_lane_allows_it(tmp_path):
    claude = AC.MockClient(["Claude can look at it."])
    router, *_ = _vision_stack(tmp_path, {"beta": AC.MockClient(["x"]), "claude": claude}, {"claude_fallback": True})
    res = router.run("see", data_class="public", instructions="look", now=AI_NOW)
    assert res["status"] == "ok" and res["route"]["provider"] == "claude"


def test_lanes_default_to_text_validate_what_they_need_and_the_shipped_lanes_and_pool_are_unchanged(tmp_path):
    from contentintel.ai import registry as R
    reg = R.load_registry("ai_providers.yaml")
    shipped = L.load_lanes("ai_lanes.yaml", reg)
    assert all(lane.needs == ("text",) for lane in shipped.values())
    pool = L.load_pool("ai_lanes.yaml", reg)
    assert len(pool) == 45 and all("text" in (reg.providers[r["provider"]].model(r["model"]).capabilities or ("text",)) for r in pool)          # every pool model can do a text job: nothing was shrunk
    bad = tmp_path / "lanes.yaml"
    bad.write_text(yaml.safe_dump(edited(LANES, lambda d: d["lanes"]["pub"].update(needs=["telepathy"]))), encoding="utf-8")
    with pytest.raises(L.LaneError, match="needs must be a list of text, vision, tools"):
        L.load_lanes(bad)
    empty = tmp_path / "lanes2.yaml"
    empty.write_text(yaml.safe_dump(edited(LANES, lambda d: d["lanes"]["pub"].update(needs=[]))), encoding="utf-8")
    with pytest.raises(L.LaneError):
        L.load_lanes(empty)


# ---- the engine's path, run for real from a relative folder (the bug the mocked tests could not see) -----------------------------------------------

def test_the_engine_starts_when_the_external_folder_is_relative_as_it_is_on_the_runner(tmp_path, monkeypatch):
    script = tmp_path / "external" / "last30days" / "skills" / "last30days" / "scripts" / "last30days.py"
    script.parent.mkdir(parents=True)
    script.write_text("import json\nprint(json.dumps({'results': [{'source': 'reddit', 'title': 'hello world thread', 'url': 'https://r/1', 'summary': 's', 'engagement': {'score': 5}, 'published_at': '2026-11-10', 'cluster': 0}], "
                      "'clusters': [{'title': 't'}], 'source_status': {'reddit': 'ok'}}))\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONTENTINTEL_EXTERNAL_DIR", raising=False)
    assert str(CONN.external_dir()) == "external"                                                    # relative, exactly as in the workflow
    ext = VOICE.ExternalLast30Days(SETTINGS, environ={"PATH": "/usr/bin:/bin"})                     # the REAL subprocess runner: nothing is mocked
    assert ext.root.is_absolute()
    items, status = ext.run("hello")
    assert [i["title"] for i in items] == ["hello world thread"] and status == {"reddit": "ok"} and ext.last_error == ""


# ---- Instagram's login page is recognised even when its header looks signed in -----------------------------------------------------------------------

def test_instagrams_login_page_counts_as_a_login_wall_even_when_signed_in_markers_are_on_the_page():
    from test_collectors import browser_with, site
    from contentintel import browser as B
    from contentintel import collectors as C
    ig = B.load_browser_settings("browser.yaml")["sites"]["instagram"]
    assert ig["signed_out_any"] == ["text=Log into Instagram", 'input[name="email"]']
    assert C.session_state(browser_with([ig["logged_in_any"][0], "text=Log into Instagram"]), ig) == "login_wall"
    assert C.session_state(browser_with([ig["logged_in_any"][0], 'input[name="email"]']), ig) == "login_wall"
    assert C.session_state(browser_with([ig["logged_in_any"][0]]), ig) == "ok" and site is not None


# ---- the Apify lane --------------------------------------------------------------------------------------------------------------------------------

class FakeApify:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def run(self, actor, payload, *, max_items, max_cost_usd):
        if actor == "trudax/reddit-scraper-lite":                              # the Reddit BACKUP (asked only when the public Reddit search failed) is tested in test_apify_budget.py
            self.reddit = getattr(self, "reddit", []) + [(actor, payload, max_items, max_cost_usd)]
            return []
        self.calls.append((actor, payload, max_items, max_cost_usd))
        if self.fail:
            raise self.fail
        return [{"text": f"{actor.split('/')[0]} caption number {i} about overalls", "likes": 100 - i, "comments": i, "url": f"https://x/{actor}/{i}", "posted_at": "2026-11-10", "title": f"post {i}"} for i in range(max_items)]


def _ledger(**used):
    from contentintel.research import Ledger
    led = Ledger({"months": {"2026-11": dict(used)}, "lifetime": {}}, "2026-11")
    return led


def test_the_apify_lane_reads_within_its_own_allowance_and_charges_both_counters():
    led, fake = _ledger(apify_items=45), FakeApify()
    lane = VOICE.ApifyVoice(fake, led, per_run=15, monthly=40, shared_cap=100, normalize=lambda s, raw: (raw, 0))
    items, notes = lane.run("sweatpant overalls")
    assert [c[0] for c in fake.calls] == ["clockworks/tiktok-scraper", "apify/instagram-hashtag-scraper"]
    assert fake.calls[0][1] == {"searchQueries": ["sweatpant overalls"], "resultsPerPage": 7} and fake.calls[1][1] == {"hashtags": ["sweatpantoveralls"], "resultsType": "posts", "resultsLimit": 8}
    assert [c[3] for c in fake.calls] == [0.50, 0.25]                                      # TikTok's actor refuses less than 0.50 per call (measured live); Instagram's 0.25 bought 27 items
    assert len(items) == 15 and {i["source"] for i in items} == {"apify tiktok", "apify instagram"} and items[0]["weight"] == 100 and items[0]["engagement"]["comments"] == 0
    assert led.month_used("voice_apify_items") == 15 and led.month_used("apify_items") == 60 and lane.spent_this_run == 15 and len(notes) == 2
    again, notes = lane.run("another topic")
    assert again == [] and fake.calls and all("no allowance left" in n for n in notes) and "this run 15/15" in notes[0]


def test_the_apify_lane_never_goes_past_the_collect_steps_allowance_nor_its_own_monthly_one():
    shared = VOICE.ApifyVoice(FakeApify(), _ledger(apify_items=98), per_run=15, monthly=40, shared_cap=100, normalize=lambda s, raw: (raw, 0))
    assert shared.room() == 2
    own = VOICE.ApifyVoice(FakeApify(), _ledger(voice_apify_items=40), per_run=15, monthly=40, shared_cap=100, normalize=lambda s, raw: (raw, 0))
    assert own.room() == 0 and own.run("x")[0] == [] and own.apify.calls == []
    assert VOICE.ApifyVoice(FakeApify(), _ledger(), per_run=15, monthly=40, shared_cap=100).room() == 15


def test_a_refusal_from_apify_is_a_note_not_an_exception_and_nothing_is_charged():
    from contentintel.providers import QuotaError
    led = _ledger()
    lane = VOICE.ApifyVoice(FakeApify(QuotaError("Apify: out of credits")), led, normalize=lambda s, raw: (raw, 0))
    items, notes = lane.run("x")
    assert items == [] and len(notes) == 2 and all("QuotaError" in n and "out of credits" in n for n in notes) and led.month_used("voice_apify_items") == 0


def test_the_voice_command_uses_the_apify_lane_by_default_unless_switched_off_and_saves_the_spending(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    fake = FakeApify()
    import contentintel.apify as A
    monkeypatch.setattr(A, "Apify", lambda token: fake)
    store = Store(tmp_path / "data")
    get = Get({"algolia": (200, HN), "reddit.com": (200, RD)})
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls", "--no-apify"), store, "2026-11-15T00:00:00Z", get=get, runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1_790_000_000)
    assert fake.calls == [] and "Apify lane" not in capsys.readouterr().out                                    # switched off: not used
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=get, runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1_790_000_000)
    out = capsys.readouterr().out
    assert "Apify lane ON for this run (switch off with --no-apify): at most 40 items in this whole run, 250 a month for voice (used so far this month: 0)" in out and "never beyond the Collect step's 1200 a month" in out and len(fake.calls) == 2 and [c[3] for c in fake.calls] == [0.50, 0.25]          # each Apify call carries its actor's cost ceiling
    assert "0.5 dollars for TikTok, 0.25 for Instagram" in out and "Apify usage after this run: voice 40/250 items this month, all Apify use 40/1200 (this run read 40)." in out
    assert store.load_usage()["months"]["2026-11"]["voice_apify_items"] == fake.calls[0][2] + fake.calls[1][2]            # the spending is saved
    RT.run_voice(_args(tmp_path, "--topic", "x"), store, "2026-11-15T00:00:00Z", get=get, runner=Runner("", 1, "boom"), environ={}, clock=lambda: 1)
    assert "Apify lane skipped: APIFY_TOKEN is not set in this run." in capsys.readouterr().out
    assert cli.build_parser().parse_args(["voice", "--brand", "x"]).apify is True and cli.build_parser().parse_args(["voice", "--brand", "x", "--no-apify"]).apify is False


# ---- Reddit posts the platform already collected -----------------------------------------------------------------------------------------------------

def test_reddit_posts_collected_by_the_observatory_become_voice_with_their_score_and_comments(tmp_path, capsys, monkeypatch):
    rows = [{"source": "reddit", "title": "Best bib overalls for women?", "text": "straps keep slipping", "url": "https://www.reddit.com/comments/a1/", "likes": 410, "comments": 77, "posted_at": "2026-11-09", "details": "r/Overalls"},
            {"source": "reddit", "title": "x", "text": "", "url": "u"}, {"source": "tiktok", "title": "not reddit at all here", "text": "caption", "url": "v"}]
    items = VOICE.reddit_items(rows)
    assert len(items) == 1 and items[0]["source"] == "reddit" and items[0]["weight"] == 410 and items[0]["engagement"] == {"score": 410, "num_comments": 77} and items[0]["cluster"] == "r/Overalls" and items[0]["mode"] == "collected"
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    store = Store(tmp_path / "data")
    args = _args(tmp_path, "--topic", "sweatpant overalls")
    RT.run_voice(args, store, NOW, get=Get({"algolia": (200, HN)}), runner=Runner("", 1, "boom"), environ={}, clock=lambda: 1)
    first = capsys.readouterr().out
    assert "no Reddit posts are collected yet" in first and "Actions > Observatory > reddit" in first
    store.append_collected("swoveralls", {"at": NOW, "run": "r", "source": "reddit", "key": "api:r", "seeds": ["Overalls"], "from_site_data": 1, "from_links": 0, "items": [dict(rows[0], item_id="reddit:a1")]})
    RT.run_voice(args, store, NOW, get=Get({"algolia": (200, HN)}), runner=Runner("", 1, "boom"), environ={}, clock=lambda: 1)
    assert "1 Reddit post(s) the platform already collected were added to the voice" in capsys.readouterr().out


# ---- the page id check -------------------------------------------------------------------------------------------------------------------------------

def test_the_page_id_check_reports_each_of_the_four_places_and_the_first_id_found(capsys):
    assert [a.split("/")[2] for a in MP.page_id_addresses("https://www.facebook.com/Chobani")] == ["www.facebook.com", "m.facebook.com", "mbasic.facebook.com", "www.facebook.com"]
    seen = []

    def get(url, headers=None):
        seen.append(url)
        if "/plugins/page.php" in url:
            return 200, '<div>{"pageID":"112002018446346"}</div>'
        return 200, "<html><title>Log in to Facebook</title>please log in</html>"
    args = cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani"])
    assert RT.run_pageid(args, get=get) == 0
    out = capsys.readouterr().out
    assert "www.facebook.com: HTTP 200" in out and "a login page" in out and "www.facebook.com/plugins: HTTP 200" in out and "PAGE ID 112002018446346" in out and "Result for https://www.facebook.com/Chobani: page id 112002018446346" in out
    assert len(seen) == 4
    RT.run_pageid(args, get=lambda url, headers=None: (403, ""))
    assert "no page id could be read from any of the four places" in capsys.readouterr().out
    assert RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://example.com/x"]), get=get) == 1


# ---- the planner's checks on the first Update 65 package ------------------------------------------------------------------------------------------

def test_the_observatory_heading_no_longer_states_the_old_cap():
    text = Path("docs/observatory/README.md").read_text(encoding="utf-8")
    assert "10 monitored, up to 6 added automatically" not in text and "## Choosing competitors: no practical cap (up to 100 monitored, up to 50 added automatically)" in text


def _voice_env(tmp_path, monkeypatch):
    monkeypatch.setattr(RT, "_settings", lambda a: SETTINGS)
    ext = tmp_path / "ext"
    shutil.copytree(checkout(tmp_path / "x"), ext / "last30days")
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(ext))
    return Store(tmp_path / "data")


def test_when_the_engine_and_the_backup_both_fail_the_run_prints_both_reasons(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls", "--no-apify"), store, NOW, get=Get({"algolia": (500, ""), "reddit.com": (500, "")}), runner=Runner(ALL_FAILED, 0, ENGINE_STDERR), environ={}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "(the last30days engine had also failed: UPSTREAM_BROKEN: every source last30days tried failed" in out and "Sign in to confirm" in out


def test_a_partial_engine_failure_still_shows_what_the_engine_said(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    good = json.loads((FIX / "l30_research_mock_real.json").read_text())
    good["source_status"] = {**good.get("source_status", {}), "youtube": "unreachable"}
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls", "--no-apify"), store, NOW, get=Get({}), runner=Runner(json.dumps(good), 0, "ERROR: Sign in to confirm you're not a bot\n[Planner] fine\n"), environ={}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "some sources failed; the engine said: ERROR: Sign in to confirm you're not a bot" in out and "fine" not in out.split("the engine said:")[1].split("\n")[0]
    for line in ("ERROR: Sign in to confirm", "HTTP Error 401: Unauthorized", "403 Forbidden", "Access denied for this region", "reddit BLOCKED"):
        ext = VOICE.ExternalLast30Days(SETTINGS, root=checkout(tmp_path / f"c{abs(hash(line))}"), environ={}, runner=Runner(ALL_FAILED, 0, line + "\n"))
        with pytest.raises(K.Failure) as exc:
            ext.run("x")
        assert line.split()[0] in exc.value.message or line in exc.value.message, line


def test_explicit_peers_are_filtered_too_and_what_was_dropped_is_said(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    RT.run_voice(_args(tmp_path, "--mode", "competitors", "--peers", "Allbirds,Madepants,Swoveralls", "--no-apify"), store, NOW, get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "competitors asked about: Madepants" in out and "Allbirds" not in out.split("competitors asked about:")[1].split("\n")[0]
    assert "not asked about (the brand itself, or set to skip in research.yaml; remove it from benchmark_pages skip to ask): Allbirds, Swoveralls" in out


def test_the_downloaded_video_is_deleted_when_the_run_ends_and_a_too_large_one_is_refused(tmp_path):
    seen = []

    def fake(cmd, **kw):
        if "yt_dlp" in " ".join(cmd) and "-o" in cmd:
            out = Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4"))
            out.write_bytes(b"x" * 1024)
            seen.append(out)
        return subprocess.CompletedProcess(cmd, 1, "", "no facts")
    RT.run_analyze(cli.build_parser().parse_args(["analyze", "--url", "https://www.tiktok.com/@a/video/1", "--pace"]), runner=fake)
    assert len(seen) == 1 and not seen[0].exists() and not seen[0].parent.exists()                         # nothing is left on the runner

    def big(cmd, **kw):
        Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", "mp4")).write_bytes(b"x" * (2 * 1024 * 1024))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    with pytest.raises(K.Failure, match="larger than 1 MB; it was deleted"):
        VM.fetch_file("https://x/v", tmp_path, run=big, max_mb=1)
    assert not list(tmp_path.glob("video.*"))


def test_the_reddit_line_says_through_which_route_the_posts_were_read(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    base = {"source": "reddit", "text": "straps keep slipping again", "comments": 3, "posted_at": "2026-11-09", "details": "r/Overalls"}
    store.append_collected("swoveralls", {"at": NOW, "run": "r", "source": "reddit", "key": "k", "seeds": ["x"], "from_site_data": 2, "from_links": 0, "items": [
        dict(base, item_id="reddit:a", title="One long enough title", url="https://r/a", likes=5, found_via="api"), dict(base, item_id="reddit:b", title="Two long enough title", url="https://r/b", likes=6, found_via="agent_reach")]})
    RT.run_voice(_args(tmp_path, "--topic", "x", "--no-apify"), store, NOW, get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={}, clock=lambda: 1)
    assert "read through: stored session through Agent-Reach 1, official Reddit API 1" in capsys.readouterr().out


def test_the_page_id_check_warns_about_regional_pages_and_accepts_numeric_addresses(capsys):
    for slug, region in (("ChobaniAu", "au"), ("ChobaniNZ", "nz"), ("chobani-nz", "nz"), ("DudeWipesUS", "us")):
        assert MP.regional_hint(slug) == region
    for slug in ("Chobani", "TheOodie", "Graza", "Snuggie", "Gymshark", "dudewipes", "HuKitchen", "DEFISnacks"):
        assert MP.regional_hint(slug) == "", slug
    get = lambda url, headers=None: (200, '{"pageID":"123456789"}')           # noqa: E731
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/ChobaniAu"]), get=get)
    out = capsys.readouterr().out
    assert "WARNING: the address looks like a regional page (au)" in out and "(a REGIONAL page: au)" in out
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani"]), get=get)
    assert "WARNING" not in capsys.readouterr().out
    called = []
    assert RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/profile.php?id=100064539012345"]), get=lambda u, h=None: called.append(u)) == 0
    assert "already carries the page id: 100064539012345" in capsys.readouterr().out and called == []


def test_the_apify_cap_is_the_owners_configured_collect_cap_and_the_per_run_allowance_is_shared_by_every_brand(tmp_path, capsys, monkeypatch):
    cfg = tmp_path / "research.yaml"
    cfg.write_text("collectors:\n  apify:\n    monthly_items: 50\n", encoding="utf-8")
    assert RT._collect_apify_cap(type("A", (), {"research_config": str(cfg)})()) == 50
    assert RT._collect_apify_cap(type("A", (), {"research_config": str(tmp_path / "missing.yaml")})()) == 1200          # no file: the built-in default
    cfg.write_text("collectors:\n  apify:\n    monthly_items: not-a-number\n", encoding="utf-8")
    assert RT._collect_apify_cap(type("A", (), {"research_config": str(cfg)})()) == 1200                              # unreadable: the default
    assert RT._collect_apify_cap(cli.build_parser().parse_args(["voice", "--brand", "x"])) == 1200                      # the repository's own research.yaml says 1200: Collect's share (6.00 of the 20 dollars at 4 working keys) at 0.005 an item
    store = _voice_env(tmp_path, monkeypatch)
    fake = FakeApify()
    import contentintel.apify as A
    monkeypatch.setattr(A, "Apify", lambda token: fake)
    cfg.write_text("collectors:\n  apify:\n    monthly_items: 70\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["voice", "--all", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands"])
    args.research_config = str(cfg)
    RT.run_voice(args, store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    assert sum(c[2] for c in fake.calls) <= 40 and fake.calls                                              # 40 for the WHOLE run across all brands, not 40 per brand
    out = capsys.readouterr().out
    assert out.count("Apify lane ON for this run") == 1 and "never beyond the Collect step's 70 a month" in out                  # the owner's configured cap, not the built-in 100


def test_the_research_tools_workflow_has_apify_on_by_default_and_passes_the_switch_off():
    wf = yaml.safe_load(Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8"))
    inputs = (wf.get("on") or wf.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["apify"]["default"] is True and inputs["apify"]["type"] == "boolean"
    body = Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")
    assert 'if [ "$USE_APIFY" = "false" ]; then args+=(--no-apify); fi' in body and "APIFY_TOKEN: ${{ secrets.APIFY_TOKEN }}" in body


# ---- Update 66: the planner's checks on the live round of Update 65 ----------------------------------------------------------------------------------

def test_the_regional_hint_knows_a_regional_address_in_any_letter_case_when_it_knows_the_brand_and_never_flags_a_plain_name():
    for slug, brand, want in (("chobaniau", "Chobani", "au"), ("ChobaniAu", "Chobani", "au"), ("CHOBANINZ", "Chobani", "nz"), ("chobani-nz", "", "nz"), ("ChobaniAu", "", "au"), ("DudeWipesUS", "", "us")):
        assert MP.regional_hint(slug, brand) == want, (slug, brand)
    for slug, brand in (("Chobani", "Chobani"), ("MadeIn", ""), ("MadeIn", "MadeIn"), ("Snuggie", ""), ("theoodie", ""), ("TheOodie", "The Oodie"), ("Graza", "Graza"), ("chobaniau", "")):
        assert MP.regional_hint(slug, brand) == "", (slug, brand)                       # without the name, a lower-case 'chobaniau' cannot be told from 'snuggie' or 'theoodie'
    assert MP.regional_hint("MadeIn", "Made") == "in" and MP.regional_hint("MadeIN", "") == "in"


def test_the_page_id_check_uses_the_brand_name_to_warn_about_a_lower_case_regional_address(capsys):
    get = lambda url, headers=None: (200, '{"pageID":"123456789"}')           # noqa: E731
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/chobaniau", "--name", "Chobani"]), get=get)
    assert "WARNING: the address looks like a regional page (au)" in capsys.readouterr().out
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/chobaniau"]), get=get)
    assert "WARNING" not in capsys.readouterr().out
    assert 'pageid) args=(pageid --url "$VIDEO_URL"); if [ -n "$TOPIC" ]; then args+=(--name "$TOPIC"); fi ;;' in Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")


def test_the_apify_overrides_are_clamped_and_a_cost_override_can_never_defeat_the_ceilings():
    lane = lambda cost: VOICE.ApifyVoice(FakeApify(), _ledger(), max_cost_usd=cost)           # noqa: E731
    assert (lane(None).ceiling("tiktok"), lane(None).ceiling("instagram")) == (0.50, 0.25)
    assert (lane(0.01).ceiling("tiktok"), lane(0.01).ceiling("instagram")) == (0.50, 0.25)         # an override cannot go below what the actor accepts
    assert (lane(0.75).ceiling("tiktok"), lane(0.75).ceiling("instagram")) == (0.75, 0.75)
    assert (lane(500).ceiling("tiktok"), lane(500).ceiling("instagram")) == (1.0, 1.0)             # nor above one dollar
    env = lambda **kw: kw                                                                            # noqa: E731
    assert RT._env_number(env(X="1000"), "X", 40, 1, 100, int) == 100 and RT._env_number(env(X="0"), "X", 40, 1, 100, int) == 1
    assert RT._env_number(env(X="abc"), "X", 40, 1, 100, int) == 40 and RT._env_number(env(), "X", 40, 1, 100, int) == 40 and RT._env_number(env(X="7.5"), "X", None, 0.0, 1.0, float) == 1.0
    assert RT._env_number(env(X="0.3"), "X", None, 0.0, 1.0, float) == 0.3 and RT._env_number(env(), "X", None, 0.0, 1.0, float) is None


def test_the_apify_class_describes_and_defaults_to_what_the_lane_really_does():
    lane = VOICE.ApifyVoice(FakeApify(), _ledger())
    assert (lane.per_run, lane.monthly, lane.shared_cap, lane.max_cost) == (40, 60, 100, None)
    doc = VOICE.ApifyVoice.__doc__
    assert "OFF unless" not in doc and "ON by default" in doc and "0.50 dollars for TikTok" in doc and "27 usable items" in doc and "TikTok asks for half of what is left to read and Instagram for the rest" in doc


def test_when_tiktok_is_refused_instagram_still_gets_the_whole_ask_and_both_are_told():
    from contentintel.providers import ProviderConfigError
    fake = FakeApify()
    real = fake.run

    def run(actor, payload, *, max_items, max_cost_usd):
        if "tiktok" in actor:
            raise ProviderConfigError("the cost ceiling for this run ($0.25) is below the minimum this actor accepts ($0.50). Nothing was run and nothing was spent.")
        return real(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd)
    fake.run = run
    lane = VOICE.ApifyVoice(fake, _ledger(), normalize=lambda s, raw: (raw, 0))
    items, notes = lane.run("sweatpant overalls")
    assert len(items) == 40 and fake.calls[0][1]["resultsLimit"] == 40 and fake.calls[0][3] == 0.25                      # what TikTok could not read is not lost
    assert "ProviderConfigError" in notes[0] and "minimum this actor accepts" in notes[0] and "asked for 40, 40 item(s) read within a 0.25 dollar ceiling" in notes[1]


def test_competitors_mode_asks_apify_about_each_competitor_never_the_brand_and_splits_the_runs_allowance(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    store.write_benchmarks("swoveralls", {"version": 1, "items": {k: {"name": n, "status": "resolved", "page_id": "1"} for k, n in enumerate(["Swoveralls", "The Oodie", "Madepants", "SKIMS"])}})
    fake = FakeApify()
    import contentintel.apify as A
    from contentintel import collectors as COL
    monkeypatch.setattr(A, "Apify", lambda token: fake)
    monkeypatch.setattr(COL, "normalize_items", lambda source, raw: (raw, 0))
    RT.run_voice(_args(tmp_path, "--mode", "competitors"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    asked = [c[1].get("searchQueries", [None])[0] or c[1]["hashtags"][0] for c in fake.calls]
    assert asked == ["The Oodie", "theoodie", "Madepants", "madepants", "SKIMS", "skims"] and "Swoveralls" not in asked and "swoveralls" not in asked          # the brand gets nothing here
    pairs = [fake.calls[i][2] + fake.calls[i + 1][2] for i in range(0, len(fake.calls), 2)]
    assert pairs == [13, 13, 14] and sum(pairs) == 40                                                          # 40 for the run: what is left is split between the competitors still to ask, so nothing is stranded
    kept = store.read_extra("swoveralls", "voice")["items"]
    assert any(i["entity"] == "The Oodie" and i["mode"] == "competitors" and i["source"].startswith("apify ") for i in kept)
    assert "this run, items per competitor: The Oodie: " in out and "The Oodie: 0;" not in out and "the brand itself: 0" in out and out.count("Apify usage after this run:") == 1 and "Apify usage after this run: voice 40/250 items this month, all Apify use 40/1200 (this run read 40)." in out
    assert "(research and competitors modes)" in Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")


def test_every_ffmpeg_install_step_fails_the_run_when_the_install_fails():
    for name in ("collect.yml", "tests.yml", "research-tools.yml"):
        wf = yaml.safe_load((Path(".github/workflows") / name).read_text(encoding="utf-8"))
        steps = [s for job in wf["jobs"].values() for s in job["steps"] if str(s.get("name", "")).startswith("Install ffmpeg")]
        assert len(steps) == (4 if name == "tests.yml" else 1), name          # tests.yml: the main job and the three sabotage jobs (Update 68)
        for step in steps:                                                                                                          # EVERY copy, not only the first (the three sabotage jobs carry one each)
            run = step["run"]
            assert "sudo apt-get install -y -qq ffmpeg" in run and "for attempt in 1 2 3" in run and "|| true" not in run and "|| echo" not in run, name       # nothing is swallowed
            assert run.rstrip().endswith("ffprobe -version | head -1") and "ffmpeg -version | head -1" in run and "FAILS" in step["name"], name           # no binary: the step (and the run) fails
            assert "command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null" in run and "exit 1" in run, name                  # the explicit check that stops the run
        if name == "research-tools.yml":
            assert steps[0]["if"] == "inputs.tool == 'analyze'"


def test_the_apify_usage_is_counted_across_runs_and_the_start_line_shows_it(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    fake = FakeApify()
    import contentintel.apify as A
    from contentintel import collectors as COL
    monkeypatch.setattr(A, "Apify", lambda token: fake)
    monkeypatch.setattr(COL, "normalize_items", lambda source, raw: (raw, 0))
    for expected_before in (0, 40):
        RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
        out = capsys.readouterr().out
        assert f"(used so far this month: {expected_before})" in out                                  # the start line shows the earlier runs; the run's own reading is in the end line
    usage = store.load_usage()["months"]["2026-11"]
    assert usage["voice_apify_items"] == 80 and usage["apify_items"] == 80                              # 40, then 40 more: the voice share of one key (125 a month) still had room
    assert "Apify usage after this run: voice 80/250 items this month, all Apify use 80/1200 (this run read 40)." in out


def test_the_collect_time_finder_already_knows_a_regional_address_from_the_brand_name_in_any_letter_case():
    page = lambda slug, title="Page": {"slug": slug, "title": title}                          # noqa: E731
    for slug in ("chobaniau", "ChobaniAu", "CHOBANINZ", "chobani-nz", "chobani.au"):
        assert MP.looks_regional("Chobani", page(slug)), slug
    for slug, brand in (("Chobani", "Chobani"), ("MadeIn", "MadeIn"), ("Snuggie", "Snuggie"), ("theoodie", "The Oodie")):
        assert not MP.looks_regional(brand, page(slug)), slug
    assert MP.looks_regional("Made", page("MadeIn"))                                            # with another brand name, 'In' is a region


# ---- Update 66: the tool finds the page id itself when the page's own HTML has none ----------------------------------------------------------------

PEOPLE = "https://www.facebook.com/people/Hu-Kitchen/100063775098221/"


def _find(results_by_query, verdict, html_id="", asked=None):
    """lookup_main_page for 'Hu Kitchen' with a page whose HTML has (or has not) an id, fake searches, and a fake reviewer that answers the page review first and the id review after."""
    asked = [] if asked is None else asked

    class Serper:
        def search(self, query, num=10):
            asked.append(query)
            hits = [r for key, rs in results_by_query.items() if key in query for r in rs]
            return [Result(rank=i + 1, title=t, url=u, snippet=s) for i, (t, u, s) in enumerate(hits)]

    class Claude:
        def json_task(self, system, user, max_tokens=0):
            if "numeric Facebook page id" in system:
                return verdict(user)
            return {"choice": "https://www.facebook.com/HuKitchen", "is_main": True, "regional": False, "reason": "the brand's own page"}
    return MP.make_finder(Serper(), Claude(), lambda url: html_id)("Hu Kitchen")


PAGE = {"Hu Kitchen": [("Hu Kitchen | Facebook", "https://www.facebook.com/HuKitchen", "Hu: chocolate and snacks")]}
IDS = {"facebook.com/people": [("Hu Kitchen | Facebook", PEOPLE, "Hu Kitchen. 52K likes"), ("Hu Kitchen NZ", "https://www.facebook.com/people/Hu-Kitchen-NZ/100090000000001/", "NZ page")]}


def test_the_numeric_id_is_found_in_search_results_and_confirmed_by_the_reviewer_when_the_page_html_has_none():
    asked = []
    out = _find({**PAGE, **IDS}, lambda user: {"choice": "100063775098221", "same_page": True, "regional": False, "reason": "the people address names Hu Kitchen with no region"}, asked=asked)
    assert out["status"] == "resolved" and out["page_id"] == "100063775098221"
    assert "the numeric id came from a search (https://www.facebook.com/people/Hu-Kitchen/100063775098221/)" in out["reason"] and "the reviewer confirmed it is the same page" in out["reason"]
    assert any("facebook.com/people" in q for q in asked)


def test_the_id_search_is_not_used_when_the_page_itself_gives_the_id_and_never_accepts_an_invented_or_regional_id():
    asked = []
    out = _find({**PAGE, **IDS}, lambda user: pytest.fail("the reviewer must not be asked"), html_id='{"pageID":"555555555"}', asked=asked)
    assert out["page_id"] == "555555555" and not any("people" in q for q in asked)
    invented = _find({**PAGE, **IDS}, lambda user: {"choice": "999999999999", "same_page": True, "regional": False, "reason": "x"})
    assert invented["status"] == "ambiguous" and "could not confirm one as this page" in invented["reason"] and invented["page_id"] == ""
    regional = _find({**PAGE, **IDS}, lambda user: {"choice": "100090000000001", "same_page": True, "regional": True, "reason": "NZ"})
    assert regional["status"] == "ambiguous" and regional["page_id"] == ""
    unsure = _find({**PAGE, **IDS}, lambda user: {"choice": None, "same_page": False, "regional": False, "reason": "two pages could match"})
    assert unsure["status"] == "ambiguous" and "(two pages could match)" in unsure["reason"]
    unsame = _find({**PAGE, **IDS}, lambda user: {"choice": "100063775098221", "same_page": False, "regional": False, "reason": "other brand"})
    assert unsame["status"] == "ambiguous"


def test_when_no_search_result_carries_a_numeric_id_the_reason_says_so_and_a_failed_search_is_not_a_crash():
    none = _find(PAGE, lambda user: pytest.fail("nothing to review"))
    assert none["status"] == "ambiguous" and "a search for the page's numeric id found no address carrying one" in none["reason"] and "no page id in the page" in none["reason"]
    calls = []

    def search(query):
        calls.append(query)
        if "people" in query:
            raise TimeoutError("slow")
        return [{"url": "https://www.facebook.com/HuKitchen", "title": "Hu Kitchen | Facebook", "snippet": "Hu"}]
    page_id, why = MP.find_id_by_search("Hu Kitchen", "https://www.facebook.com/HuKitchen", search, lambda *a: {})
    assert page_id == "" and "failed (TimeoutError)" in why


def test_id_candidates_are_read_from_every_kind_of_facebook_address_once():
    results = [{"url": PEOPLE, "title": "t", "snippet": ""}, {"url": "https://www.facebook.com/profile.php?id=100064549155148", "title": "t2", "snippet": ""},
               {"url": "https://www.facebook.com/ads/library/?active_status=all&view_all_page_id=102637398978655&country=ALL", "title": "Ads", "snippet": ""},
               {"url": "https://www.facebook.com/pages/Hu-Kitchen/227070710682171", "title": "t3", "snippet": ""}, {"url": PEOPLE, "title": "again", "snippet": ""},
               {"url": "https://www.facebook.com/HuKitchen", "title": "no id", "snippet": "no id here 123"}]
    assert [c["id"] for c in MP.id_candidates(results)] == ["100063775098221", "100064549155148", "102637398978655", "227070710682171"]


def test_the_page_id_check_lists_the_numeric_ids_a_search_finds_when_the_four_places_give_none(capsys):
    get = lambda url, headers=None: (200, "<html>log in</html>")                                # noqa: E731
    args = cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/HuKitchen", "--name", "Hu Kitchen"])
    RT.run_pageid(args, get=get, search=lambda q: [{"url": PEOPLE, "title": "Hu Kitchen | Facebook", "snippet": ""}] if "people" in q else [])
    out = capsys.readouterr().out
    assert "Search for the numeric id of 'Hu Kitchen': 1 candidate id(s) found in search results" in out and "id 100063775098221 from " + PEOPLE in out
    RT.run_pageid(args, get=get, search=None, environ={})
    assert "Search for the numeric id: not tried (SERPER_API_KEY is not set in this run)" in capsys.readouterr().out
    RT.run_pageid(args, get=lambda url, headers=None: (200, '{"pageID":"123456789"}'), search=lambda q: pytest.fail("not needed when an id is read"))


# ---- Update 67: the planner's review of the live round of Update 66 ----------------------------------------------------------------------------------

def _finder_with(monkeypatch, page_for):
    from contentintel import cli as CLI
    seen = []

    class Page:
        def __init__(self, text):
            self.text = text

    def getter(url, headers=None, timeout=0):
        seen.append((url, headers["User-Agent"]))
        return Page(page_for(url, headers["User-Agent"]))

    class Serper:
        def search(self, query, num=10):
            return [Result(rank=1, title="Hu Kitchen | Facebook", url="https://www.facebook.com/HuKitchen", snippet="Hu")]

    class Claude:
        def json_task(self, system, user, max_tokens=0):
            return {"choice": "https://www.facebook.com/HuKitchen", "is_main": True, "regional": False, "reason": "the brand's own page"}
    monkeypatch.setattr(CLI, "_HTTP_GET", getter)
    monkeypatch.setattr(CLI, "_search_providers", lambda args, purpose: {"serper_dev": Serper()})
    finder, why = CLI._page_finder_status(Claude(), type("A", (), {})())
    assert why == ""
    return finder("Hu Kitchen"), seen


HONEST = "Mozilla/5.0 (compatible; content-strategy-tool; read-only research)"


def test_the_collect_finder_asks_with_the_tools_own_honest_identity_first_and_stops_when_it_reads_the_id(monkeypatch):
    out, seen = _finder_with(monkeypatch, lambda url, ua: '{"pageID":"100064252197325"}' if ua == HONEST and "m." not in url else "<html>Log in</html>")
    assert out["status"] == "resolved" and out["page_id"] == "100064252197325" and seen == [("https://www.facebook.com/HuKitchen", HONEST)]          # one request: nothing more is asked once the id is read


def test_the_collect_finder_keeps_the_browser_identity_as_a_second_try_so_nothing_that_worked_is_lost(monkeypatch):
    out, seen = _finder_with(monkeypatch, lambda url, ua: '{"pageID":"100034777406193"}' if "Chrome" in ua else "<html>Log in</html>")        # a page only the earlier identity could read (Defi Snacks, Magic Spoon, Chobani in Collect #61)
    assert out["status"] == "resolved" and out["page_id"] == "100034777406193"
    assert [ua for _, ua in seen] == [HONEST, "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"] and all(u == "https://www.facebook.com/HuKitchen" for u, _ in seen)
    none, seen = _finder_with(monkeypatch, lambda url, ua: "<html>Log in</html>")
    assert none["status"] == "ambiguous" and "www.facebook.com as a browser: no page id in the page (19 characters)" in none["reason"] and len(seen) == 5            # honest, browser, mobile, basic, plugin


def test_the_finders_reason_says_how_many_characters_each_address_returned():
    out = MP.lookup_main_page("Hu Kitchen", lambda q: [{"url": "https://www.facebook.com/HuKitchen", "title": "Hu Kitchen | Facebook", "snippet": "Hu"}],
                              lambda n, c: {"choice": "https://www.facebook.com/HuKitchen", "is_main": True, "regional": False, "reason": "main"}, lambda url: "", "the reviewer")
    assert out["status"] == "ambiguous"
    read = MP.make_finder(type("S", (), {"search": lambda self, q, num=10: [Result(rank=1, title="Hu Kitchen | Facebook", url="https://www.facebook.com/HuKitchen", snippet="Hu")]})(),
                          type("C", (), {"json_task": lambda self, s, u, max_tokens=0: {"choice": "https://www.facebook.com/HuKitchen", "is_main": True, "regional": False, "reason": "main"}})(), lambda url: "x" * 1234)("Hu Kitchen")
    assert "www.facebook.com: no page id in the page (1,234 characters)" in read["reason"] and "www.facebook.com/plugins: no page id in the page (1,234 characters)" in read["reason"]


def test_the_engines_own_account_of_the_keyed_lanes_is_kept_and_meta_ads_is_opted_in_with_the_key(tmp_path):
    stderr = "Research quality: 3/4 core sources.\nBonus source unavailable: Instagram.\n  - Instagram returned 0 reels despite SC being configured.\n[Planner] fine\n"
    seen = []

    def runner(cmd, **kw):
        seen.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, (FIX / "l30_research_mock_real.json").read_text(), stderr)
    root = checkout(tmp_path)
    with_key = VOICE.ExternalLast30Days(SETTINGS, root=root, environ={"SCRAPECREATORS_API_KEY": "k", "INCLUDE_SOURCES": "threads"}, runner=runner)
    with_key.run("x")
    assert "Research quality: 3/4 core sources." in with_key.engine_summary and "Bonus source unavailable: Instagram." in with_key.engine_summary and "returned 0 reels" in with_key.engine_summary and "fine" not in with_key.engine_summary
    assert seen[0]["INCLUDE_SOURCES"] == "meta_ads"                                                       # the platform sets its own opt-in; nothing else is passed through
    VOICE.ExternalLast30Days(SETTINGS, root=root, environ={}, runner=runner).run("x")
    assert "INCLUDE_SOURCES" not in seen[1]                                                              # no key: no opt-in, no keyed lane


def test_the_voice_run_prints_the_engines_account_of_the_keyed_lanes_when_the_key_is_handed_over(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    stderr = "Research quality: 3/4 core sources.\nBonus source unavailable: Instagram.\n"
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls", "--no-apify"), store, NOW, get=Get({}), runner=Runner((FIX / "l30_research_mock_real.json").read_text(), 0, stderr), environ={"SCRAPECREATORS_API_KEY": "k"}, clock=lambda: 1)
    assert "the engine's own account of the keyed lanes: Research quality: 3/4 core sources. | Bonus source unavailable: Instagram." in capsys.readouterr().out
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls", "--no-apify"), store, NOW, get=Get({}), runner=Runner((FIX / "l30_research_mock_real.json").read_text(), 0, stderr), environ={}, clock=lambda: 1)
    assert "the engine's own account" not in capsys.readouterr().out


def test_several_apify_keys_fail_over_in_order_and_a_refused_key_is_not_asked_again():
    from contentintel import apify as A
    from contentintel.providers import AuthError, ProviderConfigError, QuotaError
    assert A.build_apify({}) is None and isinstance(A.build_apify({"APIFY_TOKEN": "a"}), A.Apify) and isinstance(A.build_apify({"APIFY_TOKEN": "a", "APIFY_TOKEN_2": "b"}), A.ApifyPool)
    assert isinstance(A.build_apify({"APIFY_TOKEN": "a", "APIFY_TOKEN_2": "a", "APIFY_TOKEN_3": " "}), A.Apify)                  # the same key twice counts once; a blank is no key
    calls = []

    class Key:
        def __init__(self, name, error=None):
            self.name, self.error = name, error

        def run(self, actor, payload, *, max_items, max_cost_usd, timeout=280):
            calls.append(self.name)
            if self.error:
                raise self.error
            return [{"ok": self.name}]
    pool = A.ApifyPool(["a", "b", "c"])
    pool.clients = [Key("a", AuthError("Apify: the token was rejected")), Key("b", QuotaError("Apify: out of credits")), Key("c")]
    assert pool.run("x/y", {}, max_items=1, max_cost_usd=0.25) == [{"ok": "c"}] and calls == ["a", "b", "c"]
    assert pool.run("x/y", {}, max_items=1, max_cost_usd=0.25) == [{"ok": "c"}] and calls == ["a", "b", "c", "c"]                  # a and b are not asked again in this run
    assert pool.events == ["Apify key 1 of 3 was refused (AuthError); the next key answers instead", "Apify key 2 of 3 was refused (QuotaError); the next key answers instead"]
    pool = A.ApifyPool(["a", "b"])
    pool.clients = [Key("a", ProviderConfigError("the cost ceiling is below the minimum")), Key("b")]
    calls.clear()
    with pytest.raises(ProviderConfigError):
        pool.run("x/y", {}, max_items=1, max_cost_usd=0.01)
    assert calls == ["a"] and not pool.events                                                          # a refusal another key cannot cure is raised at once
    pool = A.ApifyPool(["secret-1", "secret-2"])
    pool.clients = [Key("a", AuthError("x")), Key("b", AuthError("y"))]
    with pytest.raises(AuthError) as exc:
        pool.run("x/y", {}, max_items=1, max_cost_usd=0.25)
    assert "every key was refused" in str(exc.value) and "secret" not in str(exc.value)
    cli_source = Path("contentintel/cli.py").read_text(encoding="utf-8")
    assert "Apify(apify_token)" not in cli_source and cli_source.count("apify_mod.build_apify(os.environ)") == 3


def test_the_apify_voice_lane_reports_a_key_switch_and_the_extra_keys_reach_the_workflows(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    from contentintel import apify as A
    from contentintel import collectors as COL
    from contentintel.providers import AuthError
    monkeypatch.setattr(COL, "normalize_items", lambda source, raw: (raw, 0))
    good = FakeApify()

    class Bad:
        def run(self, *a, **k):
            raise AuthError("Apify: the token was rejected")
    pool = A.ApifyPool(["a", "b"])
    pool.clients = [Bad(), good]
    monkeypatch.setattr(A, "build_apify", lambda environ: pool)
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "a", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "Apify keys: Apify key 1 of 2 was refused (AuthError); the next key answers instead" in out and len(good.calls) == 2
    for name in ("collect.yml", "research-tools.yml", "request.yml"):
        body = (Path(".github/workflows") / name).read_text(encoding="utf-8")
        assert all(f"APIFY_TOKEN_{n}: ${{{{ secrets.APIFY_TOKEN_{n} }}}}" in body for n in (2, 3, 4)), name
    assert "APIFY_TOKEN_2" in Path("README.md").read_text(encoding="utf-8")


def test_a_brands_own_punctuated_address_is_never_regional_and_the_id_search_survives_its_reviewer():
    for slug in ("made-in", "made_in", "made.in"):
        assert MP.regional_hint(slug, "Made In") == "", slug
    assert MP.regional_hint("something-us", "Something US") == "" and MP.regional_hint("chobani-nz", "Chobani") == "nz" and MP.regional_hint("made-in", "") == "in"
    queries = []

    def search(query):
        queries.append(query)
        return [{"url": f"https://www.facebook.com/people/Hu-Kitchen/10006377509822{len(queries)}/", "title": "Hu Kitchen", "snippet": ""}] if len(queries) < 3 else [{"url": "https://www.facebook.com/ads/library/?view_all_page_id=102637398978655", "title": "Ads", "snippet": ""}]
    page_id, why = MP.find_id_by_search("Hu Kitchen", "https://www.facebook.com/HuKitchen", search, lambda *a: {"choice": None, "same_page": False, "regional": False, "reason": "none match"})
    assert len(queries) == 3 and "view_all_page_id" in queries[2] and page_id == "" and "3 numeric id(s)" in why and "(none match)" in why              # the Ad Library query ran although the first two found ids
    seen = []

    def boom(name, url, cands):
        seen.append(cands)
        raise RuntimeError("the reviewer is down")
    page_id, why = MP.find_id_by_search("Hu Kitchen", "https://www.facebook.com/HuKitchen", search, boom)
    assert page_id == "" and "the reviewer could not be asked (RuntimeError)" in why
    many = lambda q: [{"url": f"https://www.facebook.com/people/Hu-Kitchen/{100000000 + i}/", "title": "t", "snippet": ""} for i in range(12)]          # noqa: E731
    page_id, why = MP.find_id_by_search("Hu Kitchen", "u", many, lambda n, u, cands: {"choice": "100000011", "same_page": True, "regional": False, "reason": "x"})
    assert page_id == "" and "could not confirm" in why                                                    # the 12th candidate was never shown to the reviewer, so it cannot be chosen
    out = MP.lookup_main_page("Hu Kitchen", lambda q: [{"url": "https://www.facebook.com/HuKitchen", "title": "Hu Kitchen | Facebook", "snippet": "Hu"}],
                              lambda n, c: {"choice": "https://www.facebook.com/HuKitchen", "is_main": True, "regional": False, "reason": "main"}, lambda url: "", "the reviewer")
    assert "no reviewer is available to confirm a numeric id found by search, so none was looked for" in out["reason"]


def test_when_the_engine_and_the_backup_both_fail_the_apify_lane_still_asks_and_the_allowance_is_not_stranded(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    import contentintel.apify as A
    from contentintel import collectors as COL
    fake = FakeApify()
    monkeypatch.setattr(A, "Apify", lambda token: fake)
    monkeypatch.setattr(COL, "normalize_items", lambda source, raw: (raw, 0))
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (500, ""), "reddit.com": (500, "")}), runner=Runner(ALL_FAILED, 0, ENGINE_STDERR), environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "the last30days engine had also failed" in out and len(fake.calls) == 2 and "Apify usage after this run: voice 40/250" in out and "item(s) from none" not in out
    assert store.load_usage()["months"]["2026-11"]["voice_apify_items"] == 40                           # saved after the ask
    under = FakeApify()
    real = under.run

    def run(actor, payload, *, max_items, max_cost_usd):
        got = real(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd)
        return got[:2] if "Madepants" in str(payload) or "madepants" in str(payload) else got              # one competitor reads less
    under.run = run
    lane = VOICE.ApifyVoice(under, _ledger(), normalize=lambda s, raw: (raw, 0))
    total = 0
    for idx, name in enumerate(["The Oodie", "Madepants", "SKIMS"]):
        items, _ = lane.run(name, share=max(2, lane.room() // (3 - idx)), entity=name)
        total += len(items)
    assert total > 33 and lane.spent_this_run == total                              # what Madepants did not read went to SKIMS


def test_the_apify_lane_notes_an_empty_hashtag_clamps_its_own_limits_and_counts_its_asks():
    lane = VOICE.ApifyVoice(FakeApify(), _ledger(), per_run=1000, monthly=1000, shared_cap=100, normalize=lambda s, raw: (raw, 0))
    assert (lane.per_run, lane.monthly) == (300, 1000) and (VOICE.ApifyVoice(FakeApify(), _ledger(), per_run=0, monthly=-5).per_run, VOICE.ApifyVoice(FakeApify(), _ledger(), per_run=0, monthly=-5).monthly) == (1, 1)
    items, notes = lane.run("!!! ???")
    assert any("not asked (the topic has no letters or digits to make a hashtag from)" in n for n in notes) and lane.asks_made == 1 and len(items) == 50
    lane.run("again")
    assert lane.asks_made == 2


def test_the_page_id_check_validates_the_brand_name_and_says_when_none_was_given(capsys):
    get = lambda url, headers=None: (200, '{"pageID":"123456789"}')           # noqa: E731
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani"]), get=get)
    assert "No brand name was given: only the unambiguous signs of a regional page are checked" in capsys.readouterr().out
    for bad in ("https://www.facebook.com/Chobani", "x" * 70):
        RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani", "--name", bad]), get=get)
        out = capsys.readouterr().out
        assert "does not look like a name" in out and "it is ignored" in out and "No brand name was given" in out
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/Chobani", "--name", "Chobani"]), get=get)
    assert "No brand name was given" not in capsys.readouterr().out
    RT.run_pageid(cli.build_parser().parse_args(["pageid", "--url", "https://www.facebook.com/HuKitchen", "--name", "Hu Kitchen"]), get=lambda u, headers=None: (200, "log in"), search=lambda q: [])
    out = capsys.readouterr().out
    assert "the Collect finder searches for the id itself and its reviewer confirms it" in out and "has to be chosen by a person" not in out


def test_every_ffmpeg_step_checks_explicitly_for_both_programs_and_the_docs_hold_no_manual_id_instruction():
    for name in ("collect.yml", "tests.yml", "research-tools.yml"):
        body = (Path(".github/workflows") / name).read_text(encoding="utf-8")
        assert 'command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || { echo "ffmpeg or ffprobe is missing after 3 attempts: failing this run"; exit 1; }' in body, name
        wf = yaml.safe_load(body)
        step = [s for job in wf["jobs"].values() for s in job["steps"] if str(s.get("name", "")).startswith("Install ffmpeg")][0]
        assert "continue-on-error" not in step and not any(job.get("continue-on-error") for job in wf["jobs"].values()), name
    docs = Path("docs/platform/batch1-research-upgrade.md").read_text(encoding="utf-8")
    assert "benchmark_pages: Chobani" not in docs and "a person choosing the page" not in docs and "Nothing is added by hand" in docs and "nothing is ever entered by hand" in docs
    update65_row = [l for l in docs.split("\n") if l.startswith("| Apify as a voice lane")][0]
    assert "0.50 dollars for TikTok" not in update65_row and "competitors" not in update65_row.split("The live round")[0] and "0.25 dollars" in update65_row
    help_text = [x for x in cli.build_parser()._subparsers._group_actions[0].choices["voice"]._actions if "--apify" in x.option_strings][0].help
    assert "0.25 dollars)" not in help_text.split("0.50")[0] and "competitors" in help_text and "0.50 dollars for TikTok" in help_text


def test_the_apify_usage_line_is_printed_only_when_something_was_asked(tmp_path, capsys, monkeypatch):
    store = _voice_env(tmp_path, monkeypatch)
    import contentintel.apify as A
    monkeypatch.setattr(A, "Apify", lambda token: FakeApify())
    RT.run_voice(_args(tmp_path, "--mode", "competitors"), store, "2026-11-15T00:00:00Z", get=Get({}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "competitors mode needs peers" in out and "Apify usage after this run" not in out


# ---- Update 67: the Apify budget in real dollars, several keys (three planned, four held since), every month its own --------------------------------------------------------------------

def _limits_http(used, limit, ends="2026-12-01T00:00:00.000Z", status=200, body=None):
    def http(method, url, *, params=None, json_body=None, headers=None, timeout=30):
        assert method == "GET" and url == "https://api.apify.com/v2/users/me/limits" and headers["Authorization"].startswith("Bearer ")
        return status, body if body is not None else {"data": {"current": {"monthlyUsageUsd": used}, "limits": {"maxMonthlyUsageUsd": limit}, "monthlyUsageCycle": {"endAt": ends}}}, None
    return http


def test_apify_reports_its_own_monthly_dollars_and_says_so_when_it_cannot():
    from contentintel import apify as A
    assert A.Apify("t", http=_limits_http(1.25, 5)).usage() == {"used": 1.25, "limit": 5.0, "ends": "2026-12-01"}
    assert A.Apify("t", http=_limits_http(0, 5, status=500, body={"error": "x"}), sleep=lambda s: None).usage() is None
    assert A.Apify("t", http=_limits_http(0, 5, body={"data": {"current": {}, "limits": {}}})).usage() is None
    lines, left = A.budget_report(type("C", (), {"usages": lambda self: [None]})())
    assert left is None and lines[0] == "Apify budget: Apify did not report the money left, so only the item counters guard the spending (the item allowances still apply):" and lines[1] == "  key 1: usage could not be read"


def test_three_keys_make_a_fifteen_dollar_month_reported_per_key_with_nothing_carried_forward():
    from contentintel import apify as A
    pool = A.ApifyPool(["a", "b", "c"])
    pool.clients = [A.Apify("a", http=_limits_http(5.0, 5)), A.Apify("b", http=_limits_http(2.0, 5)), A.Apify("c", http=_limits_http(0.5, 5, ends="2026-12-04T00:00:00.000Z"))]
    lines, left = A.budget_report(pool)
    assert left == 7.5 and lines[0] == "Apify budget this cycle (each key's allowance is per monthly cycle and nothing carries forward): $7.50 left of $15.00 across 3 key(s); the keys' cycles end on different days (see each key), so this total is not one window"
    assert lines[1:] == ["  key 1: $5.00 of $5.00 used, cycle ends 2026-12-01", "  key 2: $2.00 of $5.00 used, cycle ends 2026-12-01", "  key 3: $0.50 of $5.00 used, cycle ends 2026-12-04"]


def test_the_apify_lane_does_not_ask_when_apify_reports_less_money_left_than_the_ceiling():
    left = {"v": 0.40}
    fake = FakeApify()
    lane = VOICE.ApifyVoice(fake, _ledger(), normalize=lambda s, raw: (raw, 0), budget_fn=lambda: left["v"])
    items, notes = lane.run("sweatpant overalls")
    assert [c[0] for c in fake.calls] == ["apify/instagram-hashtag-scraper"]                         # TikTok needs 0.50, Instagram 0.25
    assert "apify tiktok for 'sweatpant overalls': not asked (Apify reports $0.40 left on the best key this cycle, less than this call's $0.5 ceiling)" in notes[0]
    left["v"] = 0.10
    fake.calls.clear()
    assert lane.run("again")[0] == [] and fake.calls == []


def test_the_voice_run_prints_the_budget_before_asking_and_scales_its_limits_with_the_keys(tmp_path, capsys, monkeypatch):
    from contentintel import apify as A
    from contentintel import collectors as COL
    store = _voice_env(tmp_path, monkeypatch)
    fake = FakeApify()
    pool = A.ApifyPool(["a", "b", "c"])
    pool.clients = [A.Apify("a", http=_limits_http(5.0, 5)), A.Apify("b", http=_limits_http(1.0, 5)), A.Apify("c", http=_limits_http(0.0, 5))]
    pool.clients[1].run = fake.run
    pool.clients[2].run = fake.run
    monkeypatch.setattr(A, "build_apify", lambda environ: pool)
    monkeypatch.setattr(COL, "normalize_items", lambda source, raw: (raw, 0))
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "a", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert out.index("Apify budget this cycle") < out.index("Apify lane ON") and "$9.00 left of $15.00 across 3 key(s)" in out
    assert "at most 120 items in this whole run, 750 a month for voice" in out and "never beyond the Collect step's 1200 a month" in out
    assert "Apify key 1 of 3 has $0.00 left, less than the $0.25 the smallest call needs; the next key is used" in out and len(fake.calls) == 2
    assert "Apify usage after this run: voice 120/750 items this month, all Apify use 120/1200 (this run read 120)." in out


def test_every_month_is_its_own_budget_and_nothing_is_carried_forward():
    from contentintel.research import Ledger
    led = Ledger({"months": {"2026-11": {"apify_items": 300, "voice_apify_items": 180}}, "lifetime": {}}, "2026-12")
    assert led.month_used("apify_items") == 0 and led.month_used("voice_apify_items") == 0
    lane = VOICE.ApifyVoice(FakeApify(), led, per_run=40, monthly=60, shared_cap=300)
    assert lane.room() == 40                                                                             # December starts at zero whatever November spent
    docs = Path("docs/platform/batch1-research-upgrade.md").read_text(encoding="utf-8")
    assert "Every month is its own budget, nothing is carried forward" in docs and "$15.00 across 3 key(s)" in docs and "has not been read live yet" in docs


def test_the_money_left_is_read_again_before_each_call_so_a_call_that_spent_it_stops_the_next(tmp_path, capsys, monkeypatch):
    from contentintel import apify as A
    from contentintel import collectors as COL
    store = _voice_env(tmp_path, monkeypatch)
    state, calls = {"used": 0.0}, []
    client = A.Apify("a", http=lambda method, url, *, params=None, json_body=None, headers=None, timeout=30: (200, {"data": {"current": {"monthlyUsageUsd": state["used"]}, "limits": {"maxMonthlyUsageUsd": 5}}}, None))

    def run(actor, payload, *, max_items, max_cost_usd, timeout=280):
        calls.append(actor)
        client.forget_usage()                                                                              # as the real run does: the next reading is a fresh one
        state["used"] += 4.9                                                                               # this call spent almost all of the key's five dollars
        return [{"text": "a caption about overalls", "likes": 5, "url": "https://x/1"}]
    client.run = run
    monkeypatch.setattr(A, "build_apify", lambda environ: client)
    monkeypatch.setattr(COL, "normalize_items", lambda source, raw: (raw, 0))
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "a", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert calls == ["clockworks/tiktok-scraper"] and "apify instagram for 'sweatpant overalls': not asked (Apify reports $0.10 left on the best key this cycle, less than this call's $0.25 ceiling)" in out


def test_the_apify_budget_tool_prints_the_real_money_left_per_key_and_spends_nothing(tmp_path, capsys, monkeypatch):
    from contentintel import apify as A
    pool = A.ApifyPool(["a", "b", "c"])
    pool.clients = [A.Apify("a", http=_limits_http(0.0, 5)), A.Apify("b", http=_limits_http(3.25, 5)), A.Apify("c", http=_limits_http(5.0, 5, ends="2026-12-04T00:00:00.000Z"))]
    monkeypatch.setattr(A, "build_apify", lambda environ: pool)
    args = cli.build_parser().parse_args(["apify-budget", "--data-dir", str(tmp_path / "data")])
    assert RT.run_apify_budget(args, environ={"APIFY_TOKEN": "a", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}) == 0
    out = capsys.readouterr().out
    assert "$6.75 left of $15.00 across 3 key(s)" in out and "key 2: $3.25 of $5.00 used, cycle ends 2026-12-01" in out and "key 3: $5.00 of $5.00 used, cycle ends 2026-12-04" in out
    assert "Item counters (this calendar month, always applied): all Apify use 0, voice 0" in out


def test_when_apifys_endpoint_cannot_be_read_the_budget_tool_says_why_and_that_the_item_counters_still_guard(tmp_path, capsys, monkeypatch):
    from contentintel import apify as A
    client = A.Apify("a", http=_limits_http(0, 5, status=403, body={"error": {"type": "forbidden", "message": "token cannot read limits"}}), sleep=lambda s: None)
    monkeypatch.setattr(A, "build_apify", lambda environ: client)
    args = cli.build_parser().parse_args(["apify-budget", "--data-dir", str(tmp_path / "data")])
    assert RT.run_apify_budget(args, environ={"APIFY_TOKEN": "a", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}) == 1
    out = capsys.readouterr().out
    assert "so only the item counters guard the spending (the item allowances still apply)" in out and "key 1: usage could not be read (" in out and "Item counters (this calendar month, always applied)" in out
    odd = A.Apify("a", http=_limits_http(0, 5, body={"data": {"somethingElse": 1}}))
    monkeypatch.setattr(A, "build_apify", lambda environ: odd)
    RT.run_apify_budget(args, environ={"APIFY_TOKEN": "a", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"})
    assert "no monthlyUsageUsd / maxMonthlyUsageUsd numbers (fields seen: somethingElse)" in capsys.readouterr().out
    monkeypatch.setattr(A, "build_apify", lambda environ: None)
    assert RT.run_apify_budget(args, environ={}) == 1 and "no Apify key is set in this run" in capsys.readouterr().out


def test_the_research_tools_workflow_can_run_the_budget_check_and_the_voice_run_says_when_the_endpoint_failed(tmp_path, capsys, monkeypatch):
    body = Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")
    assert "apify-budget) args=(apify-budget) ;;" in body and "apify-budget, analytics, install]" in body and "APIFY_TOKEN: ${{ secrets.APIFY_TOKEN }}" in body
    from contentintel import apify as A
    from contentintel import collectors as COL
    store = _voice_env(tmp_path, monkeypatch)
    client = A.Apify("a", http=_limits_http(0, 5, status=500, body={"error": "x"}), sleep=lambda s: None)
    client.run = FakeApify().run
    monkeypatch.setattr(A, "build_apify", lambda environ: client)
    monkeypatch.setattr(COL, "normalize_items", lambda source, raw: (raw, 0))
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "a", "CONTENTINTEL_VOICE_APIFY_DAILY": "0"}, clock=lambda: 1)
    out = capsys.readouterr().out
    assert "did not report the money left, so only the item counters guard the spending" in out and "usage could not be read (" in out and "Apify lane ON" in out        # the lane still runs, guarded by the counters


def test_the_budget_command_runs_through_the_command_line_entry(tmp_path, capsys, monkeypatch):
    from contentintel import apify as A
    monkeypatch.setattr(A, "build_apify", lambda environ: A.Apify("a", http=_limits_http(1.0, 5)))
    assert cli.main(["apify-budget", "--data-dir", str(tmp_path / "data")]) == 0
    assert "$4.00 left of $5.00 across 1 key(s)" in capsys.readouterr().out
