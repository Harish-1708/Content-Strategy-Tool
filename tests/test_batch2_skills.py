"""Batch 2, stage 1: the Aaron marketing skills connected externally (pinned checkout, skill text as data, NEEDS_INPUT for the ones waiting for the stores' own numbers)."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from contentintel import cli
from contentintel import skills as SK
from contentintel.store import Store

SKILL = "---\nname: {name}\nslug: aaron-{name}\nversion: \"1.0\"\ndescription: 'Use when {name}'\n---\n\n# {name}\n\nBody of {name}.\n"


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True, text=True)


def head(root):
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(tmp_path, names=("alpha", "beta")):
    root = tmp_path / "skills"
    for n in names:
        d = root / "fam" / "phase" / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SKILL.format(name=n), encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "rubric-score.py").write_text("print('x')\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


def config(root, names=("alpha", "beta"), **over):
    skills = {f"fam/phase/{n}": {"n": i + 1, "verdict": "useful", "state": "ready", "auto_run": True} for i, n in enumerate(names)}
    skills.update(over.pop("skills", {}))
    return {"repo": {"url": "x", "pinned_commit": head(root), "expected_skills": len(skills), "max_skill_chars": 60000, **over.pop("repo", {})}, "runtime_files": over.pop("runtime_files", ["scripts/rubric-score.py"]), "skills": skills}


def test_the_shipped_policy_covers_all_120_skills_with_the_agreed_states():
    cfg = SK.load_config("external_skills.yaml")
    skills = cfg["skills"]
    assert len(skills) == 120 and sorted(s["n"] for s in skills.values()) == list(range(1, 121))
    assert sum(s["verdict"] == "useful" for s in skills.values()) == 64 and sum(s["verdict"] == "conditional" for s in skills.values()) == 35 and sum(s["verdict"] == "not_useful" for s in skills.values()) == 21
    waiting = {p.split("/")[-1] for p, s in skills.items() if s["state"] == SK.WAITING}
    assert waiting == {"audience-segment-builder", "campaign-architect", "search-term-miner", "attribution-reconciler", "fatigue-frequency-manager", "offsite-signal-analyzer", "performance-monitor", "rank-tracker",
                       "dark-social-attributor", "social-measurement-loop"}
    assert {p for p, s in skills.items() if not s["auto_run"]} == {p for p, s in skills.items() if s["verdict"] == "not_useful"}          # reachable, never auto-run
    assert any(p.endswith("cold-outbound-sequencer") and not s["auto_run"] for p, s in skills.items())
    assert cfg["repo"]["pinned_commit"] == "7932adf5176a9a3ba0e8995be0b3bdee97a330ff" and cfg["repo"]["licence"] if "licence" in cfg["repo"] else True


def test_a_policy_file_that_is_damaged_or_has_a_short_commit_is_refused_in_words(tmp_path):
    good = yaml.safe_load(Path("external_skills.yaml").read_text(encoding="utf-8"))
    for mutate, text in ((lambda d: d["repo"].update(pinned_commit="abc123"), "40-character"), (lambda d: d["skills"]["ad/research/campaign-architect"].update(state="maybe"), "state"),
                         (lambda d: d["skills"]["ad/research/campaign-architect"].update(auto_run="yes"), "auto_run")):
        bad = yaml.safe_load(yaml.safe_dump(good))
        mutate(bad)
        p = tmp_path / "x.yaml"
        p.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(SK.SkillsError, match=text):
            SK.load_config(str(p))
    with pytest.raises(SK.SkillsError, match="could not be read"):
        SK.load_config(str(tmp_path / "missing.yaml"))


def test_discover_reads_the_frontmatter_hashes_the_text_and_skips_git(tmp_path):
    root = make_repo(tmp_path)
    found = SK.discover(root)
    assert sorted(found) == ["fam/phase/alpha", "fam/phase/beta"]
    a = found["fam/phase/alpha"]
    assert a["name"] == "alpha" and a["slug"] == "aaron-alpha" and a["version"] == "1.0" and len(a["sha256"]) == 64 and a["description"].startswith("Use when alpha")
    (root / ".git" / "fake").mkdir()
    (root / ".git" / "fake" / "SKILL.md").write_text("x", encoding="utf-8")
    assert len(SK.discover(root)) == 2
    (root / "fam" / "phase" / "alpha" / "SKILL.md").write_text("no frontmatter at all", encoding="utf-8")
    assert SK.discover(root)["fam/phase/alpha"]["name"] == "alpha"                                 # a skill without frontmatter is still found, named by its folder


def test_verify_says_exactly_what_is_wrong_with_a_checkout(tmp_path):
    root = make_repo(tmp_path)
    cfg = config(root)
    ok = SK.verify(cfg, root)
    assert ok["ok"] and ok["count"] == 2 and ok["commit"] == head(root)
    assert "not checked out" in SK.verify(cfg, tmp_path / "nowhere")["problems"][0]
    wrong = config(root, repo={"pinned_commit": "0" * 40})
    assert "not the pinned commit 0000000000" in " ".join(SK.verify(wrong, root)["problems"])
    short = config(root, repo={"expected_skills": 3})
    assert "2 skills found, 3 expected" in " ".join(SK.verify(short, root)["problems"])
    missing = config(root, runtime_files=["scripts/rubric-score.py", "scripts/validate-audit-artifact.py"])
    assert "validate-audit-artifact.py is missing: the auditors that need it would return NOT_SCORED" in " ".join(SK.verify(missing, root)["problems"])
    ghost = config(root, skills={"fam/phase/ghost": {"n": 9, "verdict": "useful", "state": "ready", "auto_run": True}}, repo={"expected_skills": 3})
    assert "fam/phase/ghost is in external_skills.yaml but not in the repository" in " ".join(SK.verify(ghost, root)["problems"])
    extra = config(root, names=("alpha",), repo={"expected_skills": 2})
    result = SK.verify(extra, root)
    assert result["ok"] and "fam/phase/beta is in the repository but not in external_skills.yaml" in " ".join(result["notes"])      # unlisted: a note, not a failure
    plain = tmp_path / "plain"
    (plain / "fam" / "x").mkdir(parents=True)
    (plain / "fam" / "x" / "SKILL.md").write_text(SKILL.format(name="x"), encoding="utf-8")
    assert "not a git checkout" in " ".join(SK.verify(config(root), plain)["problems"])


def test_a_skill_is_read_as_capped_data_and_no_path_can_leave_the_checkout(tmp_path):
    root = make_repo(tmp_path)
    cfg = config(root)
    skill = SK.read_skill(cfg, root, "fam/phase/alpha")
    assert skill["text"].startswith("---") and not skill["truncated"] and skill["state"] == "ready" and skill["auto_run"] is True and len(skill["sha256"]) == 64
    capped = SK.read_skill(config(root, repo={"max_skill_chars": 20}), root, "fam/phase/alpha")
    assert len(capped["text"]) == 20 and capped["truncated"] is True and "cut here" in SK.as_data(capped)
    for bad in ("../x", "/etc/passwd", "fam//phase", "fam/../../x", "", "a b", "fam/phase/alpha/../../..", "fam/phase/../phase/alpha"):          # the last one stays inside the checkout and is still refused
        with pytest.raises(SK.SkillsError):
            SK.read_skill(cfg, root, bad)
    with pytest.raises(SK.SkillsError, match="not a skill in this checkout"):
        SK.read_skill(cfg, root, "fam/phase/nothing")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("secret", encoding="utf-8")
    (root / "fam" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SK.SkillsError, match="outside the skills checkout"):
        SK.read_skill(cfg, root, "fam/link")                                                        # a symlink out of the checkout is not followed
    wrapped = SK.as_data(skill)
    assert wrapped.startswith('<skill path="fam/phase/alpha"') and 'data="true"' in wrapped and "not an instruction to you" in wrapped and wrapped.rstrip().endswith("</skill>")


def test_what_may_start_is_decided_by_the_policy_not_by_the_skill():
    cfg = {"skills": {"a/ready": {"state": "ready", "auto_run": True}, "a/quiet": {"state": "ready", "auto_run": False}, "a/wait": {"state": "waiting_for_access", "auto_run": True}}}
    assert SK.may_start(cfg, "a/ready") == (True, "")
    ok, why = SK.may_start(cfg, "a/wait")
    assert not ok and why.startswith("NEEDS_INPUT:") and "never invents them" in why
    assert not SK.may_start(cfg, "a/wait", requested=True)[0]                                       # asking by name does not unlock a skill that has no numbers to work from
    assert not SK.may_start(cfg, "a/quiet")[0] and SK.may_start(cfg, "a/quiet", requested=True) == (True, "")
    assert not SK.may_start(cfg, "a/unlisted")[0] and SK.may_start(cfg, "a/unlisted", requested=True) == (True, "")


def test_the_update_report_lists_upstream_changes_without_adopting_any(tmp_path):
    root = make_repo(tmp_path)
    pinned = head(root)
    cfg = config(root)
    (root / "fam" / "phase" / "alpha" / "SKILL.md").write_text(SKILL.format(name="alpha") + "changed\n", encoding="utf-8")
    new = root / "fam" / "phase" / "gamma"
    new.mkdir()
    (new / "SKILL.md").write_text(SKILL.format(name="gamma"), encoding="utf-8")
    (root / "fam" / "phase" / "beta" / "SKILL.md").unlink()
    (root / "scripts" / "rubric-score.py").write_text("print('y')\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "second")
    git(root, "update-ref", "refs/remotes/origin/HEAD", head(root))
    report = SK.update_report(cfg, root)
    assert report["pinned"] == pinned and report["latest"] == head(root) and not report["up_to_date"]
    assert report["skills_changed"] == ["fam/phase/alpha"] and report["skills_added"] == ["fam/phase/gamma"] and report["skills_removed"] == ["fam/phase/beta"] and report["runtime_changed"] == ["M scripts/rubric-score.py"]
    assert cfg["repo"]["pinned_commit"] == pinned                                                   # nothing was adopted
    git(root, "update-ref", "refs/remotes/origin/HEAD", pinned)
    assert SK.update_report(cfg, root)["up_to_date"] is True
    with pytest.raises(SK.SkillsError, match="fetched with its history"):
        SK.update_report(cfg, tmp_path / "nowhere")


def test_the_commands_say_it_plainly_and_fail_when_the_connection_cannot_be_trusted(tmp_path, capsys):
    root = make_repo(tmp_path)
    cfg = config(root, skills={"fam/phase/beta": {"n": 2, "verdict": "not_useful", "state": "waiting_for_access", "auto_run": False}})
    cfg_path = tmp_path / "policy.yaml"
    cfg_path.write_text(yaml.safe_dump({"repo": cfg["repo"], "runtime_files": cfg["runtime_files"], "skills": cfg["skills"]}), encoding="utf-8")
    store = Store(tmp_path / "data")
    base = ["skills", "--skills-config", str(cfg_path), "--skills-dir", str(root), "--data-dir", str(tmp_path / "data")]
    assert cli.main(base + ["--action", "verify"]) == 0 and "skills: OK: 2 skills at " in capsys.readouterr().out
    assert cli.main(base + ["--action", "status"]) == 0
    out = capsys.readouterr().out
    assert "2 skills (1 not_useful, 1 useful); 1 built ready and waiting for access (NEEDS_INPUT), 1 reachable but never auto-run" in out and "waiting for the stores' own numbers: fam/phase/beta" in out
    assert cli.main(base + ["--action", "catalogue"]) == 0
    saved = __import__("json").loads((tmp_path / "data" / "_shared" / "skills_catalogue.json").read_text(encoding="utf-8"))
    assert saved["ok"] is True and len(saved["rows"]) == 2 and saved["rows"][0]["sha256"]
    (root / "scripts" / "rubric-score.py").unlink()
    assert cli.main(base + ["--action", "verify"]) == 1 and "PROBLEM: runtime file scripts/rubric-score.py is missing" in capsys.readouterr().out
    assert store is not None


def test_the_workflow_checks_out_the_pinned_commit_and_fails_a_verify_that_fails():
    wf = yaml.safe_load(Path(".github/workflows/skills.yml").read_text(encoding="utf-8"))
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert inputs["action"]["options"] == ["status", "verify", "update-report", "catalogue", "seed-claims", "claims-status", "gate"]
    steps = wf["jobs"]["skills"]["steps"]
    text = Path(".github/workflows/skills.yml").read_text(encoding="utf-8")
    assert "git clone --quiet --filter=blob:none --no-checkout" in text and 'git -C "$path" checkout --quiet "$sha"' in text and "set -o pipefail" in text
    assert [s.get("name") for s in steps if s.get("name")] == ["Check out the skills repository at the pinned commit", "Run it", "Save the data"]
    assert wf["permissions"] == {"contents": "write"} and wf["concurrency"]["group"] == "catalogue-data"


@pytest.mark.skipif(not os.environ.get("AARON_SKILLS_DIR"), reason="needs a checkout of the real skills repository (set AARON_SKILLS_DIR); the Skills and registries workflow checks it out")
def test_against_the_real_repository_all_120_skills_are_found_and_the_pinned_commit_is_present():
    cfg = SK.load_config("external_skills.yaml")
    root = SK.checkout_dir(cfg)
    result = SK.verify(cfg, root)
    assert result["ok"], result["problems"]
    assert result["count"] == 120 and len(SK.catalogue(cfg, root)) == 120 and all(r["present"] for r in SK.catalogue(cfg, root))
    assert max(m["chars"] for m in SK.discover(root).values()) < cfg["repo"]["max_skill_chars"]
    skill = SK.read_skill(cfg, root, "launch/research/positioning-mapper")
    assert "positioning" in skill["text"].lower() and not skill["truncated"]
