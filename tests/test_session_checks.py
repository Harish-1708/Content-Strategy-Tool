"""Telling a signed-out page from a signed-in one, naming what is wrong with a cookies secret, and the one click on a
security check's own X button.

Built from what the 2026-10-21 Check and Recon runs really saw: Gemini's signed-out page (which still shows the prompt
box) reported as "login looks ok", and TikTok's slider puzzle with an X, over a page of empty placeholders.
"""
import re
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from contentintel import browser as B
from contentintel import collectors as C
from contentintel.browser import Challenge

from fake_browser import FakeBrowser, GeminiFake
from test_collectors import CREDS, DEEP, NOW, collect, site, world

FIXTURES = Path(__file__).parent / "fixtures"
SECRET = "SECRETVALUE-do-not-print-0123456789"


@pytest.fixture
def cfg():
    return B.load_browser_settings("browser.yaml")


# ---- a tiny selector matcher, enough to test the real selectors against the real markup -------------------------

class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, dict(attrs)))


def matches(selector, html):
    """tag, tag[attr="v"], tag[attr*="v"], [attr...] and #id: the only forms the Gemini and TikTok settings use."""
    parser = _Elements()
    parser.feed(html)
    m = re.fullmatch(r'(?P<tag>[a-z][\w-]*)?(?:#(?P<id>[\w-]+))?(?:\[(?P<attr>[\w-]+)(?P<op>\*?=)"(?P<val>[^"]*)"(?P<ci>\s+i)?\])?', selector)
    assert m, f"selector form not supported by this test: {selector}"
    for tag, attrs in parser.found:
        if m["tag"] and tag != m["tag"] or m["id"] and attrs.get("id") != m["id"]:
            continue
        if m["attr"]:
            have, want = attrs.get(m["attr"]), m["val"]
            if have is not None and m["ci"]:
                have, want = have.lower(), want.lower()
            if have is None or (m["op"] == "=" and have != want) or (m["op"] == "*=" and want not in have):
                continue
        return True
    return False


GEMINI_PAGE = (FIXTURES / "gemini_signed_out_excerpt.html").read_text(encoding="utf-8")
TIKTOK_PAGE = (FIXTURES / "tiktok_challenge_excerpt.html").read_text(encoding="utf-8")


def test_the_real_signed_out_gemini_page_matches_both_the_logged_in_and_the_signed_out_selectors(cfg):
    g = cfg["sites"]["gemini"]
    assert any(matches(s, GEMINI_PAGE) for s in g["logged_in_any"])          # the prompt box: this fooled the old check
    assert any(matches(s, GEMINI_PAGE) for s in g["signed_out_any"])         # the Sign in link: this is the truth


def test_the_gemini_sign_in_selectors_name_google_itself(cfg):
    assert cfg["sites"]["gemini"]["signed_out_any"] == ['a[href*="accounts.google.com/ServiceLogin"]', 'a[aria-label="Sign in"]']


def test_the_real_tiktok_puzzle_has_the_close_button_the_settings_name(cfg):
    t = cfg["sites"]["tiktok"]
    assert t["challenge_dismiss"] == ["#captcha_close_button"] and all(matches(s, TIKTOK_PAGE) for s in t["challenge_dismiss"])
    assert "text=Drag the slider" in t["challenge_any"] and "Drag the slider to fit the puzzle" in TIKTOK_PAGE


# ---- the session state --------------------------------------------------------------------------------------

def gemini_browser(cfg, *, prompt_box=True, sign_in=False, challenge=False):
    g = cfg["sites"]["gemini"]
    here = set()
    if prompt_box:
        here |= {g["logged_in_any"][0], g["prompt_box"][0], g["deep_research_active_any"][0]}
    if sign_in:
        here.add(g["signed_out_any"][0])
    if challenge:
        here.add(g["challenge_any"][0] if g.get("challenge_any") else "x")
    flat = {s for group in g["deep_research_clicks"] for s in group}
    return FakeBrowser(present_always=here, clickable=flat)


def test_a_signed_out_gemini_page_is_a_login_wall_even_though_the_prompt_box_is_showing(cfg):
    g = cfg["sites"]["gemini"]
    assert C.session_state(gemini_browser(cfg, prompt_box=True, sign_in=True), g) == "login_wall"


def test_a_signed_in_gemini_page_is_still_ok(cfg):
    assert C.session_state(gemini_browser(cfg, prompt_box=True, sign_in=False), cfg["sites"]["gemini"]) == "ok"


def test_a_page_with_neither_the_prompt_box_nor_a_sign_in_link_is_unknown(cfg):
    assert C.session_state(gemini_browser(cfg, prompt_box=False, sign_in=False), cfg["sites"]["gemini"]) == "unknown"


def test_a_site_without_signed_out_selectors_behaves_exactly_as_before(cfg):
    site = dict(cfg["sites"]["instagram"])
    assert "signed_out_any" not in site
    both = FakeBrowser(present_always={site["logged_in_any"][0], site["login_wall_any"][0]})
    assert C.session_state(both, site) == "ok"                                   # the old order: logged-in markers first


def test_a_security_check_still_beats_everything_else(cfg):
    g = {**cfg["sites"]["gemini"], "challenge_any": ["#puzzle"]}
    b = FakeBrowser(present_always={g["logged_in_any"][0], g["signed_out_any"][0], "#puzzle"})
    assert C.session_state(b, g) == "challenge"


# ---- the check no longer calls a signed-out Gemini "ok" -----------------------------------------------------------

def run_check(cfg, tmp_path, browser, creds):
    from contentintel import research as R
    from contentintel.store import Store
    from test_collectors import world as _world
    f = _world(cfg)
    f.browsers["gemini"] = browser
    store = Store(tmp_path / "data")
    settings = C.load_collector_settings("does-not-exist.yaml")
    rows = C.check_collectors(f, creds, settings, cfg, store, R.Ledger, NOW, ["deep_research"])
    return {r["source"]: r for r in rows}


def google_cookies(names, domain=".google.com", value=SECRET, expires=None):
    import json
    return json.dumps([{"name": n, "value": value, "domain": domain, "path": "/", "secure": True, "httpOnly": True,
                        **({"expirationDate": expires} if expires else {})} for n in names])


def test_the_check_reports_a_signed_out_gemini_as_not_logged_in_and_names_the_cookies_it_had(cfg, tmp_path):
    creds = {"gemini": {"cookies": google_cookies(["NID", "__Secure-1PSIDTS", "OTZ"])}}
    row = run_check(cfg, tmp_path, gemini_browser(cfg, prompt_box=True, sign_in=True), creds)["deep_research"]
    assert row["ok"] is False
    assert "not logged in" in row["detail"] and "Gemini shows its Sign in button" in row["detail"]
    assert "3 cookies loaded (.google.com x3)" in row["detail"]
    assert "Google sign-in cookies missing: SID, __Secure-1PSID, __Secure-3PSID, SAPISID" in row["detail"]
    assert SECRET not in row["detail"]


def test_the_check_still_passes_a_signed_in_gemini(cfg, tmp_path):
    creds = {"gemini": {"cookies": google_cookies(["SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID"])}}
    row = run_check(cfg, tmp_path, gemini_browser(cfg, prompt_box=True, sign_in=False), creds)["deep_research"]
    assert row["ok"] is True and "login looks ok" in row["detail"]


def test_the_deep_research_test_stops_at_the_login_wall_with_the_cookie_facts(cfg, tmp_path):
    from contentintel import research as R
    from contentintel.store import Store
    f = world(cfg)
    f.browsers["gemini"] = gemini_browser(cfg, prompt_box=True, sign_in=True)
    creds = {"gemini": {"cookies": google_cookies(["NID"])}}
    out = C.run_deep_research_test(f, creds, cfg, C.load_collector_settings("does-not-exist.yaml"), Store(tmp_path / "data"),
                                   R.Ledger, NOW, None, tmp_path / "debug")
    assert out["ok"] is False and "not logged in" in out["detail"] and "button to switch to Deep Research" not in out["detail"]
    assert "Cookie facts (no values shown): 1 cookies loaded" in out["detail"] and SECRET not in out["detail"]
    assert "login-wall-gemini" in f.browsers["gemini"].shots


# ---- describing a cookies secret without ever printing a value ------------------------------------------------------

def test_the_description_counts_cookies_and_domains_and_never_shows_a_value():
    text = google_cookies(["A1", "B2"], ".google.com") [:-1] + "," + google_cookies(["C3"], "gemini.google.com")[1:]
    out = B.describe_cookies(text, "instagram")
    assert out == "3 cookies loaded (.google.com x2, gemini.google.com x1)" and SECRET not in out


def test_expired_cookies_are_counted():
    now = time.time()
    text = google_cookies(["old1", "old2"], expires=now - 3600)[:-1] + "," + google_cookies(["fresh"], expires=now + 86400)[1:]
    assert "3 cookies loaded" in B.describe_cookies(text, "tiktok") and "2 already expired" in B.describe_cookies(text, "tiktok")
    assert "expired" not in B.describe_cookies(google_cookies(["fresh"], expires=now + 86400), "tiktok")
    assert "expired" not in B.describe_cookies(google_cookies(["session-only"]), "tiktok")        # no expiry: a session cookie


def test_a_google_secret_missing_its_sign_in_cookies_says_which():
    some = B.describe_cookies(google_cookies(["SID", "SAPISID", "NID"]), "gemini")
    assert "Google sign-in cookies missing: __Secure-1PSID, __Secure-3PSID (export again and include hidden/HttpOnly cookies)" in some
    assert "missing" not in B.describe_cookies(google_cookies(list(B.GOOGLE_SIGNIN_COOKIES) + ["NID"]), "gemini")
    assert "missing" not in B.describe_cookies(google_cookies(["NID"]), "instagram")             # only Google has that list


@pytest.mark.parametrize("text,expect", [("", "no cookies secret is set"), ("   ", "no cookies secret is set"),
                                         ("not json", "could not be read"), ("{}", "no usable cookies were found"),
                                         ('[{"name": "x"}]', "no usable cookies were found")])
def test_an_unusable_secret_is_described_not_crashed_on(text, expect):
    assert expect in B.describe_cookies(text, "gemini")


def test_cookie_facts_are_a_sentence_or_nothing():
    assert C.cookie_facts("gemini", {}) == "" and C.cookie_facts("gemini", None) == "" and C.cookie_facts("gemini", {"cookies": ""}) == ""
    assert C.cookie_facts("gemini", {"cookies": google_cookies(["NID"])}).startswith(" Cookie facts (no values shown): 1 cookies loaded")


# ---- the security check's own X button -------------------------------------------------------------------------------

class PuzzleBrowser(FakeBrowser):
    """TikTok with a puzzle in the way. `closable` says whether pressing the X really removes it."""

    def __init__(self, cfg, *, closable, then_logged_in=True):
        t = cfg["sites"]["tiktok"]
        super().__init__(clickable={"#captcha_close_button"})
        self.t, self.closable, self.then_logged_in, self.puzzle = t, closable, then_logged_in, True

    def on_click(self, selector):
        if selector == "#captcha_close_button" and self.closable:
            self.puzzle = False

    def dynamic_present(self):
        if self.puzzle:
            return {self.t["challenge_any"][0]}
        return {self.t["logged_in_any"][0]} if self.then_logged_in else set()


def test_a_puzzle_with_a_working_x_is_closed_once_and_the_session_carries_on(cfg):
    b = PuzzleBrowser(cfg, closable=True)
    state = C.ensure_session(b, "tiktok", {**cfg["sites"]["tiktok"], "home": "https://www.tiktok.com/"}, {})
    assert state == "ok" and b.clicks == ["#captcha_close_button"] and b.shots == []
    assert b.waits.count(3) == 2                                  # the usual settle after opening, and one after pressing X


def test_a_puzzle_that_the_x_does_not_remove_still_stops_the_run_and_says_closing_did_not_help(cfg):
    b = PuzzleBrowser(cfg, closable=False)
    with pytest.raises(Challenge, match=r"security check .*\(closing it with its X button did not clear it\)"):
        C.ensure_session(b, "tiktok", {**cfg["sites"]["tiktok"], "home": "https://www.tiktok.com/"}, {"cookies": "x"})
    assert b.clicks == ["#captcha_close_button"] and "challenge-tiktok" in b.shots     # pressed ONCE, not retried


def test_a_site_with_no_close_button_configured_is_not_clicked_and_its_message_is_unchanged(cfg):
    t = {k: v for k, v in cfg["sites"]["tiktok"].items() if k != "challenge_dismiss"}
    b = PuzzleBrowser(cfg, closable=True)
    with pytest.raises(Challenge) as err:
        C.ensure_session(b, "tiktok", {**t, "home": "https://www.tiktok.com/"}, {})
    assert b.clicks == [] and "closing it" not in str(err.value)


def test_clearing_reports_false_when_there_is_nothing_to_press(cfg):
    t = cfg["sites"]["tiktok"]
    assert C.clear_challenge(FakeBrowser(), t) is False                           # the X is not on the page
    assert C.clear_challenge(FakeBrowser(clickable={"#captcha_close_button"}), {k: v for k, v in t.items() if k != "challenge_dismiss"}) is False


# (a puzzle or an error appearing while collecting is covered in test_tiktok_puzzle_and_search.py)


# ---- the same facts wherever a login wall is reported -----------------------------------------------------------------

def test_a_login_wall_while_setting_up_a_session_names_the_cookies_that_were_loaded(cfg):
    ig = {**cfg["sites"]["instagram"], "home": "https://www.instagram.com/"}
    wall = FakeBrowser(present_always={ig["login_wall_any"][0]})
    cookies = google_cookies(["sessionid", "csrftoken"], ".instagram.com")
    with pytest.raises(Exception, match=r"not logged in.*Cookie facts \(no values shown\): 2 cookies loaded \(.instagram.com x2\)") as err:
        C.ensure_session(wall, "instagram", ig, {"cookies": cookies})
    assert SECRET not in str(err.value)
    with pytest.raises(Exception) as bare:
        C.ensure_session(FakeBrowser(present_always={ig["login_wall_any"][0]}), "instagram", ig, {})
    assert "Cookie facts" not in str(bare.value)


def test_an_expired_google_login_in_a_real_collection_run_carries_the_facts_into_the_report(tmp_path, swov, cfg):
    from contentintel.store import Store
    gem = GeminiFake(site(cfg, "gemini"), logged_in=False)
    creds = {**CREDS, "gemini": {"cookies": google_cookies(["NID"])}}
    settings = C.load_collector_settings("does-not-exist.yaml")
    result = collect(Store(tmp_path / "data"), tmp_path, [swov], DEEP, world(cfg, gem=gem), cfg, settings,
                     creds=creds, sources=("deep_research",))
    detail = result.brands["swoveralls"][0].detail
    assert "GEMINI_COOKIES" in detail and "Cookie facts (no values shown): 1 cookies loaded" in detail
    assert detail in result.notes and SECRET not in detail


def test_clearing_says_whether_the_check_is_really_gone_after_the_x_was_pressed(cfg):
    t = cfg["sites"]["tiktok"]
    works, stuck = PuzzleBrowser(cfg, closable=True), PuzzleBrowser(cfg, closable=False)
    assert C.clear_challenge(works, t) is True and works.clicks == ["#captcha_close_button"]
    assert C.clear_challenge(stuck, t) is False and stuck.clicks == ["#captcha_close_button"]
