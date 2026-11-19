"""Stage 10: the browser bake-off. Public pages only; engines are commands; the go/no-go gate is the owner's."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from contentintel import bakeoff as BO
from contentintel import cli
from contentintel import stage_cli as SC
from contentintel.store import Store

PAGE = "<html><body><h1>Soft overalls</h1><p>" + "comfortable organic cotton pockets deep washing friendly delivery shipping returns " * 6 + "</p></body></html>"
THIN = "<html><body><p>" + "comfortable organic cotton " * 12 + "</p></body></html>"


def engine(command="x {url}", timeout=10):
    return {"command": command, "timeout": timeout}


class Runner:
    """Answers per command word: {program: (html, returncode)} and records the argument lists it was given."""
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, argv, capture_output, text, timeout):
        self.calls.append(argv)
        html, code = self.replies[argv[0]]
        return SimpleNamespace(stdout=html, stderr="" if code == 0 else "boom", returncode=code)


def cfg(**engines):
    return {"engines": {"current": engine("cur {url}"), **engines}, "urls": ["https://a.example", "https://b.example"], "runs": 2}


def test_a_candidate_that_answers_as_often_sees_the_words_and_is_not_too_slow_is_go_and_the_others_are_no_go_or_not_run():
    ticks = iter(range(0, 1000))
    runner = Runner({"cur": (PAGE, 0), "good": (PAGE, 0), "thin": (THIN, 0), "dead": ("", 1)})
    report = BO.run_bakeoff(cfg(good=engine("good {url}"), thin=engine("thin {url}"), dead=engine("dead {url}"), empty=engine("")), runner=runner, clock=lambda: next(ticks))
    assert report["current"]["verdict"] == "BASELINE" and report["good"]["verdict"] == "GO" and report["good"]["parity"] == 1.0 and report["good"]["answered"] == 4
    assert report["thin"]["verdict"] == "NO-GO" and report["thin"]["parity"] < BO.PARITY_AT                           # it saw too little of what the current engine saw
    assert report["dead"]["verdict"] == "NO-GO" and report["dead"]["answered"] == 0 and "exit 1" in report["dead"]["why"]
    assert report["empty"] == {"ran": False, "why": "no command is configured for this engine", "verdict": "NOT RUN"}
    assert runner.calls[0] == ["cur", "https://a.example"]                                                            # {url} is replaced in the command


def test_a_slow_candidate_and_one_that_meets_a_security_check_are_no_go_and_a_missing_program_is_not_run():
    def slow_clock():
        slow_clock.t += 1
        return slow_clock.t
    slow_clock.t = 0
    times = {"cur": 1, "slow": 9, "wall": 1}
    class Timed(Runner):
        def __call__(self, argv, capture_output, text, timeout):
            slow_clock.t += times[argv[0]]
            return super().__call__(argv, capture_output, text, timeout)
    runner = Timed({"cur": (PAGE, 0), "slow": (PAGE, 0), "wall": (PAGE + " Please verify you are human to continue", 0)})
    report = BO.run_bakeoff(cfg(slow=engine("slow {url}"), wall=engine("wall {url}")), runner=runner, clock=slow_clock)
    assert report["slow"]["verdict"] == "NO-GO" and report["slow"]["median_seconds"] > BO.SPEED_FACTOR * report["current"]["median_seconds"]
    assert report["wall"]["checks"] == 4 and report["wall"]["verdict"] == "NO-GO"                                    # a page that shows a check is reported as a check, never worked around
    missing = BO.run_bakeoff(cfg(ghost=engine("definitely-not-installed-program {url}")))
    assert missing["ghost"]["ran"] is False and "not installed on this machine" in missing["ghost"]["why"]


def test_the_config_is_validated_and_the_shipped_file_names_the_candidates_but_builds_in_no_address():
    shipped = BO.load_config("bakeoff.yaml")
    assert shipped["urls"] == [] and {"current", "lightpanda", "obscura", "agent_browser"} <= set(shipped["engines"]) and shipped["engines"]["obscura"]["command"] == ""
    text = Path("bakeoff.yaml").read_text(encoding="utf-8").lower()
    assert "no login" in text and "stealth" in text                                                                    # the rule is written next to the engines
    bad = Path("bad.yaml")
    try:
        bad.write_text("engines: {lightpanda: {command: x}}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="named 'current'"):
            BO.load_config(str(bad))
        bad.write_text("engines: {current: {command: x, stealth: true}}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="may only have"):
            BO.load_config(str(bad))
    finally:
        bad.unlink()


def test_the_words_of_a_page_ignore_scripts_and_styles_and_the_report_says_what_ran_and_what_did_not():
    assert BO.words("<style>hidden{}</style><script>var secret=1</script><p>Soft overalls</p>") == {"soft", "overalls"}
    report = BO.run_bakeoff(cfg(empty=engine("")), runner=Runner({"cur": (PAGE, 0)}))
    text = BO.format_report(report, ["https://a.example", "https://b.example"])
    assert "no login, no stealth, no puzzle solving" in text and "empty: NOT RUN: no command is configured" in text and "current: BASELINE: answered 4 of 4" in text


def test_the_bakeoff_command_needs_public_addresses_from_the_owner_and_prints_the_report(tmp_path, capsys, monkeypatch):
    args = cli.build_parser().parse_args(["stage", "--data-dir", str(tmp_path / "data"), "--action", "bakeoff"])
    assert SC.run_stage(args, Store(tmp_path / "data"), "2026-11-18T10:00:00Z") == 1 and "none is built into the tool" in capsys.readouterr().out
    args = cli.build_parser().parse_args(["stage", "--data-dir", str(tmp_path / "data"), "--action", "bakeoff", "--bakeoff-urls", "https://a.example, https://b.example"])
    runner = Runner({"curl": (PAGE, 0)})
    assert SC.run_stage(args, Store(tmp_path / "data"), "2026-11-18T10:00:00Z", runner=runner) == 0
    out = capsys.readouterr().out
    assert "Browser bake-off on 2 public address(es)" in out and "current: BASELINE" in out and "lightpanda: NOT RUN" in out
