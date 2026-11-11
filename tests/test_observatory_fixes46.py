"""Update 46: the monitor's competitor filter, the page-read summary, the Observatory workflow's AI wiring and Scrapling install."""
import json

import pytest
import yaml

from contentintel import cli
from contentintel.observatory import commands as OCMD
from conftest import ROOT


def sites():
    return {"madepants-com": {"comp": {"domain": "madepants.com", "name": "Madepants"}, "brands": ["swoveralls"]},
            "theoodie-com": {"comp": {"domain": "theoodie.com", "name": "The Oodie"}, "brands": ["swoveralls"]}}


@pytest.mark.parametrize("typed, expect", [("madepants.com", {"madepants-com"}), ("www.madepants.com", {"madepants-com"}), ("madepants", {"madepants-com"}), ("Madepants", {"madepants-com"}),
                                           ("madepants-com", {"madepants-com"}), ("https://www.madepants.com/", {"madepants-com"}), ("The Oodie", {"theoodie-com"}), ("the-oodie", {"theoodie-com"}),
                                           ("theoodie", {"theoodie-com"}), ("madepants, theoodie.com", {"madepants-com", "theoodie-com"})])
def test_a_competitor_can_be_named_by_id_domain_name_or_bare_word(typed, expect):
    found, unknown = OCMD._resolve_competitors(typed, sites())
    assert found == expect and unknown == []


def test_a_name_that_matches_nothing_is_reported_and_the_rest_still_match():
    found, unknown = OCMD._resolve_competitors("madepants, nobody.example", sites())
    assert found == {"madepants-com"} and unknown == ["nobody.example"]
    assert OCMD._resolve_competitors("x", {}) == (set(), ["x"])


@pytest.fixture
def world(tmp_path, monkeypatch):
    import shutil
    monkeypatch.chdir(tmp_path)
    for name in ("observatory.yaml", "research.yaml", "sources.yaml"):
        shutil.copy(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "brands", tmp_path / "brands")
    monkeypatch.setenv("OBSERVATORY_CONTACT", "research@kelson.example")

    def book(*cands):
        d = tmp_path / "data" / "swoveralls" / "observatory"
        d.mkdir(parents=True, exist_ok=True)
        (d / "discovery.json").write_text(json.dumps({"updated_at": "2026-11-10T10:00:00Z", "candidates": [
            {"key": k, "name": n, "domain": dom, "status": "monitoring", "decided_by": "auto", "score": 90, "reasons": ["x"]} for k, n, dom in cands]}), encoding="utf-8")

    def run(*args):
        return cli.main(["observatory", *args, "--data-dir", str(tmp_path / "data"), "--brands-dir", str(tmp_path / "brands")])
    run.book = book
    return run


def test_the_monitor_reads_the_competitor_that_discovery_chose_when_it_is_named_as_a_person_would_type_it(world, monkeypatch, capsys):
    world.book(("madepants", "Madepants", "madepants.com"), ("the-oodie", "The Oodie", "theoodie.com"))
    seen = {}

    def fake(sites_, **kw):
        seen["ids"] = kw["ids"]
        seen["planned"] = sorted(sites_)
        return []
    monkeypatch.setattr(OCMD.monitor, "run_sites", fake)
    for typed in ("madepants.com", "madepants"):
        assert world("monitor", "--all", "--only", "sitemap", "--competitor", typed) == 0
        assert seen["ids"] == {"madepants-com"} and seen["planned"] == ["madepants-com", "theoodie-com"]
    capsys.readouterr()
    assert world("monitor", "--all", "--competitor", "nobody") == 1
    out = capsys.readouterr().out
    assert "no planned competitor matches 'nobody'" in out and "madepants-com (madepants.com)" in out and "theoodie-com (theoodie.com)" in out


def test_a_monitor_run_with_no_competitor_planned_is_not_a_quiet_success(world, capsys):
    assert world("monitor", "--all") == 1
    out = capsys.readouterr().out
    assert "::warning::Nothing was monitored" in out and "Nothing is watched yet" in out
    capsys.readouterr()
    assert world("monitor", "--all", "--dry-run") == 0                                                  # a plan alone is only a plan
    assert "::warning::" not in capsys.readouterr().out


def test_the_page_read_summary_shows_the_main_content_and_the_prices_not_the_menu():
    text = "\n".join(["Home", "Shop", "[Women](https://x/w) | [Men](https://x/m) | [Kids](https://x/k)", "Cart (0)", "Sign in",
                      "Our overalls are cut from soft recycled cotton and made to be worn all day, every day of the week.",
                      "Sweatpant Overalls  $89.00", "The Sherpa Lined Overall is built for the coldest mornings you can imagine.", "Menu", "Contact"])
    preview = OCMD._main_preview(text)
    assert preview.startswith("Our overalls are cut from soft recycled cotton") and "Home" not in preview and "Cart" not in preview and "[Women]" not in preview
    assert "Sherpa Lined Overall" in preview
    assert OCMD._main_preview("Home\nShop\nCart") == ""                                                   # nothing to show: the caller falls back to the start of the page


def test_the_read_command_prints_the_main_content_and_prices(world, monkeypatch, capsys):
    page = {"title": "Madepants", "url": "https://madepants.com/", "engine": "core", "robots_note": "",
            "text": "Home\nShop\nCart\nSoft recycled cotton overalls made for all-day comfort, in sizes for everyone.\nSweatpant Overalls $89.00\nMenu"}
    monkeypatch.setattr(OCMD, "_read_url", lambda *a, **k: (page, ""))
    assert world("corpus", "--what", "read", "--url", "https://madepants.com/", "--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert "main content (menus and short labels skipped)" in out and "Soft recycled cotton overalls" in out and "prices seen on the page" in out and "Sweatpant Overalls $89.00" in out


def test_a_youtube_corpus_with_no_channel_anywhere_is_a_warning_and_a_failure_not_a_success(world, monkeypatch, capsys):
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    assert world("corpus", "--what", "youtube") == 1
    out = capsys.readouterr().out
    assert out.count("no YouTube channels to read") == 3 and "tick find_channels" in out and "::warning::No YouTube corpus was built" in out


# ---- the Observatory workflow ----------------------------------------------------------------------------------------------------------------------

def wf():
    return yaml.safe_load(open(ROOT / ".github" / "workflows" / "observatory.yml", encoding="utf-8"))


def step(name_part):
    return next(s for s in wf()["jobs"]["observatory"]["steps"] if name_part in str(s.get("name", "")) or name_part in str(s.get("uses", "")))


def test_the_observatory_workflow_passes_every_ai_secret_and_can_call_github_models():
    data = wf()
    assert data["permissions"] == {"contents": "write", "models": "read"}
    env = step("Run")["env"]
    for name in ("ANTHROPIC_API_KEY", "FREEAPI_ENCRYPTION_KEY", "FREEAPI_ADMIN_PASSWORD", "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "CEREBRAS_API_KEY",
                 "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        assert env[name] == "${{ secrets." + name + " }}", name
    assert env["GITHUB_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"


def test_the_observatory_workflow_builds_the_gateway_for_discover_and_judge_only_and_never_lets_it_stop_the_run():
    build = step("Fetch and build the gateway")
    assert "inputs.action == 'discover'" in build["if"] and "inputs.action == 'judge'" in build["if"] and build["continue-on-error"] is True
    assert "connections --all --what install --key freellmapi" in build["run"] and "ai/gateway.lock.json" in build["run"]
    node = next(s for s in wf()["jobs"]["observatory"]["steps"] if str(s.get("uses", "")).startswith("actions/setup-node"))
    assert "discover" in node["if"] and node["with"]["node-version"] == "22"
    names = [s.get("name", "") for s in wf()["jobs"]["observatory"]["steps"]]
    assert names.index(build["name"]) < names.index("Run")                                                  # built before it is needed


def test_the_workflow_installs_scrapling_by_itself_when_the_action_needs_it():
    cond = step("Install the isolated Scrapling environment")["if"]
    assert "inputs.scrapling" in cond and "inputs.browsers" in cond and "inputs.action == 'corpus'" in cond and "inputs.what == 'crawl'" in cond and "inputs.what == 'deepread'" in cond


def test_the_report_shows_the_competitors_discovery_chose_before_any_monitor_run_has_finished(world, capsys):
    world.book(("madepants", "Madepants", "madepants.com"), ("the-oodie", "The Oodie", "theoodie.com"))
    assert world("report", "--brand", "swoveralls") == 0
    out = capsys.readouterr().out
    assert "nothing is watched yet" not in out and "Madepants" in out and "The Oodie" in out
    assert world("report", "--brand", "duderobe") == 0
    assert "DudeRobe: nothing is watched yet (run: observatory discover, then observatory monitor)." in capsys.readouterr().out


def test_a_competitor_chosen_for_monitoring_is_listed_on_the_competitors_tab_before_its_first_reading(world, tmp_path):
    from contentintel import services as SV
    from contentintel.brands import load_brand
    from contentintel.observatory import report as R
    from contentintel.observatory.snapshots import SnapshotStore
    from contentintel.store import Store
    world.book(("madepants", "Madepants", "madepants.com"), ("the-oodie", "The Oodie", "theoodie.com"))
    data = str(tmp_path / "data")
    tabs = R.sheet_tabs(load_brand("swoveralls", "brands"), Store(data), SnapshotStore(data), "2026-11-10T12:00:00Z", SV.load_settings("observatory.yaml", data))
    comp = next(t for t in tabs if t["title"] == "Competitors")
    assert set(comp["rows_by_key"]) == {"candidate:madepants", "candidate:the-oodie"}
    row = comp["rows_by_key"]["candidate:madepants"]
    assert row["name"] == "Madepants" and row["domain"] == "madepants.com" and row["status"] == "monitoring (waiting for its first reading)"
