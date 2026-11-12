"""The Scrapling and Agent-Reach capability matrix: it must list every channel and connected action, and every function it cites must exist."""
import ast
import re
from pathlib import Path

import pytest

from contentintel import agent_reach_catalog as CAT

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "platform" / "capability-matrix.md").read_text(encoding="utf-8")


def _rows(heading):
    block = DOC.split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in block.splitlines() if line.startswith("|") and not line.startswith("|---") and not line.startswith("| Capability") and not line.startswith("| Channel")]


def test_every_agent_reach_channel_and_connected_action_is_in_the_matrix_with_the_right_status():
    rows = {r[0].split(" (")[0]: r for r in _rows("## Agent-Reach (14 channels)")}
    assert sorted(rows) == sorted(CAT.CHANNELS)
    for name, spec in CAT.CHANNELS.items():
        row = rows[name]
        if spec["integration"] == "none":
            assert row[3] == "not connected" and row[4] == "not applicable"
        else:
            assert row[4] == "On" and row[3] != "not connected"
            for action in spec["actions"]:
                assert action in row[2] or row[2].startswith("all") or row[2].startswith("both"), (name, action)
            for action in spec.get("unconnected_actions", {}):
                assert action in row[1]                                                                 # an action left out is named in the Agent-Reach column and explained in the last one


def test_every_function_the_matrix_cites_exists():
    cited = set(re.findall(r"`(contentintel/[\w/]+\.py)::([\w.]+)`", DOC))
    assert len(cited) >= 6
    for path, name in cited:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        assert name.split(".")[-1] in names, f"{path}::{name} is cited but does not exist"


def test_the_scrapling_rows_use_only_statuses_the_code_supports_and_the_crawl_row_is_connected():
    rows = _rows("## Scrapling 0.5")
    assert len(rows) >= 20
    crawl = next(r for r in rows if r[0] == "Site crawl to Markdown")
    assert "Worker.crawl" in crawl[2] and "scrapling_crawl" in crawl[3] and "crawl.max_pages" in crawl[4]
    spiders = next(r for r in rows if r[0].startswith("Spider engine"))
    assert "Not used, deliberately" in spiders[2] and "redirects" in spiders[5]
    challenge = next(r for r in rows if r[0] == "Challenge solving")
    assert challenge[4].startswith("On") and "kept ON by the owner" in challenge[5] and "services.scrapling.defaults.solve_challenges" in challenge[5]


@pytest.mark.parametrize("phrase", ["106 permission switches", "gateway_first", "`auto`", "crawl.max_pages", "api_approval"])
def test_the_defaults_section_states_the_defaults_that_the_code_has(phrase):
    assert phrase in DOC


def test_the_per_action_table_is_exactly_what_the_catalogue_says_and_names_every_agent_reach_action():
    block = DOC.split("### Every action of every channel", 1)[1].split("\n## ", 1)[0]
    rows = [tuple(c.strip() for c in line.strip().strip("|").split("|")) for line in block.splitlines() if line.startswith("|") and not line.startswith("|---") and not line.startswith("| Channel")]
    assert rows == [tuple(r) for r in CAT.action_rows()] and len(rows) == 40
    assert sum(1 for r in rows if r[3] == "Yes") == 25 and all(r[6] != "-" for r in rows if r[3] == "No")                   # every action left out has its reason


def test_the_scrapling_matrix_lists_the_features_that_were_asked_about_and_the_provider_switches_are_documented():
    names = {r[0] for r in _rows("## Scrapling 0.5")}
    for wanted in ("Site crawl to Markdown", "Sitemap discovery", "Feed discovery", "Auto-throttle", "Pause and resume", "Async fetchers and sessions", "Parser extraction", "Other parser tools", "Same-site link discovery"):
        assert wanted in names, wanted
    assert "services.ai_gateway.channels.<provider>" in DOC and "approved data classes" in DOC
