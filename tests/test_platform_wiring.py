"""The shared services are connected to the modules that use them: search, the AI Gateway adapter and its purposes, and the collectors' deep check. MOCK: no provider, no gateway, no network."""
import argparse
from types import SimpleNamespace

from contentintel import cli
from contentintel import collectors
from contentintel import services as SV


def args_for(tmp_path):
    return argparse.Namespace(data_dir=str(tmp_path / "data"), sources_config="sources.yaml")


class Claude:
    model = "m"

    def __init__(self):
        self.for_purpose = None

    def json_task(self, system, user, max_tokens=None):
        return {"from": "claude"}


def test_research_search_goes_through_the_shared_interface_and_obeys_the_owners_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.providers_mod, "build_providers", lambda env: {"serper_dev": object(), "tavily": object(), "exa": object()})
    a = args_for(tmp_path)
    assert sorted(cli._search_providers(a, "general_research")) == ["exa", "serper_dev", "tavily"]
    SV.save_overrides(a.data_dir, {"services.search.channels.tavily": False}, "owner", "2026-11-04T10:00:00Z", "tavily off")
    assert sorted(cli._search_providers(a, "general_research")) == ["exa", "serper_dev"]
    SV.save_overrides(a.data_dir, {"services.search.use_for.general_research": False}, "owner", "2026-11-04T10:00:00Z", "research off")
    assert cli._search_providers(a, "general_research") == {} and sorted(cli._search_providers(a, "discovery")) == ["exa", "serper_dev", "tavily"]


def test_a_settings_file_that_cannot_be_read_never_stops_search_or_the_claude_modules(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.providers_mod, "build_providers", lambda env: {"serper_dev": object()})

    def broken(*a, **k):
        raise ValueError("observatory.yaml is not valid")
    monkeypatch.setattr(SV, "load_settings", broken)
    a = args_for(tmp_path)
    assert list(cli._search_providers(a, "general_research")) == ["serper_dev"]                  # built as it always was
    claude = Claude()
    assert cli._routed(claude, "brief", a, store=None) is claude and cli._routed(None, "brief", a, store=None) is None


def test_every_claude_module_goes_through_the_gateway_adapter_by_default_and_gets_plain_claude_when_the_owner_says_so(tmp_path):
    a, claude = args_for(tmp_path), Claude()
    purposes = ("idea_bank", "planning", "brief", "autoseeds", "deep_check", "general_research")
    for purpose in purposes:
        assert isinstance(cli._routed(claude, purpose, a, store=None), SV.RoutedClaude)
    SV.save_overrides(a.data_dir, {f"services.ai_gateway.routes.{p}": "claude" for p in purposes}, "owner", "2026-11-04T10:00:00Z", "all Claude")
    for purpose in purposes:
        assert cli._routed(claude, purpose, a, store=None) is claude


def test_routing_one_purpose_to_the_gateway_changes_only_that_purpose(tmp_path, monkeypatch):
    a, claude = args_for(tmp_path), Claude()
    SV.save_overrides(a.data_dir, {"services.ai_gateway.routes.brief": "gateway_first"}, "owner", "2026-11-04T10:00:00Z", "brief routed")
    asked = []

    class Router:
        clients = {"groq": object()}                                                   # one free provider with a credential
        def run(self, lane, **kw):
            asked.append((lane, kw["item_ids"]))
            return {"status": "ok", "text": "t", "output": {"from": "gateway"}}
    monkeypatch.setattr(cli, "_health", lambda args, store: (None, None))
    monkeypatch.setattr(SV, "ai_stack", lambda store, ledger, environ, **kw: (Router(), None, None, None, [], lambda: None))
    SV.save_overrides(a.data_dir, {"services.ai_gateway.routes.planning": "claude"}, "owner", "2026-11-04T10:01:00Z", "planning back to Claude")
    brief = cli._routed(claude, "brief", a, store=None)
    assert isinstance(brief, SV.RoutedClaude) and brief.json_task("S", "U") == {"from": "gateway"} and asked == [("general_json", ("brief",))]
    assert cli._routed(claude, "planning", a, store=None) is claude                          # the owner's explicit choice for one purpose; the rest keep the default (gateway first)
    assert isinstance(cli._routed(claude, "idea_bank", a, store=None), SV.RoutedClaude)


def test_the_deep_check_has_its_own_purpose_even_though_it_shares_the_collectors_client(tmp_path, monkeypatch):
    a = args_for(tmp_path)
    plain = Claude()
    plain.for_purpose = lambda purpose: cli._routed(plain, purpose, a, None)
    monkeypatch.setattr(cli, "_health", lambda args, store: (None, None))
    monkeypatch.setattr(SV, "ai_stack", lambda store, ledger, environ, **kw: (SimpleNamespace(run=lambda *x, **y: {"status": "deferred"}), None, None, None, [], lambda: None))
    SV.save_overrides(a.data_dir, {"services.ai_gateway.routes.deep_check": "claude", "services.ai_gateway.routes.autoseeds": "claude"}, "owner", "2026-11-04T10:00:00Z", "both on Claude")
    assert collectors._for_purpose(plain, "deep_check") is plain                                  # the owner's choice: Claude
    SV.save_overrides(a.data_dir, {"services.ai_gateway.routes.deep_check": "gateway_first", "services.ai_gateway.routes.autoseeds": "claude"}, "owner", "2026-11-04T10:01:00Z", "deep check routed")
    checked = collectors._for_purpose(plain, "deep_check")
    assert isinstance(checked, SV.RoutedClaude) and checked.json_task("S", "U") == {"from": "claude"}       # the gateway had no answer, so Claude answered
    assert cli._routed(plain, "autoseeds", a, None) is plain                                        # the seed words are a different purpose and stay on Claude


def test_a_client_without_a_purpose_helper_is_used_as_it_is(tmp_path):
    class Plain:
        def json_task(self, *a, **k):
            return {}
    plain = Plain()
    assert collectors._for_purpose(plain, "deep_check") is plain
    from unittest.mock import MagicMock
    mock = MagicMock()
    assert collectors._for_purpose(mock, "deep_check") is mock                                      # a stand-in with an automatic attribute is not mistaken for a helper


# ---- structural guards: no module may bypass the shared interfaces (they read the code, so a future module is held to the same rule) ----------------

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def calls_with_parents(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    named = lambda n: n.func.id if isinstance(n.func, ast.Name) else (n.func.attr if isinstance(n.func, ast.Attribute) else "")      # noqa: E731

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"
    return [(node, named(node), enclosing(node), parents) for node in ast.walk(tree) if isinstance(node, ast.Call)]


def wrapped_in(node, parents, wrapper):
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.Call) and (cur.func.id if isinstance(cur.func, ast.Name) else cur.func.attr if isinstance(cur.func, ast.Attribute) else "") == wrapper:
            return True
    return False


def routed_names(path, function):
    """The variable names a function hands to _routed as the client (so a client built into a variable and wrapped on the next line counts as wrapped)."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    names = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == function:
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_routed" and node.args and isinstance(node.args[0], ast.Name):
                    names.add(node.args[0].id)
    return names


def assigned_to(node, parents):
    parent = parents.get(node)
    while isinstance(parent, ast.IfExp):                      # plain = Claude(...) if key else None
        parent = parents.get(parent)
    return parent.targets[0].id if isinstance(parent, ast.Assign) and isinstance(parent.targets[0], ast.Name) else ""


def test_every_claude_client_a_module_receives_is_built_through_the_routing_adapter():
    allowed_places = {"_claude", "_acceptance_context", "_routed"}                       # the factory itself, the acceptance probes (which test the real key) and the adapter
    bare = [(fn, node.lineno) for node, name, fn, parents in calls_with_parents("contentintel/cli.py")
            if name in ("_claude", "Claude") and fn not in allowed_places and not wrapped_in(node, parents, "_routed") and assigned_to(node, parents) not in routed_names("contentintel/cli.py", fn)]
    assert not bare, f"a Claude client is built outside _routed in contentintel/cli.py: {bare}"
    purposes = {}
    for node, name, fn, parents in calls_with_parents("contentintel/cli.py"):
        if name == "_routed" and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            purposes[fn] = purposes.get(fn, set()) | {node.args[1].value}
    assert purposes == {"_ideabank": {"idea_bank"}, "_request": {"planning"}, "_brief": {"brief"}, "_collect": {"autoseeds"}}


def test_search_providers_are_built_only_through_the_shared_interface():
    bare = [(fn, node.lineno) for node, name, fn, _ in calls_with_parents("contentintel/cli.py") if name == "build_providers" and fn not in ("_search_providers", "_acceptance_context")]
    assert not bare, f"a search provider set is built outside services.search_providers in cli.py: {bare}"
    builds = [(node, parents) for node, name, fn, parents in calls_with_parents("contentintel/observatory/commands.py") if name == "build_providers"]
    assert len(builds) == 2 and all(wrapped_in(node, parents, "search_providers") for node, parents in builds)             # each is the builder handed to the shared search interface
    source = (ROOT / "contentintel/observatory/commands.py").read_text(encoding="utf-8")
    assert source.count("providers_mod.build_providers(env)") == 2 and source.count("SV.search_providers(") == 2 and "providers_mod.build_providers(os.environ)" not in source


def test_the_collectors_reach_claude_only_through_the_purpose_helper_with_the_right_purpose_each():
    seen = {}
    for node, name, fn, parents in calls_with_parents("contentintel/collectors.py"):
        if name in ("prepare_prompt", "answer") and isinstance(node.func, ast.Attribute):
            target = node.func.value
            assert isinstance(target, ast.Call) and getattr(target.func, "id", "") == "_for_purpose", f"{name} at line {node.lineno} calls Claude without _for_purpose"
            seen.setdefault(name, set()).add(target.args[1].value)
        if name == "review_report":
            first = node.args[0] if node.args else None
            assert isinstance(first, ast.Call) and getattr(first.func, "id", "") == "_for_purpose", f"review_report at line {node.lineno} is not given a purpose-specific client"
            seen.setdefault("review_report", set()).add(first.args[1].value)
    assert seen == {"prepare_prompt": {"collectors"}, "answer": {"collectors"}, "review_report": {"deep_check"}}


def test_page_reading_in_the_observatory_goes_through_the_shared_reader_chain():
    source = (ROOT / "contentintel/observatory/commands.py").read_text(encoding="utf-8")
    assert source.count("SV.read_page(") == 2 and source.count("SV.scrapling_page(") == 1 and "wc.WorkerPages(comp, [base" not in source


def test_with_no_free_provider_credential_the_default_gateway_route_stays_silent_and_claude_answers(tmp_path, monkeypatch):
    a, claude = args_for(tmp_path), Claude()
    stopped = []

    class Side:
        def stop(self):
            stopped.append(1)

    class Router:
        clients = {"claude": object()}                                                 # only Claude has a credential here
    monkeypatch.setattr(cli, "_health", lambda args, store: (None, None))
    monkeypatch.setattr(SV, "ai_stack", lambda store, ledger, environ, **kw: (Router(), None, None, Side(), [], lambda: None))
    brief = cli._routed(claude, "brief", a, store=None)
    assert isinstance(brief, SV.RoutedClaude) and brief.json_task("S", "U") == claude.json_task("S", "U") and stopped == [1]
