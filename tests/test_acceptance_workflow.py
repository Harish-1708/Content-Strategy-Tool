"""The acceptance workflow must give the code every secret it reads, and say what the README says it does."""
import re

import yaml

from contentintel import browser as B
from conftest import ROOT

TEXT = (ROOT / ".github" / "workflows" / "acceptance.yml").read_text(encoding="utf-8")
FLOW = yaml.safe_load(TEXT)


def test_it_runs_weekly_and_by_hand_with_its_three_choices():
    on = FLOW[True]
    assert on["schedule"] == [{"cron": "17 6 * * 4"}]
    assert set(on["workflow_dispatch"]["inputs"]) == {"only", "plan", "brand"}
    assert on["workflow_dispatch"]["inputs"]["plan"]["type"] == "boolean"


def test_every_secret_the_checks_read_is_passed_to_the_step_that_runs_them():
    step = next(s for s in FLOW["jobs"]["acceptance"]["steps"] if s.get("id") == "acceptance")
    passed = set(step["env"])
    cfg = B.load_browser_settings("browser.yaml")
    needed = {name for site in cfg["sites"].values() for name in (site.get("secrets") or {}).values()}
    needed |= {"ANTHROPIC_API_KEY", "APIFY_TOKEN", "SERPER_API_KEY", "TAVILY_API_KEY", "SERPAPI_API_KEY"}
    assert needed <= passed, sorted(needed - passed)
    for name in needed:
        assert step["env"][name] == "${{ secrets." + name + " }}"


def test_it_shares_the_data_lock_saves_results_and_turns_red_on_a_failure_without_slack():
    assert FLOW["concurrency"]["group"] == "catalogue-data" and FLOW["permissions"] == {"contents": "write"}
    steps = FLOW["jobs"]["acceptance"]["steps"]
    assert next(s for s in steps if s.get("id") == "acceptance")["continue-on-error"] is True
    assert "git add -f data" in next(s for s in steps if s.get("name") == "Save the results")["run"]
    last = steps[-1]
    assert last["run"] == "exit 1" and "steps.acceptance.outcome == 'failure'" in last["if"]
    assert not re.search(r"slack", TEXT.split("# This workflow sends no Slack message", 1)[1].split("name: Acceptance", 1)[1], re.I)


def test_a_plan_makes_no_call_and_saves_nothing():
    steps = {s.get("name"): s for s in FLOW["jobs"]["acceptance"]["steps"]}
    for name in ("Install the browser", "Save the results", "Update Google Sheets", "Show the health of every route"):
        assert "inputs.plan != true" in steps[name]["if"], name
    assert '--plan' in steps["Check each route"]["run"]
