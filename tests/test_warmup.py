"""Warm-up visits (contentintel/warmup.py) and the TikTok challenge detection fix.

What these tests prove, with fakes (no browser is ever started here):
- warm-up visits one video page and one profile page per site, waits, and
  polls the cookie jar for the clearance cookie — and visits EVERY page kind
  even when the token was already found, because the token is scoped per kind;
- warm-up is a no-op when disabled or unconfigured, and never fails a run:
  a stale page or an unreadable cookie jar is logged and skipped;
- the shipped browser.yaml warms up TikTok (with _waftokenid) and Instagram
  (with csrftoken), but not Meta Ads — whose reads are public pages, not a
  logged-in session — and TikTok's challenge list covers the puzzle variant
  Collect #64 actually showed;
- warm-up is wired into open_browser, so a real session warms up before any
  listing read;
- the collect path never writes cookies back over Harish's exported cookies:
  no storage_state write in the browser, collectors, stealth or CLI code, and
  the only storage_state read in the codebase is the manual capture on the
  owner's own machine.
"""
from pathlib import Path

import pytest

from contentintel import browser as B
from contentintel import stealth as S
from contentintel import warmup as W

ROOT = Path(__file__).resolve().parents[1]
PACING = {"min_delay_seconds": 3, "max_delay_seconds": 8, "scroll_pause_seconds": 3}

TIKTOK_URLS = ["https://www.tiktok.com/@tiktok/video/7238789584926477594",
               "https://www.tiktok.com/@tiktok"]


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


def _site_cfg(**overrides):
    block = {"enabled": True, "urls": list(TIKTOK_URLS), "token": "_waftokenid",
             "wait_seconds": 0, "poll_seconds": 2}
    block.update(overrides)
    return {"warmup": block}


class FakeWarmupPage:
    """A page whose cookie jar gains the clearance token after a set number of reads."""

    def __init__(self, fail_urls=(), token_after=None):
        self.gotos, self.waits = [], []
        self.fail_urls = set(fail_urls)
        self.token_after = token_after      # cookies() calls before the token appears; None means never
        self._cookie_calls = 0
        self.context = self

    def goto(self, url, **kw):
        if url in self.fail_urls:
            raise RuntimeError("that page is gone")
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def cookies(self):
        self._cookie_calls += 1
        if self.token_after is not None and self._cookie_calls > self.token_after:
            return [{"name": "_waftokenid", "value": "minted-by-the-page"}]
        return []


class FakeWarmupBrowser:
    """The part of a browser warm-up touches: a page, a wait, and a last URL."""

    def __init__(self, page):
        self.page = page
        self._last_url = None

    def wait(self, seconds):
        self.page.wait_for_timeout(int(seconds * 1000))


# --- the warm-up itself ---------------------------------------------------------------------------------


def test_warm_up_visits_the_configured_pages_and_finds_the_clearance_token():
    page = FakeWarmupPage(token_after=2)      # the token appears a few seconds after the first page loads
    browser = FakeWarmupBrowser(page)
    logged = []
    report = W.warm_up(browser, _site_cfg(), log=logged.append)
    assert report["visited"] == TIKTOK_URLS
    assert report["token_found"] is True
    assert report["seconds"] >= 0
    assert page.gotos == TIKTOK_URLS
    assert browser._last_url == TIKTOK_URLS[-1]     # the next real read arrives from the warm-up page


def test_warm_up_visits_every_page_kind_even_after_the_token_is_found():
    # The token is scoped per page kind: a video-page token does not clear profile pages, so finding it on the
    # first page must not skip the second.
    page = FakeWarmupPage(token_after=0)
    browser = FakeWarmupBrowser(page)
    report = W.warm_up(browser, _site_cfg())
    assert report["visited"] == TIKTOK_URLS and report["token_found"] is True


def test_warm_up_is_a_no_op_when_disabled_or_unconfigured():
    page = FakeWarmupPage(token_after=0)
    browser = FakeWarmupBrowser(page)
    assert W.warm_up(browser, _site_cfg(enabled=False)) == {"visited": [], "token_found": False, "seconds": 0.0}
    assert W.warm_up(browser, {}) == {"visited": [], "token_found": False, "seconds": 0.0}
    assert W.warm_up(browser, {"warmup": {"enabled": True, "urls": []}}) == \
        {"visited": [], "token_found": False, "seconds": 0.0}
    assert page.gotos == []


def test_warm_up_never_fails_the_run():
    page = FakeWarmupPage(fail_urls={TIKTOK_URLS[0]}, token_after=None)
    browser = FakeWarmupBrowser(page)
    logged = []
    report = W.warm_up(browser, _site_cfg(), log=logged.append)
    assert report["visited"] == [TIKTOK_URLS[1]] and report["token_found"] is False
    assert any("could not open" in line and TIKTOK_URLS[0] in line for line in logged)
    assert any("did not appear" in line for line in logged)

    class NoCookies:
        page = None

        def wait(self, seconds):
            pass

    # A browser whose cookie jar cannot be read at all still does not raise.
    assert W.warm_up(NoCookies(), _site_cfg())["visited"] == []


def test_a_bad_warmup_block_falls_back_to_the_defaults_instead_of_failing():
    assert W.resolve_warmup({"warmup": {"urls": TIKTOK_URLS, "token": "", "wait_seconds": "soon"}}) == \
        {"enabled": True, "urls": TIKTOK_URLS, "token": "_waftokenid",
         "wait_seconds": 5, "poll_seconds": 10}
    assert W.resolve_warmup({"warmup": {"urls": ["not-a-url", TIKTOK_URLS[0]]}})["urls"] == [TIKTOK_URLS[0]]


# --- the shipped config ----------------------------------------------------------------------------------


def test_the_shipped_browser_yaml_warms_up_tiktok_and_instagram_and_not_meta_ads():
    cfg = B.load_browser_settings("browser.yaml")
    tiktok = W.resolve_warmup(cfg["sites"]["tiktok"])
    assert tiktok["urls"] == TIKTOK_URLS and tiktok["token"] == "_waftokenid"
    assert tiktok["wait_seconds"] == 5 and tiktok["poll_seconds"] == 10
    instagram = W.resolve_warmup(cfg["sites"]["instagram"])
    assert instagram["urls"] == ["https://www.instagram.com/instagram/",
                                 "https://www.instagram.com/p/Cuw6X2mOqXG/"]
    assert instagram["token"] == "csrftoken"      # Instagram has no WAF clearance cookie; this only confirms the session cookies are there
    assert W.resolve_warmup(cfg["sites"]["meta_ads"]) is None    # public Ad Library pages, not a logged-in session
    assert W.resolve_warmup(cfg["sites"]["gemini"]) is None
    assert cfg["sites"]["tiktok"]["hashtag_via_search_first"] is True


def test_the_tiktok_challenge_list_covers_the_collect_64_puzzle():
    """Collect #64's debug page showed the puzzle variant this list used to miss: 'Drag the puzzle piece into
    place' 1x, #captcha-verify-container-main-page 1x, #captcha_slide_button 1x — and the four older entries 0x
    each. Detection is what unlocks the close-once + search fallback; the old entries stay for other variants."""
    cfg = B.load_browser_settings("browser.yaml")
    challenge = cfg["sites"]["tiktok"]["challenge_any"]
    for marker in ("text=Drag the puzzle piece into place",
                   "#captcha-verify-container-main-page", "#captcha_slide_button"):
        assert marker in challenge
    for old in ("text=Verify to continue", "text=Drag the slider",
                "#captcha-verify-image", "text=Select 2 objects"):
        assert old in challenge


def test_bad_warmup_blocks_are_rejected_with_a_clear_message(tmp_path):
    import copy
    import yaml
    shipped = yaml.safe_load(open("browser.yaml", encoding="utf-8"))
    for mutate, match in (("not-a-block", "warmup"),
                          ({"urls": ["http://plain.example/x"]}, "https://"),
                          ({"token": ""}, "token"),
                          ({"wait_seconds": -1}, "wait_seconds")):
        data = copy.deepcopy(shipped)
        data["sites"]["tiktok"]["warmup"] = mutate
        path = tmp_path / "browser.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        with pytest.raises(B.BrowserError, match=match):
            B.load_browser_settings(path)


# --- wired into open_browser -----------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text):
        self._text = text

    def text(self):
        return self._text


class _FakeRequest:
    def get(self, url, timeout=None):
        return _FakeResponse("203.0.113.7")


class _FakePage:
    """A page that records its visits and refuses nothing, like the one in test_stealth.py."""

    def __init__(self):
        self.gotos, self.waits = [], []
        self.url = "https://www.tiktok.com/"
        self.context = self
        self.request = _FakeRequest()

    def goto(self, url, **kw):
        self.gotos.append(url)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def content(self):
        return ""

    def on(self, *a):
        pass

    def set_default_timeout(self, ms):
        pass

    def cookies(self):
        return []          # no clearance token minted in the fake: warm-up still visits and still does not fail


class _FakeContext:
    def __init__(self, calls, page):
        self.calls, self.page, self.cookies = calls, page, []

    def add_cookies(self, cookies):
        self.cookies = list(cookies)

    def new_page(self):
        return self.page

    def close(self):
        self.calls.append(("close_context",))


class _FakeBrowserProcess:
    def __init__(self, calls, page):
        self.calls, self.page = calls, page

    def new_context(self, **kw):
        self.calls.append(("new_context", kw))
        return _FakeContext(self.calls, self.page)

    def close(self):
        self.calls.append(("close_browser",))


class _FakePW:
    def __init__(self, calls, page):
        self.chromium = self
        self.calls, self.page = calls, page

    def launch(self, headless=True, args=None):
        self.calls.append(("launch", headless, args))
        return _FakeBrowserProcess(self.calls, self.page)

    def stop(self):
        pass


def test_warm_up_is_wired_into_open_browser(tmp_path, monkeypatch, capsys):
    cfg = B.load_browser_settings("browser.yaml")
    calls, page = [], _FakePage()
    monkeypatch.setattr(B, "_start_playwright", lambda: _FakePW(calls, page))
    monkeypatch.setattr(S, "available_engines", lambda preferred="auto": ["plain"])
    with B.open_browser("tiktok", cfg["sites"]["tiktok"], PACING, "", tmp_path,
                        identities_path=str(tmp_path / "i.json")) as browser:
        # The warm-up pages were visited before the session was handed over, in order.
        assert page.gotos[:2] == TIKTOK_URLS
        assert browser.read_log is not None
    assert "warm-up tiktok: visited 2 page(s), clearance token found: no" in capsys.readouterr().out


def test_warm_up_is_off_when_the_owner_turns_it_off(tmp_path, monkeypatch, capsys):
    cfg = B.load_browser_settings("browser.yaml")
    site_cfg = dict(cfg["sites"]["tiktok"])
    site_cfg["warmup"] = {**cfg["sites"]["tiktok"]["warmup"], "enabled": False}
    calls, page = [], _FakePage()
    monkeypatch.setattr(B, "_start_playwright", lambda: _FakePW(calls, page))
    monkeypatch.setattr(S, "available_engines", lambda preferred="auto": ["plain"])
    with B.open_browser("tiktok", site_cfg, PACING, "", tmp_path,
                        identities_path=str(tmp_path / "i.json")):
        assert page.gotos == []
    assert "warm-up tiktok" not in capsys.readouterr().out


# --- cookie write-back audit -------------------------------------------------------------------------------


def test_no_cookie_write_back_in_the_collect_path():
    """The runner must never write its cookies back over Harish's exported cookies (a runner-minted token would
    poison a good session with datacenter-seeded ones). Proved by reading the code: the collect path — the
    browser, the collectors, the stealth layer and the CLI — never saves a browser state, and no workflow ever
    writes a secret. The only storage_state read in the codebase is the manual capture in sessions.py, which
    runs by hand on the owner's own machine and writes to a file he names, never in a workflow."""
    for name in ("browser.py", "collectors.py", "stealth.py", "cli.py"):
        text = (ROOT / "contentintel" / name).read_text(encoding="utf-8")
        assert "storage_state" not in text, name
        assert "capture_session_interactive" not in text, name
    sessions = (ROOT / "contentintel" / "sessions.py").read_text(encoding="utf-8")
    assert sessions.count("storage_state") >= 1
    capture = sessions.split("def capture_session_interactive", 1)[1].split("\ndef ", 1)[0]
    assert "context.storage_state()" in capture
    assert "Never run in a workflow" in capture
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "secret set" not in workflow.read_text(encoding="utf-8"), workflow.name
    # The stealth profiles hold a browser profile on the runner's own disk (git-ignored, never committed); they
    # are not Harish's exported cookies and are never written back to a secret.
    assert ".stealth-profiles" in (ROOT / "contentintel" / "stealth.py").read_text(encoding="utf-8")


def test_the_proxy_address_is_never_shown_even_when_it_fails_at_identity_time(tmp_path):
    """A set-but-unusable proxy fails the run clearly (item 6: verified, no behaviour change): the error names
    the secret, never the address — checked here at the identity step, not just at the parse step."""
    cfg = S.resolve(None)
    # Two unusable shapes: not an address at all, and an out-of-range port (urlparse raises on .port, so this
    # used to escape as a bare ValueError instead of the clear StealthError).
    for bad in ("not a proxy address", "http://user:pass@proxy.example:99999"):
        with pytest.raises(S.StealthError) as excinfo:
            S.resolve_identity("instagram", cfg, env={"STEALTH_PROXY_INSTAGRAM": bad},
                               identities_path=str(tmp_path / "i.json"))
        assert "proxy.example" not in str(excinfo.value) and "pass" not in str(excinfo.value)
        assert "STEALTH_PROXY_INSTAGRAM" in str(excinfo.value)
