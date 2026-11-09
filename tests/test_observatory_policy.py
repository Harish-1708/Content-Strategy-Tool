"""The crawl policy, the recorded overrides and the owner's request controls (every one a setting, none behind an approval)."""
import pytest
import yaml

from contentintel import contract as K
from contentintel.observatory import gatecheck as GC
from contentintel.observatory import config as OC
from observatory_helpers import Web, client, reply, settings

SHOP = "https://shop.example"
CLOSED = "User-agent: *\nDisallow: /private\n"


def load(tmp_path, data):
    p = tmp_path / "o.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return OC.load_settings(p)


REC = {"host": "shop.example", "basis": "owner", "approved_by": "Harish", "date": "2026-11-03"}


# ---- configuration is strict, and every exception is a recorded decision -------------------------------------------------------------------------

def test_the_defaults_obey_robots_for_the_crawler_and_treat_it_as_advice_for_readers_and_authorise_nothing():
    s = OC.load_settings("missing.yaml")
    assert s["crawl_policy"] == {"robots": "obey", "reader_robots": "advisory", "basis": "", "overrides": []}
    assert "authorised_access" not in s and s["advanced"] == {"proxies": [], "rotate": "none"} and s["readers"]["jina"] is True and s["readers"]["exa"] is True
    assert s["discovery"]["auto_monitor_max"] == 6 and s["discovery"]["max_monitored"] == 10                         # the confirmed allocation


def test_advisory_robots_for_the_crawler_needs_a_written_basis(tmp_path):
    with pytest.raises(OC.ObservatoryConfigError, match="needs crawl_policy.basis"):
        load(tmp_path, {"crawl_policy": {"robots": "advisory"}})
    ok = load(tmp_path, {"crawl_policy": {"robots": "advisory", "basis": "Harish approved reading public pages on 2026-11-03"}})
    assert ok["crawl_policy"]["robots"] == "advisory"
    with pytest.raises(OC.ObservatoryConfigError, match="must be one of obey, advisory"):
        load(tmp_path, {"crawl_policy": {"robots": "ignore"}})


@pytest.mark.parametrize("entry, fragment", [
    ({**REC, "basis": "because"}, "basis must be one of"), ({k: v for k, v in REC.items() if k != "approved_by"}, "needs approved_by"),
    ({**REC, "date": "3 Nov"}, "date must look like"), ({**REC, "host": "not a host"}, "not a web site name"), ({**REC, "robots": "maybe"}, "robots must be"),
    ({**REC, "colour": 1}, "unknown setting")])
def test_a_robots_override_must_be_a_complete_valid_recorded_decision(tmp_path, entry, fragment):
    with pytest.raises(OC.ObservatoryConfigError, match=fragment):
        load(tmp_path, {"crawl_policy": {"overrides": [entry]}})


def test_a_valid_override_and_duplicates(tmp_path):
    s = load(tmp_path, {"crawl_policy": {"overrides": [{**REC, "basis": "public_page_reader", "robots": "ignore"}]}})
    assert s["crawl_policy"]["overrides"][0]["host"] == "shop.example" and s["crawl_policy"]["overrides"][0]["robots"] == "ignore"
    with pytest.raises(OC.ObservatoryConfigError, match="listed twice"):
        load(tmp_path, {"crawl_policy": {"overrides": [REC | {"basis": "owner_decision"}, REC | {"basis": "owner_decision"}]}})


def test_proxy_addresses_never_live_in_the_file(tmp_path):
    ok = load(tmp_path, {"advanced": {"proxies": ["eu1", "us_2"], "rotate": "sequential"}})
    assert ok["advanced"] == {"proxies": ["eu1", "us_2"], "rotate": "sequential"}
    with pytest.raises(OC.ObservatoryConfigError, match="never from this file"):
        load(tmp_path, {"advanced": {"proxies": ["http://user:pw@1.2.3.4:8080"]}})
    with pytest.raises(OC.ObservatoryConfigError, match="rotate"):
        load(tmp_path, {"advanced": {"rotate": "chaos"}})
    with pytest.raises(OC.ObservatoryConfigError, match="must be one of core, scrapling"):
        load(tmp_path, {"engine": {"pages": "magic"}})


# ---- the owner's request controls (there is no host list and no approval step) ---------------------------------------------------------------------

def test_there_is_no_host_list_any_more_and_a_file_that_still_has_one_is_told_so(tmp_path):
    with pytest.raises(OC.ObservatoryConfigError, match="unknown setting 'authorised_access'"):
        load(tmp_path, {"authorised_access": {"hosts": []}})


def advanced_of(tmp_path, adv):
    root = tmp_path / "brands" / "b"
    root.mkdir(parents=True, exist_ok=True)
    (root / "observatory.yaml").write_text(yaml.safe_dump({"competitors": [{"name": "C", "domain": "c.example", "advanced": adv}]}), encoding="utf-8")
    return OC.load_brand_file("b", str(tmp_path / "brands"))["competitors"][0]["advanced"]


ALL = {"browser": True, "impersonate": True, "hide_canvas": True, "block_webrtc": True, "humanize": True, "solve_challenges": True, "locale": "de-DE", "timezone": "Europe/Berlin", "cookies_env": "OBSERVATORY_COOKIES_X",
       "proxy": True, "rotate": "random", "session": True, "identity": "Kelson/2.0", "headers": {"X-A": "b"}, "accept_language": "de-DE", "delay": 20, "rest_hours": 12, "network_idle": True,
       "disable_resources": True, "wait_selector": "#app", "headless": False}


def test_every_capability_is_a_plain_setting_on_any_competitor_with_no_approval_or_record(tmp_path):
    assert advanced_of(tmp_path, ALL) == ALL


def test_the_older_names_are_still_accepted_and_mean_the_same(tmp_path):
    got = advanced_of(tmp_path, {"fingerprint": True, "solve_cloudflare": True, "impersonate": True})
    assert got == {"impersonate": True, "hide_canvas": True, "block_webrtc": True, "solve_challenges": True}
    assert advanced_of(tmp_path, {"fingerprint": False}) == {}


def test_the_approval_record_is_gone_and_a_file_that_still_has_one_is_told_so(tmp_path):
    with pytest.raises(OC.ObservatoryConfigError, match="unknown setting 'evasion'"):
        advanced_of(tmp_path, {"evasion": {"basis": "owner", "approved_by": "H", "date": "2026-11-04", "impersonate": True}})


@pytest.mark.parametrize("adv, fragment", [({"locale": "english"}, "en-GB or de-DE"), ({"timezone": "London"}, "timezone name"), ({"rest_hours": 0}, "between 1 and 168"), ({"rest_hours": 500}, "between 1 and 168"),
                                           ({"browser": "yes"}, "true or false"), ({"disable_resources": 1}, "true or false"), ({"impersonate": "yes"}, "true or false"), ({"humanize": 1}, "true or false"),
                                           ({"solve_challenges": "on"}, "true or false"), ({"hide_canvas": None}, "true or false")])
def test_the_settings_are_validated_so_a_typo_is_refused_not_ignored(tmp_path, adv, fragment):
    with pytest.raises(OC.ObservatoryConfigError, match=fragment):
        advanced_of(tmp_path, adv)


def test_the_shared_helpers_name_the_options_and_normalise_hosts():
    assert GC.flags_requested({"impersonate": True, "proxy_envs": ["X"], "cookies_env": "Y", "stealth": True, "session": True}) == ["impersonate"] and GC.flags_requested({}) == []
    assert GC.flags_requested({"fingerprint": True, "solve_cloudflare": True, "humanize": True}) == ["fingerprint", "solve_cloudflare", "humanize"]
    assert GC.host_only("https://www.Shop.example:8080/x") == "shop.example"
    assert not hasattr(GC, "record_problem") and not hasattr(GC, "BASES")                       # there is no approval step to check


# ---- the polite fetcher under the policy -----------------------------------------------------------------------------------------------------

def web():
    return Web({SHOP + "/private/page": reply(200, "<html><title>Hi</title><p>content</p></html>", {"content-type": "text/html"}), SHOP + "/ok": reply(200, "ok")}, robots=CLOSED)


def test_by_default_a_disallowed_page_is_not_read_and_the_path_is_skippable():
    w = web()
    c = client(w)
    with pytest.raises(K.Failure) as seen:
        c.get(SHOP + "/private/page")
    assert seen.value.code == K.PLATFORM_BLOCKED and getattr(seen.value, "robots_path", False) and SHOP + "/private/page" not in w.log


def test_advisory_policy_reads_the_page_and_records_that_robots_asked_otherwise():
    w = web()
    cfg = settings()
    cfg["crawl_policy"] = {"robots": "advisory", "reader_robots": "advisory", "basis": "Harish approved reading public pages (2026-11-03)", "overrides": []}
    got = client(w, cfg).get(SHOP + "/private/page")
    assert got.status == 200 and "does not allow this address; read under crawl policy 'advisory' (Harish approved" in got.robots_note
    clean = client(web(), cfg).get(SHOP + "/ok")
    assert clean.robots_note == ""                                              # only pages robots actually objected to carry the note


def test_an_override_applies_to_its_host_and_subdomains_only_and_names_its_basis():
    cfg = settings()
    cfg["crawl_policy"] = {"robots": "obey", "reader_robots": "advisory", "basis": "", "overrides": [{"host": "shop.example", "robots": "ignore", "basis": "owner",
                                                                                                       "approved_by": "Harish", "date": "2026-11-03", "note": ""}]}
    c = client(web(), cfg)
    assert c.robots_mode("shop.example")[0] == "ignore" and c.robots_mode("eu.shop.example")[0] == "ignore" and "owner, approved by Harish" in c.robots_mode("shop.example")[1]
    assert c.robots_mode("other.example")[0] == "obey" and c.robots_mode("shop.example.evil.test")[0] == "obey"
    assert c.get(SHOP + "/private/page").robots_note.startswith("robots.txt does not allow")
    other = Web({"https://other.example/private/x": reply(200, "x")}, robots=None)
    other.on_robots("https://other.example", CLOSED)
    with pytest.raises(K.Failure):
        client(other, cfg).get("https://other.example/private/x")


def test_an_unreadable_robots_file_is_read_through_only_under_an_advisory_policy():
    w = Web({SHOP + "/a": reply(200, "ok")})
    w.on(SHOP + "/robots.txt", reply(500, "oops"))
    with pytest.raises(K.Failure):
        client(w).get(SHOP + "/a")
    cfg = settings()
    cfg["crawl_policy"] = {"robots": "advisory", "reader_robots": "advisory", "basis": "approved", "overrides": []}
    w2 = Web({SHOP + "/a": reply(200, "ok")})
    w2.on(SHOP + "/robots.txt", reply(500, "oops"))
    got = client(w2, cfg).get(SHOP + "/a")
    assert got.status == 200 and "could not be read" in got.robots_note or "closed" in got.robots_note


@pytest.mark.parametrize("answer", [reply(403, "no"), reply(429, "slow", {"Retry-After": "10"}), reply(200, "<title>Just a moment...</title>", {}), reply(503, "<title>Attention Required! | Cloudflare</title>")])
def test_a_real_access_restriction_is_never_bypassed_even_when_robots_is_ignored(answer):
    cfg = settings()
    cfg["crawl_policy"] = {"robots": "advisory", "reader_robots": "advisory", "basis": "approved", "overrides": []}
    w = Web({SHOP + "/private/page": answer})
    w.on_robots(SHOP, CLOSED)
    with pytest.raises(K.Failure) as seen:
        client(w, cfg).get(SHOP + "/private/page")
    assert seen.value.code in (K.PLATFORM_BLOCKED, K.RATE_LIMITED) and w.count(SHOP + "/private/page") == 1


def test_the_ai_judge_and_enrichment_settings_exist_with_safe_defaults_and_the_old_flag_is_gone(tmp_path):
    d = OC.load_settings("missing.yaml")["discovery"]
    assert (d["judge"], d["enrich"], d["min_confidence"], d["judge_max"], d["enrich_max"], d["agent_reach_calls"]) == (True, True, 0.7, 16, 8, 12)
    with pytest.raises(OC.ObservatoryConfigError, match="unknown setting 'use_ai'"):
        load(tmp_path, {"discovery": {"use_ai": True}})
    with pytest.raises(OC.ObservatoryConfigError, match="min_confidence"):
        load(tmp_path, {"discovery": {"min_confidence": 0.1}})
    assert OC.load_settings(__import__("pathlib").Path(__file__).resolve().parents[1] / "observatory.yaml")["discovery"]["judge"] is True      # the shipped file loads



# ---- request controls for any host, and what stays gated --------------------------------------------------------------------------------------

def competitor_file(tmp_path, advanced):
    root = tmp_path / "brands" / "b"
    root.mkdir(parents=True, exist_ok=True)
    (root / "observatory.yaml").write_text(yaml.safe_dump({"competitors": [{"name": "C", "domain": "c.example", "advanced": advanced}]}), encoding="utf-8")
    return OC.load_brand_file("b", str(tmp_path / "brands"))["competitors"][0]["advanced"]


def test_identity_headers_language_session_and_delay_are_accepted_on_any_competitor(tmp_path):
    adv = {"identity": "Kelson-Research/2.0 (+https://kelson.example)", "headers": {"X-Campaign": "q4"}, "accept_language": "en-GB", "session": True, "delay": 12, "proxy": True, "rotate": "random"}
    assert competitor_file(tmp_path, adv) == adv


@pytest.mark.parametrize("name", ["Cookie", "authorization", "Proxy-Authorization", "X-Forwarded-For", "Forwarded", "Via", "Client-IP", "X-Real-IP", "True-Client-IP", "Host", "User-Agent", "From", "Content-Length"])
def test_headers_that_carry_credentials_or_falsify_where_a_request_comes_from_cannot_be_set(tmp_path, name):
    with pytest.raises(OC.ObservatoryConfigError, match="cannot be set here"):
        competitor_file(tmp_path, {"headers": {name: "x"}})


@pytest.mark.parametrize("adv, fragment", [({"identity": "two\nlines"}, "one line"), ({"identity": ""}, "short piece of text"), ({"identity": "x" * 201}, "short piece of text"),
                                           ({"headers": {"X-A": "b\r\nInjected: 1"}}, "one line"), ({"headers": {"bad name": "x"}}, "cannot be set here"), ({"delay": -1}, "between 0 and 3600"),
                                           ({"delay": True}, "between 0 and 3600"), ({"session": "yes"}, "true or false"), ({"headers": {f"X-{n}": "v" for n in range(13)}}, "at most 12")])
def test_the_new_options_are_strictly_validated(tmp_path, adv, fragment):
    with pytest.raises(OC.ObservatoryConfigError, match=fragment):
        competitor_file(tmp_path, adv)


def test_global_switches_default_to_available_and_accept_only_booleans(tmp_path):
    every = {"custom_identity": True, "proxy": True, "session": True, "browser": True, "account_cookies": True, "evasion": True}
    assert OC.load_settings("missing.yaml")["access_controls"] == every
    assert load(tmp_path, {"access_controls": {"proxy": False}})["access_controls"] == {**every, "proxy": False}
    with pytest.raises(OC.ObservatoryConfigError, match="must be true or false"):
        load(tmp_path, {"access_controls": {"proxy": "no"}})
    with pytest.raises(OC.ObservatoryConfigError, match="unknown setting"):
        load(tmp_path, {"access_controls": {"stealth": True}})                    # only the six named switches exist
