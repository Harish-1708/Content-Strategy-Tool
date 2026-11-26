"""The partial-failure save rule (Update 73 v5): one failing step never loses a run's data, a concurrent save never drops it, and a save that cannot push keeps the data as an artifact."""
import os
import re
import subprocess
from pathlib import Path

import yaml

WORKFLOWS = sorted(Path(".github/workflows").glob("*.yml"))
TRIAL = {"photo-trial.yml", "video-trial.yml"}                         # the developer's own files: not part of the shipped package
SCRIPT = Path(".github/scripts/push-with-retry.sh").resolve()


def save_steps(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in data["jobs"].values():
        for step in job.get("steps", []):
            if "push-with-retry.sh" in str(step.get("run", "")):
                yield job, step


def test_every_workflow_that_saves_data_saves_even_when_an_earlier_step_failed_and_retries_the_push():
    checked = 0
    for path in WORKFLOWS:
        text = path.read_text(encoding="utf-8")
        if path.name in TRIAL or ("git push" not in text and "push-with-retry.sh" not in text):
            continue
        steps = list(save_steps(path))
        assert steps, f"{path.name} still pushes by hand"
        for job, step in steps:
            assert "always()" in str(step.get("if", "")), f"{path.name}: the save step is skipped when an earlier step fails"
            assert "git push" not in step["run"] and "git pull" not in step["run"], path.name
            names = [s.get("name", "") for s in job["steps"]]
            keep = next(s for s in job["steps"] if s.get("name", "").startswith("Keep the unsaved data"))
            assert keep["if"] == "failure()" and keep["with"]["path"] == "unsaved-data/" and keep["with"]["if-no-files-found"] == "ignore" and names.index(keep["name"]) > names.index(step["name"])
            checked += 1
    assert checked == 20                                                                                                                       # 22 workflows save data; 2 are the developer's own trial files
    for name in TRIAL:
        assert (Path(".github/workflows") / name).read_text(encoding="utf-8").count("git push") <= 1                                         # untouched here: PLACEMENT gives their two-line change


def test_the_save_conditions_keep_what_they_meant_and_add_always():
    def cond(name):
        return next(iter(save_steps(Path(".github/workflows") / name)))[1]["if"]
    assert cond("acceptance.yml") == "always() && inputs.plan != true" and "!contains(" in cond("observatory.yml") and cond("observatory.yml").startswith("${{ always() && (")
    assert cond("sessions.yml").startswith("${{ always() && (") and "inputs.what == 'check'" in cond("sessions.yml") and cond("pool-retest.yml") == "always() && inputs.apply"
    for name in ("skills.yml", "stage-tools.yml", "research-tools.yml", "collect.yml", "brief.yml", "sync.yml", "research.yml", "request.yml", "catalogue-admin.yml"):
        assert cond(name).startswith("always()") and "inputs." not in cond(name).split("#")[0], name                                          # every action that writes data, not a list of them


def git(cwd, *args, **kw):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, **kw)


def clone_pair(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
    for cwd in (seed,):
        git(cwd, "config", "user.name", "t")
        git(cwd, "config", "user.email", "t@t")
    (seed / "data").mkdir()
    (seed / "data" / "health.json").write_text('{"v": 0}\n', encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed")
    git(seed, "push", "-q", "origin", "HEAD:main")
    runs = []
    for name in ("a", "b"):
        d = tmp_path / name
        subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True, capture_output=True)
        git(d, "config", "user.name", name)
        git(d, "config", "user.email", f"{name}@t")
        runs.append(d)
    return remote, runs


def run_script(cwd, **env):
    return subprocess.run(["bash", str(SCRIPT)], cwd=cwd, capture_output=True, text=True, env={**os.environ, "SAVE_WAIT": "0", **env})


def test_a_concurrent_save_that_changed_the_same_file_no_longer_drops_the_second_runs_data(tmp_path):
    remote, (a, b) = clone_pair(tmp_path)
    (a / "data" / "health.json").write_text('{"v": "from run A"}\n', encoding="utf-8")
    git(a, "add", "-f", "data")
    git(a, "commit", "-qm", "run A")
    assert run_script(a).returncode == 0                                                                                                       # run A saves first
    (b / "data" / "health.json").write_text('{"v": "from run B"}\n', encoding="utf-8")                                                          # run B changed the SAME file while A was saving: plain `git pull --rebase` stops here with a conflict
    (b / "data" / "extra.json").write_text('{"only": "B"}\n', encoding="utf-8")
    git(b, "add", "-f", "data")
    git(b, "commit", "-qm", "run B")
    plain = git(b, "pull", "--rebase", "origin", "main")
    assert plain.returncode != 0                                                                                                                # the old save: a conflict, the data stays on the runner
    git(b, "rebase", "--abort")
    res = run_script(b)
    assert res.returncode == 0 and "SAVED: pushed" in res.stdout
    final = tmp_path / "final"
    subprocess.run(["git", "clone", "-q", str(remote), str(final)], check=True, capture_output=True)
    assert __import__("json").loads((final / "data" / "health.json").read_text(encoding="utf-8")) == {"v": "from run B"} and (final / "data" / "extra.json").is_file()
    log = git(final, "log", "--format=%s").stdout.split()
    assert "A" in log and "B" in log                                                                                                            # both runs' commits are in the history


def test_a_push_that_keeps_being_refused_is_retried_then_kept_as_a_patch_and_fails_loudly(tmp_path):
    remote, (a, _b) = clone_pair(tmp_path)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (a / "data" / "health.json").write_text('{"v": "lost?"}\n', encoding="utf-8")
    git(a, "add", "-f", "data")
    git(a, "commit", "-qm", "run A")
    res = run_script(a, SAVE_TRIES="3")
    assert res.returncode == 1 and res.stdout.count("push refused on try") == 3 and "::error title=Save failed::" in res.stdout
    patches = list((a / "unsaved-data").glob("*.patch"))
    assert len(patches) == 1 and "lost?" in patches[0].read_text(encoding="utf-8")                                                            # the run's data is kept, not dropped


def test_the_helper_script_is_the_one_place_the_rule_lives():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "git pull --rebase --autostash origin" in text and "resolve_conflicts" in text and 'SAVE_TRIES:-5' in text and "format-patch -1 HEAD -o unsaved-data" in text and "exit 1" in text
    assert not re.search(r"\|\| true\s*$", text.split("n=1")[0])


def test_no_save_step_adds_several_named_files_in_one_git_add():
    for path in WORKFLOWS:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*git add -f (.+)$", line)
            if m:
                targets = m.group(1).split("#")[0].split()
                assert len(targets) == 1, f"{path.name}: one git add names {len(targets)} files, and git refuses the whole add when one of them is missing"
    assert "add_each()" in Path(".github/workflows/stage-tools.yml").read_text(encoding="utf-8")                                              # the stage-tools save adds each file on its own


def test_two_runs_that_each_added_data_to_the_same_json_file_both_keep_it_after_a_conflict(tmp_path):
    remote, (a, b) = clone_pair(tmp_path)
    base = {"months": {"2026-11": {"serper": 10}}, "events": [1], "note": "seed"}
    (a / "data" / "usage.json").write_text(json_dump(base), encoding="utf-8")
    git(a, "add", "-f", "data")
    git(a, "commit", "-qm", "seed usage")
    assert run_script(a).returncode == 0
    git(b, "pull", "-q", "origin", "main")
    mine_a = {"months": {"2026-11": {"serper": 10, "apify": 4}}, "events": [1, 2], "note": "seed"}
    mine_b = {"months": {"2026-11": {"serper": 10, "exa": 7}}, "events": [1, 3], "note": "run B"}
    (a / "data" / "usage.json").write_text(json_dump(mine_a), encoding="utf-8")
    git(a, "add", "-f", "data")
    git(a, "commit", "-qm", "run A usage")
    assert run_script(a).returncode == 0                                                                                                       # run A saved first
    (b / "data" / "usage.json").write_text(json_dump(mine_b), encoding="utf-8")
    git(b, "add", "-f", "data")
    git(b, "commit", "-qm", "run B usage")
    assert git(b, "pull", "--rebase", "origin", "main").returncode != 0                                                                        # a plain pull conflicts here
    git(b, "rebase", "--abort")
    res = run_script(b)
    assert res.returncode == 0 and "SAVED: pushed" in res.stdout
    final = tmp_path / "final"
    subprocess.run(["git", "clone", "-q", str(remote), str(final)], check=True, capture_output=True)
    merged = __import__("json").loads((final / "data" / "usage.json").read_text(encoding="utf-8"))
    assert merged["months"]["2026-11"] == {"serper": 10, "apify": 4, "exa": 7}                                                                 # BOTH runs' counters are in the file
    assert merged["events"] == [1, 2, 3] and merged["note"] == "run B"                                                                         # both runs' events; the value both changed takes this run's


def json_dump(data):
    return __import__("json").dumps(data, indent=2) + "\n"


def test_a_conflict_in_a_file_that_is_not_json_takes_this_runs_version_and_the_merge_helper_is_shipped_with_the_save_helper():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "merge-json-conflict.py" in text and "git checkout --theirs" in text and "rebase --continue" in text
    assert Path(".github/scripts/merge-json-conflict.py").is_file()


def test_the_json_merge_keeps_both_runs_changes_and_honours_a_deletion_that_the_other_run_left_alone():
    import importlib.util
    spec = importlib.util.spec_from_file_location("mjc", ".github/scripts/merge-json-conflict.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    merge = mod.merge
    assert merge({"a": 1, "items": [1]}, {"a": 1, "items": [1, 2], "x": 5}, {"a": 2, "items": [1, 3], "y": 6}) == {"a": 2, "items": [1, 2, 3], "x": 5, "y": 6}
    assert merge({"k": {"a": 1, "b": 2}}, {"k": {"a": 1}}, {"k": {"a": 1, "b": 2, "c": 3}}) == {"k": {"a": 1, "c": 3}}                         # the other run deleted b and I left it alone: it stays deleted, my new c stays
    assert merge({"k": {"a": 1, "b": 2}}, {"k": {"a": 1, "b": 2, "c": 3}}, {"k": {"a": 1}}) == {"k": {"a": 1, "c": 3}}                         # I deleted b, the other run left it alone: deleted; its new c stays
    assert merge({"k": {"b": 2}}, {"k": {"b": 5}}, {"k": {"a": 1}}) == {"k": {"b": 5, "a": 1}}                                                  # the other run CHANGED b: my deletion does not win over a change
    assert merge({"n": 1}, {"n": 2}, {"n": 3}) == {"n": 3} and merge([1], [1, 2], [1, 3]) == [1, 2, 3] and merge({"a": 1}, {"a": 1}, {"a": 1, "b": 2}) == {"a": 1, "b": 2}
    assert merge({"x": [{"id": 1}]}, {"x": [{"id": 1}, {"id": 2}]}, {"x": [{"id": 1}, {"id": 2}, {"id": 3}]}) == {"x": [{"id": 1}, {"id": 2}, {"id": 3}]}   # an item both runs added is not doubled
