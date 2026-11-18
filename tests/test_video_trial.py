"""The video trial harness (one clip per site, go/no-go each, dummy accounts).

What these tests prove, with fakes (no browser is ever started here, no
account exists, no clip is ever generated):
- the driver registry covers exactly the four long-term free sites plus the
  Luma stub, each with its model, cookies secret, app address and free limits,
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
- the brand save: a made clip goes only to the brand the run is for, under
  the name YYYY-MM-DD_<brand>_<site>_<model>_<prompt-slug>.mp4, into that
  brand's Drive folder and that brand's sheet tab, and a failed save never
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

    def save(self, site, model, prompt, clip_rel_path, out_dir, run_date):
        self.calls.append({"site": site, "model": model, "prompt": prompt,
                           "clip": clip_rel_path, "out_dir": str(out_dir),
                           "run_date": run_date})
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


def _logged_in_browser(key, done=True, clip=True):
    """A fake browser on the site's app page, logged in, with a Generate
    button and a prompt box. `done` puts the finished-clip marker on the
    page; `clip` makes the download link fetch a clip."""
    driver = VT.DRIVERS[key]
    present = {"video[src]"} if done else set()
    pages = {driver["app_url"]: {"present": present,
                                 "links": [{"href": "https://cdn.example/clip.mp4", "text": "Download"}] if clip else []}}
    clickable = set(driver["generate_selectors"])
    for step in driver["model_steps"]:
        clickable.add(step[0])
    browser = FakeBrowser(pages, clickable=clickable,
                          typeable=set(driver["prompt_selectors"]),
                          present_always=present)
    browser.page_html = ("<html><body>welcome, " + driver["logged_in_markers"][0] +
                         ", generate a video, my creations, new project</body></html>")
    browser.fetch_map = {"https://cdn.example/clip.mp4": (200, b"FAKECLIPBYTES")}
    return browser


# --- the registry --------------------------------------------------------------------------------------

def test_registry_covers_the_long_term_sites_plus_the_luma_stub():
    assert set(VT.DRIVERS) == {"pixverse", "google_flow", "dreamina", "kling", "luma"}
    assert VT.SITE_KEYS == ("pixverse", "google_flow", "dreamina", "kling")
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
    assert browser.opened == ["https://kling.ai"]
    assert browser.typed == ["a test prompt"]


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
    browser = _logged_in_browser("dreamina")
    browser.page_html = "<html><body>please complete the captcha to continue</body></html>"
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    entry = VT.run_site("dreamina", ctx)
    assert entry["verdict"] == "blocked"
    assert "challenge" in entry["reason"] or "check" in entry["reason"]
    assert "Generate" not in " ".join(browser.clicks)


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
