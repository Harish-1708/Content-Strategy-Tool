import copy
import sys

import pytest
import yaml

from contentintel import browser as B

SHIPPED = yaml.safe_load(open("browser.yaml", encoding="utf-8"))


def load(tmp_path, mutate):
    data = copy.deepcopy(SHIPPED)
    mutate(data)
    path = tmp_path / "browser.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return B.load_browser_settings(path)


def test_the_shipped_browser_settings_load_for_every_site():
    cfg = B.load_browser_settings("browser.yaml")
    assert sorted(cfg["sites"]) == sorted(B.SITES)
    assert cfg["sites"]["meta_ads"]["needs_login"] is False
    assert all(cfg["sites"][s]["needs_login"] for s in ("instagram", "tiktok", "gemini"))
    assert cfg["sites"]["gemini"]["wait"]["max_minutes"] >= 20          # a report takes 10+ minutes
    assert "password" not in cfg["sites"]["gemini"]["secrets"]          # Google blocks automated sign-in


def test_a_missing_settings_file_says_what_it_is_for(tmp_path):
    with pytest.raises(B.BrowserError, match="page addresses and button names"):
        B.load_browser_settings(tmp_path / "nope.yaml")


@pytest.mark.parametrize("name,mutate,match", [
    ("unknown top key", lambda d: d.update(colour=1), "unknown"),
    ("missing pacing key", lambda d: d["pacing"].pop("scroll_pause_seconds"), "pacing needs"),
    ("min above max", lambda d: d["pacing"].update(min_delay_seconds=99), "cannot be more"),
    ("negative pause", lambda d: d["pacing"].update(min_delay_seconds=-1), "min_delay_seconds"),
    ("scroll limit zero", lambda d: d["limits"].update(max_scrolls_per_page=0), "max_scrolls_per_page"),
    ("a site missing", lambda d: d["sites"].pop("tiktok"), "exactly"),
    ("an extra site", lambda d: d["sites"].update(snapchat={}), "exactly"),
    ("unknown site key", lambda d: d["sites"]["instagram"].update(colour="x"), "unknown"),
    ("missing required key", lambda d: d["sites"]["instagram"].pop("hashtag_url"), "missing"),
    ("empty selector list", lambda d: d["sites"]["instagram"].update(logged_in_any=[]), "non-empty list"),
    ("selector not text", lambda d: d["sites"]["instagram"].update(logged_in_any=[3]), "non-empty list"),
    ("plain http address", lambda d: d["sites"]["instagram"].update(home="http://www.instagram.com/"), "https://"),
    ("needs_login not a bool", lambda d: d["sites"]["instagram"].update(needs_login="yes"), "true or false"),
    ("secrets without cookies", lambda d: d["sites"]["instagram"].update(secrets={"username": "U"}), "cookies secret"),
    ("secret name with spaces", lambda d: d["sites"]["instagram"]["secrets"].update(cookies="my secret"), "plain names"),
    ("wait keys wrong", lambda d: d["sites"]["gemini"]["wait"].pop("min_chars"), "exactly"),
    ("wait not a number", lambda d: d["sites"]["gemini"]["wait"].update(poll_seconds="soon"), "poll_seconds"),
    ("clicks not lists", lambda d: d["sites"]["gemini"].update(deep_research_clicks=["Tools"]), "non-empty list"),
    ("two actions in a step", lambda d: d["sites"]["instagram"]["login_steps"].append({"wait": 1, "goto": "https://x"}), "exactly one"),
    ("unknown action", lambda d: d["sites"]["instagram"]["login_steps"].append({"tap": ["x"]}), "exactly one"),
    ("fill without who", lambda d: d["sites"]["instagram"]["login_steps"].append({"fill": ["input"]}), "with: username"),
    ("fill with a stranger", lambda d: d["sites"]["instagram"]["login_steps"].append({"fill": ["i"], "with": "cookie"}), "with: username"),
    ("wait not a number in a step", lambda d: d["sites"]["instagram"]["login_steps"].append({"wait": "x"}), "wait"),
])
def test_bad_browser_settings_are_rejected_with_a_clear_message(tmp_path, name, mutate, match):
    with pytest.raises(B.BrowserError, match=match):
        load(tmp_path, mutate)


def test_cookies_exported_from_a_browser_are_converted():
    exported = ('[{"name": "sessionid", "value": "abc", "domain": ".instagram.com", "path": "/", "secure": true,'
                ' "httpOnly": true, "sameSite": "no_restriction", "expirationDate": 1900000000.5},'
                ' {"name": "csrftoken", "value": "x", "domain": ".instagram.com", "sameSite": "unspecified"}]')
    first, second = B.parse_cookies(exported)
    assert first == {"name": "sessionid", "value": "abc", "domain": ".instagram.com", "path": "/", "secure": True,
                     "httpOnly": True, "sameSite": "None", "expires": 1900000000.5}
    assert "expires" not in second and second["sameSite"] == "Lax" and second["path"] == "/"


def test_a_saved_browser_state_works_too_and_incomplete_cookies_are_skipped():
    state = '{"cookies": [{"name": "a", "value": "1", "domain": ".x.com"}, {"name": "b"}, {"value": "2", "domain": ".x.com"}, {"name": "c", "value": "3"}, "junk"], "origins": []}'
    assert [c["name"] for c in B.parse_cookies(state)] == ["a"]


def test_no_cookies_is_not_an_error_but_bad_cookies_are_and_never_echo_the_secret():
    assert B.parse_cookies("") == [] and B.parse_cookies("   ") == []
    for bad in ("not json at all SECRETVALUE", '{"cookies": "SECRETVALUE"}', '[{"name": "a"}]', "[]"):
        with pytest.raises(B.BrowserError) as info:
            B.parse_cookies(bad)
        assert "SECRETVALUE" not in str(info.value)


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', [{"a": 1}]),
    ('for (;;);{"a": 1}', [{"a": 1}]),
    ('{"a": 1}\n{"b": 2}\n', [{"a": 1}, {"b": 2}]),
    ('for (;;);{"a": 1}\nnot json\n[1, 2]', [{"a": 1}, [1, 2]]),
    ("<html>nothing</html>", []),
    ("", []),
    (None, []),
])
def test_response_bodies_are_read_whatever_their_wrapping(text, expected):
    assert B.parse_json_text(text) == expected


def test_a_missing_playwright_is_explained(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(B.BrowserError, match="Playwright is not installed"):
        with B.open_browser("instagram", {}, {}, "", tmp_path):
            pass


def test_unusable_cookies_stop_the_browser_before_anything_starts(tmp_path):
    with pytest.raises(B.BrowserError):
        with B.open_browser("instagram", {}, {}, "this is not json", tmp_path):
            pytest.fail("the browser should not have started")
