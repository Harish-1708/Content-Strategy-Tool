"""The settings and workflow around the Deep Research fixes and the Google Doc archive: that a setting is really used,
that the workflow runs the archive after the data is collected (and before it is saved), and that a failed archive is
seen. A setting nobody reads, or a step in the wrong place, would pass every other test.
"""
import shutil
from pathlib import Path

import pytest
import yaml

from contentintel import cli
from contentintel import collectors as C
from conftest import BRANDS_DIR


@pytest.fixture
def workflow():
    return yaml.safe_load(open(".github/workflows/collect.yml", encoding="utf-8"))["jobs"]["collect"]["steps"]


def step(steps, name_part):
    (found,) = [s for s in steps if name_part in s.get("name", "")]
    return found


# ---- the prompt-size setting is read, validated and used -------------------------------------------------------------------

def test_the_prompt_size_has_a_default_and_a_place_in_research_yaml():
    assert C.load_collector_settings("does-not-exist.yaml").claude_prompt_max_tokens == 2000
    assert C.load_collector_settings("research.yaml").claude_prompt_max_tokens == 2000
    assert "prompt_max_tokens: 2000" in open("research.yaml", encoding="utf-8").read()


def test_the_prompt_size_can_be_changed_and_a_silly_one_is_refused(tmp_path):
    ok = tmp_path / "a.yaml"
    ok.write_text(yaml.safe_dump({"collectors": {"claude": {"prompt_max_tokens": 3000}}}))
    assert C.load_collector_settings(ok).claude_prompt_max_tokens == 3000
    bad = tmp_path / "b.yaml"
    bad.write_text(yaml.safe_dump({"collectors": {"claude": {"prompt_max_tokens": 100}}}))
    with pytest.raises(C.CollectError, match="prompt_max_tokens"):
        C.load_collector_settings(bad)


def test_the_command_line_hands_the_prompt_size_to_claude(tmp_path, monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, key, **kw):
            seen.update(kw)
            raise RuntimeError("stop here: only the constructor arguments matter")

    browser_yaml = str(Path("browser.yaml").resolve())          # before the folder changes
    monkeypatch.setattr(cli, "Claude", Recorder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "r.yaml"
    cfg.write_text(yaml.safe_dump({"collectors": {"claude": {"prompt_max_tokens": 2468, "max_tokens": 555}}}))
    shutil.copytree(BRANDS_DIR, tmp_path / "brands")
    with pytest.raises(RuntimeError, match="stop here"):
        cli.main(["collect", "--brand", "swoveralls", "--sources", "meta_ads", "--dry-run", "--data-dir", str(tmp_path / "d"),
                  "--brands-dir", str(tmp_path / "brands"), "--research-config", str(cfg), "--browser-config", browser_yaml])
    assert seen == {"model": "claude-sonnet-5-5", "max_tokens": 555, "prompt_max_tokens": 2468}


# ---- the workflow ----------------------------------------------------------------------------------------------------------

def test_the_archive_step_runs_after_the_sheets_and_before_the_data_is_saved(workflow):
    names = [s.get("name", "") for s in workflow]
    sheets, docs, save = (names.index(n) for n in ("Update Google Sheets", "Add Deep Research reports to the Google Doc", "Save the data"))
    assert sheets < docs < save


def test_the_archive_step_only_runs_for_a_real_run_and_cannot_stop_the_data_being_saved(workflow):
    docs = step(workflow, "Add Deep Research reports to the Google Doc")
    assert docs["id"] == "docs" and docs["if"] == "inputs.mode == 'collect' || inputs.mode == ''"
    assert docs["continue-on-error"] is True
    assert "python -m contentintel docs" in docs["run"] and "--check" not in docs["run"]
    assert docs["env"]["GOOGLE_SERVICE_ACCOUNT_JSON"] == "${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}"
    assert docs["env"]["DEEP_RESEARCH_DOC_ID"] == "${{ secrets.DEEP_RESEARCH_DOC_ID }}"


def test_the_archive_step_honours_a_single_brand_choice(workflow):
    run = step(workflow, "Add Deep Research reports to the Google Doc")["run"]
    assert 'args=(--brand "$BRAND")' in run and "args=(--all)" in run


def test_the_check_step_runs_only_in_check_mode_and_proves_the_doc(workflow):
    check = step(workflow, "Check the Google Doc")
    assert check["id"] == "docscheck" and check["if"] == "inputs.mode == 'check'" and check["continue-on-error"] is True
    assert "python -m contentintel docs --all --check" in check["run"]
    assert set(check["env"]) == {"GOOGLE_SERVICE_ACCOUNT_JSON", "DEEP_RESEARCH_DOC_ID"}


def test_a_failed_archive_or_check_alerts_and_shows_the_run_as_red(workflow):
    for name in ("Alert if collection needs attention", "Show the run as red when attention is needed"):
        condition = step(workflow, name)["if"]
        for needed in ("steps.collect.outcome", "steps.sheets.outcome", "steps.docs.outcome", "steps.docscheck.outcome"):
            assert needed in condition and f"{needed} == 'failure'" in condition


def test_the_workflow_header_tells_a_person_about_the_new_mode_behaviour():
    head = open(".github/workflows/collect.yml", encoding="utf-8").read().split("name: Collect")[0]
    assert "DEEP_RESEARCH_DOC_ID" in head and "new tab" in head
