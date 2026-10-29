"""What happened to each video, said truthfully.

The first live run of the video step (2026-10-29) showed three things: TikTok refused every download from GitHub's server ("Unexpected response
from webpage request"), Instagram answered "Failed to parse JSON" or "There is no video in this post" (an image post, which is not a failure), and
the same twelve refused downloads were attempted again and again. These tests keep each outcome distinct, make the sheet describe the files that
are really in Drive, and keep a refusing platform from being hammered."""
import re
from pathlib import Path

import pytest

from contentintel import ideabank as IB
from contentintel import videofetch as V
from contentintel.drive import DriveClient
from contentintel.store import Store

from fake_browser import FakeBrowser
from fake_drive import EMAIL, ROOT, FakeDrive
from media_fixtures import tiny_mp4
from test_ideabank import S, full_run, getter, kept_bank


@pytest.fixture
def drive():
    fake = FakeDrive()
    return DriveClient(fake, ROOT, EMAIL, sleep=lambda s: None), fake


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def posts(*ids, platform="tiktok"):
    bank = kept_bank(*ids)
    for n, item in enumerate(bank["items"].values(), 1):
        host = "https://www.tiktok.com/@c/video/%d" if platform == "tiktok" else "https://www.instagram.com/reel/R%d/"
        item.update({"platform": platform, "url": host % n})
    return bank


def runner_saying(*answers):
    """A video runner that answers each call in turn: (None, reason) or bytes to write as the video."""
    calls = []

    def run(url, dest, cookies, max_mb, timeout):
        calls.append(url)
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, bytes):
            (Path(dest) / "video.mp4").write_bytes(answer)
            return Path(dest) / "video.mp4", ""
        return None, answer
    run.calls = calls
    return run


TIKTOK_REFUSED = ("ERROR: [TikTok] 7652797339182140703: Unexpected response from webpage request; please report this issue on  "
                  "https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue template.")


# ---- telling the outcomes apart -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("message,url,status,note", [
    (TIKTOK_REFUSED, "https://www.tiktok.com/@a/video/1", "blocked", "TikTok refused the download from this server"),
    ("Failed to parse JSON (caused by JSONDecodeError(\"Expecting value in '': line 1 column 1 (char 0)\")); please report this issue on https://github.com/yt-dlp",
     "https://www.instagram.com/reel/x/", "blocked", "Instagram refused the download from this server"),
    ("Requested content is not available, rate-limit reached or login required", "https://www.instagram.com/p/x/", "blocked", "Instagram refused the download from this server"),
    ("HTTP Error 429: Too Many Requests", "https://www.tiktok.com/@a/video/1", "blocked", "TikTok refused the download from this server"),
    ("There is no video in this post", "https://www.instagram.com/p/x/", "none", "this post has no video"),
    ("No video formats found!; please report this issue on  https://github.com/yt-dlp/yt-dlp/issues", "https://www.instagram.com/p/x/", "none", "this post has no video"),
    ("Video unavailable. This video is private", "https://www.tiktok.com/@a/video/1", "gone", "the post is no longer available"),
    ("Video not available", "https://www.tiktok.com/@a/video/1", "gone", "the post is no longer available"),
    ("yt-dlp is not installed", "https://www.tiktok.com/@a/video/1", "failed", "yt-dlp is not installed"),
    ("yt-dlp gave up after 150 seconds", "https://www.tiktok.com/@a/video/1", "failed", "yt-dlp gave up after 150 seconds"),
    ("something odd; please report this issue on x", "https://www.tiktok.com/@a/video/1", "failed", "something odd"),
    ("", "https://www.tiktok.com/@a/video/1", "failed", "the download failed")])
def test_each_kind_of_failure_is_told_apart_in_plain_words(message, url, status, note):
    assert V.classify_failure(message, url) == (status, note)


def test_a_post_with_no_video_is_a_finding_not_a_failure_and_the_picture_is_then_the_creative(drive):
    client, fake = drive
    bank = posts("A", platform="instagram")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    assert IB.preview_label(bank["items"]["IB-0001"]).startswith("cover image only")
    runner = runner_saying("There is no video in this post")
    saved, lines = IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner)
    item = bank["items"]["IB-0001"]
    assert saved == 0 and item["video_status"] == "none" and item["assets"][0]["source"] == "media" and lines == ["IB-0001: no video saved: this post has no video"]
    assert IB.preview_label(item) == "image post saved (640x960), it has no video"
    IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner)
    assert len(runner.calls) == 1                                           # final: never asked again


def test_a_carousel_post_with_no_video_says_only_its_first_slide_is_saved(drive):
    client, fake = drive
    bank = posts("A", platform="instagram")
    bank["items"]["IB-0001"]["format"] = "carousel"
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_saying("No video formats found!"))
    assert IB.preview_label(bank["items"]["IB-0001"]) == "carousel: first slide saved, the other slides were not"


def test_a_refusal_is_recorded_costs_no_try_and_is_asked_again_next_run(drive):
    client, fake = drive
    bank = posts("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    runner = runner_saying(TIKTOK_REFUSED)
    for _ in range(3):
        IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner)
    item = bank["items"]["IB-0001"]
    assert len(runner.calls) == 3 and item["video_tries"] == 0 and item["video_status"] == "blocked" and item["video_note"] == "TikTok refused the download from this server"
    assert IB.preview_label(item) == "cover image only: the video could not be downloaded (the platform refused the download)"


def test_a_post_that_is_gone_is_final_and_any_other_failure_is_tried_twice(drive):
    client, fake = drive
    bank = posts("A", "B")
    gone, odd = runner_saying("Video unavailable"), runner_saying("something odd")
    for _ in range(4):
        IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, gone)
        IB.save_videos(bank, ["IB-0002"], "Swoveralls", client, S, odd)
    assert len(gone.calls) == 1 and len(odd.calls) == 2 and bank["items"]["IB-0002"]["video_tries"] == 2


@pytest.mark.parametrize("status,tries,expected", [("", 0, True), ("blocked", 0, True), ("blocked", 1, True), ("failed", 1, True), ("failed", 2, False),
                                                    ("none", 0, False), ("gone", 0, False)])
def test_which_outcomes_are_asked_again(status, tries, expected):
    item = posts("A")["items"]["IB-0001"]
    item.update({"video_status": status, "video_tries": tries})
    assert IB.needs_video(item, S) is expected


def test_a_platform_that_refuses_three_in_a_row_is_left_alone_for_the_rest_of_the_run(drive):
    client, fake = drive
    bank = posts("A", "B", "C", "D", "E")
    runner = runner_saying(TIKTOK_REFUSED)
    saved, lines = IB.save_videos(bank, list(bank["items"]), "Swoveralls", client, S, runner)
    assert len(runner.calls) == 3 and saved == 0
    assert lines[-1] == "2 more tiktok video(s) skipped this run: it refused 3 downloads in a row from this server (they are tried again next run)"
    assert [bank["items"][i]["video_status"] for i in ("IB-0004", "IB-0005")] == ["", ""] and all(bank["items"][i]["video_tries"] == 0 for i in bank["items"])


def test_a_success_in_between_starts_the_count_again_and_other_platforms_are_not_affected(drive):
    client, fake = drive
    bank = posts("A", "B", "C", "D", "E", "F")
    runner = runner_saying(TIKTOK_REFUSED, TIKTOK_REFUSED, tiny_mp4(), TIKTOK_REFUSED, TIKTOK_REFUSED, TIKTOK_REFUSED)
    IB.save_videos(bank, list(bank["items"]), "Swoveralls", client, S, runner)
    assert len(runner.calls) == 6                                           # two refusals, a success, then three more: not skipped before the third
    mixed = kept_bank("A", "B", "C", "D", "E")
    for n, item in enumerate(mixed["items"].values()):
        item.update({"platform": "tiktok" if n < 4 else "instagram", "url": (f"https://www.tiktok.com/@c/video/{n}" if n < 4 else "https://www.instagram.com/reel/R9/")})
    r2 = runner_saying(TIKTOK_REFUSED)
    IB.save_videos(mixed, list(mixed["items"]), "Swoveralls", client, S, r2)
    assert len(r2.calls) == 4 and "https://www.instagram.com/reel/R9/" in r2.calls                        # three TikTok refusals, then Instagram is still tried


# ---- the sheet describes what is in Drive ---------------------------------------------------------------------------------------------------------

def test_the_label_follows_the_files_in_drive_not_the_last_attempt(drive):
    client, fake = drive
    bank = posts("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_saying(tiny_mp4(seconds=9)))
    item = bank["items"]["IB-0001"]
    item.update({"video_status": "blocked", "video_note": "a later, failed attempt"})                  # something wrote a stale outcome
    assert IB.preview_label(item) == "video saved and checked (9s, 720x1280)"


def test_an_audit_sets_the_status_from_what_is_really_in_drive(drive):
    client, fake = drive
    bank = posts("A", "B")
    IB.save_assets(bank, list(bank["items"]), "Swoveralls", client, getter(), S)
    IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_saying(tiny_mp4()))
    bank["items"]["IB-0002"]["video_status"] = "saved"                                                  # claims a video that is not there
    video = next(a for a in bank["items"]["IB-0001"]["assets"] if a["kind"] == "video")
    del fake.files[video["drive_id"]]                                                                 # and a video removed from Drive by hand
    IB.audit_files(bank, client)
    assert bank["items"]["IB-0002"]["video_status"] == "" and IB.preview_label(bank["items"]["IB-0002"]).startswith("cover image only: the video has not been fetched")
    assert [a["kind"] for a in bank["items"]["IB-0001"]["assets"]] == ["image"] and IB.preview_label(bank["items"]["IB-0001"]).startswith("cover image only")


def test_an_audit_marks_a_video_found_in_drive_as_saved(drive):
    client, fake = drive
    bank = posts("A")
    IB.save_assets(bank, ["IB-0001"], "Swoveralls", client, getter(), S)
    IB.save_videos(bank, ["IB-0001"], "Swoveralls", client, S, runner_saying(tiny_mp4()))
    bank["items"]["IB-0001"]["video_status"] = ""
    IB.audit_files(bank, client)
    assert bank["items"]["IB-0001"]["video_status"] == "saved"


# ---- the browser as a second route ---------------------------------------------------------------------------------------------------------------

TIKTOK_PAGE = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">{"playAddr":"https:\\u002F\\u002Fv16.tiktok.com\\u002Fvideo\\u002Ftos\\u002Fx\\u002F?a=1\\u0026b=2"}</script>'
INSTAGRAM_PAGE = '<script>{"video_versions":[{"type":101,"url":"https:\\/\\/scontent.cdninstagram.com\\/v\\/t16\\/ABC.mp4?efg=1&amp;x=2"}]}</script>'


def factory_for(browser):
    class Ctx:
        def __enter__(self):
            return browser

        def __exit__(self, *exc):
            return False
    seen = []

    def factory(site):
        seen.append(site)
        return Ctx()
    factory.seen = seen
    return factory


@pytest.mark.parametrize("html,platform,expected", [
    (TIKTOK_PAGE, "tiktok", "https://v16.tiktok.com/video/tos/x/?a=1&b=2"),
    ('{"downloadAddr":"https:\\u002F\\u002Fv.tiktok.com\\u002Fd.mp4"}', "tiktok", "https://v.tiktok.com/d.mp4"),
    (INSTAGRAM_PAGE, "instagram", "https://scontent.cdninstagram.com/v/t16/ABC.mp4?efg=1&x=2"),
    ('{"video_url":"https:\\/\\/scontent.cdninstagram.com\\/old.mp4"}', "instagram", "https://scontent.cdninstagram.com/old.mp4"),
    ('<video class="x" src="https://cdn.example/v.mp4"></video>', "tiktok", "https://cdn.example/v.mp4"),
    ('<video src="blob:https://www.tiktok.com/abc"></video>', "tiktok", ""), ('<p>no video here</p>', "instagram", ""), ("", "tiktok", ""),
    ('{"playAddr":"http:\\u002F\\u002Finsecure.example\\u002Fv.mp4"}', "tiktok", "")])
def test_the_video_address_is_read_from_the_pages_own_data(html, platform, expected):
    assert V.find_video_url(html, platform) == expected


def test_the_cover_address_is_read_from_the_page_whichever_way_round_the_tag_is_written():
    assert V.find_cover_url('<meta property="og:image" content="https://cdn.example/c.jpg?x=1&amp;y=2">') == "https://cdn.example/c.jpg?x=1&y=2"
    assert V.find_cover_url('<meta content="https://cdn.example/d.jpg" property="og:image">') == "https://cdn.example/d.jpg" and V.find_cover_url("<p></p>") == ""


def test_the_browser_route_fetches_the_video_with_the_pages_own_cookies_and_referer(tmp_path):
    video = tiny_mp4(seconds=7)
    browser = FakeBrowser({"https://www.tiktok.com/@c/video/1": {}})
    browser.page_html = TIKTOK_PAGE
    browser.fetch_map = {"https://v16.tiktok.com/video/tos/x/?a=1&b=2": (200, video)}
    factory = factory_for(browser)
    path, why = V.browser_download("https://www.tiktok.com/@c/video/1", str(tmp_path), factory, 80)
    assert why == "" and path == tmp_path / "video.mp4" and path.read_bytes() == video and factory.seen == ["tiktok"]
    assert browser.opened == ["https://www.tiktok.com/@c/video/1"] and browser.fetched == [("https://v16.tiktok.com/video/tos/x/?a=1&b=2", "https://www.tiktok.com/@c/video/1")]


@pytest.mark.parametrize("page,fetch,url,reason", [
    ("<html></html>", (200, b"x"), "https://www.tiktok.com/@c/video/1", "the page shows no video address"),
    (TIKTOK_PAGE, (403, b""), "https://www.tiktok.com/@c/video/1", "the video server answered 403"),
    (TIKTOK_PAGE, (200, b""), "https://www.tiktok.com/@c/video/1", "the video server sent nothing"),
    (TIKTOK_PAGE, (200, b"x" * (2 * 1024 * 1024)), "https://www.tiktok.com/@c/video/1", "the video server sent nothing"),
    (TIKTOK_PAGE, (200, b"x"), "https://example.com/a", "not a TikTok or Instagram page")])
def test_the_browser_route_says_why_it_could_not_get_the_video(tmp_path, page, fetch, url, reason):
    browser = FakeBrowser({})
    browser.page_html, browser.fetch_map = page, {"https://v16.tiktok.com/video/tos/x/?a=1&b=2": fetch}
    path, why = V.browser_download(url, str(tmp_path), factory_for(browser), 1)
    assert path is None and why == reason


def test_a_page_that_will_not_load_is_a_reason_not_a_crash(tmp_path):
    def broken(site):
        raise ConnectionError("down")
    path, why = V.browser_download("https://www.tiktok.com/@c/video/1", str(tmp_path), broken, 80)
    assert path is None and why == "the browser could not load the page (ConnectionError: down)"


def test_the_browser_is_the_second_route_only_when_the_platform_refuses_yt_dlp(tmp_path):
    video = tiny_mp4()
    browser = FakeBrowser({})
    browser.page_html, browser.fetch_map = TIKTOK_PAGE, {"https://v16.tiktok.com/video/tos/x/?a=1&b=2": (200, video)}
    factory = factory_for(browser)
    url = "https://www.tiktok.com/@c/video/1"

    def ytdlp_ok(u, d, c, m, t):
        (Path(d) / "video.mp4").write_bytes(video)
        return Path(d) / "video.mp4", ""
    assert V.layered_runner(factory, ytdlp_ok)(url, str(tmp_path), None, 80, 150)[0] is not None and factory.seen == []          # yt-dlp worked: no browser
    refused = lambda u, d, c, m, t: (None, TIKTOK_REFUSED)  # noqa: E731
    path, why = V.layered_runner(factory, refused)(url, str(tmp_path), None, 80, 150)
    assert path is not None and why == "" and factory.seen == ["tiktok"]                                                          # refused: the browser got it
    factory.seen.clear()
    for message in ("There is no video in this post", "Video unavailable"):
        none = lambda u, d, c, m, t, message=message: (None, message)  # noqa: E731
        assert V.layered_runner(factory, none)(url, str(tmp_path), None, 80, 150) == (None, message) and factory.seen == []     # nothing to be had: no browser


def test_both_routes_failing_says_what_each_one_said(tmp_path):
    browser = FakeBrowser({})
    browser.page_html = "<html></html>"
    path, why = V.layered_runner(factory_for(browser), lambda u, d, c, m, t: (None, TIKTOK_REFUSED))("https://www.tiktok.com/@c/video/1", str(tmp_path), None, 80, 150)
    assert path is None and "Unexpected response from webpage request" in why and why.endswith("the browser route also failed: the page shows no video address")


def test_without_yt_dlp_the_browser_is_still_tried_and_without_a_browser_yt_dlp_alone_is(tmp_path):
    browser = FakeBrowser({})
    browser.page_html, browser.fetch_map = TIKTOK_PAGE, {"https://v16.tiktok.com/video/tos/x/?a=1&b=2": (200, tiny_mp4())}
    assert V.layered_runner(factory_for(browser), None)("https://www.tiktok.com/@c/video/1", str(tmp_path), None, 80, 150)[0] is not None
    refused = lambda u, d, c, m, t: (None, TIKTOK_REFUSED)  # noqa: E731
    assert V.layered_runner(None, refused)("https://www.tiktok.com/@c/video/1", str(tmp_path), None, 80, 150) == (None, TIKTOK_REFUSED)
    assert V.layered_runner(None, None)("https://www.tiktok.com/@c/video/1", str(tmp_path), None, 80, 150) == (None, "yt-dlp is not installed")


# ---- the command: which routes are available ---------------------------------------------------------------------------------------------------

class Args:
    browser_config = "browser.yaml"
    debug_dir = None


def test_the_browser_route_needs_a_login_to_use(monkeypatch):
    from contentintel import cli
    monkeypatch.setattr(cli, "_VIDEO_RUNNER", None)
    for name in ("INSTAGRAM_COOKIES", "TIKTOK_COOKIES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert cli._video_browser_factory(Args()) is None and cli._video_runner(Args()) is None and cli._video_browser_factory(object()) is None
    monkeypatch.setenv("TIKTOK_COOKIES", '[{"name": "a", "value": "b", "domain": ".tiktok.com"}]')
    assert callable(cli._video_browser_factory(Args())) and callable(cli._video_runner(Args()))       # a login alone is enough for the browser route
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/yt-dlp")
    assert callable(cli._video_runner())


def test_a_missing_browser_settings_file_leaves_yt_dlp_alone(monkeypatch):
    from contentintel import cli
    args = Args()
    args.browser_config = "does-not-exist.yaml"
    monkeypatch.setenv("TIKTOK_COOKIES", "[]")
    assert cli._video_browser_factory(args) is None


def test_the_run_uses_the_browser_route_end_to_end(store, swov, drive, monkeypatch):
    client, fake = drive
    video = tiny_mp4(seconds=11)
    browser = FakeBrowser({})
    browser.page_html, browser.fetch_map = TIKTOK_PAGE, {"https://v16.tiktok.com/video/tos/x/?a=1&b=2": (200, video)}
    runner = V.layered_runner(factory_for(browser), lambda u, d, c, m, t: (None, TIKTOK_REFUSED))
    store.write_ideabank("swoveralls", posts("A", "B"))
    new, lines = full_run(store, swov, (client, fake), video_runner=runner, cookies={})
    items = store.read_ideabank("swoveralls")["items"]
    assert "real videos saved for 2 item(s)" in "\n".join(lines) and all(IB.preview_label(i) == "video saved and checked (11s, 720x1280)" for i in items.values())


# ---- Instagram is on, and only as careful as you ask it to be ----------------------------------------------------------------------------------

def test_instagram_ships_with_no_limits_and_no_automatic_rest():
    import yaml
    from careful import CAREFUL_KEYS, careful_text
    from contentintel import collectors as C
    text = Path("browser.yaml").read_text(encoding="utf-8")
    ig = yaml.safe_load(text)["sites"]["instagram"]
    assert not set(CAREFUL_KEYS) & set(ig)
    assert C.load_collector_settings("research.yaml").cooldown_days == {"instagram": 0, "tiktok": 3} == C.load_collector_settings("none.yaml").cooldown_days
    on = yaml.safe_load(careful_text(text))["sites"]["instagram"]
    assert on["max_pages_per_run"] == 2 and on["max_pages_per_day"] == 4 and on["max_scrolls_per_page"] == 3 and on["pacing"]["min_delay_seconds"] == 20


def test_both_workflows_start_with_instagram_ticked():
    import yaml
    for name, key in ((".github/workflows/collect.yml", "use_instagram"), (".github/workflows/request.yml", "use_instagram")):
        wf = yaml.safe_load(Path(name).read_text(encoding="utf-8"))
        assert wf[True]["workflow_dispatch"]["inputs"][key]["default"] is True, name


# ---- an idea has to fit, whatever the industry ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fit,days,expected", [(50, 150, True), (49, 150, False), (None, 150, True), (80, 149, False), (45, 277, False), (40, 160, False)])
def test_a_long_running_ad_is_only_proven_if_it_fits_the_brand(fit, days, expected):
    item = {"metrics": {"days_running": days}, "score": 40, "fit": fit, "generic": False, "category": "same"}
    assert IB.is_proven(item) is expected


def test_the_scoring_instructions_name_no_one_industry():
    low = IB.SCORE_SYSTEM.lower()
    for word in ("shoes", "baby goods", "outdoor gear", "loungewear", "apparel", "garment", "overalls", "snack"):
        assert word not in low, word
    assert "never from a fixed list" in low and "so tasty" in low and "so comfy" in low


def test_the_category_cap_works_the_same_for_any_brand():
    for brand_text in ("a snack brand", "a software company", "a skincare line"):
        assert IB.OTHER_CAP == 45 and re.search(r"'other' \(a product category unrelated to ours", IB.SCORE_SYSTEM), brand_text


def test_posts_with_no_video_in_a_row_never_stop_the_rest_of_the_run(drive):
    client, fake = drive
    bank = posts("A", "B", "C", "D", "E", platform="instagram")
    runner = runner_saying("There is no video in this post")
    saved, lines = IB.save_videos(bank, list(bank["items"]), "Swoveralls", client, S, runner)
    assert len(runner.calls) == 5 and not any("skipped this run" in l for l in lines)
    runner = runner_saying("Video unavailable")
    bank = posts("A", "B", "C", "D", "E")
    IB.save_videos(bank, list(bank["items"]), "Swoveralls", client, S, runner)
    assert len(runner.calls) == 5                                           # a post that is gone is the post's trouble, not the platform refusing us
