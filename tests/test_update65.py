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
    assert "SERPER_API_KEY, SCRAPECREATORS_API_KEY. SCRAPECREATORS_API_KEY is set in this run and is handed to the engine, so TikTok, Instagram and Meta Ads are asked too." in out
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
    assert any(s["provider"] == "beta" and "cannot do what the see job needs (vision)" in s["reason"] for s in res["skipped"])


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
    assert len(pool) == 32 and all("text" in (reg.providers[r["provider"]].model(r["model"]).capabilities or ("text",)) for r in pool)          # every pool model can do a text job: nothing was shrunk
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
    assert fake.calls[0][1] == {"searchQueries": ["sweatpant overalls"], "resultsPerPage": 7} and fake.calls[1][1] == {"hashtags": ["sweatpantoveralls"], "resultsType": "posts", "resultsLimit": 8} and all(c[3] == 0.25 for c in fake.calls)
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
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls", "--no-apify"), store, "2026-11-15T00:00:00Z", get=get, runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t"}, clock=lambda: 1_790_000_000)
    assert fake.calls == [] and "Apify lane" not in capsys.readouterr().out                                    # switched off: not used
    RT.run_voice(_args(tmp_path, "--topic", "sweatpant overalls"), store, "2026-11-15T00:00:00Z", get=get, runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t"}, clock=lambda: 1_790_000_000)
    out = capsys.readouterr().out
    assert "Apify lane ON for this run (switch off with --no-apify): at most 40 items in this whole run, 60 a month for voice (used so far this month: 0)" in out and "never beyond the Collect step's 100 a month" in out and len(fake.calls) == 2 and all(c[3] == 0.25 for c in fake.calls)          # each Apify call carries the cost ceiling
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
    assert RT._collect_apify_cap(type("A", (), {"research_config": str(tmp_path / "missing.yaml")})()) == 100          # no file: the built-in default
    cfg.write_text("collectors:\n  apify:\n    monthly_items: not-a-number\n", encoding="utf-8")
    assert RT._collect_apify_cap(type("A", (), {"research_config": str(cfg)})()) == 100                               # unreadable: the default
    assert RT._collect_apify_cap(cli.build_parser().parse_args(["voice", "--brand", "x"])) == 100                      # the repository's own research.yaml says 100
    store = _voice_env(tmp_path, monkeypatch)
    fake = FakeApify()
    import contentintel.apify as A
    monkeypatch.setattr(A, "Apify", lambda token: fake)
    cfg.write_text("collectors:\n  apify:\n    monthly_items: 70\n", encoding="utf-8")
    args = cli.build_parser().parse_args(["voice", "--all", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands"])
    args.research_config = str(cfg)
    RT.run_voice(args, store, "2026-11-15T00:00:00Z", get=Get({"algolia": (200, json.dumps({"hits": []}))}), runner=Runner("", 1, "boom"), environ={"APIFY_TOKEN": "t"}, clock=lambda: 1)
    assert sum(c[2] for c in fake.calls) <= 40 and fake.calls                                              # 40 for the WHOLE run across all brands, not 40 per brand
    out = capsys.readouterr().out
    assert out.count("Apify lane ON for this run") == 1 and "never beyond the Collect step's 70 a month" in out                  # the owner's configured cap, not the built-in 100


def test_the_research_tools_workflow_has_apify_on_by_default_and_passes_the_switch_off():
    wf = yaml.safe_load(Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8"))
    inputs = (wf.get("on") or wf.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["apify"]["default"] is True and inputs["apify"]["type"] == "boolean"
    body = Path(".github/workflows/research-tools.yml").read_text(encoding="utf-8")
    assert 'if [ "$USE_APIFY" = "false" ]; then args+=(--no-apify); fi' in body and "APIFY_TOKEN: ${{ secrets.APIFY_TOKEN }}" in body
