"""analyze: the facts' route named, the comment text (yt-dlp, then Apify), the speech, the download route and the analysis kept in the repository."""
import json
import subprocess
from pathlib import Path

from contentintel import cli
from contentintel import research_tools as RT
from contentintel import videometa as VM
from contentintel.store import Store

AT = "2026-11-19T12:00:00Z"
URL = "https://www.tiktok.com/@a/video/1"
INFO = {"webpage_url": URL, "extractor_key": "TikTok", "title": "Marking snacks challenge", "duration": 19, "view_count": 2202, "like_count": 124, "comment_count": 8, "repost_count": 31, "uploader": "a"}


def fake_runner(info=None, fail_download=False, ffmpeg_ok=True, ext="mp4"):
    calls = []

    def run(cmd, **kw):
        calls.append(list(cmd))
        joined = " ".join(map(str, cmd))
        if "yt_dlp" in joined:
            if "-o" not in cmd:
                return subprocess.CompletedProcess(cmd, 0, json.dumps(info if info is not None else INFO), "")
            if fail_download:
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: unable to download")
            Path(cmd[cmd.index("-o") + 1].replace("%(ext)s", ext)).write_bytes(b"VIDEO" * 100)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd and cmd[0] == "ffmpeg":
            if not ffmpeg_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "ffmpeg is broken")
            Path(cmd[-1]).write_bytes(b"AUDIO" * 100)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")
    run.calls = calls
    return run


def groq_post(text="we are marking the snacks", status=200):
    def post(url, headers, body, timeout):
        return status, {"text": f" {text} ", "language": "en", "segments": []}
    return post


class FakeApify:
    def __init__(self, items=None, boom=False):
        self.items, self.boom, self.calls = items, boom, []

    def run(self, actor, payload, *, max_items, max_cost_usd, **kw):
        self.calls.append((actor, payload, max_items, max_cost_usd))
        if self.boom:
            raise RuntimeError("no allowance left")
        return self.items


def parse(*extra):
    return cli.build_parser().parse_args(["analyze", "--url", URL, *extra])


def go(tmp_path, *extra, runner=None, apify=None, post=None, environ=None, brand=""):
    store = Store(tmp_path / "data")
    args = parse(*extra, *(["--brand", brand] if brand else []), "--data-dir", str(tmp_path / "data"))
    code = RT.run_analyze(args, AT, runner=runner or fake_runner(), store=store, environ={"GROQ_API_KEY": "g", **(environ or {})}, apify=apify, post=post or groq_post())
    return code, store


def test_the_log_names_the_route_of_the_facts_the_download_and_the_speech_and_the_speech_is_captured(tmp_path, capsys):
    code, store = go(tmp_path)
    out = capsys.readouterr().out
    assert code == 0 and "Video facts route: yt-dlp (python -m yt_dlp --dump-single-json" in out and "Download route: yt-dlp" in out
    assert "Speech route: groq_whisper" in out and "we are marking the snacks" in out
    saved = store.read_shared("analyses")["items"][0]
    assert saved["routes"] == {"facts": "yt-dlp", "download": "yt-dlp"} and saved["speech"]["captured"] and saved["speech"]["backend"] == "groq_whisper" and saved["speech"]["text"] == "we are marking the snacks"
    assert saved["facts"]["views"] == 2202 and saved["url"] == URL and saved["at"] == AT


def test_comment_text_comes_from_yt_dlp_when_it_has_it_and_from_apify_when_it_only_counted_them(tmp_path, capsys):
    withtext = dict(INFO, comments=[{"text": "love this", "like_count": 5, "author": "x"}, {"text": "so true", "like_count": 9, "author": "y"}])
    _c, store = go(tmp_path / "a", "--comments", "--no-speech", runner=fake_runner(withtext))
    out = capsys.readouterr().out
    assert "Comments route: yt-dlp (2 comment text(s))" in out and "(9 likes) so true" in out
    assert [c["text"] for c in store.read_shared("analyses")["items"][0]["comments"]] == ["so true", "love this"] and store.read_shared("analyses")["items"][0]["routes"]["comments"] == "yt-dlp"
    apify = FakeApify([{"text": "funny one", "diggCount": 3, "uniqueId": "u1"}, {"text": "best snack", "diggCount": 12, "uniqueId": "u2"}, {"text": "  "}])
    _c, store2 = go(tmp_path / "b", "--comments", "--no-speech", apify=apify)
    out2 = capsys.readouterr().out
    saved = store2.read_shared("analyses")["items"][0]
    assert "yt-dlp gave a count of 8 but no comment text" in out2 and "Comments route: Apify clockworks/tiktok-comments-scraper (at most 20 comments, call cost ceiling 0.5 dollars): 2 comment text(s)" in out2
    assert [c["text"] for c in saved["comments"]] == ["best snack", "funny one"] and saved["routes"]["comments"].startswith("apify (clockworks/tiktok-comments-scraper")
    assert apify.calls == [("clockworks/tiktok-comments-scraper", {"postURLs": [URL], "commentsPerPost": 20, "maxRepliesPerComment": 0}, 20, 0.5)]            # capped in items and in dollars


def test_comments_that_cannot_be_read_are_reported_with_the_reason_and_never_invented(tmp_path, capsys):
    go(tmp_path / "a", "--comments", "--no-speech")
    assert "no comment text captured: yt-dlp does not read this site's comment text and no Apify key (APIFY_TOKEN) is set in this run" in capsys.readouterr().out
    _c, store = go(tmp_path / "b", "--comments", "--no-speech", apify=FakeApify(boom=True))
    out = capsys.readouterr().out
    assert "no comment text captured: Apify could not read the comments: RuntimeError: no allowance left" in out and "comments" not in store.read_shared("analyses")["items"][0]
    try:
        VM.comments_via_apify("https://example.com/video", FakeApify([]))
        raise AssertionError("an address that is not TikTok must be refused")
    except Exception as exc:                                                                                                                       # noqa: BLE001
        assert "TikTok videos only" in str(exc)


def test_speech_falls_through_the_backends_and_a_failure_is_reported_not_raised(tmp_path, capsys):
    cf = {"CLOUDFLARE_ACCOUNT_ID": "acct", "CLOUDFLARE_API_TOKEN": "tok"}

    def post(url, headers, body, timeout):
        if "groq" in url:
            return 429, {}
        return 200, {"result": {"text": "second backend words", "transcription_info": {"language": "en"}, "segments": []}}
    _c, store = go(tmp_path / "a", post=post, environ=cf)
    out = capsys.readouterr().out
    speech = store.read_shared("analyses")["items"][0]["speech"]
    assert speech["backend"] == "cloudflare_whisper" and "Speech route: cloudflare_whisper" in out and "second backend words" in out and speech["tried"][0][0] == "groq_whisper"
    _c2, store2 = go(tmp_path / "b", post=groq_post(status=500), runner=fake_runner(ffmpeg_ok=False, ext="mystery"))
    out2 = capsys.readouterr().out
    assert "Speech: not captured: ffmpeg could not turn the download into audio" in out2 and store2.read_shared("analyses")["items"][0]["speech"]["captured"] is False


def test_a_failed_download_is_named_and_no_speech_is_attempted_and_no_speech_skips_it(tmp_path, capsys):
    _c, store = go(tmp_path / "a", runner=fake_runner(fail_download=True))
    out = capsys.readouterr().out
    saved = store.read_shared("analyses")["items"][0]
    assert "Download: the video could not be downloaded by yt-dlp" in out and saved["routes"]["download"].startswith("yt-dlp failed") and "speech" not in saved
    runner = fake_runner()
    _c2, store2 = go(tmp_path / "b", "--no-speech", runner=runner)
    assert "Download route" not in capsys.readouterr().out and "speech" not in store2.read_shared("analyses")["items"][0]
    assert not any("-o" in c for c in runner.calls)                                                                                                # nothing was downloaded


def test_the_analysis_is_kept_in_the_repository_newest_first_one_per_address_and_under_the_brand_when_given(tmp_path, capsys):
    store = Store(tmp_path / "data")
    for n in range(2):
        args = parse("--no-speech", "--brand", "swoveralls")
        RT.run_analyze(args, f"2026-11-19T12:0{n}:00Z", runner=fake_runner(), store=store, environ={})
    RT.run_analyze(cli.build_parser().parse_args(["analyze", "--url", "https://www.tiktok.com/@a/video/2", "--no-speech", "--brand", "swoveralls"]), AT, runner=fake_runner(), store=store, environ={})
    items = store.read_extra("swoveralls", "analyses")["items"]
    assert [i["url"] for i in items] == ["https://www.tiktok.com/@a/video/2", URL] and items[1]["at"] == "2026-11-19T12:01:00Z"                       # the second run of the same address replaced the first
    assert "Analysis saved: data/swoveralls/extras/analyses.json (2 kept)" in capsys.readouterr().out and store.read_shared("analyses") is None
    RT.run_analyze(parse("--no-speech"), AT, runner=fake_runner(), environ={})                                                                       # no store: it prints and saves nothing, as before


def test_a_download_of_a_type_groq_accepts_is_sent_as_it_is_and_one_it_does_not_is_converted_first(tmp_path, capsys):
    runner = fake_runner()
    go(tmp_path / "a", runner=runner)
    assert not any(c and c[0] == "ffmpeg" for c in runner.calls)                                                                                   # a small mp4 needs no conversion
    runner2 = fake_runner(ext="mystery")
    _c, store = go(tmp_path / "b", runner=runner2)
    assert any(c and c[0] == "ffmpeg" for c in runner2.calls) and store.read_shared("analyses")["items"][0]["speech"]["captured"] is True
