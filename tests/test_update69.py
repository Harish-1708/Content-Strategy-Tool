"""Update 69: the approved changes to the stealth layer (the read log without the layer, the dwell from visible text, one egress lookup per route) and the Collect check ceiling."""
import yaml
from pathlib import Path

from contentintel import browser as B
from contentintel import collectors as C
from contentintel import stealth as S

from test_stealth import PACING, FakePage, FakePW, FakeRequestContext


class CountingRequest:
    def __init__(self, ip):
        self.ip, self.calls = ip, 0

    def get(self, url, timeout=0):
        self.calls += 1
        if self.ip is None:
            raise TimeoutError("slow")

        class Reply:
            def __init__(self, text):
                self._text = text

            def text(self):
                return self._text
        return Reply(self.ip)


class Ctx:
    def __init__(self, ip):
        self.request = CountingRequest(ip)


class PageWithIp:
    def __init__(self, ip="203.0.113.9"):
        self.context = Ctx(ip)


def test_the_egress_ip_is_looked_up_once_per_route_and_a_failure_is_not_retried_at_every_open(monkeypatch):
    monkeypatch.setattr(S, "_EGRESS", {})
    page = PageWithIp()
    assert [S.resolve_egress_ip(page, key="direct") for _ in range(4)] == ["203.0.113.9"] * 4 and page.context.request.calls == 1
    assert S.resolve_egress_ip(page, key="proxy.example:8000") == "203.0.113.9" and page.context.request.calls == 2          # another route: its own lookup
    down = PageWithIp(ip=None)
    assert [S.resolve_egress_ip(down, key="direct2") for _ in range(3)] == [None] * 3 and down.context.request.calls == 1      # a miss is kept for a minute
    clock = {"t": 1000.0}
    monkeypatch.setattr(S.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(S, "_EGRESS", {})
    again = PageWithIp()
    S.resolve_egress_ip(again, key="k")
    clock["t"] += 901
    S.resolve_egress_ip(again, key="k")
    assert again.context.request.calls == 2                                                                                    # a found address is kept for 15 minutes, then read again
    unkeyed = PageWithIp()
    S.resolve_egress_ip(unkeyed), S.resolve_egress_ip(unkeyed)
    assert unkeyed.context.request.calls == 2                                                                                  # with no key nothing is cached (the old behaviour)


def test_the_read_log_works_with_the_layer_switched_off_and_names_the_route(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "_EGRESS", {})
    calls, page = [], FakePage()
    monkeypatch.setattr(B, "_start_playwright", lambda: FakePW(calls, page))
    cfg = {"json_url_hints": [], "stealth": {**S.resolve(None), "enabled": False}}
    with B.open_browser("instagram", cfg, PACING, "", tmp_path) as browser:
        assert browser.identity is None and browser.read_log is not None
        browser.open("https://example.test/a")
    summary = browser.read_log.summary()
    assert summary["engine"] == "plain" and summary["identity"] is None and summary["proxy"] is None and summary["egress_ip"] == "203.0.113.7" and summary["pages"] == 1
    assert (tmp_path / "instagram" / "read-log.jsonl").exists()
    off = {"json_url_hints": [], "stealth": {**S.resolve(None), "enabled": False, "log_reads": False}}
    with B.open_browser("instagram", off, PACING, "", tmp_path) as browser2:
        assert browser2.read_log is None                                                                                       # the owner can still switch the log off


def test_the_run_report_names_a_read_without_the_layer_as_a_read_log_not_as_stealth():
    stats = {"stealth": {"pages": 3, "identity": None, "engine": "plain", "proxy": None, "egress_ip": "203.0.113.7", "human_score": 35}}
    text = C._page_notes(stats)
    assert "read log: 3 page(s) read through plain (stealth off) with no proxy; egress IP 203.0.113.7; human score 35/100" in text and "stealth:" not in text
    stats["stealth"].update(identity="instagram", engine="patchright")
    assert " - stealth: 3 page(s) read as instagram through patchright with no proxy; human score 35/100" in C._page_notes(stats)


class DwellHuman:
    def __init__(self):
        self.chars = None
        self.pauses = self.scrolls = self.mouse_moves = 0

    def before_page(self, pacing):
        pass

    def after_load(self, page, content_chars=0):
        self.chars = content_chars


class TextPage(FakePage):
    def __init__(self, visible, html):
        super().__init__(content=html)
        self._visible = visible

    def inner_text(self, selector):
        assert selector == "body"
        return self._visible


def test_the_dwell_is_worked_out_from_the_text_a_person_can_read_not_from_the_html(tmp_path):
    human = DwellHuman()
    browser = B.PlaywrightBrowser(TextPage("word " * 200, "<div>" * 60000), tmp_path, PACING, [], human=human)
    browser.open("https://example.test/a")
    assert human.chars == 1000                                                                                                # 200 words, not 300,000 characters of markup
    human2 = DwellHuman()
    broken = FakePage(content="x" * 500)                                                                                       # no inner_text: the page's content is the fallback
    B.PlaywrightBrowser(broken, tmp_path, PACING, [], human=human2).open("https://example.test/b")
    assert human2.chars == 500


def test_the_collect_check_ceiling_ships_at_fifty_cents_so_the_tiktok_actor_can_be_checked():
    assert C.load_collector_settings("research.yaml").apify_check_max_cost_usd == 0.5 == C.load_collector_settings("none.yaml").apify_check_max_cost_usd
    assert yaml.safe_load(Path("research.yaml").read_text(encoding="utf-8"))["collectors"]["apify"]["check_max_cost_usd"] == 0.5


def test_the_two_engine_tests_no_longer_depend_on_the_machine():
    body = Path("tests/test_stealth.py").read_text(encoding="utf-8")
    assert body.count('monkeypatch.setattr(S, "_importable"') == 2 and body.count('monkeypatch.delenv("DISPLAY", raising=False)') >= 4
    assert FakeRequestContext("203.0.113.7") is not None
