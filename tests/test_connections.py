"""External connections: the repositories themselves, with the platform's own versions as the backup, all under the owner's controls."""
import json

import pytest

from contentintel import agent_reach_external as ARX
from contentintel import connections as CONN
from contentintel import contract as K
from contentintel import guards
from contentintel import services as SV
from contentintel.observatory import worker_client as WC

NOW = "2026-11-09T10:00:00Z"


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONTENTINTEL_EXTERNAL_DIR", str(tmp_path / "ext"))
    return SV.load_settings("observatory.yaml", str(tmp_path / "data"))


class Done:
    def __init__(self, out="", err="", code=0):
        self.stdout, self.stderr, self.returncode = out, err, code


# ---- the owner's controls -------------------------------------------------------------------------------------------------------------------------

def test_every_connection_is_on_external_first_latest_and_gets_its_declared_secrets_by_default(s):
    assert set(CONN.CONNECTIONS) >= {"agent_reach", "scrapling", "freellmapi", "instagram_skill", "youtube_skill", "video_shotcraft"}
    for cid in CONN.CONNECTIONS:
        assert CONN.decide(s, cid) == {"on": True, "route": "external_first", "version": "latest", "secrets": True}


def test_the_owner_turns_any_connection_off_changes_its_route_version_or_secrets_and_it_sticks(tmp_path, monkeypatch):
    data = str(tmp_path)
    SV.save_overrides(data, {"services.connections.channels.agent_reach": False, "services.connection_routes.routes.scrapling": "builtin_first",
                             "services.connection_versions.routes.freellmapi": "pinned", "services.connection_secrets.channels.freellmapi": False}, "owner", NOW, "choices")
    s = SV.load_settings("observatory.yaml", data)
    assert CONN.decide(s, "agent_reach")["on"] is False and CONN.decide(s, "scrapling")["route"] == "builtin_first"
    assert CONN.decide(s, "freellmapi")["version"] == "pinned" and CONN.decide(s, "freellmapi")["secrets"] is False and CONN.decide(s, "instagram_skill")["on"] is True


# ---- secrets are withheld -----------------------------------------------------------------------------------------------------------------------

def test_a_repository_gets_only_the_secrets_it_declares_and_only_when_the_owner_allows_it(s):
    environ = {"PATH": "/usr/bin", "HOME": "/home/x", "ANTHROPIC_API_KEY": "claude", "GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "INSTAGRAM_PASSWORD": "pw", "GROQ_API_KEY": "groq", "GITHUB_TOKEN": "gh",
               "EXA_API_KEY": "exa", "OBSERVATORY_CONTACT": "me@x.example"}
    env = CONN.external_env(s, "agent_reach", environ)
    assert env["GROQ_API_KEY"] == "groq" and env["GITHUB_TOKEN"] == "gh" and env["EXA_API_KEY"] == "exa" and env["PATH"] == "/usr/bin"
    assert not {"ANTHROPIC_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON", "INSTAGRAM_PASSWORD", "OBSERVATORY_CONTACT"} & set(env)                # everything not declared is withheld
    s["services"]["connection_secrets"]["channels"]["agent_reach"] = False
    assert not {"GROQ_API_KEY", "GITHUB_TOKEN", "EXA_API_KEY"} & set(CONN.external_env(s, "agent_reach", environ))                   # the owner withholds even the declared ones
    assert CONN.external_env(s, "youtube_skill", environ).keys() <= set(CONN.BASE_ENV) | {"PYTHONDONTWRITEBYTECODE"}                   # a data repository gets no secret at all


# ---- health ---------------------------------------------------------------------------------------------------------------------------------------

def test_the_health_score_counts_recent_attempts_more_and_names_the_state(tmp_path):
    led = CONN.ConnectionHealth(tmp_path / "h.json")
    assert led.summary("agent_reach") == {"score": None, "state": "unknown", "attempts": 0, "last_success": "", "last_error": None, "avg_ms": None, "builtin_used": 0}
    for ok in (True, True, True, True, False):
        led.record("agent_reach", ok, NOW, latency_ms=100, code="" if ok else "TIMEOUT", message="" if ok else "slow")
    good = led.summary("agent_reach")
    assert good["score"] == 67 and good["state"] == "degraded" and good["last_error"]["code"] == "TIMEOUT" and good["avg_ms"] == 100
    for ok in (False, False, False, False, True):
        led.record("scrapling", ok, NOW)
    assert led.summary("scrapling")["score"] == 33 and led.summary("scrapling")["state"] == "down"
    for _ in range(6):
        led.record("youtube_skill", True, NOW)
    assert led.summary("youtube_skill") == {**led.summary("youtube_skill"), "score": 100, "state": "healthy"}
    led.record("agent_reach", True, NOW, route="builtin")
    assert led.summary("agent_reach")["builtin_used"] == 1 and led.summary("agent_reach")["attempts"] == 5                           # the backup is counted, it is not a health attempt
    led.save()
    assert CONN.ConnectionHealth(tmp_path / "h.json").summary("agent_reach")["score"] == 67                                       # kept between runs


def test_the_rows_for_the_command_and_the_sheet_carry_the_owners_choices_and_the_health(s, tmp_path):
    led = CONN.ConnectionHealth(tmp_path / "h.json")
    led.record("agent_reach", False, NOW, code="UPSTREAM_BROKEN", message="not installed")
    s["services"]["connections"]["channels"]["video_shotcraft"] = False
    rows = {r["connection"]: r for r in CONN.rows(s, led)}
    assert rows["agent_reach"]["health_score"] == 0 and rows["agent_reach"]["state"] == "down" and "UPSTREAM_BROKEN: not installed" in rows["agent_reach"]["last_error"]
    assert rows["video_shotcraft"]["on"] == "off" and rows["scrapling"]["health_score"] == "" and rows["scrapling"]["state"] == "unknown" and "built-in polite reader" in rows["scrapling"]["fallback"]
    tab = CONN.sheet_tab(s)
    assert tab["title"] == "Connections Health" and set(tab["rows_by_key"]) == set(CONN.CONNECTIONS) and tab["key_col"] == "connection"


# ---- routes and the backup -----------------------------------------------------------------------------------------------------------------------

def _run(s, route, ext, blt, tmp_path, on=True):
    s["services"]["connection_routes"]["routes"]["agent_reach"] = route
    s["services"]["connections"]["channels"]["agent_reach"] = on
    led = CONN.ConnectionHealth(tmp_path / "h.json")
    return CONN.run_connection(s, "agent_reach", ext, blt, now=NOW, ledger=led), led


def test_the_route_decides_the_order_a_failure_falls_back_to_the_other_and_health_records_the_repository_attempts(s, tmp_path):
    calls = []
    ext_ok, blt_ok = (lambda: calls.append("ext") or "E"), (lambda: calls.append("blt") or "B")

    def ext_bad():
        calls.append("ext")
        raise K.Failure(K.UPSTREAM_BROKEN, "the repo broke")
    (res, which), led = _run(s, "external_first", ext_ok, blt_ok, tmp_path)
    assert (res, which, calls) == ("E", "external", ["ext"]) and led.summary("agent_reach")["score"] == 100
    calls.clear()
    (res, which), led = _run(s, "external_first", ext_bad, blt_ok, tmp_path)
    assert (res, which, calls) == ("B", "builtin", ["ext", "blt"]) and led.summary("agent_reach")["last_error"]["code"] == "UPSTREAM_BROKEN" and led.summary("agent_reach")["builtin_used"] == 1
    calls.clear()
    (res, which), _ = _run(s, "builtin_first", ext_ok, blt_ok, tmp_path)
    assert (res, which, calls) == ("B", "builtin", ["blt"])
    calls.clear()
    (res, which), _ = _run(s, "builtin_only", ext_ok, blt_ok, tmp_path)
    assert (res, which, calls) == ("B", "builtin", ["blt"])
    calls.clear()
    with pytest.raises(K.Failure, match="the repo broke"):
        _run(s, "external_only", ext_bad, blt_ok, tmp_path)
    assert calls == ["ext"]                                                                                        # external_only never uses the backup


def test_a_connection_switched_off_never_runs_the_repository_and_both_failing_raises_the_last_failure(s, tmp_path):
    calls = []
    (res, which), _ = _run(s, "external_first", lambda: calls.append("ext") or "E", lambda: calls.append("blt") or "B", tmp_path, on=False)
    assert (res, which, calls) == ("B", "builtin", ["blt"])
    with pytest.raises(K.Failure, match="switched off"):
        _run(s, "external_only", lambda: "E", lambda: "B", tmp_path, on=False)

    def bad(msg):
        def f():
            raise K.Failure(K.TIMEOUT, msg)
        return f
    with pytest.raises(K.Failure, match="second"):
        _run(s, "external_first", bad("first"), bad("second"), tmp_path)


# ---- installing and updating --------------------------------------------------------------------------------------------------------------------

def test_latest_installs_the_newest_pinned_uses_the_recorded_versions_and_off_installs_nothing(s, tmp_path):
    cmds = []
    runner = lambda argv, **kw: cmds.append(argv) or Done("")                                                     # noqa: E731
    ok, msg = CONN.install(s, "scrapling", root=tmp_path / "e", runner=runner)
    assert ok and any(c[-4:] == ["--upgrade", "scrapling[fetchers]", "markdownify"] or ("--upgrade" in c and "scrapling[fetchers]" in c and "markdownify" in c) for c in cmds)
    cmds.clear()
    s["services"]["connection_versions"]["routes"]["scrapling"] = "pinned"
    (tmp_path / "req").mkdir()
    (tmp_path / "req" / "requirements-scrapling.txt").write_text("scrapling[fetchers]==0.5\n")
    assert CONN.install(s, "scrapling", root=tmp_path / "e", runner=runner, requirements_root=str(tmp_path / "req"))[0] and any("-r" in c for c in cmds)
    cmds.clear()
    s["services"]["connections"]["channels"]["scrapling"] = False
    ok, msg = CONN.install(s, "scrapling", root=tmp_path / "e", runner=runner)
    assert not ok and "switched off" in msg and cmds == []


def test_a_data_repository_is_cloned_and_a_node_server_latest_is_the_newest_tag_while_pinned_is_the_recorded_commit(s, tmp_path, monkeypatch):
    cmds = []

    def runner(argv, **kw):
        cmds.append(argv)
        if argv[:2] == ["git", "ls-remote"]:
            return Done("aaa\trefs/tags/v0.16.17\nbbb\trefs/tags/v0.15.0\n")
        return Done("")
    ok, msg = CONN.install(s, "youtube_skill", root=tmp_path / "e", runner=runner)
    assert ok and ["git", "remote", "add", "origin", CONN.CONNECTIONS["youtube_skill"]["repo"]] in cmds
    monkeypatch.setenv("CONTENTINTEL_GATEWAY_DIR", str(tmp_path / "gw"))
    cmds.clear()
    assert CONN.install(s, "freellmapi", runner=runner)[0] and ["git", "fetch", "--quiet", "--depth", "1", "origin", "v0.16.17"] in cmds and any(c[:2] == ["npm", "run"] for c in cmds)
    monkeypatch.chdir(__import__("pathlib").Path(__file__).resolve().parent.parent)
    s["services"]["connection_versions"]["routes"]["freellmapi"] = "pinned"
    cmds.clear()
    shutil_gw = tmp_path / "gw2"
    monkeypatch.setenv("CONTENTINTEL_GATEWAY_DIR", str(shutil_gw))
    assert CONN.install(s, "freellmapi", runner=runner)[0]
    assert ["git", "fetch", "--quiet", "--depth", "1", "origin", json.load(open("ai/gateway.lock.json"))["gateway"]["commit"]] in cmds


def test_an_install_that_fails_says_so_and_names_the_backup(s, tmp_path):
    ok, msg = CONN.install(s, "agent_reach", root=tmp_path / "e", runner=lambda argv, **kw: Done("", "boom", 1))
    assert not ok and "could not be installed" in msg and "platform's own version is used instead" in msg


def test_a_probe_records_real_health_and_a_missing_install_is_a_recorded_failure(s, tmp_path):
    led = CONN.ConnectionHealth(tmp_path / "h.json")
    ok, msg = CONN.probe(s, "agent_reach", now=NOW, runner=lambda argv, **kw: Done("{}"), root=tmp_path / "none", ledger=led)
    assert not ok and "not installed" in msg and led.summary("agent_reach")["attempts"] == 0 and led.summary("agent_reach")["last_error"] is None      # a missing install is our setup, not the repository failing: it is reported, not scored
    exe = tmp_path / "ext" / "agent_reach-env" / "bin" / "agent-reach"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    seen = {}
    ok, msg = CONN.probe(s, "agent_reach", now=NOW, runner=lambda argv, **kw: seen.update(argv=argv, env=kw["env"]) or Done("{}"), root=tmp_path / "ext", environ={"PATH": "/bin", "ANTHROPIC_API_KEY": "k"}, ledger=led)
    assert ok and seen["argv"] == [str(exe), "channels"] and "ANTHROPIC_API_KEY" not in seen["env"]
    assert led.summary("agent_reach")["attempts"] == 1 and led.summary("agent_reach")["last_success"] == NOW                            # the working probe is recorded; the missing install before it was only reported
    ok, msg = CONN.probe(s, "youtube_skill", now=NOW, runner=lambda argv, **kw: Done("abcdef1234567\tHEAD\n"), ledger=led)
    assert ok and "latest commit abcdef1234" in msg
    assert not CONN.probe(s, "youtube_skill", now=NOW, runner=lambda argv, **kw: Done("", "", 128), ledger=led)[0]


# ---- Agent-Reach itself -------------------------------------------------------------------------------------------------------------------------

def _ext(s, out="", code=0, seen=None, environ=None):
    def runner(argv, **kw):
        if seen is not None:
            seen.update(argv=argv, env=kw["env"])
        return Done(out, "", code)
    return ARX.ExternalAgentReach(s, binary="agent-reach", environ=environ or {"PATH": "/bin"}, runner=runner, timeout=40)


def test_agent_reach_is_run_with_its_own_command_line_and_its_json_result_becomes_rows(s):
    seen = {}
    ok = json.dumps({"ok": True, "channel": "twitter", "action": "search", "items": [{"title": "t", "url": "https://x.com/1"}], "error": None})
    rows = _ext(s, "noise\n" + ok, seen=seen).call_action("twitter", "search", {"query": "protein bars", "limit": "5"})
    assert rows == [{"title": "t", "url": "https://x.com/1"}] and seen["argv"] == ["agent-reach", "twitter", "search", "protein bars", "--limit", "5", "--timeout", "40"]
    _ext(s, ok, seen=seen).call_action("linkedin", "jobs", {"query": "growth", "location": "Austin"})
    assert seen["argv"][:4] == ["agent-reach", "linkedin", "jobs", "growth"] and ["--location", "Austin"] == seen["argv"][4:6]
    _ext(s, ok, seen=seen).call_action("twitter", "profile", {"username": "acme"})
    assert seen["argv"][:4] == ["agent-reach", "twitter", "profile", "acme"]


def test_agent_reachs_errors_keep_their_codes_and_every_failure_mode_is_a_clear_failure(s):
    bad = json.dumps({"ok": False, "items": [], "error": {"code": "NEED_LOGIN", "message": "log in first"}})
    with pytest.raises(K.Failure) as seen:
        _ext(s, bad, 1).call_action("twitter", "search", {"query": "q"})
    assert seen.value.code == K.NEED_LOGIN and "log in first" in str(seen.value)
    odd = json.dumps({"ok": False, "error": {"code": "SOMETHING_NEW", "message": "?"}})
    with pytest.raises(K.Failure) as seen:
        _ext(s, odd, 1).call_action("twitter", "search", {"query": "q"})
    assert seen.value.code == K.UPSTREAM_BROKEN
    with pytest.raises(K.Failure, match="no readable result"):
        _ext(s, "not json at all", 1).call_action("twitter", "search", {"query": "q"})
    with pytest.raises(K.Failure) as seen:
        _ext(s).call_action("twitter", "nosuchaction", {"query": "q"})
    assert seen.value.code == K.BAD_INPUT

    def missing(argv, **kw):
        raise FileNotFoundError

    def slow(argv, **kw):
        raise __import__("subprocess").TimeoutExpired(argv, 1)
    for runner, code in ((missing, K.NEED_SETUP), (slow, K.TIMEOUT)):
        with pytest.raises(K.Failure) as seen:
            ARX.ExternalAgentReach(s, binary="x", environ={}, runner=runner).call_action("twitter", "search", {"query": "q"})
        assert seen.value.code == code


def test_the_repositorys_process_never_sees_a_secret_it_did_not_declare(s):
    seen = {}
    ok = json.dumps({"ok": True, "items": []})
    _ext(s, ok, seen=seen, environ={"PATH": "/bin", "ANTHROPIC_API_KEY": "claude", "GROQ_API_KEY": "groq", "INSTAGRAM_COOKIES": "c"}).call_action("twitter", "search", {"query": "q"})
    assert seen["env"].get("GROQ_API_KEY") == "groq" and "ANTHROPIC_API_KEY" not in seen["env"] and "INSTAGRAM_COOKIES" not in seen["env"]


class Builtin:
    needs_browser = True
    environ = {"x": "y"}

    def __init__(self):
        self.calls = []

    def call(self, args):
        self.calls.append(args)
        return [{"title": "from the backup"}]


def test_the_routed_client_uses_the_repository_first_and_the_backup_when_it_fails_and_the_health_shows_it(s, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path))
    good = json.dumps({"ok": True, "items": [{"title": "from the repo"}]})
    blt = Builtin()
    c = ARX.RoutedAgentReach(s, _ext(s, good), blt)
    assert c.call_action("twitter", "search", {"query": "q"}, ["search", "q"], ["search", "q"]) == [{"title": "from the repo"}] and c.last_route == "external" and blt.calls == []
    assert c.needs_browser is True and c.environ == {"x": "y"}
    c = ARX.RoutedAgentReach(s, _ext(s, "garbage", 1), blt)
    assert c.call_action("twitter", "search", {"query": "q"}, ["search", "q"], ["search", "q"]) == [{"title": "from the backup"}] and c.last_route == "builtin" and blt.calls == [["search", "q"]]
    summary = CONN.ConnectionHealth().summary("agent_reach")
    assert summary["attempts"] == 2 and summary["score"] == 33 and summary["builtin_used"] == 1


def test_the_shared_client_runs_the_repository_only_when_it_is_installed_connected_and_allowed(s, tmp_path, monkeypatch):
    monkeypatch.setattr(CONN, "binary_path", lambda cid, root=None: "")
    plain = SV.agent_reach_client(s, "instagram", "social_discovery", environ={})
    assert not isinstance(plain, ARX.RoutedAgentReach)
    monkeypatch.setattr(CONN, "binary_path", lambda cid, root=None: "/ext/agent-reach")
    assert isinstance(SV.agent_reach_client(s, "instagram", "social_discovery", environ={}), ARX.RoutedAgentReach)
    s["services"]["connection_routes"]["routes"]["agent_reach"] = "builtin_only"
    assert not isinstance(SV.agent_reach_client(s, "instagram", "social_discovery", environ={}), ARX.RoutedAgentReach)
    s["services"]["connection_routes"]["routes"]["agent_reach"] = "external_first"
    s["services"]["connections"]["channels"]["agent_reach"] = False
    assert not isinstance(SV.agent_reach_client(s, "instagram", "social_discovery", environ={}), ARX.RoutedAgentReach)
    s["services"]["connections"]["channels"]["agent_reach"] = True
    sentinel = object()
    assert SV.agent_reach_client(s, "instagram", "social_discovery", environ={}, factory=lambda: sentinel) is sentinel


# ---- Scrapling: the page readers ------------------------------------------------------------------------------------------------------------------

def _engines(log, scrapling=None):
    def make(name, result=None, fail=None):
        def fn(url):
            log.append(name)
            if fail:
                raise fail
            return {"text": name}
        return fn
    return {"scrapling": scrapling or make("scrapling"), "jina": make("jina"), "core": make("core")}


def test_the_scrapling_connection_decides_whether_and_where_it_sits_in_the_reader_chain_and_its_health_is_scored(s, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path))
    log = []
    assert SV.read_page(s, "https://x.example/", "general_research", _engines(log))["engine"] == "scrapling"
    assert CONN.ConnectionHealth().summary("scrapling")["score"] == 100
    for route, expect in (("builtin_first", "core"), ("builtin_only", "jina"), ("external_only", "scrapling")):
        s["services"]["connection_routes"]["routes"]["scrapling"] = route
        assert SV.read_page(s, "https://x.example/", "general_research", _engines([]))["engine"] == expect
    s["services"]["connection_routes"]["routes"]["scrapling"] = "external_first"
    s["services"]["connections"]["channels"]["scrapling"] = False
    page = SV.read_page(s, "https://x.example/", "general_research", _engines(log := []))
    assert page["engine"] == "jina" and "scrapling" not in log and any("connection is switched off" in x for x in page["skipped"])


def test_a_repository_failure_lowers_the_health_but_a_sites_refusal_does_not_and_a_refusal_passes_on_only_when_the_rest_guard_is_off(s, tmp_path, monkeypatch):
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path))
    broken = lambda url: (_ for _ in ()).throw(K.Failure(K.UPSTREAM_BROKEN, "worker crashed"))                       # noqa: E731
    page = SV.read_page(s, "https://x.example/", "general_research", _engines([], scrapling=broken))
    assert page["engine"] == "jina" and CONN.ConnectionHealth().summary("scrapling")["state"] == "down"
    refused = lambda url: (_ for _ in ()).throw(K.Failure(K.PLATFORM_BLOCKED, "403"))                                  # noqa: E731
    guards.configure({g: g == "rest_after_refusal" for g in guards.GUARDS})                                           # the owner's rest guard ON: a refusal ends the chain
    with pytest.raises(K.Failure) as seen:
        SV.read_page(s, "https://x.example/", "general_research", _engines([], scrapling=refused))
    assert seen.value.code == K.PLATFORM_BLOCKED
    guards.configure({g: False for g in guards.GUARDS})                                                               # OFF (the default): the next reader gets a go
    assert SV.read_page(s, "https://x.example/", "general_research", _engines([], scrapling=refused))["engine"] == "jina"
    assert CONN.ConnectionHealth().summary("scrapling")["attempts"] == 1                                              # neither refusal counted against the repository


def test_the_worker_job_pins_the_scrapling_version_only_when_the_owner_pins_it(s):
    assert WC.build_job([], s, "a@b.example")["expected_version"] == ""                                               # latest: any version
    s["services"]["connection_versions"]["routes"]["scrapling"] = "pinned"
    assert WC.build_job([], s, "a@b.example")["expected_version"] == WC.EXPECTED_VERSION


# ---- declaring more connections without code (Stage B) -------------------------------------------------------------------------------------------

def test_more_connections_can_be_declared_in_a_file_and_a_bad_entry_is_skipped_with_its_reason(tmp_path):
    f = tmp_path / "connections_extra.yaml"
    f.write_text("connections:\n  - {id: demo_tool, repo: https://github.com/example/demo, kind: pip_cli, binary: demo, secrets: [DEMO_KEY], fallback: our own demo}\n"
                 "  - {id: Bad-Name, repo: https://github.com/example/x}\n  - {id: agent_reach, repo: https://github.com/example/y}\n  - {id: odd_kind, repo: https://github.com/example/z, kind: spaceship}\n")
    try:
        added, problems = CONN.load_declared(str(f))
        assert added == ["demo_tool"] and len(problems) == 3 and CONN.CONNECTIONS["demo_tool"]["secrets"] == ("DEMO_KEY",) and CONN.CONNECTIONS["demo_tool"]["pip"] == "git+https://github.com/example/demo"
    finally:
        CONN.CONNECTIONS.pop("demo_tool", None)
    assert CONN.load_declared(str(tmp_path / "missing.yaml")) == ([], [])


# ---- the command ----------------------------------------------------------------------------------------------------------------------------------

def test_the_connections_command_lists_checks_and_installs_and_names_a_bad_key(tmp_path, monkeypatch, capsys):
    from contentintel import cli
    monkeypatch.setenv("CONTENTINTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OBSERVATORY_CONTACT", "a@b.example")
    monkeypatch.setattr(CONN, "probe", lambda settings, cid, **kw: (cid != "agent_reach", f"{cid}: probed"))
    monkeypatch.setattr(CONN, "install", lambda settings, cid, **kw: (True, f"{cid}: installed"))
    base = ["observatory", "connections", "--data-dir", str(tmp_path / "data"), "--all"]
    assert cli.main(base) == 0
    out = capsys.readouterr().out
    assert all(cid in out for cid in CONN.CONNECTIONS) and "external_first" in out and "latest" in out
    assert cli.main(base + ["--what", "check", "--key", "agent_reach"]) == 0
    out = capsys.readouterr().out
    assert "not working: agent_reach: probed" in out
    assert cli.main(base + ["--what", "install", "--key", "scrapling"]) == 0
    assert "installed: scrapling: installed" in capsys.readouterr().out
    assert cli.main(base + ["--key", "nonsense"]) == 1 and "no connection called 'nonsense'" in capsys.readouterr().out


# ---- what counts toward a connection's health (Update 48) -----------------------------------------------------------------------------------------

def test_a_failure_that_is_not_the_repositorys_fault_is_never_scored_and_a_real_one_is(tmp_path):
    led = CONN.ConnectionHealth(tmp_path / "h.json")
    codes = ["BAD_INPUT", "NEED_LOGIN", "NEED_SETUP", "NOT_FOUND", "PLATFORM_BLOCKED", "RATE_LIMITED"]                 # written out, so a code cannot quietly leave the set and the test together
    assert sorted(CONN.NOT_THE_REPOSITORYS_FAULT) == codes
    for code in codes:
        led.record("scrapling", False, NOW, code=code, message="x")
    assert led.summary("scrapling")["attempts"] == 0 and led.summary("scrapling")["state"] == "unknown" and led.summary("scrapling")["last_error"] is None
    led.record("scrapling", False, NOW, code="UPSTREAM_BROKEN", message="the library crashed")
    led.record("scrapling", False, NOW, code="TIMEOUT", message="too slow")
    assert led.summary("scrapling")["attempts"] == 2 and led.summary("scrapling")["state"] == "down" and led.summary("scrapling")["last_error"]["code"] == "TIMEOUT"
    led.record("scrapling", True, NOW)
    assert led.summary("scrapling")["attempts"] == 3


def test_history_from_before_version_2_loses_its_failures_once_and_keeps_its_successes(tmp_path):
    path = tmp_path / "h.json"
    old = {"scrapling": {"attempts": [{"at": "2026-11-01T00:00:00Z", "ok": False, "ms": 0}] * 14 + [{"at": "2026-11-02T00:00:00Z", "ok": True, "ms": 90}] + [{"at": "2026-11-03T00:00:00Z", "ok": False, "ms": 0}],
                          "last_success": "2026-11-02T00:00:00Z", "last_error": {"at": "2026-11-03T00:00:00Z", "code": "NEED_SETUP", "message": "no environment"}, "builtin_used": 2}}
    path.write_text(json.dumps(old))
    led = CONN.ConnectionHealth(path)
    first = led.summary("scrapling")
    assert first["attempts"] == 1 and first["score"] == 100 and first["state"] == "healthy" and first["last_error"] is None and first["builtin_used"] == 2      # 16 / down becomes what it really was
    led.record("scrapling", False, NOW, code="UPSTREAM_BROKEN", message="real")
    led.save()
    again = CONN.ConnectionHealth(path)
    assert again.summary("scrapling")["attempts"] == 2 and again.data["scrapling"]["v"] == CONN.HISTORY_VERSION                  # a second load changes nothing
    everything_failed = {"scrapling": {"attempts": [{"at": "2026-11-01T00:00:00Z", "ok": False, "ms": 0}] * 16, "last_success": "", "last_error": None, "builtin_used": 0}}
    path.write_text(json.dumps(everything_failed))
    s = CONN.ConnectionHealth(path).summary("scrapling")
    assert s["attempts"] == 0 and s["state"] == "unknown" and s["score"] is None                                               # not "down": nothing is proved either way
    path.write_text(json.dumps({"x": "not a dict", "y": {"v": 2, "attempts": [{"at": "a", "ok": False, "ms": 0}], "last_error": {"code": "TIMEOUT"}}}))
    assert CONN.ConnectionHealth(path).summary("y")["attempts"] == 1                                                            # an up-to-date entry is left alone
