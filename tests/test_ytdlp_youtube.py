"""Agent-Reach's YouTube channel through yt-dlp. MOCK: a scripted yt-dlp; nothing reaches YouTube."""
import json
import subprocess

import pytest

from contentintel import contract as K
from contentintel import ytdlp_youtube as YT
from contentintel.observatory import corpus as CP


class Run:
    def __init__(self, stdout="", stderr="", code=0, files=None, raises=None):
        self.stdout, self.stderr, self.code, self.files, self.raises, self.calls = stdout, stderr, code, files or {}, raises, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raises:
            raise self.raises
        for name, text in self.files.items():
            open(f"{kw['cwd']}/{name}", "w", encoding="utf-8").write(text)
        return subprocess.CompletedProcess(argv, self.code, self.stdout, self.stderr)


def client(run):
    return YT.YtDlpYouTube(run=run, python="py", timeout=33, installed=True)


def entry(n, **kw):
    return {"id": f"vid{n}", "title": f"Video {n}", "url": f"https://www.youtube.com/watch?v=vid{n}", "channel": f"Chan {n}", "channel_id": "UC" + str(n) * 22, "channel_url": f"https://www.youtube.com/channel/UC{str(n) * 22}",
            "uploader_id": f"@chan{n}", "view_count": 1000 * n, "duration": 60, "description": None, **kw}


def test_search_runs_the_same_yt_dlp_command_as_agent_reach_and_returns_one_row_shape():
    run = Run(stdout="\n".join(json.dumps(entry(n)) for n in (1, 2)) + "\n\n")
    rows = client(run).search("snack brand growth", 7)
    assert run.calls[0][0] == ["py", "-m", "yt_dlp", "--no-warnings", "--flat-playlist", "--dump-json", "ytsearch7:snack brand growth"] and run.calls[0][1]["timeout"] == 33
    assert rows[0] == {"title": "Video 1", "url": "https://www.youtube.com/watch?v=vid1", "channel": "Chan 1", "channel_id": "UC" + "1" * 22, "channel_url": "https://www.youtube.com/channel/UC" + "1" * 22,
                       "uploader_id": "@chan1", "description": "", "published": "", "views": 1000, "duration": 60} and len(rows) == 2


def test_the_opencli_style_call_interface_maps_to_search_read_and_subtitle():
    run = Run(stdout=json.dumps(entry(1, webpage_url="https://www.youtube.com/watch?v=vid1", upload_date="20260101", description="desc")))
    c = client(run)
    assert c.call(["search", "q", "--limit", 3]) and run.calls[0][0][-1] == "ytsearch3:q"
    assert c.call(["search", "q"]) and run.calls[1][0][-1] == "ytsearch5:q"                          # the default number of results
    read = c.call(["read", "vid1"])
    assert read[0]["published"] == "20260101" and run.calls[2][0][-3:] == ["--dump-json", "--skip-download", "https://www.youtube.com/watch?v=vid1"]
    with pytest.raises(K.Failure) as seen:
        c.call(["transcribe", "vid1"])
    assert seen.value.code == K.BAD_INPUT and "search, read and subtitle" in str(seen.value)
    with pytest.raises(K.Failure):
        c.call(["search"])


def test_subtitles_are_downloaded_to_a_temporary_folder_and_returned_as_plain_text():
    vtt = "WEBVTT\nKind: captions\n\n00:00:00.000 --> 00:00:02.000\nHello <c>there</c>\n\n00:00:02.000 --> 00:00:04.000\nHello there\n\n00:00:04.000 --> 00:00:06.000\nsecond line\n"
    run = Run(stdout="A talk\n", files={"vid1.en.vtt": vtt})
    rows = client(run).call(["subtitle", "https://www.youtube.com/watch?v=vid1"])
    assert rows[0]["text"] == "Hello there second line" and rows[0]["title"] == "A talk" and "--write-auto-subs" in run.calls[0][0] and run.calls[0][1]["cwd"] != "."
    with pytest.raises(K.Failure) as seen:
        client(Run(stdout="A talk\n")).subtitle("vid1")
    assert seen.value.code == K.NOT_FOUND and "no subtitles" in str(seen.value)
    assert YT.vtt_to_text("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nOnly line\n") == "Only line"


@pytest.mark.parametrize("stderr, code", [("ERROR: Sign in to confirm you're not a bot", K.PLATFORM_BLOCKED), ("ERROR: HTTP Error 429: Too Many Requests", K.RATE_LIMITED), ("ERROR: Video unavailable", K.NOT_FOUND),
                                          ("ERROR: Private video. Sign in if you've been granted access", K.NOT_FOUND), ("ERROR: something odd", K.UPSTREAM_BROKEN)])
def test_a_yt_dlp_failure_gets_its_real_code_and_a_bot_check_is_a_refusal_never_worked_around(stderr, code):
    with pytest.raises(K.Failure) as seen:
        client(Run(stderr=stderr, code=1)).search("q")
    assert seen.value.code == code
    if code == K.PLATFORM_BLOCKED and "bot" in stderr:
        assert "was not worked around" in str(seen.value)
    if code == K.RATE_LIMITED:
        assert seen.value.retry_after == 3600


def test_a_missing_yt_dlp_a_timeout_and_a_start_failure_are_classified():
    with pytest.raises(K.Failure) as seen:
        YT.YtDlpYouTube(run=Run(), installed=False).search("q")
    assert seen.value.code == K.NEED_SETUP and "pip install yt-dlp" in str(seen.value)
    with pytest.raises(K.Failure) as seen:
        client(Run(raises=subprocess.TimeoutExpired("x", 1))).search("q")
    assert seen.value.code == K.TIMEOUT
    with pytest.raises(K.Failure) as seen:
        client(Run(raises=FileNotFoundError())).search("q")
    assert seen.value.code == K.NEED_SETUP
    assert YT.YtDlpYouTube.needs_browser is False and YT.YtDlpYouTube(installed=True).available() is True


def test_the_youtube_client_never_refuses_a_hosted_runner_because_it_needs_no_login():
    c = YT.YtDlpYouTube(run=Run(stdout=json.dumps(entry(1))), environ={"RUNNER_ENVIRONMENT": "github-hosted"}, installed=True)
    assert c.needs_browser is False and c.search("q", 1)[0]["title"] == "Video 1"


def test_channels_are_found_from_search_rows_by_id_or_handle_once_each():
    rows = [("q1", {"channel_id": "UC" + "a" * 22, "channel": "A Chan", "uploader_id": "@achan"}), ("q1", {"channel_id": "UC" + "a" * 22, "channel": "A Chan again"}),
            ("q2", {"channel_url": "https://www.youtube.com/@bchan", "channel": "B Chan"}), ("q2", {"channel_url": "https://www.youtube.com/channel/UC" + "c" * 22, "channel": "C Chan"}),
            ("q3", {"channel": "No reference"}), ("q3", {"channel_id": "notanid", "uploader_id": "@dchan", "channel": "D\u200b Chan"})]
    assert CP.channels_from_rows(rows) == [("UC" + "a" * 22, "A Chan", "q1"), ("@bchan", "B Chan", "q2"), ("UC" + "c" * 22, "C Chan", "q2"), ("@dchan", "D Chan", "q3")]
