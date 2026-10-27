"""Playwright's synchronous API allows only ONE running instance per process.

The first real multi-site collection (Meta ads, then TikTok) failed on 2026-10-27 with
    "It looks like you are using Playwright Sync API inside the asyncio loop. Please use the Async API instead."
because every site started its own Playwright while the previous site's browser was still open. These tests use a fake Playwright that
refuses a second start with that very message, so they fail on the old code and pass only when all sites share one instance.
"""
import sys
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from contentintel import browser as B
from contentintel import cli
from contentintel import collectors as C
from contentintel import research as R
from contentintel.store import Store

from test_collectors import CREDS, NOW, statuses, write_research

REAL_MESSAGE = "It looks like you are using Playwright Sync API inside the asyncio loop.\nPlease use the Async API instead."


class FakePlaywright:
    """A stand-in with the one rule that matters: a second start while one is running raises Playwright's own error."""
    running = False
    log = []

    def __init__(self):
        self.browsers = []
        self.chromium = self

    @classmethod
    def start(cls):
        if cls.running:
            raise Exception(REAL_MESSAGE)
        cls.running = True
        cls.log.append("start")
        return cls()

    def launch(self, headless=True, args=None):
        browser = FakeBrowserProcess(self)
        self.browsers.append(browser)
        return browser

    def stop(self):
        FakePlaywright.running = False
        FakePlaywright.log.append("stop")


def fake_page():
    """A page that answers the few calls whose results are used as text; everything else is a harmless mock."""
    page = MagicMock()
    page.url = "https://example.test/"
    page.content.return_value = "<html><body></body></html>"
    page.title.return_value = "Example"
    page.evaluate.return_value = []
    page.locator.return_value.count.return_value = 0
    return page


class FakeBrowserProcess:
    def __init__(self, pw):
        self.pw, self.closed = pw, False

    def new_context(self, **kw):
        context = MagicMock()
        context.new_page.return_value = fake_page()
        return context

    def close(self):
        self.closed = True
        FakePlaywright.log.append("close")


@pytest.fixture(autouse=True)
def fake_playwright(monkeypatch):
    FakePlaywright.running, FakePlaywright.log = False, []
    monkeypatch.setattr(B, "_start_playwright", FakePlaywright.start)
    monkeypatch.setattr(B, "_SHARED", B._SharedPlaywright())
    monkeypatch.setattr(B.time, "sleep", lambda seconds: None)                 # the real pauses (up to 60 seconds a page) are not the point here
    yield FakePlaywright
    assert FakePlaywright.running is False and B._SHARED.users == 0          # nothing is ever left running or counted


def opened(site="meta_ads", tmp="debug"):
    return B.open_browser(site, {"json_url_hints": []}, {"min_delay_seconds": 0, "max_delay_seconds": 0, "scroll_pause_seconds": 0}, "", tmp)


# ---- the fake itself is faithful ----------------------------------------------------------------------------------------------------

def test_the_fake_refuses_a_second_start_with_playwrights_own_message():
    FakePlaywright.start()
    with pytest.raises(Exception, match="inside the asyncio loop") as err:
        FakePlaywright.start()
    assert "Please use the Async API instead" in str(err.value)
    FakePlaywright.running = False


# ---- the failure that happened ----------------------------------------------------------------------------------------------------------

def test_two_sites_open_at_the_same_time_share_one_playwright(fake_playwright):
    with opened("meta_ads") as first, opened("tiktok") as second:
        assert first is not second and fake_playwright.running is True
        assert fake_playwright.log == ["start"]                                    # started once, not once per site
    assert fake_playwright.log == ["start", "close", "close", "stop"]


def test_all_four_sites_at_once_work_and_each_gets_its_own_browser(fake_playwright):
    from contextlib import ExitStack
    with ExitStack() as stack:
        sites = [stack.enter_context(opened(name)) for name in ("meta_ads", "tiktok", "instagram", "gemini")]
        assert len({id(s.page) for s in sites}) == 4 and fake_playwright.log == ["start"]
    assert fake_playwright.log.count("start") == 1 and fake_playwright.log.count("close") == 4 and fake_playwright.log.count("stop") == 1


def test_playwright_is_stopped_only_when_the_last_browser_closes(fake_playwright):
    first, second = opened("meta_ads"), opened("tiktok")
    first.__enter__(), second.__enter__()
    second.__exit__(None, None, None)
    assert fake_playwright.running is True and "stop" not in fake_playwright.log        # the first site is still using it
    first.__exit__(None, None, None)
    assert fake_playwright.running is False and fake_playwright.log[-1] == "stop"


def test_the_order_of_closing_does_not_matter(fake_playwright):
    first, second = opened("meta_ads"), opened("tiktok")
    first.__enter__(), second.__enter__()
    first.__exit__(None, None, None)
    assert fake_playwright.running is True
    second.__exit__(None, None, None)
    assert fake_playwright.running is False and fake_playwright.log.count("stop") == 1


def test_a_site_opened_after_everything_closed_starts_playwright_again(fake_playwright):
    with opened("meta_ads"):
        pass
    with opened("tiktok"):
        pass
    assert fake_playwright.log == ["start", "close", "stop", "start", "close", "stop"]


def test_a_site_opened_while_another_is_still_open_after_one_closed_does_not_start_again(fake_playwright):
    with opened("meta_ads"):
        with opened("tiktok"):
            pass
        with opened("instagram"):
            pass
    assert fake_playwright.log.count("start") == 1


# ---- when something goes wrong ------------------------------------------------------------------------------------------------------------

def test_a_browser_that_cannot_launch_is_a_browser_error_and_leaves_nothing_running(monkeypatch, fake_playwright):
    monkeypatch.setattr(FakePlaywright, "launch", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("no chromium")))
    with pytest.raises(B.BrowserError, match="could not start the browser for tiktok: .*no chromium"):
        with opened("tiktok"):
            pass
    assert fake_playwright.running is False


def test_a_failed_launch_does_not_stop_a_playwright_another_site_is_using(monkeypatch, fake_playwright):
    with opened("meta_ads"):
        monkeypatch.setattr(FakePlaywright, "launch", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("no chromium")))
        with pytest.raises(B.BrowserError):
            with opened("tiktok"):
                pass
        assert fake_playwright.running is True and B._SHARED.users == 1


def test_an_error_inside_a_site_still_releases_it(fake_playwright):
    with pytest.raises(ValueError):
        with opened("meta_ads"):
            raise ValueError("task failed")
    assert fake_playwright.running is False


def test_playwright_failing_to_start_is_a_browser_error_that_keeps_the_real_reason(monkeypatch, fake_playwright):
    monkeypatch.setattr(B, "_start_playwright", lambda: (_ for _ in ()).throw(Exception(REAL_MESSAGE)))
    with pytest.raises(B.BrowserError, match="could not start Playwright for tiktok: .*inside the asyncio loop"):
        with opened("tiktok"):
            pass
    assert B._SHARED.users == 0 and B._SHARED.pw is None


def test_playwright_not_installed_is_explained(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(B.BrowserError, match="Playwright is not installed"):
        B._start_playwright()
    B._SHARED.users, B._SHARED.pw = 0, None


# ---- the path that failed: a collection run with two sites ----------------------------------------------------------------------------

def test_a_collect_run_with_meta_ads_and_tiktok_opens_both_and_does_not_crash(tmp_path, swov, fake_playwright):
    cfg = B.load_browser_settings("browser.yaml")
    write_research(tmp_path, swov, {"track_own_brand": False, "ads": {"keywords": ["overalls"]}, "social": {"tiktok": {"hashtags": ["overalls"]}}})
    factory = cli._real_factory(cfg, CREDS, str(tmp_path / "debug"))
    settings = replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False, auto_enabled=False)
    result = C.run_collect([swov], Store(tmp_path / "data"), browser_factory=factory, creds=CREDS, settings=settings, browser_cfg=cfg,
                           ledger_cls=R.Ledger, sources=["meta_ads", "tiktok"], now=NOW, run_id="r", root=str(tmp_path / "brands"))
    assert {s for s, _ in statuses(result)} == {"meta_ads", "tiktok"}                   # both sites were reached; neither crashed the run
    assert fake_playwright.log.count("start") == 1 and fake_playwright.log.count("close") == 2 and fake_playwright.log[-1] == "stop"


def test_a_collect_run_with_every_browser_site_opens_each_once(tmp_path, swov, fake_playwright):
    cfg = B.load_browser_settings("browser.yaml")
    write_research(tmp_path, swov, {"track_own_brand": False, "ads": {"keywords": ["overalls"]},
                                     "social": {"tiktok": {"hashtags": ["overalls"]}, "instagram": {"hashtags": ["overalls"]}}})
    factory = cli._real_factory(cfg, CREDS, str(tmp_path / "debug"))
    settings = replace(C.load_collector_settings("does-not-exist.yaml"), benchmarks_enabled=False, auto_enabled=False)
    C.run_collect([swov], Store(tmp_path / "data"), browser_factory=factory, creds=CREDS, settings=settings, browser_cfg=cfg,
                  ledger_cls=R.Ledger, sources=["meta_ads", "tiktok", "instagram"], now=NOW, run_id="r", root=str(tmp_path / "brands"))
    assert fake_playwright.log.count("start") == 1 and fake_playwright.log.count("close") == 3 and fake_playwright.log.count("stop") == 1


def test_open_browser_is_a_context_manager_and_the_helper_is_a_plain_function():
    import inspect
    assert hasattr(opened("meta_ads"), "__enter__") and not inspect.isgeneratorfunction(B._start_playwright)
