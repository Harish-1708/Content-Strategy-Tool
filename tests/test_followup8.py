"""Update 73 v6 (follow-up 8): the Instagram login-wall wording, the one shared capture, the password-fallback truth, and the brief improvements from the real production briefs."""
import json
from pathlib import Path

import pytest
import yaml

from contentintel import cli
from contentintel import collectors as C
from contentintel import sessions as S


def test_a_refused_session_is_reported_as_not_accepted_never_as_expired_and_the_reason_no_password_was_tried_is_given(monkeypatch):
    cfg = yaml.safe_load(Path("browser.yaml").read_text(encoding="utf-8"))["sites"]["instagram"]
    monkeypatch.setattr(C, "session_state", lambda b, c: "login_wall")
    monkeypatch.setattr(C, "check_warning", lambda *a, **k: None)

    class Br:
        identity = {"allow_login": False}

        def open(self, url): pass
        def wait(self, s): pass
        def screenshot(self, n): pass
        def click(self, s): return False
    with pytest.raises(C.LoginWall) as e:
        C.ensure_session(Br(), "instagram", {**cfg, "secrets": {"cookies": "INSTAGRAM_COOKIES"}}, {"cookies": '[{"name": "a", "value": "b", "domain": ".instagram.com", "path": "/"}]', "username": "u", "password": "p"})
    msg = str(e.value)
    assert "the site showed its login page, so the session in INSTAGRAM_COOKIES was not accepted from this runner" in msg and "may have expired" not in msg          # Collect #73: zero of ten cookies were expired by date
    assert "cannot tell an expired session from one the site refuses from a datacenter address" in msg and "No password login was tried: the identity is cookie-only" in msg and "Cookie facts (no values shown)" in msg
    never = dict(cfg, password_fallback="never")
    Br.identity = {"allow_login": True}
    with pytest.raises(C.LoginWall) as e2:
        C.ensure_session(Br(), "instagram", {**never, "secrets": {"cookies": "INSTAGRAM_COOKIES"}}, {"cookies": "[]", "username": "u", "password": "p"})
    assert "this site's setting says never" in str(e2.value)


def stored_session_env(platform="instagram"):
    state = {"cookies": [{"name": n, "value": "v", "domain": f".{S.PLATFORMS[platform]['hosts'][0]}", "path": "/", "expires": 4102444800} for n in S.PLATFORMS[platform]["required"]], "origins": []}
    import base64
    return {S.PLATFORMS[platform]["secret"]: base64.b64encode(json.dumps(state).encode()).decode()}


def test_the_collectors_use_the_stored_session_capture_so_one_export_serves_both_routes_and_fall_back_to_their_own_cookies():
    browser_cfg = yaml.safe_load(Path("browser.yaml").read_text(encoding="utf-8"))
    own = '[{"name": "own", "value": "x", "domain": ".instagram.com", "path": "/"}]'
    both = cli._site_creds(browser_cfg, {**stored_session_env(), "INSTAGRAM_COOKIES": own})
    assert both["instagram"]["cookies_from"] == "INSTAGRAM_SESSION_B64" and json.loads(both["instagram"]["cookies"])[0]["name"] in S.PLATFORMS["instagram"]["required"]
    only_own = cli._site_creds(browser_cfg, {"INSTAGRAM_COOKIES": own})
    assert only_own["instagram"]["cookies"] == own and "cookies_from" not in only_own["instagram"]                                          # no stored session: as before
    bad = cli._site_creds(browser_cfg, {"INSTAGRAM_SESSION_B64": "not base64 at all !!", "INSTAGRAM_COOKIES": own})
    assert bad["instagram"]["cookies"] == own and "cookies_from" not in bad["instagram"]                                                  # an unusable capture never replaces working cookies
    assert cli._site_creds(browser_cfg, {})["instagram"]["cookies"] is None
    assert "cookies_from" not in cli._site_creds(browser_cfg, stored_session_env("tiktok"))["instagram"]                                      # a capture belongs to its own platform
    tt = cli._site_creds(browser_cfg, stored_session_env("tiktok"))["tiktok"]
    assert tt["cookies_from"] == "TIKTOK_SESSION_B64"
    assert "read from TIKTOK_SESSION_B64, the stored session the sessions check proves" in C.cookie_facts("tiktok", tt)


def test_the_password_fallback_setting_says_it_is_gated_by_the_stealth_identity_and_instagram_does_have_its_login_steps():
    text = Path("browser.yaml").read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)["sites"]
    assert text.count("applies ONLY when the stealth identity allows a login") == 2
    assert cfg["instagram"].get("login_steps") and cfg["tiktok"].get("login_steps")                                                          # the fallback is intentionally off by the identity gate, not missing steps


def test_an_apify_skip_for_a_missing_key_names_every_key_looked_at_so_it_cannot_be_mistaken_for_a_probe_that_was_never_wired():
    assert cli.NO_APIFY_KEY == "no key: set APIFY_TOKEN (none of APIFY_TOKEN, APIFY_TOKEN_2, APIFY_TOKEN_3, APIFY_TOKEN_4 is set in this step: the skip is about the environment of the step, not about a probe)"
    text = Path("contentintel/cli.py").read_text(encoding="utf-8")
    assert text.count("= NO_APIFY_KEY") == 2 and 'if apify is None:' in text
    wf = Path(".github/workflows/acceptance.yml").read_text(encoding="utf-8")
    assert all(f"APIFY_TOKEN{suffix}: ${{{{ secrets.APIFY_TOKEN{suffix} }}}}" in wf for suffix in ("", "_2", "_3", "_4"))                          # the Acceptance step sets all four names


def test_the_brief_command_and_workflow_take_kind_for_and_offer_and_pass_them_on():
    args = cli.build_parser().parse_args(["brief", "--brand", "swoveralls", "--brief", "x", "--ask", "a", "--kind", "static", "--for", "Sarah", "--offer", "Buy 2+, 30% off. No code needed."])
    assert (args.kind, args.for_person, args.offer) == ("static", "Sarah", "Buy 2+, 30% off. No code needed.")
    assert cli.build_parser().parse_args(["brief", "--brand", "swoveralls", "--brief", "x", "--ask", "a"]).kind == ""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["brief", "--brand", "swoveralls", "--brief", "x", "--ask", "a", "--kind", "reels"])                      # only the four kinds exist
    wf = yaml.safe_load(Path(".github/workflows/brief.yml").read_text(encoding="utf-8"))
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["kind"]["options"] == ["video", "static", "carousel", "cuts"] and inputs["kind"]["default"] == "video" and inputs["for_person"]["default"] == "" and inputs["offer"]["default"] == ""
    run = next(s["run"] for s in wf["jobs"]["brief"]["steps"] if "--ask" in s.get("run", ""))
    assert '--kind "$KIND"' in run and '[ -z "$REVISE" ]' in run and '--for "$FOR_PERSON"' in run and '--offer "$OFFER"' in run


def test_reddit_through_apify_is_on_with_a_lane_setting_and_a_quality_line(monkeypatch, tmp_path):
    from contentintel import apify as AP
    from contentintel import voice as V
    text = Path("research.yaml").read_text(encoding="utf-8")
    assert "reddit_hold: false" in text and "reddit_lane: always" in text and AP.reddit_held("research.yaml") is False and AP.reddit_lane("research.yaml") == "always"
    for value, expect in (("backup", "backup"), ("off", "off"), ("sometimes", "always"), ("ALWAYS", "always")):
        f = tmp_path / f"{value}.yaml"
        f.write_text(f"collectors:\n  apify:\n    reddit_lane: {value}\n", encoding="utf-8")
        assert AP.reddit_lane(str(f)) == expect
    top = tmp_path / "top.yaml"
    top.write_text("apify:\n  reddit_hold: false\n", encoding="utf-8")
    held = tmp_path / "held.yaml"
    held.write_text("collectors:\n  apify:\n    reddit_hold: true\n", encoding="utf-8")
    silent = tmp_path / "silent.yaml"
    silent.write_text("collectors:\n  apify:\n    mode: fallback\n", encoding="utf-8")
    assert AP.reddit_held(str(silent)) is False and AP.reddit_held(str(tmp_path / "missing.yaml")) is True                                       # on by default when the file says nothing (every feature is ON unless the owner holds it); an unreadable file stays on hold
    assert AP.reddit_held(str(top)) is False and AP.reddit_held(str(held)) is True                                                           # the setting is read where it sits: before this fix the file was never consulted
    assert AP.reddit_lane(str(tmp_path / "missing.yaml")) == "always"
    ok, failed = {"reddit": "ok"}, {"reddit": "unreachable"}
    assert V.reddit_wanted(ok, "always") and V.reddit_wanted(failed, "always") and not V.reddit_wanted(ok, "backup") and V.reddit_wanted(failed, "backup") and not V.reddit_wanted(failed, "off") and V.reddit_wanted(ok)
    raw = [{"title": "a", "upVotes": 10, "numberOfComments": 3, "body": "x", "communityName": "r/overalls"}, {"title": "b", "score": 0, "communityName": "r/overalls"}, {"title": "c", "body": "y", "parsedCommunityName": "r/farming"}, "junk"]
    assert V.reddit_quality(raw) == "3 post(s): 1 with a score, 1 with a comment count, 2 with text, 3 with a community (r/overalls, r/farming)" or V.reddit_quality(raw).startswith("3 post(s)")
    assert V.reddit_quality([]) == "0 post(s): 0 with a score, 0 with a comment count, 0 with text, 0 with a community"
    src = Path("contentintel/research_tools.py").read_text(encoding="utf-8")
    assert "reddit_backup=VOICE.reddit_wanted(status)" in src and "apify reddit quality for" in Path("contentintel/voice.py").read_text(encoding="utf-8")
