"""Scrapling and Agent-Reach as shared platform services: central settings, optional target overrides, owner controls and the Controls tab. MOCK: no browser, no OpenCLI, no network."""
import json

import pytest
import yaml

from contentintel import contract as K
from contentintel import agent_reach_catalog as CAT
from contentintel import services as SV
from contentintel.observatory import config as OC
from contentintel.observatory import worker_client as WC

ON = dict(OC.SCRAPLING_ON_DEFAULTS)                                   # every per-request option is on by default
RESOLVED = {k: v for k, v in ON.items() if k != "proxy"}                # a central proxy default is skipped while no proxy is listed

PURPOSES = ("competitors", "discovery", "social_discovery", "content_research", "youtube_research", "general_research", "idea_bank", "planning", "brief", "deep_check", "autoseeds", "collectors")


def load(tmp_path, services=None, **extra):
    path = tmp_path / "o.yaml"
    path.write_text(yaml.safe_dump({"version": 1, **({"services": services} if services is not None else {}), **extra}), encoding="utf-8")
    return OC.load_settings(path)


# ---- the initial defaults (these are the ones documented for the owner) ------------------------------------------------------------------------

def test_the_initial_defaults_turn_every_service_channel_and_purpose_on_and_use_the_gateway_and_auto_readers_everywhere():
    s = OC.load_settings("missing.yaml")["services"]
    assert sorted(s) == ["agent_reach", "ai_gateway", "page_readers", "scrapling", "search"]
    assert s["scrapling"] == {"enabled": True, "use_for": {p: True for p in PURPOSES}, "defaults": ON, "overrides": []}
    assert s["agent_reach"] == {"enabled": True, "channels": {c: True for c in CAT.integrated()}, "use_for": {p: True for p in PURPOSES}}
    assert s["search"] == {"enabled": True, "use_for": {p: True for p in PURPOSES}, "channels": {n: True for n in ("serper_dev", "tavily", "serpapi", "exa", "exa_mcp", "agent_reach_reddit", "agent_reach_youtube")}}
    assert s["ai_gateway"]["routes"] == {p: "gateway_first" for p in ("general_research", "idea_bank", "planning", "brief", "deep_check", "autoseeds", "collectors")}          # on for every purpose: the free providers first, Claude when none can answer
    assert s["page_readers"]["routes"] == {p: "auto" for p in PURPOSES}                     # Scrapling first when installed, then Jina, then the built-in reader, for every purpose
    assert OC.load_settings("missing.yaml")["engine"]["pages"] == "core"                       # the older single switch is only a way to make Scrapling the sole reader; the routes above are the full control


def test_every_control_has_a_default_that_matches_the_settings_defaults():
    settings = OC.load_settings("missing.yaml")
    assert len(SV.CONTROLS) == len(SV.BY_KEY) == 129        # 5 services x (1 master + 12 purposes) + 23 channels + 19 routes + 15 central defaults + 6 request switches + 1 engine
    for defn in SV.CONTROLS:
        assert SV.current(settings, defn["key"]) == defn["default"], defn["key"]


# ---- validation: the same rules as a competitor's own settings -----------------------------------------------------------------------------------

def test_central_defaults_and_overrides_follow_the_same_rules_as_a_competitors_options(tmp_path):
    ok = load(tmp_path, {"scrapling": {"defaults": {"locale": "en-GB", "browser": True, "headers": {"X-A": "b"}, "impersonate": True, "fingerprint": True},
                                       "overrides": [{"match": "*.example.com", "identity": "K/2.0"}, {"match": "purpose:discovery", "delay": 30}, {"match": "all", "session": True}]}})
    sc = ok["services"]["scrapling"]
    assert sc["defaults"] == {**ON, "locale": "en-GB", "headers": {"X-A": "b"}}        # older names are normalised here too
    assert [o["match"] for o in sc["overrides"]] == ["*.example.com", "purpose:discovery", "all"]


@pytest.mark.parametrize("services, fragment", [
    ({"scrapling": {"defaults": {"locale": "english"}}}, "services.scrapling.defaults.locale must look like"), ({"scrapling": {"defaults": {"headers": {"Cookie": "x=1"}}}}, "cannot be set here"),
    ({"scrapling": {"defaults": {"browser": "yes"}}}, "true or false"), ({"scrapling": {"overrides": [{"identity": "x"}]}}, "needs match"), ({"scrapling": {"overrides": [{"match": "not a site", "delay": 1}]}}, "needs match"),
    ({"scrapling": {"overrides": [{"match": "purpose:gossip", "delay": 1}]}}, "needs match"), ({"scrapling": {"overrides": [{"match": "all", "rest_hours": 0}]}}, "between 1 and 168"),
    ({"scrapling": {"overrides": "all"}}, "must be a list"), ({"scrapling": {"use_for": {"gossip": True}}}, "unknown setting"), ({"scrapling": {"use_for": {"discovery": "no"}}}, "true or false"),
    ({"scrapling": {"enabled": "off"}}, "true or false"), ({"scrapling": {"magic": 1}}, "unknown setting"), ({"agent_reach": {"channels": {"tiktok": True}}}, "unknown setting"),
    ({"agent_reach": {"enabled": 1}}, "true or false"), ({"agent_reach": {"use_for": {"competitors": None}}}, "true or false"), ({"firecrawl": {}}, "unknown setting")])
def test_a_wrong_services_setting_is_refused_with_its_name_never_ignored(tmp_path, services, fragment):
    with pytest.raises(OC.ObservatoryConfigError, match=fragment):
        load(tmp_path, services)


# ---- resolving what a request uses ----------------------------------------------------------------------------------------------------------------

def settings_with(**sc):
    s = OC.load_settings("missing.yaml")
    s["services"]["scrapling"].update(sc)
    return s


def test_central_defaults_then_matching_overrides_in_order_then_the_targets_own_settings():
    s = settings_with(defaults={"identity": "central", "locale": "en-GB", "session": True},
                      overrides=[{"match": "*.shop.example", "identity": "for-shop", "delay": 10}, {"match": "purpose:discovery", "delay": 99}, {"match": "shop.example", "locale": "de-DE"}, {"match": "all", "rest_hours": 6}])
    assert SV.resolve_scrapling(s, "shop.example", "competitors", None) == {"identity": "for-shop", "locale": "de-DE", "session": True, "delay": 10, "rest_hours": 6}
    assert SV.resolve_scrapling(s, "www.shop.example", "discovery", None)["delay"] == 99 and SV.resolve_scrapling(s, "www.shop.example", "discovery", None)["identity"] == "for-shop"
    assert SV.resolve_scrapling(s, "other.example", "competitors", None) == {"identity": "central", "locale": "en-GB", "session": True, "rest_hours": 6}      # untouched by another host's override
    assert SV.resolve_scrapling(s, "shop.example", "competitors", {"identity": "mine", "delay": 3, "proxy": True}) == {"identity": "mine", "locale": "de-DE", "session": True, "delay": 3, "rest_hours": 6, "proxy": True}
    assert SV.resolve_scrapling(s, "notshop.example", "competitors", None)["identity"] == "central"                          # a look-alike name does not match the override
    assert SV.resolve_scrapling(s, "shop.example.evil.test", "competitors", None)["identity"] == "central"


def test_an_override_written_with_www_matches_the_site_with_and_without_it():
    s = settings_with(overrides=[{"match": "www.shop.example", "identity": "W/1"}, {"match": "*.other.example", "identity": "O/1"}])
    for host in ("shop.example", "www.shop.example", "eu.shop.example"):
        assert SV.resolve_scrapling(s, host, "competitors", None) == {**RESOLVED, "identity": "W/1"}, host
    assert SV.resolve_scrapling(s, "other.example", "competitors", None) == {**RESOLVED, "identity": "O/1"}          # *.other.example covers the site itself as well
    assert SV.resolve_scrapling(s, "notshop.example", "competitors", None) == RESOLVED


def test_the_central_settings_are_not_changed_by_resolving():
    s = settings_with(defaults={"headers": {"X-A": "b"}}, overrides=[{"match": "all", "session": True}])
    got = SV.resolve_scrapling(s, "a.example", "competitors", {"identity": "x"})
    got["identity"] = "changed"
    assert s["services"]["scrapling"]["defaults"] == {"headers": {"X-A": "b"}} and s["services"]["scrapling"]["overrides"] == [{"match": "all", "session": True}]


@pytest.mark.parametrize("purpose", PURPOSES)
def test_each_purpose_has_its_own_scrapling_switch_and_the_master_switch_overrides_them_all(purpose):
    s = OC.load_settings("missing.yaml")
    assert SV.scrapling_allowed(s, purpose) == (True, "")
    s["services"]["scrapling"]["use_for"][purpose] = False
    ok, why = SV.scrapling_allowed(s, purpose)
    assert not ok and f"services.scrapling.use_for.{purpose}" in why
    with pytest.raises(K.Failure) as seen:
        SV.resolve_scrapling(s, "a.example", purpose, None)
    assert seen.value.code == K.BAD_INPUT and f"use_for.{purpose}" in str(seen.value)
    other = next(p for p in PURPOSES if p != purpose)
    assert SV.scrapling_allowed(s, other)[0] is True                                           # the other purposes are unaffected
    s["services"]["scrapling"]["enabled"] = False
    assert "services.scrapling.enabled" in SV.scrapling_allowed(s, other)[1]


def test_agent_reach_has_a_master_switch_a_channel_switch_and_a_purpose_switch():
    s = OC.load_settings("missing.yaml")
    assert SV.agent_reach_allowed(s, "youtube", "youtube_research") == (True, "")
    s["services"]["agent_reach"]["channels"]["youtube"] = False
    assert "services.agent_reach.channels.youtube" in SV.agent_reach_allowed(s, "youtube", "youtube_research")[1] and SV.agent_reach_allowed(s, "reddit", "content_research")[0]
    s["services"]["agent_reach"]["use_for"]["content_research"] = False
    assert "services.agent_reach.use_for.content_research" in SV.agent_reach_allowed(s, "reddit", "content_research")[1] and SV.agent_reach_allowed(s, "reddit", "competitors")[0]
    s["services"]["agent_reach"]["enabled"] = False
    assert "services.agent_reach.enabled" in SV.agent_reach_allowed(s, "reddit", "competitors")[1]
    with pytest.raises(K.Failure) as seen:
        SV.agent_reach_client(s, "reddit", "competitors", environ={})
    assert seen.value.code == K.NEED_SETUP and "services.agent_reach.enabled" in str(seen.value)


def test_the_agent_reach_client_depends_on_the_channel_browser_channels_refuse_a_hosted_runner_and_youtube_goes_through_yt_dlp():
    s = OC.load_settings("missing.yaml")
    hosted = {"RUNNER_ENVIRONMENT": "github-hosted", "OPENCLI_BIN": "ocli"}
    cli = SV.agent_reach_client(s, "reddit", "content_research", environ=hosted, timeout=33)
    assert cli.site == "reddit" and cli.binary == "ocli" and cli.timeout == 33
    with pytest.raises(K.Failure) as seen:
        cli.call(["search", "x"])
    assert seen.value.code == K.NEED_SETUP and "GitHub-hosted runner" in str(seen.value)
    from contentintel import ytdlp_youtube
    yt = SV.agent_reach_client(s, "youtube", "youtube_research", environ=hosted, timeout=44)
    assert isinstance(yt, ytdlp_youtube.YtDlpYouTube) and yt.needs_browser is False and yt.timeout == 44          # Agent-Reach's YouTube channel is yt-dlp: no login, so no hosted-runner refusal
    sentinel = object()
    assert SV.agent_reach_client(s, "reddit", "content_research", environ={}, factory=lambda: sentinel) is sentinel


# ---- the page service passes the resolved options to the worker, for any purpose -----------------------------------------------------------------

def test_the_page_service_gives_the_worker_the_options_resolved_for_that_host_and_purpose(tmp_path, monkeypatch):
    from contentintel.observatory.snapshots import SnapshotStore
    seen = {}

    def fake_run(job, python, environ=None, runner=None):
        seen["job"] = job
        return {"ok": True, "hosts": {}, "results": [{"url": job["items"][0]["url"], "ok": True, "status": 200, "title": "T", "markdown": "# Hello", "final_url": job["items"][0]["url"]}]}
    monkeypatch.setattr(WC, "run_worker", fake_run)
    s = settings_with(defaults={"locale": "en-GB", "browser": True}, overrides=[{"match": "purpose:content_research", "identity": "Research/1.0"}, {"match": "site.example", "delay": 12}])
    page = SV.scrapling_page(s, "https://www.site.example/post", "content_research", contact="a@b.example", python="py", snaps=SnapshotStore(tmp_path), target_advanced={"timezone": "Europe/London"})
    adv = seen["job"]["items"][0]["advanced"]
    assert page["title"] == "T" and "Hello" in page["text"] and adv == {"stealth": True, "locale": "en-GB", "timezone": "Europe/London", "identity": "Research/1.0", "delay": max(12.0, float(s["politeness"]["min_delay_seconds"]))}
    with pytest.raises(K.Failure) as off:
        s["services"]["scrapling"]["use_for"]["content_research"] = False
        SV.scrapling_page(s, "https://site.example/", "content_research", contact="a@b.example", python="py", snaps=SnapshotStore(tmp_path))
    assert "use_for.content_research" in str(off.value)


# ---- controls: values, defaults, sources, persistence ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("key, raw, expected", [("services.scrapling.enabled", "off", False), ("services.scrapling.enabled", "YES", True), ("services.scrapling.defaults.delay", "20", 20),
                                                ("services.scrapling.defaults.delay", "2.5", 2.5), ("services.scrapling.defaults.locale", "de-DE", "de-DE"), ("engine.pages", "scrapling", "scrapling"),
                                                ("services.agent_reach.channels.reddit", "0", False)])
def test_control_values_are_understood(key, raw, expected):
    assert SV.coerce(SV.BY_KEY[key], raw) == expected


@pytest.mark.parametrize("key, raw", [("services.scrapling.enabled", "maybe"), ("services.scrapling.defaults.delay", "fast"), ("services.scrapling.defaults.delay", "-1"), ("services.scrapling.defaults.delay", "99999"),
                                      ("engine.pages", "magic"), ("services.scrapling.defaults.identity", "a\nb"), ("services.scrapling.defaults.identity", "x" * 300)])
def test_a_value_that_cannot_be_understood_is_refused_with_the_controls_name(key, raw):
    with pytest.raises(SV.ControlError, match=key.split(".")[-1] if "." in key else key):
        SV.coerce(SV.BY_KEY[key], raw)


def test_owner_changes_are_saved_applied_on_top_of_the_file_and_labelled_by_source(tmp_path):
    data = tmp_path / "data"
    SV.save_overrides(data, {"services.scrapling.enabled": False, "services.scrapling.defaults.locale": "fr-FR", "not.a.control": 1}, "owner", "2026-11-04T10:00:00Z", "changed two")
    assert SV.load_overrides(data) == {"services.scrapling.enabled": False, "services.scrapling.defaults.locale": "fr-FR"}              # an unknown key is ignored on load
    s = SV.apply_controls(OC.load_settings("missing.yaml"), SV.load_overrides(data))
    assert s["services"]["scrapling"]["enabled"] is False and s["services"]["scrapling"]["defaults"] == {**ON, "locale": "fr-FR"}
    rows = {r["key"]: r for r in SV.effective_rows(s, data)}
    assert rows["services.scrapling.enabled"]["source"] == "controls" and rows["services.scrapling.defaults.locale"]["source"] == "controls" and rows["services.agent_reach.enabled"]["source"] == "default"
    assert json.loads(SV.overrides_path(data).read_text())["history"][-1]["change"] == "changed two"


def test_putting_a_central_default_back_removes_it_so_it_cannot_mask_a_targets_own_setting():
    s = OC.load_settings("missing.yaml")
    SV.apply_controls(s, {"services.scrapling.defaults.identity": "X/1", "services.scrapling.defaults.delay": 20})
    assert s["services"]["scrapling"]["defaults"] == {**ON, "identity": "X/1", "delay": 20}
    SV.apply_controls(s, {"services.scrapling.defaults.identity": "", "services.scrapling.defaults.delay": 0})
    assert s["services"]["scrapling"]["defaults"] == ON                                   # back to the defaults: everything on, the numbers and texts unset
    SV.apply_controls(s, {"services.scrapling.defaults.solve_challenges": False})
    assert s["services"]["scrapling"]["defaults"] == {**ON, "solve_challenges": False}     # the owner turns one off


def test_a_value_set_in_the_file_is_labelled_observatory_yaml_not_default(tmp_path):
    s = load(tmp_path, {"agent_reach": {"channels": {"reddit": False}}})
    rows = {r["key"]: r for r in SV.effective_rows(s, tmp_path / "data")}
    assert rows["services.agent_reach.channels.reddit"]["source"] == "observatory.yaml" and SV.display(rows["services.agent_reach.channels.reddit"]["value"]) == "off"


# ---- the Controls tab and its read-back -------------------------------------------------------------------------------------------------------------

def tab_grid(spec, edits=None):
    grid = [spec["columns"]] + [[spec["rows_by_key"][k][c] for c in spec["columns"]] for k in spec["rows_by_key"]]
    for key, value in (edits or {}).items():
        for row in grid[1:]:
            if row[0] == key:
                row[1] = value
    return grid


def test_the_controls_tab_lists_every_control_with_its_value_default_source_and_meaning(tmp_path):
    s = OC.load_settings("missing.yaml")
    spec = SV.sheet_tab(s, tmp_path / "data", "2026-11-04T10:00:00Z")
    assert spec["title"] == "Controls" and spec["key_col"] == "key" and spec["columns"] == ["key", "value", "default", "source", "scope", "description"] and len(spec["rows_by_key"]) == len(SV.CONTROLS)
    row = spec["rows_by_key"]["services.scrapling.use_for.youtube_research"]
    assert row["value"] == "on" and row["default"] == "on" and row["source"] == "default" and "youtube research may use Scrapling" in row["description"]


def test_an_edit_in_the_tab_is_read_back_applied_saved_and_explained_while_the_tools_own_values_are_not_edits(tmp_path):
    data, now = tmp_path / "data", "2026-11-04T10:00:00Z"
    s = OC.load_settings("missing.yaml")
    spec = SV.sheet_tab(s, data, now)
    assert SV.apply_sheet_controls(tab_grid(spec), s, data, now) == ([], [])                    # nothing edited, nothing read
    grid = tab_grid(spec, {"services.scrapling.enabled": "off", "services.scrapling.defaults.delay": "25", "engine.pages": "scrapling", "services.agent_reach.channels.reddit": "OFF"})
    changes, problems = SV.apply_sheet_controls(grid, s, data, now)
    assert problems == [] and sorted(changes) == ["engine.pages: core -> scrapling", "services.agent_reach.channels.reddit: on -> off", "services.scrapling.defaults.delay: 0 -> 25", "services.scrapling.enabled: on -> off"]
    assert SV.load_overrides(data) == {"services.scrapling.enabled": False, "services.scrapling.defaults.delay": 25, "engine.pages": "scrapling", "services.agent_reach.channels.reddit": False}
    fresh = SV.load_settings(None, data)
    assert fresh["engine"]["pages"] == "scrapling" and fresh["services"]["scrapling"]["enabled"] is False and fresh["services"]["scrapling"]["defaults"] == {**ON, "delay": 25}


def test_a_stale_copy_of_the_tab_in_another_brands_sheet_cannot_undo_a_change(tmp_path):
    data, now = tmp_path / "data", "2026-11-04T10:00:00Z"
    s = OC.load_settings("missing.yaml")
    spec_a = SV.sheet_tab(s, data, now, sheet_id="brand-a")
    spec_b = SV.sheet_tab(s, data, now, sheet_id="brand-b")
    b_copy = tab_grid(spec_b)                                                                   # what brand B's sheet still shows
    changes, _ = SV.apply_sheet_controls(tab_grid(spec_a, {"services.scrapling.enabled": "off"}), s, data, now, sheet_id="brand-a")      # brand A's sheet: the owner switched Scrapling off
    assert changes == ["services.scrapling.enabled: on -> off"]
    s2 = SV.load_settings(None, data)
    SV.sheet_tab(s2, data, now, sheet_id="brand-a")                                             # brand A's tab is rewritten with the new value
    changes, problems = SV.apply_sheet_controls(b_copy, s2, data, now, sheet_id="brand-b")      # brand B's sheet is read next, still showing "on"
    assert changes == [] and SV.load_overrides(data) == {"services.scrapling.enabled": False}
    spec_b2 = SV.sheet_tab(s2, data, now, sheet_id="brand-b")
    assert spec_b2["rows_by_key"]["services.scrapling.enabled"]["value"] == "off"               # and is then rewritten to match
    changes, _ = SV.apply_sheet_controls(tab_grid(spec_b2, {"services.scrapling.enabled": "on"}), s2, data, now, sheet_id="brand-b")      # a real edit in brand B's sheet still counts
    assert changes == ["services.scrapling.enabled: off -> on"] and SV.load_overrides(data) == {}


def test_a_value_set_back_to_its_default_removes_the_owners_override_and_a_bad_value_is_reported_and_ignored(tmp_path):
    data, now = tmp_path / "data", "2026-11-04T10:00:00Z"
    s = OC.load_settings("missing.yaml")
    spec = SV.sheet_tab(s, data, now)
    SV.apply_sheet_controls(tab_grid(spec, {"services.scrapling.defaults.locale": "de-DE"}), s, data, now)
    s2 = SV.load_settings(None, data)
    spec2 = SV.sheet_tab(s2, data, now)
    changes, problems = SV.apply_sheet_controls(tab_grid(spec2, {"services.scrapling.defaults.locale": ""}), s2, data, now)       # a blank cell is not an edit
    assert changes == [] and problems == []
    changes, problems = SV.apply_sheet_controls(tab_grid(spec2, {"services.scrapling.enabled": "perhaps", "services.scrapling.defaults.delay": "fast"}), s2, data, now)
    assert changes == [] and sorted(problems) == ["services.scrapling.defaults.delay must be a number", "services.scrapling.enabled must be on or off"]
    s3 = OC.load_settings("missing.yaml")
    spec3 = SV.sheet_tab(SV.apply_controls(s3, {"services.scrapling.defaults.locale": "de-DE"}), data, now)
    SV.apply_sheet_controls(tab_grid(spec3, {"services.scrapling.defaults.locale": "off"}), s3, data, now)
    assert SV.apply_sheet_controls([], s3, data, now) == ([], []) and SV.apply_sheet_controls([["a", "b"]], s3, data, now) == ([], [])


def test_acceptance_checks_leave_out_and_explain_a_route_the_owner_switched_off():
    from contentintel import agent_reach_instagram as AR
    from contentintel.observatory import acceptance_checks as AK
    s = OC.load_settings("missing.yaml")
    ready = AR.OpenCLI("opencli", which=lambda b: "/bin/opencli", environ={}, site="reddit")
    assert ("reddit", "agent_reach") in AK.reddit_checks(s, "a@b.example", environ={}, cli=ready)
    s["services"]["agent_reach"]["channels"]["reddit"] = False
    assert ("reddit", "agent_reach") not in AK.reddit_checks(s, "a@b.example", environ={}, cli=ready)
    missing = AK.reddit_missing(s, "a@b.example", environ={})
    assert missing[("reddit", "agent_reach")].startswith("switched off: the reddit channel is switched off (services.agent_reach.channels.reddit)")
    s["services"]["agent_reach"]["use_for"]["youtube_research"] = False
    assert "services.agent_reach.use_for.youtube_research" in AK.reddit_missing(s, "a@b.example", environ={})[("agent_reach_research", "youtube")]


# ---- the registry: a new repository adds a service and gets validation, defaults, controls and the Controls tab -----------------------------------

@pytest.fixture
def demo_service():
    SV.register_service("demo_repo", label="Demo repository (adapter)", channels=("alpha", "beta"), purposes=("competitors", "general_research", "collectors"),
                        routes={"purposes": ("collectors",), "choices": ("old", "new"), "default": "old"})
    yield "demo_repo"
    SV.unregister_service("demo_repo")


def test_a_registered_service_is_validated_defaulted_controlled_and_shown_with_no_other_change(demo_service, tmp_path):
    s = OC.load_settings("missing.yaml")["services"]["demo_repo"]
    assert s == {"enabled": True, "use_for": {"competitors": True, "general_research": True, "collectors": True}, "channels": {"alpha": True, "beta": True}, "routes": {"collectors": "old"}}
    keys = [c["key"] for c in SV.CONTROLS if c["key"].startswith("services.demo_repo.")]
    assert keys == ["services.demo_repo.enabled", "services.demo_repo.use_for.competitors", "services.demo_repo.use_for.general_research", "services.demo_repo.use_for.collectors",
                    "services.demo_repo.channels.alpha", "services.demo_repo.channels.beta", "services.demo_repo.routes.collectors"]
    got = load(tmp_path, {"demo_repo": {"enabled": True, "channels": {"beta": False}, "routes": {"collectors": "new"}, "use_for": {"competitors": False}}})["services"]["demo_repo"]
    assert got["channels"] == {"alpha": True, "beta": False} and got["routes"] == {"collectors": "new"} and got["use_for"]["competitors"] is False
    settings = OC.load_settings("missing.yaml")
    assert SV.allowed(settings, "demo_repo", "collectors") == (True, "") and "channels.beta" in SV.allowed({**settings, "services": {**settings["services"], "demo_repo": got}}, "demo_repo", "collectors", "beta")[1]
    spec = SV.sheet_tab(settings, tmp_path / "d", "2026-11-04T10:00:00Z")
    assert "services.demo_repo.routes.collectors" in spec["rows_by_key"] and spec["rows_by_key"]["services.demo_repo.channels.alpha"]["scope"] == "Demo repository"


def test_a_registered_services_switches_can_be_edited_in_the_controls_tab_and_survive_a_reload(demo_service, tmp_path):
    data, now = tmp_path / "data", "2026-11-04T10:00:00Z"
    s = OC.load_settings("missing.yaml")
    spec = SV.sheet_tab(s, data, now)
    changes, problems = SV.apply_sheet_controls(tab_grid(spec, {"services.demo_repo.channels.beta": "off", "services.demo_repo.routes.collectors": "new"}), s, data, now)
    assert problems == [] and sorted(changes) == ["services.demo_repo.channels.beta: on -> off", "services.demo_repo.routes.collectors: old -> new"]
    fresh = SV.load_settings(None, data)
    assert fresh["services"]["demo_repo"]["channels"]["beta"] is False and fresh["services"]["demo_repo"]["routes"]["collectors"] == "new"


def test_registration_rules_and_the_built_in_services_cannot_be_removed():
    with pytest.raises(ValueError, match="lowercase"):
        SV.register_service("Bad Name")
    for name in ("scrapling", "agent_reach", "search", "ai_gateway", "page_readers"):
        with pytest.raises(ValueError, match="built-in"):
            SV.unregister_service(name)
    before = len(SV.CONTROLS)
    SV.register_service("temp_one")
    SV.register_service("temp_one", channels=("x",))                                            # registering again refines the entry rather than duplicating it
    assert len([c for c in SV.CONTROLS if c["key"].startswith("services.temp_one.")]) == 12 + 1 + 1
    SV.unregister_service("temp_one")
    assert len(SV.CONTROLS) == before and "temp_one" not in OC.load_settings("missing.yaml")["services"]


def test_a_service_nobody_registered_is_refused_in_the_settings_and_by_allowed(tmp_path):
    with pytest.raises(OC.ObservatoryConfigError, match="unknown setting 'firecrawl'"):
        load(tmp_path, {"firecrawl": {"enabled": True}})
    ok, why = SV.allowed(OC.load_settings("missing.yaml"), "firecrawl", "competitors")
    assert not ok and "not a registered service" in why


@pytest.mark.parametrize("services, fragment", [({"page_readers": {"routes": {"brief": "scrapling"}}}, "unknown setting|must be one of"), ({"page_readers": {"routes": {"competitors": "magic"}}}, "must be one of jina, core, scrapling, auto"),
                                                ({"ai_gateway": {"routes": {"brief": "gateway"}}}, "must be one of claude, gateway_first"), ({"ai_gateway": {"routes": {"competitors": "claude"}}}, "unknown setting"),
                                                ({"search": {"channels": {"bing": True}}}, "unknown setting"), ({"search": {"channels": {"tavily": "no"}}}, "true or false"),
                                                ({"ai_gateway": {"defaults": {}}}, "unknown setting"), ({"search": "off"}, "must be name: value settings")])
def test_the_new_services_settings_are_validated_with_their_own_names(tmp_path, services, fragment):
    if fragment.startswith("unknown setting|"):
        services = {"page_readers": {"routes": {"nonsense_purpose": "core"}}}
        fragment = "unknown setting"
    with pytest.raises(OC.ObservatoryConfigError, match=fragment):
        load(tmp_path, services)


# ---- search: one interface for every provider, under the owner's switches --------------------------------------------------------------------------

class P:
    def __init__(self, name):
        self.name = name


def built(env):
    return {n: P(n) for n in ("serper_dev", "tavily", "serpapi", "exa", "exa_mcp")}


def test_search_providers_are_filtered_by_the_service_the_purpose_and_each_provider():
    s = OC.load_settings("missing.yaml")
    assert sorted(SV.search_providers(s, "general_research", {}, build=built)) == ["exa", "exa_mcp", "serpapi", "serper_dev", "tavily"]
    s["services"]["search"]["channels"]["serper_dev"] = False
    assert "serper_dev" not in SV.search_providers(s, "general_research", {}, build=built) and len(SV.search_providers(s, "discovery", {}, build=built)) == 4
    s["services"]["search"]["use_for"]["discovery"] = False
    assert SV.search_providers(s, "discovery", {}, build=built) == {} and len(SV.search_providers(s, "general_research", {}, build=built)) == 4
    s["services"]["search"]["enabled"] = False
    assert SV.search_providers(s, "general_research", {}, build=built) == {}


def test_search_providers_are_built_from_the_environment_by_default_and_a_missing_key_means_no_provider(monkeypatch):
    for name in ("SERPER_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY", "EXA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    s = OC.load_settings("missing.yaml")
    assert "serper_dev" not in SV.search_providers(s, "general_research", {})
    assert "serper_dev" in SV.search_providers(s, "general_research", {"SERPER_API_KEY": "k-123456"})


# ---- the AI Gateway: a Claude-compatible adapter that falls back to Claude ------------------------------------------------------------------------

class Claude:
    model = "claude-x"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens=None):
        self.calls.append("complete")
        return "claude text"

    def json_task(self, system, user, max_tokens=None):
        self.calls.append("json")
        return {"from": "claude"}

    def json_task_with_images(self, system, user, images, max_tokens=None):
        self.calls.append("images")
        return {"from": "claude-vision"}

    def answer(self, a, b, c):
        return "answered"

    def prepare_prompt(self, question, context):
        return "claude prompt"


class Router:
    def __init__(self, status="ok", text="gateway text", output=None):
        self.status, self.text, self.output, self.asked = status, text, output if output is not None else {"from": "gateway"}, []

    def run(self, lane, *, data_class, instructions, context=(), now, system="", item_ids=()):
        self.asked.append((lane, data_class, instructions, system, item_ids))
        return {"status": self.status, "text": self.text, "output": self.output, "reason": "no route produced a usable answer"}


def routed_settings(purpose="brief", route="gateway_first"):
    s = OC.load_settings("missing.yaml")
    s["services"]["ai_gateway"]["routes"][purpose] = route
    return s


def factory_for(router, finished):
    return lambda: (router, lambda: finished.append(1))


def test_by_default_every_module_tries_the_gateway_first_and_the_owner_can_send_any_purpose_back_to_plain_claude():
    claude = Claude()
    for purpose in ("general_research", "idea_bank", "planning", "brief", "deep_check", "autoseeds"):
        assert isinstance(SV.ai_client(OC.load_settings("missing.yaml"), purpose, claude, stack_factory=lambda: None, now_fn=lambda: "t"), SV.RoutedClaude)
        s = OC.load_settings("missing.yaml")
        s["services"]["ai_gateway"]["routes"][purpose] = "claude"                   # the owner's explicit choice
        assert SV.ai_client(s, purpose, claude, stack_factory=lambda: None, now_fn=lambda: "t") is claude
    assert SV.ai_client(routed_settings(), "brief", None, stack_factory=lambda: None, now_fn=lambda: "t") is None


def test_the_gateway_is_used_only_for_a_purpose_routed_to_it_and_only_while_its_switches_are_on():
    claude = Claude()
    wrapped = SV.ai_client(routed_settings("brief"), "brief", claude, stack_factory=lambda: None, now_fn=lambda: "t")
    s = routed_settings("brief")
    s["services"]["ai_gateway"]["routes"]["planning"] = "claude"
    assert isinstance(wrapped, SV.RoutedClaude) and SV.ai_client(s, "planning", claude, stack_factory=lambda: None, now_fn=lambda: "t") is claude
    s["services"]["ai_gateway"]["use_for"]["brief"] = False
    assert SV.ai_client(s, "brief", claude, stack_factory=lambda: None, now_fn=lambda: "t") is claude
    s = routed_settings("brief")
    s["services"]["ai_gateway"]["enabled"] = False
    assert SV.ai_client(s, "brief", claude, stack_factory=lambda: None, now_fn=lambda: "t") is claude


def test_a_routed_client_answers_from_the_gateway_with_internal_data_and_the_right_lane_and_closes_it_once():
    claude, router, finished = Claude(), Router(), []
    wrapped = SV.RoutedClaude(claude, "brief", factory_for(router, finished), lambda: "2026-11-04T10:00:00Z")
    assert wrapped.complete("SYS", "USER", 500) == "gateway text" and wrapped.json_task("SYS2", "USER2") == {"from": "gateway"}
    assert [(a[0], a[1], a[3], a[4]) for a in router.asked] == [("general_text", "internal", "SYS", ("brief",)), ("general_json", "internal", "SYS2", ("brief",))]
    assert claude.calls == [] and wrapped.calls == ["gateway", "gateway"] and wrapped.model == "claude-x"
    wrapped.close()
    wrapped.close()
    assert finished == [1]


@pytest.mark.parametrize("router", [Router(status="deferred"), Router(status="refused"), Router(output={}), Router(output="not json")])
def test_when_the_gateway_has_no_usable_answer_the_module_gets_claudes_exactly_as_before(router):
    claude = Claude()
    wrapped = SV.RoutedClaude(claude, "planning", factory_for(router, []), lambda: "t")
    assert wrapped.json_task("S", "U") == {"from": "claude"} and claude.calls == ["json"] and wrapped.calls == ["claude"]
    if router.status != "ok":
        assert any("AI Gateway had no answer for planning" in n and "Claude was used" in n for n in wrapped.notes)


def test_the_collectors_two_prompt_calls_go_through_the_gateway_when_routed_and_keep_claudes_no_question_rule():
    claude, router = Claude(), Router(text="Please research protein bar trends in the UK.")
    wrapped = SV.RoutedClaude(claude, "collectors", factory_for(router, []), lambda: "t")
    assert wrapped.prepare_prompt("What is trending?", "brand facts") == "Please research protein bar trends in the UK."
    assert "Brand context:\nbrand facts" in router.asked[0][2] and "Research question:\nWhat is trending?" in router.asked[0][2] and router.asked[0][1] == "internal"
    assert wrapped.answer("orig", "ctx", "Shall I continue?") == "Please research protein bar trends in the UK."
    quiet = SV.RoutedClaude(claude, "collectors", factory_for(Router(text="NO_QUESTION"), []), lambda: "t")
    assert quiet.answer("orig", "ctx", "Here is the plan") == ""                                  # the same rule Claude's own method follows
    down = SV.RoutedClaude(claude, "collectors", factory_for(Router(status="deferred"), []), lambda: "t")
    assert down.prepare_prompt("q", "c") == "claude prompt" and down.answer("o", "c", "m") == "answered" and down.calls == ["claude", "claude"]


def test_a_blank_text_answer_falls_back_to_claude_for_text_only_while_a_valid_json_answer_is_still_used():
    claude = Claude()
    wrapped = SV.RoutedClaude(claude, "autoseeds", factory_for(Router(text="   "), []), lambda: "t")
    assert wrapped.complete("S", "U") == "claude text" and wrapped.json_task("S", "U") == {"from": "gateway"} and wrapped.calls == ["claude", "gateway"]


def test_pictures_always_go_to_claude_and_a_gateway_that_cannot_start_never_stops_the_module():
    claude = Claude()
    wrapped = SV.RoutedClaude(claude, "idea_bank", factory_for(Router(), []), lambda: "t")
    assert wrapped.json_task_with_images("S", "U", ["img"]) == {"from": "claude-vision"} and wrapped.calls == ["claude"]

    def broken():
        raise RuntimeError("the gateway is not built")
    down = SV.RoutedClaude(Claude(), "brief", broken, lambda: "t")
    assert down.complete("S", "U") == "claude text" and down.json_task("S", "U") == {"from": "claude"} and down.calls == ["claude", "claude"]
    assert down.notes == ["AI Gateway not available: RuntimeError: the gateway is not built"]                           # said once, not on every call
    none_stack = SV.RoutedClaude(Claude(), "brief", lambda: None, lambda: "t")
    assert none_stack.json_task("S", "U") == {"from": "claude"}


# ---- the page readers: the owner's choice, an auto chain, and no escalation after a refusal ----------------------------------------------------------

def page(engine):
    return lambda url: {"url": url, "title": engine + " title", "text": engine + " text"}


def failing(code, message="boom"):
    def read(url):
        raise K.Failure(code, message)
    return read


ENGINES = {"scrapling": page("scrapling"), "jina": page("jina"), "core": page("core")}


@pytest.mark.parametrize("purpose, expected", [("content_research", "scrapling"), ("competitors", "scrapling"), ("youtube_research", "scrapling"), ("general_research", "scrapling"), ("brief", "scrapling")])
def test_every_purpose_reads_with_scrapling_first_by_default_and_with_the_owners_engine_when_chosen(purpose, expected):
    got = SV.read_page(OC.load_settings("missing.yaml"), "https://x.example/", purpose, ENGINES)
    for chosen in ("jina", "core"):
        s = OC.load_settings("missing.yaml")
        s["services"]["page_readers"]["routes"][purpose] = chosen
        assert SV.read_page(s, "https://x.example/", purpose, ENGINES)["engine"] == chosen
    assert got["engine"] == expected and got["text"] == expected + " text" and got["social"] == {} and got["skipped"] == []


def test_an_explicit_engine_wins_over_the_route_and_an_unknown_one_is_refused():
    s = OC.load_settings("missing.yaml")
    assert SV.read_page(s, "https://x.example/", "content_research", ENGINES, engine="core")["engine"] == "core"
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "content_research", ENGINES, engine="magic")
    assert seen.value.code == K.BAD_INPUT and "is not one of jina, core, scrapling, auto" in str(seen.value)


def test_auto_passes_the_page_on_when_an_engine_is_unavailable_or_fails_for_an_ordinary_reason():
    s = OC.load_settings("missing.yaml")
    got = SV.read_page(s, "https://x.example/", "general_research", {"scrapling": failing(K.NEED_SETUP), "jina": failing(K.UPSTREAM_BROKEN), "core": page("core")})
    assert got["engine"] == "core" and got["skipped"] == ["scrapling: NEED_SETUP", "jina: UPSTREAM_BROKEN"]
    got = SV.read_page(s, "https://x.example/", "general_research", {"core": page("core")})
    assert got["engine"] == "core" and got["skipped"] == ["scrapling: not available here", "jina: not available here"]
    s["services"]["scrapling"]["use_for"]["general_research"] = False
    got = SV.read_page(s, "https://x.example/", "general_research", ENGINES)
    assert got["engine"] == "jina" and "scrapling: Scrapling is switched off for general research (services.scrapling.use_for.general_research)" in got["skipped"]


@pytest.mark.parametrize("code", [K.PLATFORM_BLOCKED, K.RATE_LIMITED, K.NOT_FOUND])
def test_a_refusal_ends_the_chain_so_no_other_engine_is_ever_tried_after_it(code):
    tried = []

    def spy(name):
        def read(url):
            tried.append(name)
            return {"url": url, "title": "", "text": "x"}
        return read
    s = OC.load_settings("missing.yaml")
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "general_research", {"scrapling": failing(code), "jina": spy("jina"), "core": spy("core")})
    assert seen.value.code == code and tried == []
    tried.clear()
    with pytest.raises(K.Failure) as seen:                                                      # the same in the middle of the chain
        SV.read_page(s, "https://x.example/", "general_research", {"scrapling": failing(K.NEED_SETUP), "jina": failing(code), "core": spy("core")})
    assert seen.value.code == code and tried == []


def test_an_explicit_engine_raises_its_own_failure_and_a_chain_with_nothing_available_says_so():
    s = OC.load_settings("missing.yaml")
    s["services"]["page_readers"]["routes"]["content_research"] = "jina"                    # the owner's explicit choice of one engine: its own failure, no other engine tried
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "content_research", {"jina": failing(K.UPSTREAM_BROKEN, "jina is down"), "core": page("core")})
    assert seen.value.code == K.UPSTREAM_BROKEN and "jina is down" in str(seen.value)
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "general_research", {})
    assert seen.value.code == K.NEED_SETUP and "no page reader could read this page: scrapling: not available here; jina: not available here; core: not available here" in str(seen.value)
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "general_research", {"scrapling": failing(K.TIMEOUT), "jina": failing(K.UPSTREAM_BROKEN), "core": failing(K.TIMEOUT)})
    assert seen.value.code == K.TIMEOUT


def test_the_reader_service_and_each_purpose_have_their_own_switches_and_naming_the_switch_in_the_message():
    s = OC.load_settings("missing.yaml")
    s["services"]["page_readers"]["use_for"]["brief"] = False
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "brief", ENGINES)
    assert seen.value.code == K.BAD_INPUT and "services.page_readers.use_for.brief" in str(seen.value)
    assert SV.read_page(s, "https://x.example/", "planning", ENGINES)["engine"] == "scrapling"
    s["services"]["page_readers"]["enabled"] = False
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "planning", ENGINES)
    assert "services.page_readers.enabled" in str(seen.value)


def test_the_older_single_switch_makes_scrapling_the_only_reader_for_competitor_and_discovery_pages_and_never_overrides_an_owner_route():
    s = OC.load_settings("missing.yaml")
    assert SV.effective_engine(s, "competitors", scrapling_installed=True) == "auto"          # the default already tries Scrapling first
    s["engine"]["pages"] = "scrapling"
    for purpose in ("competitors", "discovery", "social_discovery"):
        assert SV.effective_engine(s, purpose, scrapling_installed=True) == "scrapling" and SV.effective_engine(s, purpose, scrapling_installed=False) == "auto"
    assert SV.effective_engine(s, "content_research", scrapling_installed=True) == "auto" and SV.effective_engine(s, "brief", scrapling_installed=True) == "auto"
    for chosen in ("jina", "core"):
        s["services"]["page_readers"]["routes"]["competitors"] = chosen
        assert SV.effective_engine(s, "competitors", scrapling_installed=True) == chosen            # an explicit route beats the older switch (and the workflow's box)

# ---- the crawl service: any module, any repository, under the same controls --------------------------------------------------------------------------

def _crawl_result(pages, skipped=(), stopped="no more pages to follow", hosts=None):
    return {"ok": True, "scrapling_version": "0.5", "hosts": hosts or {}, "results": [{"url": "https://site.example/", "ok": True, "mode": "crawl", "pages": list(pages), "skipped": list(skipped), "stopped": stopped}]}


def test_the_crawl_service_resolves_the_options_like_a_page_uses_the_owners_limits_and_clamps_silly_ones(monkeypatch):
    seen = {}

    def fake_run(job, python, timeout=600, environ=None, runner=None):
        seen.update(job=job, timeout=timeout)
        return _crawl_result([{"url": "https://site.example/", "title": "Home", "markdown": "# Home"}])
    monkeypatch.setattr(WC, "run_worker", fake_run)
    s = settings_with(defaults={"locale": "en-GB"}, overrides=[{"match": "site.example", "delay": 12}])
    got = SV.scrapling_crawl(s, "https://www.site.example/", "general_research", contact="a@b.example", python="py")
    item = seen["job"]["items"][0]
    assert got["pages"][0]["title"] == "Home" and item["crawl"] == {"max_pages": 25, "depth": 2, "sitemap": False} and item["advanced"]["locale"] == "en-GB" and item["advanced"]["delay"] >= 12
    s["crawl"] = {"max_pages": 7, "depth": 1}
    SV.scrapling_crawl(s, "https://site.example/", "general_research", contact="a@b.example", python="py")
    assert seen["job"]["items"][0]["crawl"] == {"max_pages": 7, "depth": 1, "sitemap": False}                      # the owner's crawl settings are the defaults
    SV.scrapling_crawl(s, "https://site.example/", "general_research", contact="a@b.example", python="py", max_pages=100000, depth=0)
    assert seen["job"]["items"][0]["crawl"] == {"max_pages": 200, "depth": 0, "sitemap": False}                    # a caller's own limits win, within the hard caps
    SV.scrapling_crawl(s, "https://site.example/", "general_research", contact="a@b.example", python="py", max_pages=1, depth=99)
    assert seen["job"]["items"][0]["crawl"] == {"max_pages": 1, "depth": 6, "sitemap": False} and seen["timeout"] >= 600


def test_the_crawl_obeys_the_owners_scrapling_switches_and_a_resting_site_is_not_asked(monkeypatch, tmp_path):
    from contentintel.observatory.pauses import PauseLedger
    calls = []
    monkeypatch.setattr(WC, "run_worker", lambda job, python, **kw: calls.append(job) or _crawl_result([{"url": "u", "title": "t", "markdown": "m"}]))
    for key in ("enabled", "use_for"):
        s = settings_with()
        if key == "enabled":
            s["services"]["scrapling"]["enabled"] = False
        else:
            s["services"]["scrapling"]["use_for"]["brief"] = False
        with pytest.raises(K.Failure) as seen:
            SV.scrapling_crawl(s, "https://site.example/", "brief", contact="a@b.example", python="py")
        assert seen.value.code == K.NEED_SETUP and "services.scrapling" in str(seen.value) and calls == []
    pauses = PauseLedger(str(tmp_path))
    pauses.pause("site.example", 3600, "slow down", "2026-11-04T10:00:00Z", K.RATE_LIMITED)
    with pytest.raises(K.Failure) as seen:
        SV.scrapling_crawl(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py", pauses=pauses, now="2026-11-04T10:05:00Z")
    assert seen.value.code == K.RATE_LIMITED and "resting" in str(seen.value) and calls == []


def test_a_refusal_during_the_crawl_rests_the_site_and_a_crawl_that_read_nothing_raises_the_workers_reason(monkeypatch, tmp_path):
    from contentintel.observatory.pauses import PauseLedger
    monkeypatch.setattr(WC, "run_worker", lambda job, python, **kw: _crawl_result([{"url": "u", "title": "t", "markdown": "m"}], stopped="RATE_LIMITED: slow down", hosts={"site.example": {"requests": 3, "rest": "slow down"}}))
    pauses = PauseLedger(str(tmp_path))
    got = SV.scrapling_crawl(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py", pauses=pauses, now="2026-11-04T10:00:00Z")
    assert got["stopped"].startswith("RATE_LIMITED") and pauses.paused("site.example", "2026-11-04T10:01:00Z")                     # the site rests for the next run too
    monkeypatch.setattr(WC, "run_worker", lambda job, python, **kw: {"ok": True, "hosts": {}, "results": [{"url": "u", "ok": False, "mode": "crawl", "pages": [], "skipped": [], "stopped": "", "error": {"code": "PLATFORM_BLOCKED", "message": "robots.txt does not allow this address"}}]})
    with pytest.raises(K.Failure) as seen:
        SV.scrapling_crawl(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py")
    assert seen.value.code == K.PLATFORM_BLOCKED


def test_a_crawl_that_stopped_early_keeps_what_is_left_and_resume_hands_it_back_then_forgets_it(monkeypatch, tmp_path):
    jobs = []
    state = {"queue": [["https://site.example/b", 1]], "seen": ["https://site.example/a", "https://site.example/b"]}

    def fake_run(job, python, timeout=600, environ=None, runner=None):
        jobs.append(job["items"][0])
        done = len(jobs) > 1
        res = _crawl_result([{"url": "https://site.example/a", "title": "A", "markdown": "a"}], stopped="no more pages to follow" if done else "page limit reached")["results"][0]
        if not done:
            res["state"] = state
        return {"ok": True, "hosts": {}, "results": [res]}
    monkeypatch.setattr(WC, "run_worker", fake_run)
    kw = dict(contact="a@b.example", python="py", state_dir=str(tmp_path))
    first = SV.scrapling_crawl(settings_with(), "https://site.example/", "general_research", sitemap=True, **kw)
    assert first["state_saved"] and not first["resumed"] and "resume" not in jobs[0]["crawl"] and jobs[0]["crawl"]["sitemap"] is True
    saved = list((tmp_path / "_shared" / "crawl_state").glob("*.json"))
    assert len(saved) == 1
    again = SV.scrapling_crawl(settings_with(), "https://site.example/", "general_research", resume=True, extract=[{"name": "p", "css": ".price"}], **kw)
    assert again["resumed"] and jobs[1]["crawl"]["resume"] == state and jobs[1]["extract"] == [{"name": "p", "css": ".price"}] and not again["state_saved"] and not list((tmp_path / "_shared" / "crawl_state").glob("*.json"))
    jobs.clear()
    path = SV._crawl_state_path(str(tmp_path), "https://site.example/")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")                                                  # a paused crawl is waiting
    SV.scrapling_crawl(settings_with(), "https://site.example/", "general_research", **kw)                 # but a plain crawl (no resume) starts from the beginning, as asked
    assert "resume" not in jobs[0]["crawl"]


def test_feed_discovery_returns_advertised_feeds_and_feeds_that_really_answer_at_the_usual_places(monkeypatch):
    seen = {}

    def fake_run(job, python, timeout=600, environ=None, runner=None):
        seen["items"] = job["items"]
        probes = [{"url": it["url"], "ok": True, "raw": "<?xml version='1.0'?><rss version='2.0'><channel/></rss>" if it["url"].endswith("/feed") else "<html>not a feed</html>"} for it in job["items"][1:]]
        probes[-1] = {"url": job["items"][-1]["url"], "ok": False, "error": {"code": "UPSTREAM_BROKEN", "message": "x"}}
        return {"ok": True, "hosts": {}, "results": [{"url": "https://site.example/", "ok": True, "final_url": "https://site.example/", "alternates": [{"href": "/blog.atom"}, {"href": "https://site.example/blog.atom"}]}] + probes}
    monkeypatch.setattr(WC, "run_worker", fake_run)
    found = SV.discover_feeds(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py")
    assert found == [{"url": "https://site.example/blog.atom", "how": "linked from the page"}, {"url": "https://site.example/feed", "how": "found at a usual address"}]
    assert seen["items"][0] == {"url": "https://site.example/", "advanced": seen["items"][0]["advanced"]} and all(i.get("raw") for i in seen["items"][1:]) and 2 <= len(seen["items"]) <= 9
    s = settings_with()
    s["services"]["scrapling"]["use_for"]["general_research"] = False
    with pytest.raises(K.Failure) as off:
        SV.discover_feeds(s, "https://site.example/", "general_research", contact="a@b.example", python="py")
    assert off.value.code == K.NEED_SETUP and "services.scrapling.use_for.general_research" in str(off.value)
    monkeypatch.setattr(WC, "run_worker", lambda job, python, **kw: {"ok": True, "hosts": {}, "results": [{"url": "u", "ok": False, "error": {"code": "PLATFORM_BLOCKED", "message": "blocked"}}] * len(job["items"])})
    with pytest.raises(K.Failure) as blocked:
        SV.discover_feeds(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py")
    assert blocked.value.code == K.PLATFORM_BLOCKED


def test_feed_discovery_leaves_a_resting_site_alone_and_a_refusal_during_it_rests_the_site(monkeypatch, tmp_path):
    from contentintel.observatory.pauses import PauseLedger
    calls = []
    monkeypatch.setattr(WC, "run_worker", lambda job, python, **kw: calls.append(job) or {"ok": True, "hosts": {"site.example": {"requests": 2, "rest": "slow down"}},
                                                                                           "results": [{"url": "https://site.example/", "ok": True, "final_url": "https://site.example/", "alternates": [{"href": "/f.atom"}]}]})
    pauses = PauseLedger(str(tmp_path))
    found = SV.discover_feeds(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py", pauses=pauses, now="2026-11-04T10:00:00Z")
    assert found[0]["url"] == "https://site.example/f.atom" and pauses.paused("site.example", "2026-11-04T10:01:00Z")          # the refusal it met rests the site for the next run
    calls.clear()
    with pytest.raises(K.Failure) as seen:
        SV.discover_feeds(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py", pauses=pauses, now="2026-11-04T10:05:00Z")
    assert seen.value.code == K.RATE_LIMITED and "resting" in str(seen.value) and calls == []                                  # a resting site is not asked at all


# ---- every per-request option is on by default, and the defaults never cause a refusal themselves ---------------------------------------------------

def test_a_page_read_asks_for_every_option_by_default_including_challenge_solving_and_the_owner_can_turn_any_one_off(monkeypatch, tmp_path):
    from contentintel.observatory.snapshots import SnapshotStore
    seen = {}
    monkeypatch.setattr(WC, "run_worker", lambda job, python, **kw: seen.update(job=job) or {"ok": True, "hosts": {}, "results": [{"url": job["items"][0]["url"], "ok": True, "status": 200, "title": "T", "markdown": "# Hi", "final_url": job["items"][0]["url"]}]})
    SV.scrapling_page(settings_with(), "https://site.example/", "general_research", contact="a@b.example", python="py", snaps=SnapshotStore(tmp_path))
    adv = seen["job"]["items"][0]["advanced"]
    assert adv["stealth"] is True and adv["solve_cloudflare"] is True and adv["impersonate"] is True and adv["humanize"] is True and adv["hide_canvas"] is True and adv["block_webrtc"] is True
    assert adv["session"] is True and adv["disable_resources"] is True and "proxy_envs" not in adv                      # no proxy is listed, so the proxy default does nothing
    s = settings_with()
    s["services"]["scrapling"]["defaults"]["solve_challenges"] = False                                                   # the owner's choice
    SV.scrapling_page(s, "https://site.example/", "general_research", contact="a@b.example", python="py", snaps=SnapshotStore(tmp_path))
    assert "solve_cloudflare" not in seen["job"]["items"][0]["advanced"] and seen["job"]["items"][0]["advanced"]["humanize"] is True


def test_options_that_come_only_from_the_defaults_are_skipped_when_their_permission_is_off_but_an_explicit_request_still_refuses():
    s = settings_with()
    s["access_controls"].update(browser=False, evasion=False, session=False)
    got = SV.resolve_scrapling(s, "site.example", "competitors", None)
    assert not {"browser", "impersonate", "hide_canvas", "block_webrtc", "humanize", "solve_challenges", "session"} & set(got) and got["disable_resources"] is True
    comp = {"domain": "site.example", "advanced": SV.resolve_scrapling(s, "site.example", "competitors", None)}
    assert WC.advanced_for(comp, s) == {"disable_resources": True}                                                         # no refusal from the defaults themselves
    for explicit in ({"browser": True}, {"solve_challenges": True}, {"session": True}):
        comp = {"domain": "site.example", "advanced": SV.resolve_scrapling(s, "site.example", "competitors", explicit)}
        with pytest.raises(K.Failure) as seen:
            WC.advanced_for(comp, s)
        assert seen.value.code == K.BAD_INPUT and "switched off" in str(seen.value)                                        # a person's own request is refused, loudly, as before


def test_a_central_proxy_default_uses_the_listed_proxies_and_an_explicit_proxy_request_without_any_still_refuses():
    s = settings_with()
    assert "proxy" not in SV.resolve_scrapling(s, "site.example", "competitors", None)
    s["advanced"]["proxies"] = ["PROXY_A"]
    assert SV.resolve_scrapling(s, "site.example", "competitors", None)["proxy"] is True
    comp = {"domain": "site.example", "advanced": SV.resolve_scrapling(s, "site.example", "competitors", None)}
    assert WC.advanced_for(comp, s)["proxy_envs"] == ["OBSERVATORY_PROXY_PROXY_A"]
    s["advanced"]["proxies"] = []
    assert SV.resolve_scrapling(s, "site.example", "competitors", {"proxy": True})["proxy"] is True                         # explicit: kept, and advanced_for refuses with the reason
    with pytest.raises(K.Failure, match="no proxy names"):
        WC.advanced_for({"domain": "site.example", "advanced": {"proxy": True}}, s)
