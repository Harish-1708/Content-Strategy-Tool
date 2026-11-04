"""The crawl policy, the recorded overrides and the authorised-access gate."""
import pytest
import yaml

from contentintel import contract as K
from contentintel.observatory import access as AC
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
    assert s["authorised_access"] == {"hosts": []} and s["advanced"] == {"proxies": [], "rotate": "none"} and s["readers"]["jina"] is True and s["readers"]["exa"] is True
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


@pytest.mark.parametrize("entry, fragment", [
    ({**REC}, "methods must list"), ({**REC, "methods": []}, "methods must list"), ({**REC, "methods": ["magic"]}, "methods must list"),
    ({**REC, "methods": ["stealth"], "basis": "public_page_reader"}, "basis must be one of owner, client_authorised, written_permission"),
    ({**REC, "methods": ["stealth"], "date": ""}, "needs date")])
def test_authorised_access_needs_methods_and_a_real_basis(tmp_path, entry, fragment):
    with pytest.raises(OC.ObservatoryConfigError, match=fragment):
        load(tmp_path, {"authorised_access": {"hosts": [entry]}})


def test_proxy_addresses_never_live_in_the_file(tmp_path):
    ok = load(tmp_path, {"advanced": {"proxies": ["eu1", "us_2"], "rotate": "sequential"}})
    assert ok["advanced"] == {"proxies": ["eu1", "us_2"], "rotate": "sequential"}
    with pytest.raises(OC.ObservatoryConfigError, match="never from this file"):
        load(tmp_path, {"advanced": {"proxies": ["http://user:pw@1.2.3.4:8080"]}})
    with pytest.raises(OC.ObservatoryConfigError, match="rotate"):
        load(tmp_path, {"advanced": {"rotate": "chaos"}})
    with pytest.raises(OC.ObservatoryConfigError, match="must be one of core, scrapling"):
        load(tmp_path, {"engine": {"pages": "magic"}})


# ---- the gate ---------------------------------------------------------------------------------------------------------------------------------

def gate(tmp_path, *methods, host="shop.example", **extra):
    return AC.Gate(load(tmp_path, {"authorised_access": {"hosts": [{**REC, "host": host, "methods": list(methods) or ["stealth"], **extra}]}}))


def test_nothing_advanced_is_allowed_for_any_host_by_default():
    g = AC.Gate(OC.load_settings("missing.yaml"))
    for method in OC.ACCESS_METHODS:
        ok, why = g.allows("competitor.example", method)
        assert ok is False and "not listed under authorised_access" in why
    with pytest.raises(K.Failure) as seen:
        g.require("competitor.example", "stealth")
    assert seen.value.code == K.BAD_INPUT and "authorised_access" in seen.value.hint


def test_a_listed_host_gets_exactly_the_listed_methods_and_its_subdomains_but_not_look_alikes(tmp_path):
    g = gate(tmp_path, "stealth", "proxy")
    assert g.allows("shop.example", "stealth")[0] and g.allows("www.shop.example", "proxy")[0] and g.allows("eu.shop.example", "stealth")[0]
    ok, why = g.allows("shop.example", "challenge")
    assert not ok and "authorised for proxy, stealth, not for challenge" in why
    for other in ("notshop.example", "shop.example.evil.test", "example", "shop.exampl"):
        assert g.allows(other, "stealth")[0] is False, other
    assert g.allows("shop.example", "nonsense")[0] is False
    assert g.require("shop.example", "stealth", "proxy") == "owner: approved by Harish on 2026-11-03"
    with pytest.raises(K.Failure):
        g.require("shop.example", "stealth", "challenge")                        # all methods asked for must be allowed
    assert g.listing() == [{"host": "shop.example", "methods": ["proxy", "stealth"], "basis": "owner", "approved_by": "Harish", "date": "2026-11-03"}]


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


def test_the_unbuilt_ai_judge_setting_is_refused_not_silently_ignored(tmp_path):
    assert OC.load_settings("missing.yaml")["discovery"]["use_ai"] is False
    with pytest.raises(OC.ObservatoryConfigError, match="not built yet"):
        load(tmp_path, {"discovery": {"use_ai": True}})
