"""The stealth / human-behaviour layer for logged-in sessions (Update 68).

What these tests prove, with fakes (no browser is ever started here):
- the layer is on by default and the owner can turn it off, and turning it off
  leaves a read exactly as it was before the layer existed;
- each account gets one pinned identity that never changes across runs, and
  the accounts differ from each other;
- a stealth session never logs in from the runner;
- the proxy comes from a secret, is used when configured and cleanly skipped
  when not, and its address is never written down or shown;
- the stealth browser (not plain Playwright) is used for session reads when
  one is available, and plain Playwright is the fallback when none is;
- human pacing applies to cookie reads (Bezier mouse, dwell proportional to
  content, one keystroke at a time), and the per-read log records pages,
  timing, IP and a human score.
"""
import json
import random
import sys
from types import ModuleType

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import human as H
from contentintel import stealth as S

from fake_browser import FakeBrowser, IG_JSON

PACING = {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 3}


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


@pytest.fixture(autouse=True)
def _clean_drivers(monkeypatch):
    """The shared drivers are process-wide; a test that leaves one running would change what the next test sees."""
    B._SHARED.users, B._SHARED.pw = 0, None
    B._PATCHRIGHT.users, B._PATCHRIGHT.pw = 0, None
    B._XVFB["instance"] = None
    for name in ("STEALTH_PROXY_URL", "STEALTH_PROXY_INSTAGRAM", "STEALTH_PROXY_TIKTOK"):
        monkeypatch.delenv(name, raising=False)
    yield
    B._SHARED.users, B._SHARED.pw = 0, None
    B._PATCHRIGHT.users, B._PATCHRIGHT.pw = 0, None
    B._XVFB["instance"] = None


# --- config ------------------------------------------------------------------------------------------


def test_the_stealth_block_defaults_to_on_and_rejects_bad_settings():
    cfg = S.resolve(None)
    assert cfg["enabled"] is True and cfg["engine"] == "auto" and cfg["log_reads"] is True
    assert S.resolve({}) == cfg
    for bad in ({"engine": "safari"}, {"enabled": "yes"}, {"nope": 1}, {"proxy_secret": "not a name!"},
                {"identities_file": ""}, [1], "on"):
        with pytest.raises(S.StealthError):
            S.resolve(bad)


def test_the_shipped_browser_yaml_carries_a_valid_stealth_block_on_by_default(tmp_path):
    cfg = B.load_browser_settings("browser.yaml")
    assert cfg["stealth"]["enabled"] is True
    assert all(cfg["sites"][name].get("stealth") == cfg["stealth"] for name in B.SITES)
    assert cfg["human"]["type_ms"] == [70, 180] and cfg["human"]["dwell_wpm"] == 200
    import copy
    import yaml
    shipped = yaml.safe_load(open("browser.yaml", encoding="utf-8"))
    data = copy.deepcopy(shipped)
    data["stealth"]["engine"] = "safari"
    path = tmp_path / "browser.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(B.BrowserError, match="engine"):
        B.load_browser_settings(path)


def test_password_fallback_never_is_accepted_and_a_nonsense_value_is_not(tmp_path):
    import copy
    import yaml
    shipped = yaml.safe_load(open("browser.yaml", encoding="utf-8"))
    data = copy.deepcopy(shipped)
    data["sites"]["instagram"]["password_fallback"] = "never"
    path = tmp_path / "browser.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert B.load_browser_settings(path)["sites"]["instagram"]["password_fallback"] == "never"
    data["sites"]["instagram"]["password_fallback"] = "sometimes"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(B.BrowserError, match="password_fallback"):
        B.load_browser_settings(path)


# --- one stable identity per account -------------------------------------------------------------------


def test_an_identity_is_pinned_across_runs_and_differs_per_account(tmp_path):
    cfg = S.resolve(None)
    path = str(tmp_path / "identities.json")
    first = S.resolve_identity("instagram", cfg, env={}, identities_path=path, rng=random.Random(1))
    again = S.resolve_identity("instagram", cfg, env={}, identities_path=path, rng=random.Random(99))
    assert (again["user_agent"], again["viewport"], again["timezone_id"]) == \
        (first["user_agent"], first["viewport"], first["timezone_id"])
    other = S.resolve_identity("tiktok", cfg, env={}, identities_path=path, rng=random.Random(2))
    assert other["profile_dir"] != first["profile_dir"]
    assert (other["user_agent"], other["viewport"], other["timezone_id"]) != \
        (first["user_agent"], first["viewport"], first["timezone_id"])
    assert first["allow_login"] is False
    # The file holds no secret: only the pinned values and the NAME of the secret.
    text = (tmp_path / "identities.json").read_text(encoding="utf-8")
    assert "STEALTH_PROXY_INSTAGRAM" in text and "http" not in text


def test_the_identity_summary_never_shows_a_proxy_address(tmp_path):
    cfg = S.resolve(None)
    identity = S.resolve_identity("instagram", cfg,
                                  env={"STEALTH_PROXY_INSTAGRAM": "http://user:pass@proxy.example:8080"},
                                  identities_path=str(tmp_path / "i.json"), rng=random.Random(1))
    summary = S.identity_summary(identity)
    assert "proxy.example" not in summary and "user" not in summary and "pass" not in summary


# --- proxy ---------------------------------------------------------------------------------------------


def test_the_proxy_is_none_when_nothing_is_configured_and_parsed_when_it_is():
    assert S.parse_proxy("STEALTH_PROXY_URL", env={}) is None
    assert S.parse_proxy("STEALTH_PROXY_URL", env={"STEALTH_PROXY_URL": "http://proxy.example:3128"}) == \
        {"server": "http://proxy.example:3128"}
    assert S.parse_proxy("STEALTH_PROXY_URL", env={"STEALTH_PROXY_URL": "socks5://user:pass@proxy.example:1080"}) == \
        {"server": "socks5://proxy.example:1080", "username": "user", "password": "pass"}


def test_a_set_but_unusable_proxy_fails_without_showing_the_address():
    with pytest.raises(S.StealthError) as excinfo:
        S.parse_proxy("STEALTH_PROXY_URL", env={"STEALTH_PROXY_URL": "not a proxy address"})
    assert "not a proxy address" not in str(excinfo.value)


def test_the_per_account_proxy_wins_and_the_shared_one_is_the_fallback(tmp_path):
    cfg = S.resolve(None)
    env = {"STEALTH_PROXY_INSTAGRAM": "http://user:pass@proxy.example:8080",
           "STEALTH_PROXY_URL": "http://shared.example:3128"}
    path = str(tmp_path / "i.json")
    own = S.resolve_identity("instagram", cfg, env=env, identities_path=path, rng=random.Random(1))
    assert own["proxy"] == {"server": "http://proxy.example:8080", "username": "user", "password": "pass"}
    assert own["proxy_secret_used"] == "STEALTH_PROXY_INSTAGRAM"
    shared = S.resolve_identity("tiktok", cfg, env=env, identities_path=path, rng=random.Random(2))
    assert shared["proxy"] == {"server": "http://shared.example:3128"}
    assert shared["proxy_secret_used"] == "STEALTH_PROXY_URL"
    assert "proxy.example" not in (tmp_path / "i.json").read_text(encoding="utf-8")


# --- human score and read log --------------------------------------------------------------------------


def test_the_human_score_is_zero_for_an_instant_empty_read_and_high_for_a_long_read():
    assert S.human_score(0, 0, 0, 0, 0) == 0
    assert S.human_score(120, 40000, 12, 8, 5) >= 90
    assert S.human_score(5, 1000, 2, 1, 1) < 60
    assert S.human_score(10, 0, 9, 6, 4) >= 55          # a photo page with nothing to read still scores for staying, moving and scrolling


def test_the_read_log_records_pages_timing_ip_and_score_and_writes_jsonl(tmp_path):
    identity = {"site": "instagram", "proxy": {"server": "http://proxy.example:8080"}}
    log = S.ReadLog("instagram", "camoufox", identity, egress_ip="203.0.113.7")
    entry = log.record("https://example.test/a", 1000.0, 1010.0, content_chars=5000,
                       mouse_moves=9, scroll_events=6, pauses=4)
    assert entry["dwell_seconds"] == 10.0 and entry["egress_ip"] == "203.0.113.7"
    assert entry["proxy"] == "proxy.example" and entry["human_score"] > 0
    updated = log.update(entry, ended_at=1020.0, scroll_events=9)
    assert updated["dwell_seconds"] == 20.0 and updated["scroll_events"] == 9
    summary = log.summary()
    assert summary["pages"] == 1 and summary["engine"] == "camoufox" and summary["human_score"] == updated["human_score"]
    assert summary["egress_ip"] == "203.0.113.7"
    path = tmp_path / "read-log.jsonl"
    assert log.write_jsonl(str(path)) is True
    saved = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert saved["url"] == "https://example.test/a" and saved["dwell_seconds"] == 20.0
    assert S.ReadLog("instagram", "plain", identity).summary() is None


def test_the_egress_ip_is_best_effort_and_never_fails_a_read():
    assert S.resolve_egress_ip(FakePage()) == "203.0.113.7"

    class NoNetwork:
        context = None

    assert S.resolve_egress_ip(NoNetwork()) is None


def test_reads_can_be_shuffled_without_losing_or_repeating_one():
    urls = [f"https://example.test/{i}" for i in range(8)]
    shuffled = S.shuffle_reads(urls, random.Random(7))
    assert sorted(shuffled) == sorted(urls) and shuffled != urls and urls == [f"https://example.test/{i}" for i in range(8)]


def test_xvfb_is_skipped_cleanly_when_it_is_not_installed(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    xvfb = S.Xvfb()
    assert xvfb.start() is False
    xvfb.stop()                                            # stopping what never started is a no-op


def test_xvfb_records_how_its_start_attempt_went(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    xvfb = S.Xvfb()
    assert xvfb.status is None                            # nothing attempted yet
    assert xvfb.start() is False
    assert xvfb.status == "not_available"
    assert xvfb.status_line() == "Xvfb: not available — patchright ran headless"
    monkeypatch.setenv("DISPLAY", ":0")
    xvfb = S.Xvfb()
    assert xvfb.start() is True
    assert xvfb.status == "already_running"
    assert "already running" in xvfb.status_line()
    xvfb.stop()


def test_launch_patchright_records_the_xvfb_outcome_on_the_context(tmp_path, monkeypatch):
    calls, page = [], FakePage()
    driver = FakePW(calls, page)
    identity = {"profile_dir": str(tmp_path / "profile"), "viewport": [1366, 768],
                "user_agent": "test-agent", "timezone_id": "Europe/London",
                "locale": "en-GB", "proxy": None}

    class Unavailable:
        status = "not_available"

    context = S.launch_patchright(driver, identity, S.resolve(None), headless=True, xvfb=Unavailable())
    assert context.xvfb_status == "not_available"
    assert [c for c in calls if c[0] == "persistent"][0][1]["headless"] is True


def test_the_read_log_carries_the_xvfb_outcome(tmp_path):
    identity = {"site": "instagram", "proxy": None}
    log = S.ReadLog("instagram", "patchright", identity, xvfb_status="started")
    log.record("https://example.test/a", 1000.0, 1010.0)
    assert log.summary()["xvfb"] == "started"
    plain = S.ReadLog("instagram", "plain", identity)
    plain.record("https://example.test/a", 1000.0, 1010.0)
    assert plain.summary()["xvfb"] is None          # no X server was involved, so nothing is claimed


# --- fakes for the browser tests ------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


class FakeRequest:
    def __init__(self, ip):
        self._ip = ip

    def get(self, url, timeout=None):
        return FakeResponse(self._ip)


class FakeRequestContext:
    def __init__(self, ip="203.0.113.7"):
        self.request = FakeRequest(ip)


class FakePage:
    """A page that records what it is asked to do and refuses nothing but looking, like the one in test_update62.py."""

    def __init__(self, content=""):
        self.log, self.gotos = [], []
        self.mouse = self
        self.url = "https://example.test/"
        self._content = content
        self.context = FakeRequestContext()

    def move(self, x, y, steps=1):
        self.log.append(("move", x, y, steps))

    def wheel(self, dx, dy):
        self.log.append(("wheel", dy))

    def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))

    def goto(self, url, **kw):
        self.gotos.append((url, kw))

    def content(self):
        return self._content

    def on(self, *a):
        pass

    def set_default_timeout(self, ms):
        pass


class FakeContext:
    def __init__(self, calls, page):
        self.calls, self.page, self.cookies = calls, page, []

    def add_cookies(self, cookies):
        self.cookies = list(cookies)

    def new_page(self):
        return self.page

    def close(self):
        self.calls.append(("close_context",))


class FakeBrowserProcess:
    def __init__(self, calls, page):
        self.calls, self.page, self.closed = calls, page, False

    def new_context(self, **kw):
        self.calls.append(("new_context", kw))
        return FakeContext(self.calls, self.page)

    def close(self):
        self.closed = True
        self.calls.append(("close_browser",))


class FakeChromium:
    def __init__(self, calls, page):
        self.calls, self.page = calls, page

    def launch(self, headless=True, args=None):
        self.calls.append(("launch", headless, args))
        return FakeBrowserProcess(self.calls, self.page)

    def launch_persistent_context(self, **kw):
        self.calls.append(("persistent", kw))
        return FakeContext(self.calls, self.page)


class FakePW:
    def __init__(self, calls, page):
        self.chromium = FakeChromium(calls, page)

    def stop(self):
        pass


class FakeBox:
    def __init__(self):
        self.calls = []

    def click(self, timeout=None):
        self.calls.append(("click",))

    def fill(self, text):
        self.calls.append(("fill", text))

    def type(self, text):
        self.calls.append(("type", text))

    def press(self, key):
        self.calls.append(("press", key))


def _stub_module(name, **attrs):
    """A fake module in sys.modules (with its parent package), for an engine that is not installed here."""
    parent_name, _, _ = name.rpartition(".")
    stub = ModuleType(name)
    for key, value in attrs.items():
        setattr(stub, key, value)
    parent = ModuleType(parent_name)
    parent.__path__ = []
    setattr(parent, name.rpartition(".")[2], stub)
    return parent, stub


def _stealth_cfg(**overrides):
    cfg = {"json_url_hints": [], "stealth": S.resolve(None)}
    cfg["stealth"].update(overrides)
    return cfg


# --- human pacing on cookie reads ----------------------------------------------------------------------


def test_a_cookie_read_is_paced_like_a_person_and_logged(tmp_path):
    person = H.Human(None, rng=random.Random(3), sleep=lambda seconds: None)
    page = FakePage(content="word " * 5000)               # a long page: 25,000 characters to read
    browser = B.PlaywrightBrowser(page, tmp_path, PACING, [], human=person)
    browser.read_log = S.ReadLog("instagram", "plain", {"site": "instagram", "proxy": None})
    browser.open("https://www.instagram.com/explore/tags/overalls/")
    browser.scroll(2, 3)
    entry = browser.read_log.entries[0]
    assert entry["url"] == "https://www.instagram.com/explore/tags/overalls/"
    assert entry["content_chars"] == 25000
    assert entry["mouse_moves"] > 0 and entry["scroll_events"] > 0 and entry["pauses"] > 0
    assert entry["human_score"] >= 40
    waits = [e[1] for e in page.log if e[0] == "wait"]
    assert len([w for w in waits if w >= 1500]) >= 3      # the settle, the dwell for the long page, and the reading pauses
    browser.open("https://www.instagram.com/explore/tags/wool/")
    assert page.gotos[1][1].get("referer") == "https://www.instagram.com/explore/tags/overalls/"


def test_the_mouse_follows_a_curve_that_overshoots_and_settles_back():
    person = H.Human(None, rng=random.Random(3), sleep=lambda seconds: None)
    page = FakePage()
    person.after_load(page)
    moves = [e for e in page.log if e[0] == "move"]
    assert any(8 <= e[3] <= 22 for e in moves)            # the first smooth leg, as before
    assert len(moves) > 1                                 # then the curve, in small steps
    assert all(0 <= e[1] <= 1366 and 0 <= e[2] <= 900 for e in moves)


def test_typing_is_one_keystroke_at_a_time_and_long_text_is_pasted():
    person = H.Human({"typo_chance": 0.0}, rng=random.Random(5), sleep=lambda seconds: None)
    box = FakeBox()
    assert person.type_like_person(box, "hello") == "typed"
    assert [c for c in box.calls if c[0] == "fill"] == []     # never filled instantly
    assert "".join(c[1] for c in box.calls if c[0] == "type") == "hello"
    box = FakeBox()
    assert person.type_like_person(box, "x" * 61) == "pasted"
    assert box.calls == [("fill", "x" * 61)]


def test_a_typo_is_noticed_corrected_and_the_text_still_comes_out_right():
    person = H.Human({"typo_chance": 1.0}, rng=random.Random(5), sleep=lambda seconds: None)
    box = FakeBox()
    person.type_like_person(box, "overalls")
    assert ("press", "Backspace") in box.calls
    typed = []
    for kind, value in box.calls:
        if kind == "type":
            typed.append(value)
        elif kind == "press" and value == "Backspace":
            typed.pop()
    assert "".join(typed) == "overalls"


# --- the engines ---------------------------------------------------------------------------------------


def test_a_session_read_uses_camoufox_when_it_is_available(tmp_path, monkeypatch):
    calls, page = [], FakePage()
    fake_pw = FakePW(calls, page)
    monkeypatch.setattr(B, "_start_playwright", lambda: fake_pw)
    parent, stub = _stub_module("camoufox.sync_api", NewBrowser=lambda pw, **kw: (calls.append(("camoufox", kw)), FakeContext(calls, page))[1])
    monkeypatch.setitem(sys.modules, "camoufox", parent)
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", stub)
    with B.open_browser("instagram", _stealth_cfg(), PACING, "", tmp_path,
                        identities_path=str(tmp_path / "i.json")) as browser:
        assert browser.identity is not None and browser.identity["engine"] == "camoufox"
        assert browser.read_log is not None and browser.read_log.engine == "camoufox"
        assert browser.read_log.egress_ip == "203.0.113.7"
        assert not [c for c in calls if c[0] == "launch"]     # plain Chromium was never launched
    assert B._SHARED.users == 0 and B._SHARED.pw is None
    # Nothing was read, so no log file is written at all (an empty file would never reach the repository).
    assert not (tmp_path / "instagram" / "read-log.jsonl").exists()


def test_patchright_is_used_when_camoufox_is_missing(tmp_path, monkeypatch):
    calls, page = [], FakePage()
    driver = FakePW(calls, page)

    class Started:
        def start(self):
            return driver

    parent, stub = _stub_module("patchright.sync_api", sync_playwright=lambda: Started())
    monkeypatch.setitem(sys.modules, "patchright", parent)
    monkeypatch.setitem(sys.modules, "patchright.sync_api", stub)

    class NoXvfb:
        def start(self):
            return False

        def stop(self):
            pass

    monkeypatch.setattr(S, "Xvfb", NoXvfb)
    with B.open_browser("instagram", _stealth_cfg(), PACING, "", tmp_path,
                        identities_path=str(tmp_path / "i.json")) as browser:
        assert browser.identity["engine"] == "patchright"
        assert browser.read_log is not None and browser.read_log.engine == "patchright"
    persistent = [c for c in calls if c[0] == "persistent"]
    assert persistent and persistent[0][1]["headless"] is True            # no X server, so headless
    assert persistent[0][1]["user_agent"] == browser.identity["user_agent"]
    assert B._PATCHRIGHT.users == 0 and B._PATCHRIGHT.pw is None


def test_plain_playwright_is_the_fallback_when_no_stealth_engine_is_installed(tmp_path, monkeypatch):
    assert "camoufox.sync_api" not in sys.modules and "patchright.sync_api" not in sys.modules
    calls, page = [], FakePage()
    fake_pw = FakePW(calls, page)
    monkeypatch.setattr(B, "_start_playwright", lambda: fake_pw)
    with B.open_browser("instagram", _stealth_cfg(), PACING, "", tmp_path,
                        identities_path=str(tmp_path / "i.json")) as browser:
        assert browser.identity is not None and browser.identity["engine"] == "plain"
        assert browser.read_log is not None and browser.read_log.engine == "plain"
    assert [c for c in calls if c[0] == "launch"][0][2] == ["--no-sandbox"]
    context = [c for c in calls if c[0] == "new_context"][0][1]
    width, height = browser.identity["viewport"]
    assert context["viewport"] == {"width": width, "height": height}
    assert context["user_agent"] == browser.identity["user_agent"]
    assert context["timezone_id"] == browser.identity["timezone_id"]
    assert B._SHARED.users == 0 and B._SHARED.pw is None


def test_with_the_layer_off_a_read_is_exactly_as_before(tmp_path, monkeypatch):
    calls, page = [], FakePage()
    fake_pw = FakePW(calls, page)
    monkeypatch.setattr(B, "_start_playwright", lambda: fake_pw)
    with B.open_browser("instagram", {"json_url_hints": []}, PACING, "", tmp_path) as browser:
        assert browser.identity is None and browser.read_log is None
    context = [c for c in calls if c[0] == "new_context"][0][1]
    assert context == {"viewport": {"width": 1366, "height": 900}, "locale": "en-US",
                       "user_agent": B._UA, "timezone_id": "America/New_York"}


def test_a_missing_playwright_is_still_explained(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(B.BrowserError, match="Playwright is not installed"):
        with B.open_browser("instagram", {"json_url_hints": []}, PACING, "", tmp_path):
            pass


# --- collectors: no login, shuffled reads, the report line ----------------------------------------------


def _login_wall_world(cfg):
    """An Instagram that shows its login wall, with a username and password ready to be used."""
    site_cfg = cfg["sites"]["instagram"]
    browser = FakeBrowser({"https://www.instagram.com/": {}},
                          present_always=set(site_cfg["login_wall_any"]))
    return browser, site_cfg


def test_a_stealth_session_never_logs_in_from_the_runner(cfg):
    browser, site_cfg = _login_wall_world(cfg)
    browser.identity = {"allow_login": False}
    creds = {"cookies": "[{\"name\": \"sessionid\", \"value\": \"x\", \"domain\": \".instagram.com\"}]",
             "username": "dummy", "password": "secret"}
    with pytest.raises(B.LoginWall):
        C.ensure_session(browser, "instagram", site_cfg, creds)
    assert browser.opened == ["https://www.instagram.com/"]     # the login page was never opened
    assert browser.typed == [] and browser.clicks == []


def test_without_the_identity_the_same_wall_does_try_the_password_login(cfg):
    browser, site_cfg = _login_wall_world(cfg)
    creds = {"cookies": "[{\"name\": \"sessionid\", \"value\": \"x\", \"domain\": \".instagram.com\"}]",
             "username": "dummy", "password": "secret"}
    with pytest.raises(B.BrowserError, match="could not find the username box"):
        C.ensure_session(browser, "instagram", site_cfg, creds)
    assert "https://www.instagram.com/accounts/login/" in browser.opened


def test_password_fallback_never_stops_the_login_even_without_an_identity(cfg):
    browser, site_cfg = _login_wall_world(cfg)
    site_cfg = {**site_cfg, "password_fallback": "never"}
    creds = {"cookies": "[{\"name\": \"sessionid\", \"value\": \"x\", \"domain\": \".instagram.com\"}]",
             "username": "dummy", "password": "secret"}
    with pytest.raises(B.LoginWall):
        C.ensure_session(browser, "instagram", site_cfg, creds)
    assert browser.opened == ["https://www.instagram.com/"]


def _instagram_world(cfg, tags):
    pages = {f"https://www.instagram.com/explore/tags/{tag}/": {"json": [IG_JSON]} for tag in tags}
    return FakeBrowser(pages, present_always={cfg["sites"]["instagram"]["logged_in_any"][0]})


def test_the_planned_pages_are_shuffled_but_careful_mode_keeps_its_rotation(cfg, tmp_path):
    tags = ["a", "b", "c", "d", "e", "f"]
    task = C.Task("instagram", "t", {"hashtags": tags}, "k", len(tags))
    site_cfg = {**cfg["sites"]["instagram"], "settle_seconds": 0}
    identity = S.resolve_identity("instagram", S.resolve(None), env={},
                                  identities_path=str(tmp_path / "i.json"), rng=random.Random(7))
    identity["rng"] = random.Random(7)                     # a fixed shuffle order, so the test is not luck
    browser = _instagram_world(cfg, tags)
    browser.identity = identity
    C.run_browser_task(browser, task, 50, site_cfg, cfg, {"cookies": "x"})
    opened = [url.split("/tags/")[1].rstrip("/") for url in browser.opened[1:]]
    assert sorted(opened) == sorted(tags) and opened != tags
    browser = _instagram_world(cfg, tags)
    browser.identity = identity
    C.run_browser_task(browser, task, 50, site_cfg, cfg, {"cookies": "x"}, page_budget=2, cursor=0)
    assert [url.split("/tags/")[1].rstrip("/") for url in browser.opened[1:]] == tags[:2]


def test_the_run_report_carries_the_stealth_line_and_the_stats_carry_the_summary(cfg):
    stats = {"fallbacks": [], "pages_planned": 2, "pages_total": 2, "page_errors": 0,
             "stealth": {"pages": 3, "engine": "camoufox", "identity": "instagram",
                         "proxy": "proxy.example", "egress_ip": "203.0.113.7", "human_score": 82,
                         "dwell_seconds": 12.5}}
    notes = C._page_notes(stats)
    assert "stealth: 3 page(s) read as instagram through camoufox" in notes
    assert "through the proxy (proxy.example)" in notes and "human score 82/100" in notes
    assert "203.0.113.7" not in notes                          # the IP is in the log file, never in the report
    assert C._page_notes({"fallbacks": [], "pages_planned": 2, "pages_total": 2, "page_errors": 0}) == ""

    class Summary:
        def summary(self):
            return {"pages": 1, "engine": "plain", "identity": "instagram", "proxy": None,
                    "egress_ip": None, "human_score": 60, "dwell_seconds": 4.0}

    browser = _instagram_world(cfg, ["a"])
    browser.read_log = Summary()
    task = C.Task("instagram", "t", {"hashtags": ["a"]}, "k", 1)
    _, stats = C.run_browser_task(browser, task, 50, {**cfg["sites"]["instagram"], "settle_seconds": 0},
                                  cfg, {"cookies": "x"})
    assert stats["stealth"]["engine"] == "plain" and stats["stealth"]["pages"] == 1
