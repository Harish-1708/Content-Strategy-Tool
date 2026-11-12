"""How ANOTHER repository plugs into the platform's shared services. SIMULATED: the 'other repository' is a throwaway package written into a temporary folder, using only the public interface of contentintel.services.
This shows the mechanism works; it is not evidence from a real second repository."""
import importlib
import importlib.metadata
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
import yaml

from contentintel import contract as K
from contentintel import services as SV
from contentintel.observatory import config as OC

ROOT = Path(__file__).resolve().parents[1]

OTHER_REPO_MODULE = '''
"""A module of ANOTHER repository. It knows nothing of this repository except the public services interface."""
from contentintel import services as SV


def register(api):
    api.register_purpose("outreach", ai_route=True)
    api.register_service("mailer_enrich", label="Mailer enrichment (the other repository's adapter)", channels=("lookup",), purposes=("outreach",),
                         routes={"purposes": ("outreach",), "choices": ("old", "new"), "default": "old"})


def enrich(settings, url, claude):
    ok, why = SV.allowed(settings, "mailer_enrich", "outreach", "lookup")
    if not ok:
        return {"skipped": why}
    page = SV.read_page(settings, url, "outreach", {"core": lambda u: {"url": u, "title": "T", "text": "page text"}})
    ai = SV.ai_client(settings, "outreach", claude, stack_factory=lambda: None, now_fn=lambda: "t")
    return {"engine": page["engine"], "ai": type(ai).__name__, "route": SV.route_for(settings, "mailer_enrich", "outreach")}
'''


class EntryPoint:
    """What an installed package's entry point looks like to the loader."""

    def __init__(self, name, target):
        self.name, self.target = name, target

    def load(self):
        module, _, attr = self.target.partition(":")
        return getattr(importlib.import_module(module), attr)


@pytest.fixture
def other_repo(tmp_path, monkeypatch):
    pkg = tmp_path / "repo" / "other_repo_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "collector.py").write_text(OTHER_REPO_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path / "repo"))
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group=None: [EntryPoint("other_repo", "other_repo_pkg.collector:register")] if group == SV.PLUGIN_GROUP else [])
    monkeypatch.delenv("CONTENTINTEL_SERVICES_FILE", raising=False)
    monkeypatch.chdir(tmp_path)                                                                  # no services_extra.yaml here
    SV.NOTES.clear()
    yield pkg
    SV.unregister_service("mailer_enrich") if "mailer_enrich" in OC.SERVICE_REGISTRY else None
    if "outreach" in OC.PURPOSES:
        SV.unregister_purpose("outreach")
    sys.modules.pop("other_repo_pkg.collector", None)
    sys.modules.pop("other_repo_pkg", None)
    SV._PLUGINS_DONE = False


def settings_file(tmp_path, **services):
    path = tmp_path / "observatory.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "services": services}), encoding="utf-8")
    return path


def test_a_plugin_in_another_repository_registers_its_purpose_and_service_by_being_installed(other_repo, tmp_path):
    assert SV.load_plugins(force=True) == ["other_repo"] and SV.NOTES == []
    assert "outreach" in OC.PURPOSES and "outreach" in OC.AI_PURPOSES and "mailer_enrich" in OC.SERVICE_REGISTRY
    s = SV.load_settings(settings_file(tmp_path, mailer_enrich={"routes": {"outreach": "new"}, "channels": {"lookup": True}}, scrapling={"overrides": [{"match": "purpose:outreach", "locale": "en-GB"}]}), tmp_path / "data")
    for service in ("scrapling", "agent_reach", "search", "ai_gateway", "page_readers"):
        assert s["services"][service]["use_for"]["outreach"] is True                             # every shared service has a switch for the new kind of work
    assert s["services"]["ai_gateway"]["routes"]["outreach"] == "gateway_first" and s["services"]["page_readers"]["routes"]["outreach"] == "auto"
    assert s["services"]["mailer_enrich"]["routes"] == {"outreach": "new"} and s["services"]["scrapling"]["overrides"] == [{"match": "purpose:outreach", "locale": "en-GB"}]
    keys = {c["key"] for c in SV.CONTROLS}
    assert {"services.scrapling.use_for.outreach", "services.search.use_for.outreach", "services.mailer_enrich.channels.lookup", "services.mailer_enrich.routes.outreach", "services.ai_gateway.routes.outreach"} <= keys


def test_the_other_repositorys_code_uses_every_shared_interface_under_the_owners_controls(other_repo, tmp_path):
    SV.load_plugins(force=True)
    import other_repo_pkg.collector as collector
    data = tmp_path / "data"
    s = SV.load_settings(None, data)
    claude = object()
    assert collector.enrich(s, "https://x.example/", claude) == {"engine": "core", "ai": "RoutedClaude", "route": "old"}          # defaults: the gateway first (no provider here, so Claude answers)
    spec = SV.sheet_tab(s, data, "2026-11-04T10:00:00Z", sheet_id="other")
    grid = [spec["columns"]] + [[spec["rows_by_key"][k][c] for c in spec["columns"]] for k in spec["rows_by_key"]]
    for row in grid[1:]:
        if row[0] == "services.ai_gateway.routes.outreach":
            row[1] = "claude"                                                                   # the owner sends this repository's purpose back to plain Claude (the default is the gateway first)
        if row[0] == "services.mailer_enrich.routes.outreach":
            row[1] = "new"
    changes, problems = SV.apply_sheet_controls(grid, s, data, "2026-11-04T10:00:00Z", sheet_id="other")
    assert problems == [] and len(changes) == 2                                                  # the owner changed the other repository's settings in the Controls tab, with no developer
    s = SV.load_settings(None, data)
    assert collector.enrich(s, "https://x.example/", claude) == {"engine": "core", "ai": "object", "route": "new"}
    s["services"]["mailer_enrich"]["channels"]["lookup"] = False
    assert "services.mailer_enrich.channels.lookup" in collector.enrich(s, "https://x.example/", claude)["skipped"]
    s = SV.load_settings(None, data)
    s["services"]["page_readers"]["use_for"]["outreach"] = False
    with pytest.raises(K.Failure) as seen:
        collector.enrich(s, "https://x.example/", claude)
    assert "services.page_readers.use_for.outreach" in str(seen.value)


def test_the_other_repository_gets_search_scrapling_and_agent_reach_for_its_own_purpose_with_the_same_switches(other_repo, tmp_path):
    SV.load_plugins(force=True)
    s = SV.load_settings(settings_file(tmp_path, scrapling={"defaults": {"identity": "Central/1.0"}, "overrides": [{"match": "purpose:outreach", "locale": "en-GB"}, {"match": "*.partner.example", "delay": 30}]}), tmp_path / "data")
    assert SV.resolve_scrapling(s, "www.partner.example", "outreach", {"timezone": "Europe/London"}) == {**{k: v for k, v in OC.SCRAPLING_ON_DEFAULTS.items() if k != "proxy"}, "identity": "Central/1.0", "locale": "en-GB", "delay": 30, "timezone": "Europe/London"}
    built = lambda env: {"serper_dev": object(), "tavily": object()}                            # noqa: E731
    assert sorted(SV.search_providers(s, "outreach", {}, build=built)) == ["serper_dev", "tavily"]
    s["services"]["search"]["use_for"]["outreach"] = False
    assert SV.search_providers(s, "outreach", {}, build=built) == {}
    assert SV.agent_reach_client(s, "reddit", "outreach", environ={}, factory=lambda: "client") == "client"
    s["services"]["agent_reach"]["use_for"]["outreach"] = False
    with pytest.raises(K.Failure) as seen:
        SV.agent_reach_client(s, "reddit", "outreach", environ={}, factory=lambda: "client")
    assert "services.agent_reach.use_for.outreach" in str(seen.value)


def test_a_repository_can_declare_services_and_purposes_in_a_yaml_file_with_no_code(other_repo, tmp_path, monkeypatch):
    declared = tmp_path / "declared.yaml"
    declared.write_text(yaml.safe_dump({"purposes": ["outreach", {"name": "reporting", "ai_route": True}, 5],
                                        "services": [{"name": "firecrawl", "label": "Firecrawl", "channels": ["scrape"], "purposes": ["outreach"], "routes": {"purposes": ["outreach"], "choices": ["old", "new"], "default": "old"}},
                                                     {"name": "Bad Name"}, {"name": "extra_keys", "surprise": 1}, {"name": "bad_routes", "routes": {"choices": ["a"]}}, "not a mapping"]}))
    monkeypatch.setenv("CONTENTINTEL_SERVICES_FILE", str(declared))
    try:
        assert SV.load_declared() == ["firecrawl"]                                               # the good entry is registered; every bad one is skipped
        assert {"outreach", "reporting"} <= set(OC.PURPOSES) and "reporting" in OC.AI_PURPOSES and "outreach" not in OC.AI_PURPOSES
        assert len(SV.NOTES) == 5 and any("Bad Name" in n for n in SV.NOTES) and any("surprise" in n or "may have" in n for n in SV.NOTES) and any("routes needs choices and default" in n for n in SV.NOTES)
        s = SV.load_settings(settings_file(tmp_path, firecrawl={"channels": {"scrape": False}}), tmp_path / "data")
        assert s["services"]["firecrawl"]["channels"] == {"scrape": False} and SV.allowed(s, "firecrawl", "outreach", "scrape")[0] is False
        assert "services.firecrawl.routes.outreach" in {c["key"] for c in SV.CONTROLS}
    finally:
        SV.unregister_service("firecrawl")
        for name in ("reporting", "outreach"):
            if name in OC.PURPOSES:
                SV.unregister_purpose(name)


def test_a_missing_or_broken_declaration_file_never_stops_the_platform(tmp_path, monkeypatch):
    SV.NOTES.clear()
    monkeypatch.setenv("CONTENTINTEL_SERVICES_FILE", str(tmp_path / "nothing.yaml"))
    assert SV.load_declared() == [] and SV.NOTES == []
    broken = tmp_path / "broken.yaml"
    broken.write_text("services: [unclosed")
    monkeypatch.setenv("CONTENTINTEL_SERVICES_FILE", str(broken))
    assert SV.load_declared() == [] and "not read" in SV.NOTES[0]
    broken.write_text("- a list\n- not a mapping\n")
    assert SV.load_declared() == [] and "must contain name: value settings" in SV.NOTES[-1]
    assert sorted(SV.load_settings(None, tmp_path / "d")["services"]) == ["agent_reach", "ai_gateway", "connection_routes", "connection_secrets", "connection_versions", "connections", "guards", "page_readers", "scrapling", "search", "stored_sessions"]
    assert (ROOT / "services_extra.yaml").is_file() and SV.load_declared(str(ROOT / "services_extra.yaml")) == []        # the shipped file declares nothing, so it changes nothing


def test_one_repositorys_failing_plugin_is_reported_and_does_not_stop_another(other_repo, tmp_path, monkeypatch):
    def boom(api):
        raise RuntimeError("this plugin is broken")
    importlib.import_module("other_repo_pkg.collector").broken = boom
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda group=None: [EntryPoint("bad", "other_repo_pkg.collector:broken"), EntryPoint("good", "other_repo_pkg.collector:register"),
                                                                                  EntryPoint("missing", "no_such_pkg.module:register")] if group == SV.PLUGIN_GROUP else [])
    assert SV.load_plugins(force=True) == ["good"] and "mailer_enrich" in OC.SERVICE_REGISTRY
    assert any("plugin bad skipped: RuntimeError" in n for n in SV.NOTES) and any("plugin missing skipped: ModuleNotFoundError" in n for n in SV.NOTES)
    assert SV.load_plugins() == []                                                               # plugins are loaded once per process, not on every settings read


def test_built_in_purposes_and_services_cannot_be_removed_and_a_purpose_registers_once():
    for name in OC.BUILT_IN_PURPOSES:
        with pytest.raises(ValueError, match="built-in purpose"):
            SV.unregister_purpose(name)
    before = OC.PURPOSES
    SV.register_purpose("temp_work")
    SV.register_purpose("temp_work")
    assert OC.PURPOSES == before + ("temp_work",)
    with pytest.raises(ValueError, match="lowercase"):
        SV.register_purpose("Bad Purpose")
    SV.unregister_purpose("temp_work")
    assert OC.PURPOSES == before and OC._match_ok("purpose:competitors") and not OC._match_ok("purpose:temp_work")


def test_the_package_builds_into_a_wheel_that_a_clean_environment_can_install_and_use(tmp_path):
    pytest.importorskip("setuptools")
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["cp", "-r", str(ROOT / "contentintel"), str(src / "contentintel")], check=True)
    subprocess.run(["cp", str(ROOT / "pyproject.toml"), str(src)], check=True)
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "-q", "-w", str(tmp_path / "wheels"), str(src)], capture_output=True, text=True, timeout=240)
    if built.returncode != 0:
        pytest.skip(f"a wheel could not be built here: {built.stderr[-200:]}")
    wheel = next((tmp_path / "wheels").glob("contentintel-*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    assert "contentintel/services.py" in names and "contentintel/ytdlp_youtube.py" in names and "contentintel/observatory/data/youtube_hooks.json" in names
    site = tmp_path / "site"
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "-q", "--target", str(site), str(wheel)], check=True, capture_output=True, timeout=240)
    work = tmp_path / "elsewhere"
    work.mkdir()
    code = textwrap.dedent("""
        import contentintel, sys
        from contentintel import services as S
        s = S.load_settings(None, 'data')
        print(contentintel.__file__.startswith(sys.argv[1]), sorted(s['services']), len(S.CONTROLS))
    """)
    ran = subprocess.run([sys.executable, "-c", code, str(site)], cwd=work, env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(site)}, capture_output=True, text=True, timeout=120)
    assert ran.returncode == 0, ran.stderr[-400:]
    assert ran.stdout.strip() == f"True ['agent_reach', 'ai_gateway', 'connection_routes', 'connection_secrets', 'connection_versions', 'connections', 'guards', 'page_readers', 'scrapling', 'search', 'stored_sessions'] {len(SV.CONTROLS)}"


def test_reading_the_settings_itself_applies_every_repositorys_registrations_with_nothing_registered_beforehand(other_repo, tmp_path, monkeypatch):
    assert "mailer_enrich" not in OC.SERVICE_REGISTRY and "outreach" not in OC.PURPOSES
    SV._PLUGINS_DONE = False                                                                       # a fresh process: nothing loaded yet
    declared = tmp_path / "declared.yaml"
    declared.write_text(yaml.safe_dump({"purposes": ["reporting"], "services": [{"name": "firecrawl", "channels": ["scrape"]}]}))
    monkeypatch.setenv("CONTENTINTEL_SERVICES_FILE", str(declared))
    try:
        s = SV.load_settings(settings_file(tmp_path, mailer_enrich={"channels": {"lookup": False}}, firecrawl={"channels": {"scrape": False}}), tmp_path / "data")      # the file names services nobody registered by hand
        assert s["services"]["mailer_enrich"]["channels"] == {"lookup": False} and s["services"]["firecrawl"]["channels"] == {"scrape": False}      # one came from a plugin, one from the declaration file
        assert s["services"]["scrapling"]["use_for"]["outreach"] is True and s["services"]["scrapling"]["use_for"]["reporting"] is True
    finally:
        SV.unregister_service("firecrawl") if "firecrawl" in OC.SERVICE_REGISTRY else None
        if "reporting" in OC.PURPOSES:
            SV.unregister_purpose("reporting")
