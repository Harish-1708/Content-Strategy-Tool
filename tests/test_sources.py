"""sources.yaml: the declared sources, and the checks that the declaration matches the code that uses it."""
import copy

import pytest
import yaml

from contentintel import collectors as C
from contentintel import contract
from contentintel import providers as P
from contentintel import sources as S

from stage1_helpers import SHIPPED, shipped


def raw():
    return yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))


def load(tmp_path, data):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return S.load_sources(path)


def test_the_shipped_file_loads_and_every_value_is_in_its_vocabulary():
    reg = shipped()
    assert len(reg.sources) == 13 and sum(len(s.routes) for s in reg.sources.values()) == 20
    for src in reg.sources.values():
        assert src.kind in S.KINDS and src.status in S.STATUSES and src.stale_after_days >= 1
        assert src.capabilities and src.limitations, f"{src.id} must say what it gives and what it cannot"
        for rt in src.routes.values():
            assert rt.tier in S.TIERS and rt.access_type in S.ACCESS_TYPES


def test_the_declaration_matches_the_code_that_uses_it():
    reg = shipped()
    for source in C.SOURCES:                                              # every collector the tool has is declared, with its browser route
        assert source in reg.sources and "browser" in reg.sources[source].routes
    for source in C.BROWSER_SOURCES:                                      # and the Apify route the backends option allows
        assert "apify" in reg.sources[source].routes
    assert set(reg.sources["web_search"].routes) == set(P.PROVIDER_NAMES)  # a renamed provider would otherwise be silently unobserved
    assert "public_json" in reg.sources["shopify_catalogue"].routes
    assert set(reg.sources) >= {"video_tiktok", "video_instagram", "youtube_public", "claude"}


def test_what_each_route_returns_is_labelled_honestly():
    reg = shipped()
    assert all(r.access_type == "search_snippet" for r in reg.sources["web_search"].routes.values())     # a search result is never a page
    assert reg.route("deep_research", "browser").access_type == "report_text"
    assert reg.route("tiktok", "browser").tier == "session_based" and reg.route("meta_ads", "browser").tier == "public_polite"
    assert reg.route("video_tiktok", "layered").access_type == "media_file"


def test_the_agent_reach_instagram_route_is_declared_but_off_and_experimental():
    rt = shipped().route("instagram", "agent_reach")
    assert rt.enabled is False and rt.experimental is True and "stealth" in rt.note
    assert [r.name for r in shipped().source("instagram").enabled_routes()] == ["browser", "apify"]


def test_planned_sources_are_declared_but_not_established():
    reg = shipped()
    assert {s.id for s in reg.sources.values() if s.status == "planned"} == {"reddit_public", "competitor_stores", "competitor_pages"}
    assert reg.source("youtube_public").status == "experimental"


def test_defaults_and_pauses_are_read():
    reg = shipped()
    assert (reg.stale_after_days, reg.acceptance_max_age_days, reg.window_days) == (14, 14, 7)
    assert (reg.degrade_after_failures, reg.recover_after_successes, reg.history_cap) == (3, 2, 200)
    assert reg.pause_for(contract.RATE_LIMITED) == 3600 and reg.pause_for(contract.PLATFORM_BLOCKED) == 86400
    assert reg.pause_for(contract.NOT_FOUND) == 0 and reg.pause_for(contract.BAD_INPUT) == 0
    assert reg.source("shopify_catalogue").stale_after_days == 3 and reg.source("meta_ads").stale_after_days == 14


def test_asking_for_something_undeclared_says_so():
    reg = shipped()
    with pytest.raises(S.SourcesError, match="not declared"):
        reg.source("myspace")
    with pytest.raises(S.SourcesError, match="no route"):
        reg.route("tiktok", "carrier_pigeon")


@pytest.mark.parametrize("edit, message", [
    (lambda d: d.update(version=2), "version must be 1"),
    (lambda d: d.update(extra=1), "unknown setting"),
    (lambda d: d["defaults"].update(stale_after_dayz=3), "unknown setting"),
    (lambda d: d["defaults"].update(stale_after_days=0), "at least 1"),
    (lambda d: d["defaults"].update(history_cap=True), "whole number"),
    (lambda d: d["defaults"].update(pause_seconds={"MAYBE": 5}), "not an error code"),
    (lambda d: d["defaults"].update(pause_seconds={"TIMEOUT": -1}), "at least 0"),
    (lambda d: d.update(sources={}), "at least one source"),
    (lambda d: d["sources"]["tiktok"].update(kind="robot"), "kind must be"),
    (lambda d: d["sources"]["tiktok"].update(status="live"), "status must be"),
    (lambda d: d["sources"]["tiktok"].update(colour="red"), "unknown setting"),
    (lambda d: d["sources"]["tiktok"].update(routes={}), "at least one route"),
    (lambda d: d["sources"]["tiktok"]["routes"]["browser"].update(tier="magic"), "tier must be"),
    (lambda d: d["sources"]["tiktok"]["routes"]["browser"].update(access_type="vibes"), "access_type must be"),
    (lambda d: d["sources"]["tiktok"]["routes"]["browser"].update(enabled="no"), "true or false"),
    (lambda d: d["sources"]["tiktok"]["routes"]["browser"].update(speed=1), "unknown setting"),
    (lambda d: d["sources"]["tiktok"].update(capabilities="posts"), "list of non-empty"),
    (lambda d: d["sources"]["tiktok"].update(limitations=[""]), "list of non-empty"),
    (lambda d: d["sources"]["tiktok"].update(stale_after_days=1.5), "whole number"),
])
def test_a_mistake_in_the_file_is_refused_with_a_reason(tmp_path, edit, message):
    data = copy.deepcopy(raw())
    edit(data)
    with pytest.raises(S.SourcesError, match=message):
        load(tmp_path, data)


def test_a_missing_or_unreadable_file_is_refused(tmp_path):
    with pytest.raises(S.SourcesError, match="is missing"):
        S.load_sources(tmp_path / "nope.yaml")
    (tmp_path / "bad.yaml").write_text("a: [unclosed", encoding="utf-8")
    with pytest.raises(S.SourcesError, match="could not be read"):
        S.load_sources(tmp_path / "bad.yaml")
    (tmp_path / "list.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(S.SourcesError, match="list of settings"):
        S.load_sources(tmp_path / "list.yaml")


def test_the_route_hash_changes_with_the_route_and_only_then(tmp_path):
    reg = shipped()
    base = S.route_hash(reg, "tiktok", "browser")
    assert base == S.route_hash(shipped(), "tiktok", "browser") and len(base) == 12
    assert base != S.route_hash(reg, "tiktok", "apify") and base != S.route_hash(reg, "instagram", "browser")
    assert base != S.route_hash(reg, "tiktok", "browser", {"selector": "a"})                   # the site settings are part of what was proven
    assert S.route_hash(reg, "tiktok", "browser", {"selector": "a"}) != S.route_hash(reg, "tiktok", "browser", {"selector": "b"})
    data = copy.deepcopy(raw())
    data["sources"]["tiktok"]["routes"]["browser"]["tier"] = "official_api"
    assert S.route_hash(load(tmp_path, data), "tiktok", "browser") != base                     # a changed declaration is a changed route
    data = copy.deepcopy(raw())
    data["sources"]["tiktok"]["limitations"] = ["a note that does not change how the route works"]
    assert S.route_hash(load(tmp_path, data), "tiktok", "browser") == base


def test_two_routes_declared_alike_still_have_different_fingerprints_and_switching_one_on_changes_it(tmp_path):
    data = copy.deepcopy(raw())
    data["sources"]["tiktok"]["routes"]["second"] = copy.deepcopy(data["sources"]["tiktok"]["routes"]["browser"])
    reg = load(tmp_path, data)
    assert S.route_hash(reg, "tiktok", "browser") != S.route_hash(reg, "tiktok", "second")
    off = shipped()
    data = copy.deepcopy(raw())
    data["sources"]["instagram"]["routes"]["agent_reach"]["enabled"] = True
    assert S.route_hash(load(tmp_path, data), "instagram", "agent_reach") != S.route_hash(off, "instagram", "agent_reach")
    data = copy.deepcopy(raw())
    data["sources"]["instagram"]["routes"]["agent_reach"]["experimental"] = False
    assert S.route_hash(load(tmp_path, data), "instagram", "agent_reach") != S.route_hash(off, "instagram", "agent_reach")
