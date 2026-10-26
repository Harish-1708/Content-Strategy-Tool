"""What the real run does when a TikTok tag page will not load: close the puzzle's X once, then search for the tag
the way a person does (click the box, type "#tag", press Enter), and look again if the error only appears after the
page has been read once. Modelled on the 2026-10-21 runs: the tag page showed "Something went wrong" in Check and a
slider puzzle in Recon, while a hand search for #overalls worked.
"""
import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel.browser import Challenge

from fake_browser import FakeBrowser, TT_JSON

TAG = "https://www.tiktok.com/tag/overalls"
SEARCH_ADDRESS = "https://www.tiktok.com/search?q=%23overalls"
BOX = 'input[data-e2e="search-user-input"]'
X = "#captcha_close_button"


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


class TikTok(FakeBrowser):
    """A TikTok whose pages can show a puzzle or an error. `puzzle_on` / `error_on` name the pages that do
    ("tag", "typed", "address"); `closable` says whether the puzzle's X really closes it; `late_error` makes the tag
    page's error appear only after the page has been scrolled once, as when it was still loading."""

    def __init__(self, cfg, *, puzzle_on=(), error_on=(), closable=False, box=True, late_error=False, data_on=("typed", "address"),
                 reappear=0):
        t = cfg["sites"]["tiktok"]
        super().__init__(clickable={X}, typeable={BOX} if box else set(), present_always={t["logged_in_any"][0]})
        self.t, self.puzzle_on, self.error_on = t, set(puzzle_on), set(error_on)
        self.closable, self.late_error, self.data_on = closable, late_error, set(data_on)
        self.page, self.closed, self.typed_queries = "other", set(), []
        self.reappear, self.checks_after_close, self.scroll_pages = reappear, 0, []

    def _kind(self, url):
        return "tag" if "/tag/" in url else "address" if "/search" in url else "other"

    def open(self, url):
        super().open(url)
        self.page = self._kind(url)
        self._load()

    def type_text(self, selectors, text, submit=False):
        ok = super().type_text(selectors, text, submit)
        if ok and submit:
            self.typed_queries.append(text)
            self.page = "typed"
            self._load()
        return ok

    def _load(self):
        self.spec = {"json": [TT_JSON]} if self.page in self.data_on or (self.page == "tag" and "tag" in self.data_on) else {}
        self._json = list(self.spec.get("json", []))

    def scroll(self, times=1, pause=3):
        self.scroll_pages.append(self.page)
        super().scroll(times, pause)

    def on_click(self, selector):
        if selector == X and self.closable:
            self.closed.add(self.page)

    def exists(self, selectors):
        if self.reappear and self.page in self.puzzle_on and self.page in self.closed and self.t["challenge_any"][0] in selectors:
            self.checks_after_close += 1                  # only the looks at the puzzle count: it seems closed for `reappear` of them
        return super().exists(selectors)

    def dynamic_present(self):
        out = set()
        if self.page in self.puzzle_on and self.page in self.closed and self.reappear:
            if self.checks_after_close > self.reappear:   # ... and then it comes back
                out.add(self.t["challenge_any"][0])
        elif self.page in self.puzzle_on and self.page not in self.closed:
            out.add(self.t["challenge_any"][0])
        shows_error = self.page in self.error_on and (not self.late_error or self.scrolls >= 1)
        if shows_error:
            out.add(self.t["error_any"][0])
        return out


def read(cfg, browser):
    task = C.Task("tiktok", "tags", {"hashtags": ["overalls"]}, "k")
    return C.run_browser_task(browser, task, 10, {**cfg["sites"]["tiktok"], "settle_seconds": 0}, cfg, {})


def test_a_puzzle_that_the_x_closes_lets_the_tag_page_be_read_and_nothing_is_searched(cfg):
    b = TikTok(cfg, puzzle_on={"tag"}, closable=True, data_on={"tag"})
    rows, stats = read(cfg, b)
    assert len(rows) == 2 and b.clicks == [X] and b.typed_queries == [] and stats["fallbacks"] == []


def test_a_puzzle_the_x_cannot_close_sends_the_tool_to_search_by_typing_and_that_works(cfg):
    b = TikTok(cfg, puzzle_on={"tag"}, closable=False, data_on={"typed"})
    rows, stats = read(cfg, b)
    assert len(rows) == 2 and b.typed_queries == ["#overalls"] and SEARCH_ADDRESS not in b.opened
    assert b.clicks == [X]                                                      # the X was pressed once, on the page that had the puzzle
    assert stats["fallbacks"] == ["#overalls"] and stats["fallback_how"] == ["typed"] and stats["page_errors"] == 0
    assert all(r["found_via"] == "#overalls" for r in rows)


def test_when_the_search_page_has_a_puzzle_too_the_x_is_pressed_once_on_it_and_the_run_stops_saying_so(cfg):
    b = TikTok(cfg, puzzle_on={"tag", "typed"}, closable=False)
    with pytest.raises(Challenge, match=r"security check .*\(closing it with its X button did not clear it\)"):
        read(cfg, b)
    assert b.clicks == [X, X] and b.typed_queries == ["#overalls"]               # one press per page, never a retry on the same page
    assert "challenge-tiktok" in b.shots


def test_a_puzzle_on_the_tag_page_that_the_x_closes_on_the_search_page_only_is_read_from_search(cfg):
    class ClosableOnSearch(TikTok):
        def on_click(self, selector):
            if selector == X and self.page == "typed":
                self.closed.add("typed")
    b = ClosableOnSearch(cfg, puzzle_on={"tag", "typed"}, data_on={"typed"})
    rows, stats = read(cfg, b)
    assert len(rows) == 2 and b.clicks == [X, X] and stats["fallback_how"] == ["typed"]


def test_with_no_search_box_found_the_search_address_is_opened_instead(cfg):
    b = TikTok(cfg, puzzle_on={"tag"}, box=False, data_on={"address"})
    rows, stats = read(cfg, b)
    assert len(rows) == 2 and SEARCH_ADDRESS in b.opened and b.typed_queries == [] and stats["fallback_how"] == ["address"]


def test_a_tag_page_error_is_replaced_by_the_typed_search_without_first_reading_the_failing_page(cfg):
    b = TikTok(cfg, error_on={"tag"}, data_on={"typed"})
    rows, stats = read(cfg, b)
    assert len(rows) == 2 and stats["fallbacks"] == ["#overalls"] and b.clicks == [] and b.typed_queries == ["#overalls"]
    assert "tag" not in b.scroll_pages and "typed" in b.scroll_pages           # an error seen at once is never scrolled through


def test_an_error_that_only_appears_after_the_first_read_is_still_caught(cfg):
    """The real Check showed no note at all: the error came after the first wait, so it was looked for too early."""
    b = TikTok(cfg, error_on={"tag"}, late_error=True, data_on={"typed"})
    rows, stats = read(cfg, b)
    assert b.scrolls >= 1 and b.typed_queries == ["#overalls"] and "tag" in b.scroll_pages          # it WAS read first, then replaced
    assert len(rows) == 2 and stats["fallbacks"] == ["#overalls"] and stats["page_errors"] == 0


def test_a_late_error_that_search_cannot_fix_is_counted_and_nothing_is_invented(cfg):
    b = TikTok(cfg, error_on={"tag", "typed"}, late_error=True, data_on=())
    rows, stats = read(cfg, b)
    assert rows == [] and stats["fallbacks"] == ["#overalls"] and stats["page_errors"] == 1 and "empty-tiktok" in b.shots


def test_a_working_tag_page_is_read_as_before_with_no_clicks_typing_or_notes(cfg):
    b = TikTok(cfg, data_on={"tag"})
    rows, stats = read(cfg, b)
    assert len(rows) == 2 and b.clicks == [] and b.typed_queries == [] and stats["fallbacks"] == [] and C._page_notes(stats) == ""


def test_a_tag_page_with_no_posts_and_no_error_is_not_searched(cfg):
    b = TikTok(cfg, data_on=())
    rows, stats = read(cfg, b)
    assert rows == [] and b.typed_queries == [] and stats["fallbacks"] == []


def test_the_search_is_used_only_when_the_setting_allows_it(cfg):
    task = C.Task("tiktok", "tags", {"hashtags": ["overalls"]}, "k")
    b = TikTok(cfg, error_on={"tag"}, data_on={"typed"})
    site = {**cfg["sites"]["tiktok"], "settle_seconds": 0, "hashtag_via_search_on_error": False}
    rows, stats = C.run_browser_task(b, task, 10, site, cfg, {})
    assert rows == [] and b.typed_queries == [] and stats["page_errors"] == 1


def test_a_profile_page_that_errors_is_never_replaced_by_a_tag_search(cfg):
    task = C.Task("tiktok", "profile", {"hashtags": [], "profiles": ["acme"]}, "k")
    b = TikTok(cfg, error_on={"other"}, data_on=())
    rows, stats = C.run_browser_task(b, task, 10, {**cfg["sites"]["tiktok"], "settle_seconds": 0}, cfg, {})
    assert stats["fallbacks"] == [] and b.typed_queries == [] and stats["page_errors"] == 1


# ---- searching the way a person does ---------------------------------------------------------------------------------

def test_searching_types_the_hash_and_the_tag_into_the_box_and_presses_enter(cfg):
    b = TikTok(cfg)
    assert C.search_for_tag(b, cfg["sites"]["tiktok"], "overalls") == "typed" and b.typed_queries == ["#overalls"]


def test_a_tag_with_accents_is_typed_as_written_and_encoded_only_in_the_address(cfg):
    typed, addressed = TikTok(cfg), TikTok(cfg, box=False)
    C.search_for_tag(typed, cfg["sites"]["tiktok"], "café")
    C.search_for_tag(addressed, cfg["sites"]["tiktok"], "café")
    assert typed.typed_queries == ["#café"] and addressed.opened == ["https://www.tiktok.com/search?q=%23caf%C3%A9"]


def test_the_search_box_setting_is_the_one_on_the_real_page(cfg):
    real = open("tests/fixtures/tiktok_search_box_excerpt.html", encoding="utf-8").read()
    assert cfg["sites"]["tiktok"]["search_box"][0] == BOX and 'data-e2e="search-user-input"' in real and 'type="search"' in real
    from test_session_checks import matches
    assert matches(BOX, real) and matches('input[type="search"]', real) is True
    assert 'name="q"' in real                                                  # the second selector's other half


def test_recon_searches_the_same_way_a_run_does(cfg, tmp_path):
    from test_collectors import CREDS, world
    f = world(cfg)
    f.browsers["tiktok"] = TikTok(cfg, data_on={"typed", "tag"})
    lines = C.recon(f, CREDS, cfg, ["tiktok"], tmp_path / "debug")
    assert any(l.startswith("  tiktok (search):") for l in lines)
    assert f.browsers["tiktok"].typed_queries == ["#overalls"]
    for name in ("recon-tiktok-search-network.json", "recon-tiktok-search-page-data.json"):
        assert (tmp_path / "debug" / name).is_file(), name


def test_a_puzzle_that_comes_back_after_the_x_is_not_pressed_a_second_time_on_the_same_page(cfg):
    b = TikTok(cfg, puzzle_on={"tag"}, closable=True, reappear=2, data_on=())
    with pytest.raises(Challenge, match="closing it with its X button did not clear it"):
        read(cfg, b)
    assert b.clicks == [X] and b.typed_queries == []                          # pressed once; it looked cleared, so no search, then it came back
