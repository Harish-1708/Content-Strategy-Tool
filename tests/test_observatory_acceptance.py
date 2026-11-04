"""The acceptance probes for the competitor routes."""
import pytest

from contentintel import acceptance as A
from contentintel import contract as K
from contentintel import health as HL
from contentintel import sources as SRC
from contentintel.observatory import acceptance_checks as AK
from contentintel.store import Store
from conftest import ROOT
from observatory_helpers import Web, client, reply, settings
from test_observatory_monitor import BASE, ROBOTS, atom, homepage, sitemap
from observatory_helpers import product, register_store

NOW = "2026-11-03T12:00:00Z"
REG = SRC.load_sources(ROOT / "sources.yaml")


def good_web():
    web = Web()
    register_store(web, "shop.example", [product(1, "Bar", "3.00"), product(2, "Other", "4.00")], {"all": [1, 2], "best": [1]}, robots=ROBOTS)
    web.on(BASE + "/", reply(200, homepage(), {"content-type": "text/html"}))
    web.on(BASE + "/sitemap.xml", reply(200, sitemap(), {"content-type": "application/xml"}))
    web.on(BASE + "/blogs/news.atom", reply(200, atom(["Hello", "World"]), {"content-type": "application/atom+xml"}))
    return web


def checks(web, **kw):
    cfg = settings()
    return AK.make_checks("shop.example", cfg, "research@kelson.example", NOW, client=client(web, cfg), reader_http=lambda *a: reply(200, "Title: T\nMarkdown Content:\n" + "words about snacks " * 12), **kw)


def test_each_route_has_a_check_that_reads_something_real_and_says_what():
    c = checks(good_web())
    assert set(c) == set(AK.NEEDS)
    items, detail = c[("competitor_stores", "public_json")]()
    assert items == 2 and "read 2 products and 2 collection(s) of shop.example" in detail
    items, detail = c[("competitor_stores", "sitemap")]()
    assert items == 3 and "3 address(es)" in detail
    assert c[("competitor_pages", "static_fetch")]()[0] == 1
    items, detail = c[("competitor_pages", "feed")]()
    assert items == 2 and "atom feed" in detail
    assert "through the reader" in c[("competitor_pages", "reader")]()[1]


def test_nothing_can_be_checked_without_a_site_or_a_contact_address():
    cfg = settings()
    assert AK.make_checks("", cfg, "a@b.example", NOW) == {} and AK.make_checks("shop.example", cfg, "", NOW) == {}


def test_a_site_with_no_feed_is_reported_as_not_found_not_as_a_pass():
    web = good_web()
    web.routes.pop(BASE + "/blogs/news.atom")
    web.on(BASE + "/", reply(200, homepage().replace('<link rel="alternate" type="application/atom+xml" href="https://shop.example/blogs/news.atom">', ""), {"content-type": "text/html"}))
    with pytest.raises(K.Failure) as seen:
        checks(web)[("competitor_pages", "feed")]()
    assert seen.value.code == K.NOT_FOUND and "advertises no feed" in str(seen.value)


def test_a_block_is_a_failure_with_its_real_code():
    web = good_web()
    web.on(BASE + "/", reply(403, "no"))
    with pytest.raises(K.Failure) as seen:
        checks(web)[("competitor_pages", "static_fetch")]()
    assert seen.value.code == K.PLATFORM_BLOCKED


def ctx(observatory):
    return A.Context(registry=REG, observatory_checks=observatory)


def test_the_probes_exist_for_the_new_routes_run_when_checkable_and_are_skipped_with_a_reason_when_not():
    probes = {(p.source, p.route): p for p in A.build_probes(ctx(checks(good_web())))}
    for key, action in AK.NEEDS.items():
        assert probes[key].action == action and probes[key].skip == "" and probes[key].run is not None
    bare = {(p.source, p.route): p for p in A.build_probes(ctx({}))}
    assert all("OBSERVATORY_CONTACT" in bare[k].skip and bare[k].run is None for k in AK.NEEDS)


def test_a_passing_probe_earns_the_route_trust_and_a_failing_one_does_not(tmp_path):
    store = Store(tmp_path)
    hl = HL.HealthLedger.load(store, REG)
    probes = [p for p in A.build_probes(ctx(checks(good_web()))) if p.source in ("competitor_stores", "competitor_pages") and p.route in ("public_json", "static_fetch")]
    results = A.run_acceptance(probes, health=hl, store=store, now=NOW, version="abc", run_id="r1")
    assert {r["status"] for r in results} == {"passed"}
    rows = {(r["source"], r["route"]): r for r in hl.rows(NOW)}
    assert rows[("competitor_stores", "public_json")]["acceptance_result"] == "pass" and rows[("competitor_stores", "public_json")]["verified"]
    web = good_web()
    web.on(BASE + "/", reply(403, "no"))
    bad = [p for p in A.build_probes(ctx(checks(web))) if p.route == "static_fetch"]
    res = A.run_acceptance(bad, health=hl, store=store, now=NOW, version="abc", run_id="r2")
    assert res[0]["status"] == "failed" and res[0]["code"] == K.PLATFORM_BLOCKED
