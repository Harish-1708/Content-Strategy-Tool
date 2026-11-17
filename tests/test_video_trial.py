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
  Flow in peak hours, and a dry run that never opens a browser.
"""
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from contentintel import video_trial as VT
from fake_browser import FakeBrowser

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


def _ctx(tmp_path, env=None, factory=None, now=None, max_wait_seconds=None):
    return VT.TrialContext(SETTINGS, env or {}, tmp_path, "a test prompt",
                           VT.TrialLog(), VT.ClipBudget(), browser_factory=factory,
                           now=now, max_wait_seconds=max_wait_seconds, poll_seconds=0.01)


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
    assert (tmp_path / "pixverse" / "clip.mp4").read_bytes() == b"FAKECLIPBYTES"
    second = VT.run_site("pixverse", ctx)
    assert second["verdict"] == "no-go"
    assert "clip budget is spent" in second["reason"]
    assert browser.clicks.count("button:has-text(\"Generate\")") == 1


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

def test_happy_path_makes_one_clip_and_downloads_it(tmp_path):
    env = {"KLING_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("kling")
    ctx = _ctx(tmp_path, env=env, factory=_fake_factory(browser))
    entry = VT.run_site("kling", ctx)
    assert entry["verdict"] == "go"
    assert entry["clip"] == "kling/clip.mp4"
    assert entry["model"] == "Kling 4.0"
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
                                  tmp_path, dry_run=True, browser_factory=exploding_factory)
    assert code == 0
    assert [v["verdict"] for v in verdicts] == ["skipped", "skipped"]
    written = json.loads((tmp_path / "video_trial.json").read_text(encoding="utf-8"))
    assert written["dry_run"] is True
    assert [v["site"] for v in written["verdicts"]] == ["pixverse", "luma"]


def test_written_files_never_hold_a_secret_value(tmp_path):
    env = {"PIXVERSE_COOKIES": COOKIE_JSON}
    browser = _logged_in_browser("pixverse")
    verdicts, code = VT.run_trial(["pixverse"], "a test prompt", SETTINGS, env, tmp_path,
                                  browser_factory=_fake_factory(browser))
    assert code == 0
    assert verdicts[0]["verdict"] == "go"
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
    assert VT.main(["--site", "pixverse", "--dry-run", "--out", "out"]) == 0
    assert (tmp_path / "out" / "video_trial.json").is_file()
    with pytest.raises(SystemExit) as exc:
        VT.main(["--site", "nope"])
    assert exc.value.code == 2
    assert VT.main(["--site", "pixverse", "--dry-run", "--prompt", "   "]) == 1
    assert VT.main(["--site", "pixverse", "--dry-run", "--browser-config", "missing.yaml"]) == 1
