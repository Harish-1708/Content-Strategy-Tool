"""The video trial harness (one clip per site, go/no-go each, dummy accounts).

What these tests prove, with fakes (no browser is ever started here, no
account exists, no clip is ever generated):
- the driver registry covers exactly the video sites (four long-term free
  sites plus the Luma stub) and the three image studios (Dreamina, Kling,
  Gemini), each with its model, cookies secret, app address and free limits,
  and Dreamina never tries Seedance 2.5 (free credits cannot buy one);
- the hard cap holds: one clip per site per run, a second generation is
  refused before the button is pressed;
- the settings load from browser.yaml's pacing/human/stealth blocks (never
  from load_browser_settings, which would reject a video site) and bad
  settings fail plainly;
- a missing or unreadable cookies secret is "not-set-up", never a failure,
  and no secret value ever reaches a written file;
- a site's trial, driven by a fake browser: GO when one clip is made and
  downloaded, NOT SET UP at a login wall, BLOCKED on a challenge, NO-GO when
  the generation never finishes, SKIPPED for the Luma stub and for Google
  Flow in peak hours, and a dry run that never opens a browser;
- the recon: a site's creation page is captured (screenshot + HTML) and every
  selector the driver would use is probed on its own — nothing is typed,
  nothing is generated, nothing is saved — and the report is written to
  recon.json; the Luma stub, a missing cookie, a login wall and a challenge
  are all reported, never a failure; the three image studios are captured the
  same way, and an image studio is trialled by naming it, the same way as a
  video site, saving to Photo / the Photos tab;
- the brand save: a made clip goes only to the brand the run is for, under
  the name YYYY-MM-DD_<brand>_<site>_<model>_<prompt-slug>.mp4, into that
  brand's Drive folder and that brand's sheet tab, and a made image goes
  only to the brand the run is for, under
  YYYY-MM-DD_<brand>_<site>_<model>_<prompt-slug>.jpg|png, into that
  brand's Photo folder and the Photos tab — and a failed save never
  loses the verdict — the trial result stands, the exact error is recorded,
  and the run still exits 0.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from contentintel import brands as brands_mod
from contentintel import drive as drive_mod
from contentintel import video_trial as VT
from fake_browser import FakeBrowser
from fake_drive import EMAIL as DRIVE_EMAIL
from fake_drive import ROOT as DRIVE_ROOT
from fake_drive import FakeDrive
from fake_sheets import FakeClient
from fake_sheets import FakeSpreadsheet
from fake_sheets import read as read_sheet

PACING = {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 3}
SETTINGS = {"pacing": PACING, "human": {"enabled": True}, "stealth": {"enabled": True}}
COOKIE_JSON = json.dumps([{"name": "session", "value": "SECRETVALUE123",
                           "domain": ".example.com", "path": "/",
                           "secure": True, "httpOnly": True,
                           "expirationDate": 2000000000}])

BROWSER_YAML = """pacing:
  min_delay_seconds: 3
  max_delay_seconds: 8
  scroll_pause_seconds: 3
human:
  enabled: true
stealth:
  enabled: true
"""


FIXED_NOW = lambda: datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)   # off-peak for Google Flow

CLIP_PIXVERSE = "pixverse/2026-11-18_duderobe_pixverse_v6_a-test-prompt.mp4"
CLIP_KLING = "kling/2026-11-18_duderobe_kling_kling-4-0_a-test-prompt.mp4"


class RecordingSaver:
    """A BrandSaver stand-in: records every save, returns a fixed Drive link,
    and never touches Google. `fail` makes save() raise; `sheet_error` makes
    it return the link with a Sheet failure, the way the real saver does."""

    def __init__(self, link="https://drive.google.com/file/d/FAKEFILE/view"):
        self.calls = []
        self.link = link
        self.fail = None
        self.sheet_error = None

    def save(self, site, model, prompt, clip_rel_path, out_dir, run_date, media="video"):
        self.calls.append({"site": site, "model": model, "prompt": prompt,
                           "clip": clip_rel_path, "out_dir": str(out_dir),
                           "run_date": run_date, "media": media})
        if self.fail:
            raise VT.TrialError(self.fail)
        return self.link, self.sheet_error


def _brand(slug="duderobe"):
    return brands_mod.load_brand(slug)


def _ctx(tmp_path, env=None, factory=None, now=None, max_wait_seconds=None, saver=None):
    return VT.TrialContext(SETTINGS, env or {}, tmp_path, "a test prompt",
                           VT.TrialLog(), VT.ClipBudget(), browser_factory=factory,
                           now=now or FIXED_NOW, max_wait_seconds=max_wait_seconds, poll_seconds=0.01,
                           brand=_brand(), saver=saver if saver is not None else RecordingSaver())


def _fake_factory(browser):
    calls = {"n": 0}

    @contextmanager
    def factory(ctx, key, cookies_text):
        calls["n"] += 1
        yield browser

    factory.calls = calls
    return factory


def _logged_in_browser(key, done=True, clip=True, page_text=None):
    """A fake browser on the site's app page, logged in, with a Generate
    button and a prompt box. `done` puts the finished-clip marker on the
    page; `clip` makes the download link fetch a clip. The finished-clip
    marker is the downloadable .mp4 link itself (a bare video element is
    NOT a done signal — a showcase video matches it on the real pages).
    `page_text` sets the page's VISIBLE text, separately from page_html —
    the challenge check reads visible text only, so a test that models a
    page whose code mentions "captcha" must set its visible text apart."""
    driver = VT.DRIVERS[key]
    present = {"a[href$=\".mp4\"]"} if done else set()
    page = {"present": present,
            "links": [{"href": "https://cdn.example/clip.mp4", "text": "Download"}] if clip else []}
    if page_text is not None:
        page["text"] = page_text
    pages = {driver["app_url"]: page}
    clickable = set(driver["generate_selectors"])
    for step in driver["model_steps"]:
        clickable.add(step[0])
    clickable.update(driver.get("enter_selectors", []))
    browser = FakeBrowser(pages, clickable=clickable,
                          typeable=set(driver["prompt_selectors"]),
                          present_always=present)
    browser.page_html = ("<html><body>welcome, " + driver["logged_in_markers"][0] +
                         ", generate a video, my creations, new project</body></html>")
    browser.fetch_map = {"https://cdn.example/clip.mp4": (200, b"FAKECLIPBYTES")}
    return browser


def _logged_in_image_browser(key, done=True, image=True, bad_bytes=False):
    """A fake browser on an image studio's page, logged in, with a Generate
    button and a prompt box. `done` puts the finished-image marker on the
    page; `image` makes the download link fetch an image. The finished-image
    marker is the downloadable .jpg link itself (a bare <img> is NOT a done
    signal — a showcase picture matches it on the real pages). `bad_bytes`
    makes the download return HTML instead of an image, the way an error
    page or a login redirect would."""
    driver = VT.DRIVERS[key]
    present = {"a[href$=\".jpg\"]"} if done else set()
    page = {"present": present,
            "links": [{"href": "https://cdn.example/image.jpg", "text": "Download"}] if image else []}
    pages = {driver["app_url"]: page}
    clickable = set(driver["generate_selectors"])
    for step in driver["model_steps"]:
        clickable.add(step[0])
    clickable.update(driver.get("enter_selectors", []))
    clickable.update(driver.get("dismiss_selectors", []))
    browser = FakeBrowser(pages, clickable=clickable,
                          typeable=set(driver["prompt_selectors"]),
                          present_always=present)
    browser.page_html = ("<html><body>welcome, " + driver["logged_in_markers"][0] +
                         ", generate an image, my works, credits</body></html>")
    body = b"<html>not an image</html>" if bad_bytes else b"\xff\xd8\xffFAKEIMAGEBYTES"
    browser.fetch_map = {"https://cdn.example/image.jpg": (200, body)}
    return browser


# --- the registry --------------------------------------------------------------------------------------

def test_registry_covers_the_long_term_sites_plus_the_luma_stub():
    assert set(VT.DRIVERS) == {"pixverse", "google_flow", "dreamina", "kling", "luma",
                               "dreamina-image", "kling-image", "gemini-image"}
    assert VT.SITE_KEYS == ("pixverse", "google_flow", "dreamina", "kling",
                            "dreamina-image", "kling-image", "gemini-image")
    assert VT.VIDEO_KEYS == ("pixverse", "google_flow", "dreamina", "kling", "luma")
    assert VT.IMAGE_KEYS == ("dreamina-image", "kling-image", "gemini-image")
    for key, driver in VT.DRIVERS.items():
        assert driver["app_url"].startswith("https://")
        assert driver["cookies_secret"].endswith("_COOKIES")
        assert driver["free"]["watermarked"] is True
        assert driver["free"]["commercial"] is False
        assert driver["max_wait_seconds"] >= 60


def test_dreamina_never_tries_seedance_2_5_and_flow_uses_lite():
    assert VT.DRIVERS["dreamina"]["model"] == "Seedance 2.0"
    assert "Seedance 2.5" in VT.DRIVERS["dreamina"]["forbidden_models"]
    assert "Seedance 2.5" not in VT.DRIVERS["dreamina"].get("models", [])
    assert VT.DRIVERS["google_flow"]["model"] == "Veo 3.1 Lite"
    assert VT.DRIVERS["google_flow"]["peak_hours_utc"] == (14, 15, 16)


def test_luma_is_a_stub_only():
    assert VT.DRIVERS["luma"].get("stub") is True
    assert VT.DRIVERS["luma"]["prompt_selectors"] == []
    assert VT.DRIVERS["luma"]["generate_selectors"] == []


# --- the hard cap --------------------------------------------------------------------------------------

def test_clip_budget_refuses_a_second_clip_for_the_same_site():
    budget = VT.ClipBudget()
    assert budget.allow("pixverse") is True
    budget.record("pixverse")
    assert budget.allow("pixverse") is False
    with pytest.raises(VT.TrialError, match="clip budget is spent"):
        budget.record("pixverse")
    assert budget.allow("kling") is True      # another site is unaffected


def test_clip_budget_names_the_unit_it_refused():
    budget = VT.ClipBudget()
    budget.record("kling-image", unit="image")
    with pytest.raises(VT.TrialError, match="1 image per site per run"):
        budget.record("kling-image", unit="image")
    budget = VT.ClipBudget()
    budget.record("pixverse")
    with pytest.raises(VT.TrialError, match="1 clip per site per run"):
        budget.record("pixverse")


def test_image_filename_and_detect_image():
    assert VT.image_filename("2026-11-18", "duderobe", "kling-image", "Image 4.0",
                             "puppy in park", "jpg") == "2026-11-18_duderobe_kling-image_image-4-0_puppy-in-park.jpg"
    assert VT.image_filename("2026-11-18", "duderobe", "kling-image", "Image 4.0",
                             "puppy in park", "png") == "2026-11-18_duderobe_kling-image_image-4-0_puppy-in-park.png"
    assert VT.detect_image(b"\xff\xd8\xffFAKE") == ("jpg", "image/jpeg")
    assert VT.detect_image(b"\x89PNG\r\n\x1a\nFAKE") == ("png", "image/png")
    assert VT.detect_image(b"<html>not an image</html>") is None
    assert VT.detect_image(b"") is None


def test_a_second_run_for_the_same_site_is_a_no_go_not_a_second_clip(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("pixverse")
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    first = VT.run_site("pixverse", ctx)
    assert first["verdict"] == "go"
    assert (tmp_path / CLIP_PIXVERSE).read_bytes() == b"FAKECLIPBYTES"
    second = VT.run_site("pixverse", ctx)
    assert second["verdict"] == "no-go"
    assert "clip budget is spent" in second["reason"]
    # The generate button is the "Create" button on PixVerse's real page.
    assert browser.clicks.count("button:has-text(\"Create\")") == 1


# --- settings and cookies ------------------------------------------------------------------------------

def test_settings_load_from_browser_yaml_blocks_and_reject_bad_ones(tmp_path, monkeypatch):
    good = tmp_path / "browser.yaml"
    good.write_text(BROWSER_YAML, encoding="utf-8")
    cfg = VT.load_trial_settings(str(good))
    assert cfg["pacing"]["min_delay_seconds"] == 3
    assert cfg["human"]["enabled"] is True
    assert cfg["stealth"]["enabled"] is True
    with pytest.raises(VT.TrialError, match="is missing"):
        VT.load_trial_settings(str(tmp_path / "nope.yaml"))
    bad = tmp_path / "bad.yaml"
    bad.write_text(BROWSER_YAML.replace("enabled: true", "engine: safari", 1), encoding="utf-8")
    with pytest.raises(VT.TrialError):
        VT.load_trial_settings(str(bad))


def test_missing_or_unreadable_cookies_are_not_set_up_never_a_failure():
    state, line = VT.check_cookies("pixverse", {})
    assert state == "not-set-up" and "PIXVERSE_COOKIES" in line
    state, line = VT.check_cookies("pixverse", {"PIXVERSE_COOKIES": "not json at all"})
    assert state == "not-set-up" and "could not be read" in line
    state, line = VT.check_cookies("pixverse", {"PIXVERSE_COOKIES": COOKIE_JSON})
    assert state == "ok" and "1 cookies loaded" in line
    assert "SECRETVALUE123" not in line


# --- one site's trial, on a fake browser -----------------------------------------------------------------

def test_happy_path_makes_one_clip_downloads_and_saves_it_for_the_brand(tmp_path):
    env = {"KLING_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("kling")
    saver = RecordingSaver()
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser), saver=saver)
    entry = VT.run_site("kling", ctx)
    assert entry["verdict"] == "go"
    assert entry["clip"] == CLIP_KLING
    assert (tmp_path / CLIP_KLING).read_bytes() == b"FAKECLIPBYTES"
    assert entry["drive_link"] == saver.link
    assert "save_error" not in entry
    assert len(saver.calls) == 1
    assert saver.calls[0]["site"] == "kling" and saver.calls[0]["model"] == "Kling 4.0"
    assert saver.calls[0]["run_date"] == "2026-11-18"
    # The homepage first, then straight into the creation studio (the
    # prompt box is not on the homepage).
    assert browser.opened == ["https://kling.ai",
                              "https://kling.ai/app?designerRouteName=video"]
    assert browser.typed == ["a test prompt"]


IMAGE_KLING = "kling-image/2026-11-18_duderobe_kling-image_image-4-0_a-test-prompt.jpg"


def test_image_trial_happy_path_makes_one_image_downloads_and_saves_it_for_the_brand(tmp_path):
    env = {"KLING_COOKIES": COOKIE_JSON}
    browser = _logged_in_image_browser("kling-image")
    saver = RecordingSaver()
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser), saver=saver)
    entry = VT.run_site("kling-image", ctx)
    assert entry["verdict"] == "go"
    assert entry["clip"] == IMAGE_KLING
    assert (tmp_path / IMAGE_KLING).read_bytes() == b"\xff\xd8\xffFAKEIMAGEBYTES"
    assert entry["drive_link"] == saver.link
    assert "save_error" not in entry
    assert len(saver.calls) == 1
    assert saver.calls[0]["site"] == "kling-image" and saver.calls[0]["model"] == "Image 4.0"
    assert saver.calls[0]["media"] == "image"
    assert saver.calls[0]["run_date"] == "2026-11-18"
    # The homepage first, then straight into the image studio (the prompt
    # box is not on the homepage); the promo modal is dismissed before typing.
    assert browser.opened == ["https://kling.ai",
                              "https://kling.ai/app?designerRouteName=omni-image&kolorsVersion=4.0"]
    assert ".kling-dialog-omni .close" in browser.clicks
    assert browser.typed == ["a test prompt"]
    assert f"(image: {IMAGE_KLING})" in VT.verdict_line(entry)


def test_image_trial_rejects_a_download_that_is_not_an_image(tmp_path):
    env = {"KLING_COOKIES": COOKIE_JSON}
    browser = _logged_in_image_browser("kling-image", bad_bytes=True)
    saver = RecordingSaver()
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser), saver=saver)
    entry = VT.run_site("kling-image", ctx)
    assert entry["verdict"] == "no-go"
    assert "no image could be downloaded" in entry["reason"]
    assert saver.calls == []
    assert not (tmp_path / IMAGE_KLING).exists()


def test_trial_keys_all_is_the_video_studios_only_and_an_image_studio_is_named(tmp_path):
    assert VT.trial_keys("all") == list(VT.VIDEO_KEYS)
    assert VT.trial_keys("kling") == ["kling"]
    assert VT.trial_keys("kling-image") == ["kling-image"]
    assert VT.trial_keys("dreamina-image") == ["dreamina-image"]


def test_login_wall_is_not_set_up_and_nothing_is_generated(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("pixverse")
    browser.page_html = "<html><body>Log in to continue <a>Sign in</a></body></html>"
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    entry = VT.run_site("pixverse", ctx)
    assert entry["verdict"] == "not-set-up"
    assert "capture a new session" in entry["reason"]
    assert "Generate" not in " ".join(browser.clicks)


def test_challenge_is_blocked_and_the_account_is_left_alone(tmp_path):
    env = {"DREAMINA_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("dreamina",
                                 page_text="please complete the captcha to continue")
    browser.page_html = "<html><body>please complete the captcha to continue</body></html>"
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    entry = VT.run_site("dreamina", ctx)
    assert entry["verdict"] == "blocked"
    assert "challenge" in entry["reason"] or "check" in entry["reason"]
    assert "Generate" not in " ".join(browser.clicks)


def test_google_flow_opens_a_new_project_before_the_model_pick(tmp_path):
    env = {"GOOGLE_FLOW_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("google_flow")
    off_peak = lambda: datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser), now=off_peak)
    entry = VT.run_site("google_flow", ctx)
    assert entry["verdict"] == "go"
    # The prompt box lives inside a project, not on the projects list.
    assert "button:has-text(\"New project\")" in browser.clicks


def test_credits_exhausted_stops_the_wait_at_once(tmp_path):
    env = {"GOOGLE_FLOW_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("google_flow", done=False)
    browser.page_html = ("<html><body>welcome, new project, generate a video — "
                         "not enough credits to generate</body></html>")
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser), max_wait_seconds=600)
    entry = VT.run_site("google_flow", ctx)
    assert entry["verdict"] == "no-go"
    assert "free credits are exhausted" in entry["reason"]


def test_generation_that_never_finishes_is_a_no_go(tmp_path):
    env = {"GOOGLE_FLOW_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("google_flow", done=False)
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser), max_wait_seconds=0.03)
    entry = VT.run_site("google_flow", ctx)
    assert entry["verdict"] == "no-go"
    assert "did not finish" in entry["reason"]


def test_luma_stub_never_opens_a_browser(tmp_path):
    factory = _fake_factory(_logged_in_browser("pixverse"))
    ctx = _ctx(tmp_path, env={"LUMA_COOKIES": COOKIE_JSON}, factory=factory)
    entry = VT.run_site("luma", ctx)
    assert entry["verdict"] == "skipped"
    assert "needs sign-in check" in entry["reason"]
    assert factory.calls["n"] == 0


def test_google_flow_skips_peak_hours_without_opening_a_browser(tmp_path):
    factory = _fake_factory(_logged_in_browser("google_flow"))
    peak = lambda: datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
    ctx = _ctx(tmp_path, env={"GOOGLE_FLOW_COOKIES": COOKIE_JSON}, factory=factory, now=peak)
    entry = VT.run_site("google_flow", ctx)
    assert entry["verdict"] == "skipped"
    assert "peak hours" in entry["reason"]
    assert factory.calls["n"] == 0
    off_peak = lambda: datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)
    ctx = _ctx(tmp_path, env={"GOOGLE_FLOW_COOKIES": COOKIE_JSON},
               factory=_fake_factory(_logged_in_browser("google_flow")), now=off_peak)
    entry = VT.run_site("google_flow", ctx)
    assert entry["verdict"] == "go"


# --- the run -------------------------------------------------------------------------------------------

def test_dry_run_never_opens_a_browser_and_writes_verdicts(tmp_path):
    def exploding_factory(ctx, key, cookies_text):
        raise AssertionError("a dry run must never open a browser")
    verdicts, code = VT.run_trial(["pixverse", "luma"], "a test prompt", SETTINGS, {},
                                  tmp_path, brand=_brand(), dry_run=True, browser_factory=exploding_factory)
    assert code == 0
    assert [v["verdict"] for v in verdicts] == ["skipped", "skipped"]
    written = json.loads((tmp_path / "video_trial.json").read_text(encoding="utf-8"))
    assert written["dry_run"] is True
    assert written["brand"] == "duderobe"
    assert [v["site"] for v in written["verdicts"]] == ["pixverse", "luma"]


def test_written_files_never_hold_a_secret_value(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("pixverse")
    verdicts, code = VT.run_trial(["pixverse"], "a test prompt", SETTINGS, env, tmp_path,
                                  brand=_brand(), browser_factory=_fake_factory(browser),
                                  saver=RecordingSaver())
    assert code == 0
    assert verdicts[0]["verdict"] == "go"
    assert verdicts[0]["drive_link"] == RecordingSaver().link
    for path in (tmp_path / "video_trial.json", tmp_path / "trial-log.jsonl"):
        assert "SECRETVALUE123" not in path.read_text(encoding="utf-8")


def test_verdict_lines_are_plain_and_complete():
    line = VT.verdict_line(VT.verdict("kling", "go", "one clip made", clip="kling/clip.mp4"))
    assert line == "video-trial: kling: GO — one clip made (clip: kling/clip.mp4)"
    with pytest.raises(VT.TrialError, match="unknown verdict"):
        VT.verdict("kling", "maybe", "nope")


# --- the CLI -------------------------------------------------------------------------------------------

def test_cli_dry_run_returns_zero_and_bad_site_is_rejected(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "browser.yaml").write_text(BROWSER_YAML, encoding="utf-8")
    brand_dir = tmp_path / "brands" / "duderobe"
    brand_dir.mkdir(parents=True)
    (brand_dir / "brand.yaml").write_text(
        "slug: duderobe\nname: DudeRobe\nplatform: shopify\ndomain: duderobe.com\ncategory_schema: apparel\n",
        encoding="utf-8")
    assert VT.main(["--site", "pixverse", "--dry-run", "--brand", "duderobe", "--out", "out"]) == 0
    assert (tmp_path / "out" / "video_trial.json").is_file()
    with pytest.raises(SystemExit) as exc:
        VT.main(["--site", "nope", "--brand", "duderobe"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:          # --brand is required: no silent default brand
        VT.main(["--site", "pixverse", "--dry-run", "--out", "out"])
    assert exc.value.code == 2
    assert VT.main(["--site", "pixverse", "--dry-run", "--brand", "duderobe", "--prompt", "   "]) == 1
    assert VT.main(["--site", "pixverse", "--dry-run", "--brand", "duderobe", "--browser-config", "missing.yaml"]) == 1


def test_cli_rejects_an_unknown_brand(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "browser.yaml").write_text(BROWSER_YAML, encoding="utf-8")
    assert VT.main(["--site", "pixverse", "--dry-run", "--brand", "nope", "--out", "out"]) == 1
    assert "unknown brand" in capsys.readouterr().out


# --- the brand save ----------------------------------------------------------------------------------------

def test_clip_filename_carries_the_brand_site_model_and_prompt():
    assert VT.clip_filename("2026-11-18", "duderobe", "pixverse", "V6", "puppy in park") == \
        "2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"
    assert VT.clip_filename("2026-11-18", "duderobe", "google_flow", "Veo 3.1 Lite", "a red bicycle") == \
        "2026-11-18_duderobe_google-flow_veo-3-1-lite_a-red-bicycle.mp4"
    assert VT.clip_filename("2026-11-18", "swoveralls", "dreamina", "Seedance 2.0", "") == \
        "2026-11-18_swoveralls_dreamina_seedance-2-0_clip.mp4"
    long_prompt = "word " * 30
    name = VT.clip_filename("2026-11-18", "duderobe", "kling", "Kling 4.0", long_prompt)
    assert name.startswith("2026-11-18_duderobe_kling_kling-4-0_")
    assert name.endswith(".mp4") and len(name) <= 120


def test_resolve_brand_accepts_the_tool_brands_and_rejects_the_rest():
    brand = VT.resolve_brand("duderobe")
    assert brand.slug == "duderobe" and brand.name == "DudeRobe"
    assert VT.resolve_brand("swoveralls").name == "Swoveralls"
    assert VT.resolve_brand("defi_snacks").name == "Defi Snacks"
    with pytest.raises(VT.TrialError, match="unknown brand"):
        VT.resolve_brand("defi-snacks")     # the tool's key is defi_snacks, with an underscore
    with pytest.raises(VT.TrialError, match="unknown brand"):
        VT.resolve_brand("nope")


def test_run_trial_refuses_to_start_without_a_brand(tmp_path):
    with pytest.raises(VT.TrialError, match="no brand was given"):
        VT.run_trial(["pixverse"], "a test prompt", SETTINGS, {}, tmp_path)


def _real_saver(tmp_path, brand_slug="duderobe", sheet_ids=None):
    """A BrandSaver wired to the fakes: a FakeDrive and a FakeClient holding
    one spreadsheet per id in `sheet_ids`. No Google call is ever made."""
    brand = brands_mod.load_brand(brand_slug)
    drive_client = drive_mod.DriveClient(FakeDrive(), DRIVE_ROOT, DRIVE_EMAIL)
    sheets_client = FakeClient([FakeSpreadsheet(sid, f"{slug} catalogue")
                                for slug, sid in (sheet_ids or {}).items()])
    config = tmp_path / "sheets.yaml"
    config.write_text("sheets:\n" + "".join(f"  {slug}: \"{sid}\"\n"
                                            for slug, sid in (sheet_ids or {}).items()),
                      encoding="utf-8")
    saver = VT.BrandSaver(brand, {}, data_dir=str(tmp_path / "data"), sheets_config=str(config),
                          drive_client=drive_client, sheets_client=sheets_client)
    return saver, drive_client, sheets_client


SHEET_IDS = {"duderobe": "sheet-duderobe", "swoveralls": "sheet-swoveralls",
             "defi_snacks": "sheet-defi-snacks"}


def test_duderobe_clip_lands_only_in_duderobes_folder_and_sheet(tmp_path):
    saver, drive_client, sheets_client = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    rel = "pixverse/2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"
    (tmp_path / "pixverse").mkdir()
    (tmp_path / rel).write_bytes(b"FAKECLIPBYTES")
    link, sheet_error = saver.save("pixverse", "V6", "puppy in park", rel, tmp_path, "2026-11-18")
    assert sheet_error is None
    assert link.startswith("https://drive.google.com/file/d/") and link.endswith("/view")
    # Drive: one file, in Idea Bank / Generated Content / VIDEO / DudeRobe, with the right name and bytes
    assert drive_client.find_folder(DRIVE_ROOT, "DudeRobe") is None  # no brand folder at the root any more
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    assert generated_id is not None
    assert drive_client.find_folder(generated_id, "Photo") is None  # the video save never creates the Photo side
    video_id = drive_client.find_folder(generated_id, "VIDEO")
    assert video_id is not None
    brand_id = drive_client.find_folder(video_id, "DudeRobe")
    assert brand_id is not None
    assert drive_client.find_folder(video_id, "duderobe") is None
    files = drive_client.list_files(brand_id)
    assert list(files) == ["2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"]
    assert drive_client.download(files["2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"]) == b"FAKECLIPBYTES"
    # the other brands' folders were never created
    assert drive_client.find_folder(video_id, "Swoveralls") is None
    assert drive_client.find_folder(video_id, "Defi Snacks") is None
    # Sheet: the duderobe sheet has the tab with the header and one row; the other sheets have no such tab
    ws = sheets_client.spreadsheets["sheet-duderobe"].worksheet("Video Trials")
    header, rows = read_sheet(ws)
    assert header == VT.VIDEO_TRIALS_HEADERS
    assert len(rows) == 1
    assert rows[0] == {"Date": "2026-11-18", "Brand": "duderobe", "Site": "pixverse", "Model": "V6",
                       "Prompt": "puppy in park", "Verdict": "go", "Clip": link,
                       "Credits used": "", "Notes": ""}
    for sid in ("sheet-swoveralls", "sheet-defi-snacks"):
        assert "Video Trials" not in [w.title for w in sheets_client.spreadsheets[sid].worksheets()]


def test_duderobe_image_lands_only_in_duderobes_photo_folder_and_sheet(tmp_path):
    saver, drive_client, sheets_client = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    rel = "kling-image/2026-11-18_duderobe_kling-image_image-4-0_puppy-in-park.jpg"
    (tmp_path / "kling-image").mkdir()
    (tmp_path / rel).write_bytes(b"\xff\xd8\xffFAKEIMAGEBYTES")
    link, sheet_error = saver.save("kling-image", "Image 4.0", "puppy in park", rel, tmp_path,
                                   "2026-11-18", media="image")
    assert sheet_error is None
    assert link.startswith("https://drive.google.com/file/d/") and link.endswith("/view")
    # Drive: one file, in Idea Bank / Generated Content / Photo / DudeRobe, with the right name and bytes
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    assert generated_id is not None
    assert drive_client.find_folder(generated_id, "VIDEO") is None  # the image save never creates the VIDEO side
    photo_id = drive_client.find_folder(generated_id, "Photo")
    assert photo_id is not None
    brand_id = drive_client.find_folder(photo_id, "DudeRobe")
    assert brand_id is not None
    files = drive_client.list_files(brand_id)
    assert list(files) == ["2026-11-18_duderobe_kling-image_image-4-0_puppy-in-park.jpg"]
    assert drive_client.download(files["2026-11-18_duderobe_kling-image_image-4-0_puppy-in-park.jpg"]) == b"\xff\xd8\xffFAKEIMAGEBYTES"
    # Sheet: the duderobe sheet has the Photos tab with its header and one row; the other sheets have no such tab
    ws = sheets_client.spreadsheets["sheet-duderobe"].worksheet("Photos")
    header, rows = read_sheet(ws)
    assert header == VT.PHOTOS_HEADERS
    assert len(rows) == 1
    assert rows[0] == {"Date": "2026-11-18", "Brand": "duderobe", "Source": "kling-image", "Model": "Image 4.0",
                       "Prompt": "puppy in park", "Verdict": "go", "Photo": link,
                       "Credits used": "", "Notes": ""}
    for sid in ("sheet-swoveralls", "sheet-defi-snacks"):
        assert "Photos" not in [w.title for w in sheets_client.spreadsheets[sid].worksheets()]


def test_an_image_save_refuses_bytes_that_are_not_an_image(tmp_path):
    saver, _, _ = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    rel = "kling-image/2026-11-18_duderobe_kling-image_image-4-0_puppy-in-park.jpg"
    (tmp_path / "kling-image").mkdir()
    (tmp_path / rel).write_bytes(b"<html>not an image</html>")
    with pytest.raises(VT.TrialError, match="is not a JPEG or a PNG"):
        saver.save("kling-image", "Image 4.0", "puppy in park", rel, tmp_path,
                   "2026-11-18", media="image")


def test_a_second_save_with_the_same_name_replaces_the_first_clip(tmp_path):
    saver, drive_client, sheets_client = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    rel = "pixverse/2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"
    (tmp_path / "pixverse").mkdir()
    (tmp_path / rel).write_bytes(b"FIRSTCLIP")
    first_link, first_error = saver.save("pixverse", "V6", "puppy in park", rel, tmp_path, "2026-11-18")
    assert first_error is None
    (tmp_path / rel).write_bytes(b"SECONDCLIP")
    second_link, second_error = saver.save("pixverse", "V6", "puppy in park", rel, tmp_path, "2026-11-18")
    assert second_error is None
    assert second_link != first_link
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    video_id = drive_client.find_folder(generated_id, "VIDEO")
    brand_id = drive_client.find_folder(video_id, "DudeRobe")
    files = drive_client.list_files(brand_id)
    assert list(files) == ["2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"]
    assert drive_client.download(files["2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"]) == b"SECONDCLIP"
    ws = sheets_client.spreadsheets["sheet-duderobe"].worksheet("Video Trials")
    _, rows = read_sheet(ws)
    assert len(rows) == 2


def test_a_hand_changed_header_is_left_alone_and_the_link_is_kept(tmp_path):
    saver, _, sheets_client = _real_saver(tmp_path, sheet_ids=SHEET_IDS)
    ws = sheets_client.spreadsheets["sheet-duderobe"].add_worksheet(title="Video Trials", rows=1000, cols=30)
    ws.update(values=[["Date", "Wrong", "Columns"]], range_name="A1")
    rel = "pixverse/2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"
    (tmp_path / "pixverse").mkdir()
    (tmp_path / rel).write_bytes(b"FAKECLIPBYTES")
    link, sheet_error = saver.save("pixverse", "V6", "puppy in park", rel, tmp_path, "2026-11-18")
    assert link is not None
    assert sheet_error is not None and "different header" in sheet_error
    header, rows = read_sheet(ws)
    assert header == ["Date", "Wrong", "Columns"] and rows == []


def test_a_clip_in_drive_with_no_sheet_mapping_keeps_its_link(tmp_path):
    saver, drive_client, _ = _real_saver(tmp_path, sheet_ids={})
    rel = "kling/2026-11-18_duderobe_kling_kling-4-0_a-test-prompt.mp4"
    (tmp_path / "kling").mkdir()
    (tmp_path / rel).write_bytes(b"FAKECLIPBYTES")
    link, sheet_error = saver.save("kling", "Kling 4.0", "a test prompt", rel, tmp_path, "2026-11-18")
    assert link is not None and link.startswith("https://drive.google.com/file/d/")
    assert sheet_error is not None and "No spreadsheet is set up" in sheet_error
    generated_id = drive_client.find_folder(DRIVE_ROOT, "Generated Content")
    video_id = drive_client.find_folder(generated_id, "VIDEO")
    brand_id = drive_client.find_folder(video_id, "DudeRobe")
    assert list(drive_client.list_files(brand_id)) == ["2026-11-18_duderobe_kling_kling-4-0_a-test-prompt.mp4"]


def test_a_failed_save_keeps_the_verdict_and_exits_zero(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON}
    saver = RecordingSaver()
    saver.fail = "GOOGLE_SERVICE_ACCOUNT_JSON is not set"
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(_logged_in_browser("pixverse")), saver=saver)
    entry = VT.run_site("pixverse", ctx)
    assert entry["verdict"] == "go"
    assert entry["clip"] == CLIP_PIXVERSE
    assert "drive_link" not in entry
    assert entry["save_error"] == "GOOGLE_SERVICE_ACCOUNT_JSON is not set"
    verdicts, code = VT.run_trial(["pixverse"], "a test prompt", SETTINGS, env, tmp_path,
                                  brand=_brand(), browser_factory=_fake_factory(_logged_in_browser("pixverse")),
                                  saver=saver)
    assert code == 0
    assert verdicts[0]["verdict"] == "go"
    assert verdicts[0]["save_error"] == "GOOGLE_SERVICE_ACCOUNT_JSON is not set"


def test_dry_run_shows_the_save_plan_and_touches_nothing(tmp_path, capsys):
    class ExplodingSaver:
        def save(self, *args, **kwargs):
            raise AssertionError("a dry run must never save")

    verdicts, code = VT.run_trial(["pixverse", "luma"], "a test prompt", SETTINGS, {},
                                  tmp_path, brand=_brand(), dry_run=True, now=FIXED_NOW,
                                  browser_factory=_fake_factory(_logged_in_browser("pixverse")),
                                  saver=ExplodingSaver())
    assert code == 0
    assert [v["verdict"] for v in verdicts] == ["skipped", "skipped"]
    out = capsys.readouterr().out
    assert "brand: duderobe (DudeRobe)" in out
    assert "Generated Content / VIDEO / DudeRobe / 2026-11-18_duderobe_pixverse_v6_a-test-prompt.mp4" in out
    assert 'tab "Video Trials"' in out
    assert "save: none" in out        # the Luma stub


def test_the_luma_stub_and_a_missing_cookie_save_nothing(tmp_path):
    saver = RecordingSaver()
    ctx = _ctx(tmp_path, env={"LUMA_COOKIES": COOKIE_JSON},
               factory=_fake_factory(_logged_in_browser("pixverse")), saver=saver)
    entry = VT.run_site("luma", ctx)
    assert entry["verdict"] == "skipped"
    assert saver.calls == []
    ctx = _ctx(tmp_path, env={}, factory=_fake_factory(_logged_in_browser("pixverse")), saver=saver)
    entry = VT.run_site("pixverse", ctx)
    assert entry["verdict"] == "not-set-up"
    assert saver.calls == []


def test_verdict_lines_show_the_drive_link_and_a_failed_save():
    entry = VT.verdict("kling", "go", "one clip made", clip=CLIP_KLING)
    entry["drive_link"] = "https://drive.google.com/file/d/FILE/view"
    assert VT.verdict_line(entry).endswith("(saved: https://drive.google.com/file/d/FILE/view)")
    entry = VT.verdict("kling", "go", "one clip made", clip=CLIP_KLING)
    entry["save_error"] = "GOOGLE_SERVICE_ACCOUNT_JSON is not set"
    assert "(Drive save failed: GOOGLE_SERVICE_ACCOUNT_JSON is not set)" in VT.verdict_line(entry)


def test_a_real_save_without_the_service_account_key_fails_plainly(tmp_path):
    brand = brands_mod.load_brand("duderobe")
    saver = VT.BrandSaver(brand, {}, data_dir=str(tmp_path / "data"),
                          sheets_config=str(tmp_path / "sheets.yaml"))
    rel = "pixverse/2026-11-18_duderobe_pixverse_v6_puppy-in-park.mp4"
    (tmp_path / "pixverse").mkdir()
    (tmp_path / rel).write_bytes(b"FAKECLIPBYTES")
    with pytest.raises(VT.TrialError, match="GOOGLE_SERVICE_ACCOUNT_JSON is not set"):
        saver.save("pixverse", "V6", "puppy in park", rel, tmp_path, "2026-11-18")


# --- the recon (capture the page, probe the selectors, generate nothing) ----------------------------------

def _recon_browser(key, present=None, page_html=None, page_text=None):
    """A fake browser on the site's app page, logged in, with the given
    selectors present on the page. `present` defaults to every selector the
    driver would use (prompt + generate + done), so the probe finds them all.
    `page_text` sets the page's VISIBLE text, separately from page_html."""
    driver = VT.DRIVERS[key]
    if present is None:
        present = (set(driver["prompt_selectors"]) | set(driver["generate_selectors"])
                   | set(driver["done_markers"]))
    page = {"present": set(present)}
    if page_text is not None:
        page["text"] = page_text
    pages = {driver["app_url"]: page}
    browser = FakeBrowser(pages, clickable=set(driver["generate_selectors"]),
                          typeable=set(driver["prompt_selectors"]),
                          present_always=set(present))
    browser.page_html = page_html or ("<html><body>welcome, " + driver["logged_in_markers"][0] +
                                      ", generate a video, my creations</body></html>")
    return browser


def test_recon_captures_the_creation_page_and_probes_every_selector(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON}
    # Only the first prompt selector, the first generate selector and the
    # done marker are present — the probe must report each one on its own.
    present = {VT.DRIVERS["pixverse"]["prompt_selectors"][0],
               VT.DRIVERS["pixverse"]["generate_selectors"][0],
               VT.DRIVERS["pixverse"]["done_markers"][0]}
    browser = _recon_browser("pixverse", present=present)
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    report = VT.recon_site("pixverse", ctx)
    assert report["state"] == "ok"
    assert report["reason"] == "creation page captured; selectors probed"
    assert report["login"] == "in"
    assert report["credits_exhausted"] is False
    assert report["prompt_selectors"][VT.DRIVERS["pixverse"]["prompt_selectors"][0]] is True
    assert report["prompt_selectors"][VT.DRIVERS["pixverse"]["prompt_selectors"][1]] is False
    assert report["generate_selectors"][VT.DRIVERS["pixverse"]["generate_selectors"][0]] is True
    assert report["done_markers"][VT.DRIVERS["pixverse"]["done_markers"][0]] is True
    assert "recon" in browser.shots          # the capture was taken
    assert browser.typed == []               # nothing was ever typed
    assert browser.clicks == [] or all(c in VT.DRIVERS["pixverse"]["dismiss_selectors"]
                                       for c in browser.clicks)


def test_recon_on_the_luma_stub_is_skipped_and_opens_nothing(tmp_path):
    env = {"LUMA_COOKIES": COOKIE_JSON}
    factory = _fake_factory(_recon_browser("pixverse"))
    ctx = _ctx(tmp_path, env=env, factory=factory)
    report = VT.recon_site("luma", ctx)
    assert report["state"] == "skipped"
    assert "needs sign-in check" in report["reason"]
    assert factory.calls["n"] == 0


def test_recon_with_missing_cookies_is_not_set_up(tmp_path):
    factory = _fake_factory(_recon_browser("pixverse"))
    ctx = _ctx(tmp_path, env={}, factory=factory)
    report = VT.recon_site("pixverse", ctx)
    assert report["state"] == "not-set-up"
    assert factory.calls["n"] == 0


def test_recon_at_a_login_wall_is_not_set_up(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON}
    browser = _recon_browser("pixverse", page_html="<html><body>log in to continue</body></html>")
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    report = VT.recon_site("pixverse", ctx)
    assert report["state"] == "not-set-up"
    assert report["login"] == "wall"
    assert "recon-login-wall" in browser.shots


def test_recon_blocked_on_a_challenge(tmp_path):
    env = {"DREAMINA_COOKIES": COOKIE_JSON}
    browser = _recon_browser("dreamina", page_html="<html><body>please complete the captcha</body></html>",
                             page_text="please complete the captcha")
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    report = VT.recon_site("dreamina", ctx)
    assert report["state"] == "blocked"
    assert "captcha" in report["reason"]


def test_captcha_word_in_page_code_only_is_not_a_challenge():
    # Generation #4 (2026-11-19): Dreamina loads a captcha SDK on every
    # page, so the word "captcha" sits in a script address — that is not a
    # challenge, and the recon must not call it one.
    driver = VT.DRIVERS["dreamina"]
    browser = _recon_browser(
        "dreamina",
        page_html="<html><head><script src=\"https://sf16-web-login-neutral.capcutstatic.com/obj/capcut-web-login-static/bric-captcha-capcut/core-captcha/capcut-row/2.35.11/1.0.0.27/captcha.js\"></script></head><body>welcome, dreamina, seedream 5.5, newly launched, my creations</body></html>",
        page_text="welcome, dreamina, seedream 5.5, newly launched, my creations",
    )
    browser.open(driver["app_url"])
    assert VT.challenge_or_warning(browser, driver) is None


def test_recaptcha_disclaimer_is_not_a_challenge_and_the_flow_recon_reports_not_set_up(tmp_path):
    # Generation #4 (2026-11-19): Google Flow's logged-out page carries the
    # reCAPTCHA disclaimer ("This site is protected by reCAPTCHA") in its
    # footer — that is not a challenge. The page is a login wall, so the
    # recon reports not-set-up, never blocked.
    env = {"GOOGLE_FLOW_COOKIES": COOKIE_JSON}
    browser = _recon_browser(
        "google_flow",
        page_html="<html><body>Google Flow — Your AI creative studio built with Google's advanced generative models. Create with Google Flow. This site is protected by reCAPTCHA.</body></html>",
        page_text="google flow, your ai creative studio built with google's advanced generative models, create with google flow, this site is protected by recaptcha",
    )
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    report = VT.recon_site("google_flow", ctx)
    assert report["state"] == "not-set-up"
    assert "captcha" not in report["reason"]
    assert "recon-login-wall" in browser.shots


def test_visible_robot_check_and_slider_are_challenges():
    # The phrases a real challenge actually shows on the page.
    driver = VT.DRIVERS["dreamina"]
    for text in ("please confirm — i'm not a robot",
                 "slide to verify you are human"):
        browser = _recon_browser("dreamina", page_text=text)
        browser.open(driver["app_url"])
        found = VT.challenge_or_warning(browser, driver)
        assert found is not None and found[0] == "challenge"


def test_kling_image_generate_button_is_the_button_not_the_sidebar_link():
    # Generation #4: the image studio's Generate control is a real <button
    # class="generic-button ... button-pay">. Plain text=Generate matches
    # the sidebar "Generate" nav link first and would navigate away, so it
    # is not a generate selector here.
    driver = VT.DRIVERS["kling-image"]
    assert driver["generate_selectors"] == ["button.generic-button.button-pay",
                                            "button:has-text(\"Generate\")"]
    assert "text=Generate" not in driver["generate_selectors"]
    assert driver["dismiss_selectors"] == [".kling-dialog-omni .close",
                                           "[data-testid=\"nav-image-40-bubble-close\"]"]


def test_kling_image_recon_dismisses_the_image_40_promo_modal(tmp_path):
    # Generation #4: a "Kling Image 4.0" promo modal covers the image
    # studio's editor on first open; the recon must close it before probing.
    env = {"KLING_COOKIES": COOKIE_JSON}
    browser = _recon_browser("kling-image")
    browser.clickable.update(VT.DRIVERS["kling-image"]["dismiss_selectors"])
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    report = VT.recon_site("kling-image", ctx)
    assert report["state"] == "ok"
    assert ".kling-dialog-omni .close" in browser.clicks


def test_run_recon_writes_recon_json(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON, "KLING_COOKIES": COOKIE_JSON}
    factory = _fake_factory(_recon_browser("pixverse"))
    reports, code = VT.run_recon(["pixverse", "luma"], SETTINGS, env, tmp_path,
                                 brand=_brand(), browser_factory=factory)
    assert code == 0
    assert [r["site"] for r in reports] == ["pixverse", "luma"]
    assert reports[0]["state"] == "ok"
    assert reports[1]["state"] == "skipped"
    saved = json.loads((tmp_path / "recon.json").read_text(encoding="utf-8"))
    assert saved["mode"] == "recon"
    assert saved["brand"] == "duderobe"
    assert [r["site"] for r in saved["reports"]] == ["pixverse", "luma"]


def test_recon_dry_run_opens_nothing_and_reports_skipped(tmp_path, capsys):
    factory = _fake_factory(_recon_browser("pixverse"))
    reports, code = VT.run_recon(["pixverse", "luma"], SETTINGS, {}, tmp_path,
                                 brand=_brand(), browser_factory=factory, dry_run=True)
    assert code == 0
    assert [r["state"] for r in reports] == ["skipped", "skipped"]
    assert factory.calls["n"] == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "PIXVERSE_COOKIES" in out


# --- the image studios (recon only: capture the page, generate nothing) ----

def test_image_recon_captures_the_image_studio_and_probes_its_selectors(tmp_path):
    env = {"DREAMINA_COOKIES": COOKIE_JSON}
    present = {VT.DRIVERS["dreamina-image"]["prompt_selectors"][0],
               VT.DRIVERS["dreamina-image"]["generate_selectors"][0],
               VT.DRIVERS["dreamina-image"]["done_markers"][0]}
    browser = _recon_browser("dreamina-image", present=present)
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    report = VT.recon_site("dreamina-image", ctx)
    assert report["state"] == "ok"
    assert report["reason"] == "creation page captured; selectors probed"
    assert report["login"] == "in"
    assert report["pages_opened"] == [VT.DRIVERS["dreamina-image"]["app_url"]]
    assert report["prompt_selectors"][VT.DRIVERS["dreamina-image"]["prompt_selectors"][0]] is True
    assert report["generate_selectors"][VT.DRIVERS["dreamina-image"]["generate_selectors"][0]] is True
    assert report["done_markers"][VT.DRIVERS["dreamina-image"]["done_markers"][0]] is True
    assert "recon" in browser.shots
    assert browser.typed == []


def test_image_recon_kling_image_enters_the_omni_image_studio(tmp_path):
    env = {"KLING_COOKIES": COOKIE_JSON}
    browser = _recon_browser("kling-image")
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    report = VT.recon_site("kling-image", ctx)
    assert report["state"] == "ok"
    assert VT.DRIVERS["kling-image"]["enter_url"] in report["pages_opened"]
    assert VT.DRIVERS["kling-image"]["enter_url"] in browser.opened
    assert report["prompt_selectors"][VT.DRIVERS["kling-image"]["prompt_selectors"][0]] is True


def test_image_recon_gemini_with_missing_cookies_is_not_set_up(tmp_path):
    factory = _fake_factory(_recon_browser("gemini-image"))
    ctx = _ctx(tmp_path, env={}, factory=factory)
    report = VT.recon_site("gemini-image", ctx)
    assert report["state"] == "not-set-up"
    assert factory.calls["n"] == 0


def test_an_image_studio_is_trialled_by_naming_it_not_refused(tmp_path, capsys):
    code = VT.main(["--site", "kling-image", "--brand", "duderobe", "--out", "out", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Generated Content / Photo / DudeRobe /" in out
    assert "kling-image_image-4-0_a-red-bicycle-leaning-against-a-wooden.jpg" in out
    assert 'tab "Photos"' in out
