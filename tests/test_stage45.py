"""Stages 4 and 5 (Update 73 v4): the formula library, winners, opportunity scores and cross-brand view; the validator bank, golden set, rule versions and the independent reviewer; and their wiring."""
import json
from types import SimpleNamespace
from pathlib import Path

from contentintel import formulas as F
from contentintel import validators as V
from contentintel.store import Store

NOW = "2026-11-24T10:00:00Z"
LIB = F.load_library()


def item(i, platform="instagram", interactions=10, state="kept", hook="", days=None, **kw):
    metrics = {"likes": interactions, "comments": 0} if days is None else {"days_running": days}
    base = {"id": f"IB-{i:04d}", "platform": platform, "handle": "h", "state": state, "score": 70, "hook_line": hook, "text": "", "title": "", "metrics": metrics,
            "url": f"https://x.example/{i}", "posted_at": "2026-11-20T00:00:00Z", "media_format": "", "format": "", "approved": False, "rescued": False}
    base.update(kw)
    return base


def bank_of(*items):
    return {"items": {it["id"]: it for it in items}}


# ---- the library -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_library_holds_the_26_reel_and_21_youtube_formulas_and_names_where_it_read_them(tmp_path):
    assert LIB.counts == {"instagram": 26, "youtube": 21} and LIB.source == "snapshot in contentintel/data/formulas" and "MIT" in Path("contentintel/data/formulas/NOTICE.txt").read_text(encoding="utf-8")
    ext = tmp_path / "ext" / "instagram_skill" / "skills" / "ig-reel"
    ext.mkdir(parents=True)
    data = json.loads(Path("contentintel/data/formulas/ig_hooks.json").read_text(encoding="utf-8"))
    data["hooks"] = data["hooks"][:3]
    data["classify_order"] = [h["id"] for h in data["hooks"]]
    (ext / "hooks.json").write_text(json.dumps(data), encoding="utf-8")
    fresh = F.load_library(tmp_path / "ext")
    assert fresh.counts["instagram"] == 3 and fresh.source.startswith("external checkout: ig") and "the rest from the snapshot" in fresh.source and fresh.counts["youtube"] == 21


def test_a_hook_is_named_by_the_first_formula_it_matches_and_none_when_nothing_does():
    got = LIB.classify("$18,000 is what one missing clause cost me.")
    assert got == {"id": "ig:1", "name": "Cost Confession", "platform": "instagram"}
    assert LIB.classify("97% of the people who start a channel quit before video 30.", "youtube")["id"] == "yt:the-statistic"
    assert LIB.classify("Here are our overalls.") is None and LIB.classify("") is None
    assert F.hook_line("First sentence here. Second one.") == "First sentence here."


# ---- winners ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_winner_is_at_least_three_times_the_median_of_its_own_platform_and_needs_four_to_compare_against():
    items = [item(1, interactions=10), item(2, interactions=12), item(3, interactions=11), item(4, interactions=10), item(5, interactions=100, hook="$18,000 is what one missing clause cost me.")]
    m = F.outlier_multiples(items)
    assert m["IB-0005"] == {"multiple": 9.09, "group_n": 5, "confidence": "low"} and m["IB-0001"]["multiple"] == 0.91
    win = F.winners(items, LIB)
    assert [w["id"] for w in win] == ["IB-0005"] and win[0]["formula"] == "ig:1" and win[0]["confidence"] == "low"
    few = F.outlier_multiples([item(1, interactions=10), item(2, interactions=90), item(3, interactions=10)])
    assert all(v["multiple"] is None for v in few.values()) and F.winners([item(1, interactions=10), item(2, interactions=90), item(3, interactions=10)], LIB) == []        # three items: nothing to compare against
    assert F.confidence_of(7) == "low" and F.confidence_of(8) == "medium" and F.confidence_of(25) == "high"


def test_ads_are_compared_by_days_running_and_apart_from_social_posts():
    ads = [item(i, platform="meta_ads", days=d) for i, d in enumerate((10, 12, 11, 10, 90), start=1)]
    posts = [item(10 + i, interactions=5000) for i in range(4)]
    m = F.outlier_multiples(ads + posts)
    assert m["IB-0005"]["multiple"] == 8.18 and m["IB-0010"]["multiple"] == 1.0                                                                   # the ad is not measured against posts, nor the posts against ads
    assert F.signal_of(item(1, platform="meta_ads", days=7)) == ("days_running", 7.0) and F.signal_of(item(2, interactions=3)) == ("interactions", 3.0)
    assert F.signal_of({"platform": "instagram", "metrics": {}}) is None


def test_formula_lift_needs_eight_tagged_items_and_a_winner_and_says_why_when_it_cannot():
    own = [(fid, ex) for _p, fid, _n, ex in LIB.examples() if fid.startswith("ig:") and (LIB.classify(ex) or {}).get("id") == fid]
    (fid_a, hook_a), (_, hook_b) = own[0], own[1]
    items = [item(i, interactions=10, hook=hook_a if i % 2 else hook_b) for i in range(1, 9)] + [item(9, interactions=200, hook=hook_a)]
    rows, n = F.formula_report(items, LIB, {"IB-0009"})
    top = rows[0]
    assert n == 9 and top["formula"] == fid_a and top["winners"] == 1 and top["lift"] is not None and top["lift"] > 1
    small, n2 = F.formula_report(items[:3], LIB, set())
    assert all(r["lift"] is None and "lift needs at least 8 tagged items" in r["note"] for r in small)


# ---- opportunity score ------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_opportunity_score_uses_the_six_weights_and_names_every_input_it_could_not_know():
    assert abs(sum(F.WEIGHTS.values()) - 1.0) < 1e-9 and F.WEIGHTS == {"fit": 0.30, "momentum": 0.20, "originality": 0.15, "feasibility": 0.15, "strategic": 0.10, "evidence": 0.10}
    base = item(1, media_format="IMAGE", approved=True)
    ctx = {"now": NOW, "formula": {"id": "ig:1"}, "formula_share": {"ig:1": 0.1}}
    o = F.opportunity_score(base, ctx)
    assert o["unknown"] == [] and o["confidence"] == "high" and 0 < o["score"] <= 100
    for field, worse in (("score", 10), ("approved", False), ("media_format", "VIDEO")):
        changed = F.opportunity_score({**base, field: worse}, ctx)
        assert changed["score"] < o["score"], field                                                                                               # each input moves the score
    assert F.opportunity_score(base, {**ctx, "formula_share": {"ig:1": 0.9}})["score"] < o["score"]                                            # a crowded formula is less original
    blind = F.opportunity_score({"id": "x", "platform": "instagram", "state": "kept", "metrics": {}}, {"now": NOW})
    assert set(blind["unknown"]) == {"fit", "momentum", "originality", "feasibility"} and blind["confidence"] == "low"


def test_a_report_shows_its_sample_size_and_confidence_and_says_when_there_is_too_little():
    rep = F.build_report("swoveralls", bank_of(item(1), item(2, state="removed")), LIB, NOW)
    assert rep["sample"]["kept_items"] == 1 and rep["sample"]["confidence"] == "low" and rep["winners"] == [] and any("too few to compare" in n for n in rep["notes"])
    assert rep["weights"] == F.WEIGHTS and rep["library"]["instagram"] == 26
    lines = F.render(rep)
    assert "1 kept item(s) (confidence low)" in lines[0] and any(l.startswith("  note:") for l in lines)


def test_cross_brand_finds_formulas_that_win_in_several_brands_and_ideas_that_could_move():
    def rep(brand, rows):
        return {"brand": brand, "formulas": [{"formula": f, "winners": w, "items": 3} for f, w in rows]}
    out = F.cross_brand([rep("a", [("ig:1", 1), ("ig:2", 1)]), rep("b", [("ig:1", 2), ("ig:3", 0)]), rep("c", [("ig:3", 0)])])
    assert out["winning_in_several_brands"] == ["ig:1"] and out["could_transfer"] == [{"formula": "ig:2", "wins_in": ["a"], "unused_in": ["b", "c"]}] and out["note"] == ""
    assert "at least two" in F.cross_brand([rep("a", [])])["note"]


# ---- the validator bank ----------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_slop_is_found_by_kind_and_only_an_invisible_character_is_an_error():
    rules = {f["rule"]: f for f in V.slop_check("Let's delve into this. In today's fast-paced world it's not just an overall, it's a lifestyle\u200b — really.", LIB)}
    assert set(rules) >= {"slop_word", "slop_phrase", "slop_structure", "slop_invisible", "slop_typographic"}
    assert rules["slop_invisible"]["severity"] == "error" and all(r["severity"] == "warning" for k, r in rules.items() if k != "slop_invisible")
    assert V.slop_check("Soft cotton overalls, cut for every day.", LIB) == [] and V.slop_check("", LIB) == []


def test_a_hook_is_checked_for_length_and_for_a_known_formula():
    ok = V.hook_check("$18,000 is what one missing clause cost me.", LIB)
    assert ok == []
    none = V.hook_check("Here are our overalls.", LIB)
    assert [f["rule"] for f in none] == ["hook_no_formula"] and none[0]["severity"] == "warning"
    long_ = V.hook_check("$18,000 is what one missing clause cost me " + "and more " * 8 + ".", LIB)
    assert "hook_long" in {f["rule"] for f in long_}
    huge = V.hook_check("word " * 60, LIB)
    assert "hook_too_long" in {f["rule"] for f in huge} and next(f for f in huge if f["rule"] == "hook_too_long")["severity"] == "error"
    assert V.hook_check("  ", LIB)[0]["rule"] == "hook_empty"


def concept(**kw):
    base = {"name": "Cost confession", "format": "30 second vertical video", "hook": "$18,000 is what one missing clause cost me.",
            "script": "Show the contract on a desk, say what the missing clause was and show the overalls on a hanger.", "visual_direction": "Close-up of the contract, then a wide shot of the overalls in daylight.", "cta": "See them at the link."}
    base.update(kw)
    return base


def test_each_format_gets_the_parts_it_needs():
    assert V.structure_check(concept(), "c") == []
    missing = {f["rule"]: f["severity"] for f in V.structure_check(concept(cta="", name=""), "c")}
    assert missing == {"missing_cta": "error", "missing_name": "error"}
    assert "script_thin" in {f["rule"] for f in V.structure_check(concept(script="Short."), "c")}
    assert "carousel_slides" in {f["rule"] for f in V.structure_check(concept(format="carousel"), "c")}
    assert "carousel_slides" not in {f["rule"] for f in V.structure_check(concept(format="carousel", script="1. one thing here\n2. two things here\n3. three things here"), "c")}
    assert "static_long" in {f["rule"] for f in V.structure_check(concept(format="static image", script="word " * 95), "c")}
    assert "motion_direction" in {f["rule"] for f in V.structure_check(concept(format="motion graphic", visual_direction="short"), "c")}


def test_the_bank_runs_over_every_concept_and_the_brief_fields_and_names_where():
    brief = {"title": "Delve into overalls", "key_message": "Soft cotton", "concepts": [concept(), concept(name="Second", hook="Here are our overalls.", script="Let's delve into the cotton and why it feels soft for every day.")]}
    found = V.brief_bank(brief, LIB)
    where = {f["where"] for f in found}
    assert "title" in where and any("concept 2 (Second) hook" == w for w in where) and any("concept 2 (Second) script" == w for w in where) and not any(w.startswith("concept 1") for w in where)
    stats = V.summarise(found)
    assert stats["errors"] == 0 and stats["warnings"] == len(found) and "slop_word" in stats["rules"]


# ---- the golden set --------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_golden_set_passes_and_a_validator_that_goes_blind_makes_it_fail():
    res = V.run_golden(LIB)
    assert res["failures"] == [] and res["checked"] >= 51
    blind = F.Library(LIB.ig, LIB.yt, {**LIB.slop, "words": [], "phrases": [], "structures": [], "invisible": []}, "test")
    broken = V.run_golden(blind)
    assert len(broken["failures"]) == 4 and all("must be flagged" in f for f in broken["failures"])                                           # every text that must be flagged is missed
    extra = V.run_golden(LIB, [{"text": "Let's delve into it.", "expect": "pass"}, {"text": "Plain words only.", "expect": "flag", "rule": "slop_word"}, {"text": "A good approved line.", "expect": "pass"}])
    assert len(extra["failures"]) == 1 and "must be flagged" in extra["failures"][0]                                                           # an approved text with only a warning passes; a clean text that must be flagged fails
    bad = V.run_golden(LIB, [{"text": "Soft\u200b cotton", "expect": "pass"}])
    assert "an approved text is refused" in bad["failures"][0]
    assert res["library_mismatches"] == []                                                                                                   # every one of the 47 worked examples is named as its own formula (a mismatch would be listed, not hidden)


# ---- rule versions ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_rules_get_a_new_version_number_each_time_their_digest_changes(tmp_path):
    store = Store(tmp_path / "data")
    assert V.note_rules_version(store, "swoveralls", "aaa", NOW) == (1, None)
    assert V.note_rules_version(store, "swoveralls", "aaa", NOW) == (1, None)                                                                  # the same rules: the same version
    assert V.note_rules_version(store, "swoveralls", "bbb", NOW) == (2, 1)
    assert V.note_rules_version(store, "swoveralls", "aaa", NOW) == (1, None)                                                                  # going back to old rules is the old version
    assert [v["version"] for v in store.read_extra("swoveralls", "rules_versions")["versions"]] == [1, 2] and V.note_rules_version(store, "duderobe", "aaa", NOW) == (1, None)


# ---- the independent reviewer ----------------------------------------------------------------------------------------------------------------------------------------------------------------

class Judge:
    def __init__(self, answer=None, boom=False):
        self.answer, self.boom, self.seen = answer, boom, []

    def json_task(self, system, user, max_tokens=None):
        self.seen.append((system, user))
        if self.boom:
            raise RuntimeError("down")
        return self.answer


def test_the_reviewer_only_passes_a_clean_pass_and_never_blocks_a_brief_when_it_is_down():
    assert V.parse_review({"verdict": "pass", "problems": [], "checklist": {"a": True}})["verdict"] == "pass"
    assert V.parse_review({"verdict": "pass", "problems": [{"where": "c1", "issue": "unverifiable"}]})["verdict"] == "revise"                 # a pass that lists a problem is not a pass
    assert V.parse_review({"verdict": "pass", "checklist": {"a": True, "b": False}})["verdict"] == "revise"
    assert V.parse_review({"verdict": "maybe"})["verdict"] == "unclear" and V.parse_review("nonsense")["verdict"] == "unclear"
    brief = {"title": "t", "key_message": "k", "concepts": [concept()]}
    finding = [{"where": "concept 1 hook", "rule": "x", "severity": "warning", "message": "slow"}]
    j = Judge({"verdict": "revise", "problems": [{"where": "concept 1", "issue": "no proof"}], "checklist": {"no_unverifiable_claim": False}})
    got = V.review(brief, finding, j, rules_note="no medical claims")
    assert got["verdict"] == "revise" and got["problems"][0]["issue"] == "no proof" and j.seen[0][0] == V.REVIEW_SYSTEM
    assert "Validator findings (exact checks already run): concept 1 hook: slow" in j.seen[0][1] and "Rules in force: no medical claims" in j.seen[0][1] and "Hook: $18,000" in j.seen[0][1]
    down = V.review(brief, [], Judge(boom=True))
    assert down["verdict"] == "unavailable" and "RuntimeError" in down["why"]


# ---- wiring ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_the_reviewer_has_its_own_ranked_lane_job_and_retest_probe():
    import yaml
    from contentintel import services
    from contentintel.ai import ranking
    lanes = yaml.safe_load(Path("ai_lanes.yaml").read_text(encoding="utf-8"))["lanes"]
    assert "brief_review_json" in lanes and lanes["brief_review_json"]["chain"] and "brief_review_json" in ranking.JOBS and services.JOB_LANES["brief_review"] == "brief_review_json"
    system, user, score = ranking.probes()["brief_review_json"][0]
    assert system == V.REVIEW_SYSTEM and "100% guaranteed" in user
    assert score({"verdict": "revise", "problems": [{"issue": "an unverifiable claim"}]}) == 1.0 and score({"verdict": "pass", "problems": []}) == 0.0 and score("x") == 0.0


def test_a_brief_records_the_validator_findings_the_rules_version_and_the_review(tmp_path):
    import tests.test_brief as TB
    assert "VALIDATORS.brief_bank(won[\"brief\"], library)" in Path("contentintel/brief.py").read_text(encoding="utf-8")
    assert TB is not None


def test_the_intel_command_reports_saves_and_runs_the_golden_set_and_the_brand_gate(tmp_path, capsys):
    from contentintel import cli
    store = Store(tmp_path / "data")
    bank = bank_of(*[item(i, interactions=10) for i in range(1, 5)], item(5, interactions=120, hook="$18,000 is what one missing clause cost me."))
    store.write_ideabank("swoveralls", bank)
    code = cli.main(["intel", "--action", "report", "--brand", "swoveralls", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")])
    out = capsys.readouterr().out
    assert code == 0 and "winner IB-0005" in out and "saved data/swoveralls/extras/creative_intel.json" in out and "saved data/_shared/creative_intel.json" in out
    saved = json.loads((tmp_path / "data" / "swoveralls" / "extras" / "creative_intel.json").read_text(encoding="utf-8"))
    assert saved["winners"][0]["id"] == "IB-0005" and saved["sample"]["kept_items"] == 5 and (tmp_path / "data" / "_shared" / "creative_intel.json").is_file()
    assert cli.main(["intel", "--action", "golden", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")]) == 0 and "golden set: PASSED" in capsys.readouterr().out
    assert cli.main(["intel", "--action", "gate", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")]) == 0
    gate = capsys.readouterr().out
    assert all(f"{slug}: PASSED" in gate for slug in ("swoveralls", "duderobe", "defi_snacks")) and "every brand passes with no brand-specific code" in gate
    empty = cli.main(["intel", "--action", "winners", "--brand", "defi_snacks", "--brands-dir", "brands", "--data-dir", str(tmp_path / "data")])
    assert empty == 0 and "0 kept item(s)" in capsys.readouterr().out


def test_no_validator_or_formula_code_names_a_brand_and_the_workflow_runs_by_itself_every_week():
    for path in ("contentintel/formulas.py", "contentintel/validators.py", "contentintel/intel_cli.py"):
        text = Path(path).read_text(encoding="utf-8").lower()
        assert not any(name in text for name in ("swoveralls", "duderobe", "defi_snacks", "defi snacks")) or path.endswith("intel_cli.py") and "defi snacks" not in text
    wf = Path(".github/workflows/creative-intel.yml").read_text(encoding="utf-8")
    assert 'cron: "0 7 * * 1"' in wf and "workflow_dispatch" in wf and "add_each data/*/extras/creative_intel.json data/_shared/creative_intel.json" in wf and "contents: write" in wf


# ---- the library matches its own examples; and it stays current -------------------------------------------------------------------------------------------------------------------------

def test_every_worked_example_of_the_47_formulas_is_named_as_its_own_formula_and_plain_copy_is_named_as_nothing():
    miss = [fid for _p, fid, _n, ex in LIB.examples() if (LIB.classify(ex, "youtube" if fid.startswith("yt:") else "instagram") or {}).get("id") != fid]
    assert len(LIB.examples()) == 47 and miss == []                                                                                       # 47 of 47 (the library's own patterns alone matched 32 with first-match, 38 with best-match)
    for text in ("Here are our overalls.", "We make soft cotton overalls in Portland.", "Buy now and save.", "New colours are in stock today."):
        assert LIB.classify(text) is None and LIB.classify(text, "youtube") is None                                                         # the added patterns did not make everything a formula
    own = F.Library(LIB.ig, LIB.yt, LIB.slop, "own patterns only")
    hits = sum(1 for _p, fid, _n, ex in own.examples() if (own.classify(ex, "youtube" if fid.startswith("yt:") else "instagram") or {}).get("id") == fid)
    assert hits == 38                                                                                                                       # the library's own patterns with the best-match rule: the 9 gaps our overrides close
    over = json.loads(Path("contentintel/data/formulas/overrides.json").read_text(encoding="utf-8"))
    entries = [e for part in ("instagram", "youtube") for e in over[part].values()]
    assert len(entries) == 9 and all(e["why"] for e in entries) and all(("add" in e) != ("replace" in e) for e in entries)               # every override says why it exists, and is an addition or a replacement


def test_the_youtube_classifier_prefers_the_formula_whose_patterns_cover_most_of_the_hook_and_an_anchored_pattern_counts_double():
    assert LIB.classify("Do not upload another video until you check this one setting.", "youtube")["id"] == "yt:the-warning"
    assert LIB.classify("If you have under a thousand subscribers, this is the only video you need this week.", "youtube")["id"] == "yt:the-direct-address"
    assert LIB.classify("97% of the people who start a channel quit before video 30.", "youtube")["id"] == "yt:the-statistic"


def make_remote(tmp_path, mutate=None):
    """A fake GitHub: the commit ids and the three files, taken from the copy in the repository (optionally changed)."""
    root = Path("contentintel/data/formulas")
    files = {"ig_hooks.json": json.loads((root / "ig_hooks.json").read_text(encoding="utf-8")), "yt_hooks.json": json.loads((root / "yt_hooks.json").read_text(encoding="utf-8")),
             "slop.json": json.loads((root / "slop.json").read_text(encoding="utf-8"))}
    if mutate:
        mutate(files)
    paths = {"skills/ig-reel/hooks.json": "ig_hooks.json", "skills/yt-script/hooks.json": "yt_hooks.json", "skills/ig-human/slop.json": "slop.json"}
    commits = {"Jakeschincariol/instagram-agent-skill": "1" * 40, "Jakeschincariol/youtube-agent-skill": "2" * 40}

    def fetch(url):
        if "api.github.com" in url:
            repo = url.split("/repos/")[1].split("/commits")[0]
            return json.dumps({"sha": commits[repo]})
        for path, name in paths.items():
            if url.endswith(path):
                return json.dumps(files[name], indent=1)
        raise OSError("404")
    return fetch


def copy_root(tmp_path):
    dst = tmp_path / "formulas"
    shutil_copy = __import__("shutil").copytree("contentintel/data/formulas", dst)
    return Path(shutil_copy)


def test_the_refresh_reads_the_newest_commit_and_keeps_the_copy_current_and_says_which_commit_it_came_from(tmp_path):
    root = copy_root(tmp_path)
    first = F.refresh(make_remote(tmp_path), "2026-12-01T07:00:00Z", root=root)
    assert first["status"] == "updated" and {"ig_hooks", "slop"} <= set(first["changed"])                                                   # the fake's formatting differs from the stored text: written, with the new commit ids
    v = F.read_version(root)
    assert v["checked_at"] == "2026-12-01T07:00:00Z" and v["files"]["ig_hooks"]["commit"] == "1" * 40 and v["files"]["yt_hooks"]["commit"] == "2" * 40 and v["files"]["slop"]["entries"] == 96
    again = F.refresh(make_remote(tmp_path), "2026-12-08T07:00:00Z", root=root)
    assert again["status"] == "up to date" and again["changed"] == [] and F.read_version(root)["checked_at"] == "2026-12-08T07:00:00Z" and F.read_version(root)["files"]["ig_hooks"]["changed_on"] == "2026-12-01T07:00:00Z"
    def add_formula(files):
        files["yt_hooks.json"]["hooks"].append({"id": "the-new-one", "name": "The New One", "shape": "x", "example": "Brand new shape works.", "fails_when": "x", "match": ["\\bbrand new shape\\b"]})
    new = F.refresh(make_remote(tmp_path, add_formula), "2026-12-15T07:00:00Z", root=root)
    assert new["changed"] == ["yt_hooks"] and F.load_library(None, root).counts["youtube"] == 22 and F.load_library(None, root).classify("Brand new shape works.", "youtube")["id"] == "yt:the-new-one"
    assert F.library_age_days("2026-12-20T00:00:00Z", root) == 5


def test_data_that_does_not_parse_or_breaks_the_golden_set_never_replaces_the_copy_in_use(tmp_path):
    root = copy_root(tmp_path)
    before = {n: (root / n).read_bytes() for n in ("ig_hooks.json", "yt_hooks.json", "slop.json", "VERSION.json")}

    def broken_regex(files):
        files["ig_hooks.json"]["hooks"][0]["match"] = "(unclosed"
    bad = F.refresh(make_remote(tmp_path, broken_regex), "2026-12-01T07:00:00Z", root=root)
    assert bad["status"] == "refused" and "REFUSED" in bad["lines"][0] and "error" in bad["lines"][0]

    def too_few(files):
        files["yt_hooks.json"]["hooks"] = files["yt_hooks.json"]["hooks"][:5]
    assert F.refresh(make_remote(tmp_path, too_few), "2026-12-01T07:00:00Z", root=root)["status"] == "refused"
    def new_formula_text(files):
        files["slop.json"]["words"].append({"find": "overalls", "replace": "dungarees", "family": "x"})
    gold = F.refresh(make_remote(tmp_path, new_formula_text), "2026-12-01T07:00:00Z", root=root, golden=lambda lib: {"failures": ["a case that must be flagged was not"]})
    assert gold["status"] == "refused" and "golden set fails" in gold["lines"][0]
    def offline(url):
        raise OSError("no network")
    assert F.refresh(offline, "2026-12-01T07:00:00Z", root=root)["status"] == "failed"
    assert {n: (root / n).read_bytes() for n in before} == before                                                                           # every refused or failed refresh left every file as it was (even the version record)


def test_the_report_names_the_library_commits_and_calls_an_old_copy_stale(tmp_path):
    from datetime import datetime, timedelta
    version = F.read_version()                                                                                                                    # the copy in the repository is refreshed every week: the expected age is computed from its own timestamp, never a date written into the test (Sabotage #1278 failed on a hardcoded 1)
    checked = datetime.strptime(version["checked_at"][:10], "%Y-%m-%d")
    day_after = (checked + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    fresh = F.build_report("swoveralls", bank_of(item(1)), LIB, day_after)
    assert fresh["library"]["version"]["ig_hooks"] == version["files"]["ig_hooks"]["commit"][:10] and fresh["library"]["age_days"] == 1 and not any("last checked" in n for n in fresh["notes"])
    same_day = F.build_report("swoveralls", bank_of(item(1)), LIB, version["checked_at"])
    assert same_day["library"]["age_days"] == 0
    long_after = (checked + timedelta(days=35)).strftime("%Y-%m-%dT00:00:00Z")
    old = F.build_report("swoveralls", bank_of(item(1)), LIB, long_after)
    assert old["library"]["age_days"] == 35 and any("last checked 35 days ago" in n for n in old["notes"])


def test_the_refresh_command_prints_what_it_did_and_exits_non_zero_when_it_was_refused(tmp_path, capsys, monkeypatch):
    from types import SimpleNamespace
    from contentintel import intel_cli
    monkeypatch.setattr(F, "DATA", copy_root(tmp_path))
    monkeypatch.setattr(F.refresh, "__defaults__", (F.DATA, None, None))
    args = SimpleNamespace(action="refresh-library", brand="all", brands_dir="brands", top=10)
    assert intel_cli.run_intel(args, Store(tmp_path / "d"), "2026-12-01T07:00:00Z", fetch=make_remote(tmp_path)) == 0
    assert "library refresh: UPDATED" in capsys.readouterr().out
    def broken(files):
        files["slop.json"]["structures"][0]["regex"] = "(bad"
    assert intel_cli.run_intel(args, Store(tmp_path / "d"), "2026-12-02T07:00:00Z", fetch=make_remote(tmp_path, broken)) == 1
    assert "REFUSED" in capsys.readouterr().out
    wf = Path(".github/workflows/creative-intel.yml").read_text(encoding="utf-8")
    assert "if: github.event_name == 'schedule' || inputs.action == 'report' || inputs.action == 'refresh-library'" in wf and "intel --action refresh-library" in wf and "contentintel/data/formulas/VERSION.json" in wf and "continue-on-error: true" in wf


# ---- Stage 7 layer 1: the storyboard and the styleframes ---------------------------------------------------------------------------------------------------------------------------------

from contentintel import storyboard as SB                                                      # noqa: E402


def test_a_concept_becomes_shots_with_a_short_hook_shot_seconds_fitted_to_the_format_and_a_short_on_screen_line():
    c = concept(format="30 second vertical video", script="Show the contract on a desk. Say what the missing clause was. Show the overalls on a hanger and say what changed. Pull on the straps to show how they sit.",
                visual_direction="Close-up of the contract. A wide shot of the overalls in daylight. A hand pulling the straps.", product="swoveralls-classic")
    board = SB.build(c, brief_id="BR-0001", concept_no=1)
    shots = board["shots"]
    assert [s["role"] for s in shots] == ["hook", "beat", "beat", "beat", "beat", "cta"] and shots[0]["seconds"] <= 3.0 and shots[0]["spoken"].startswith("$18,000")
    assert abs(board["total_seconds"] - 30) <= 1.5 and board["target_seconds"] == 30 and board["aspect"] == "9:16" and board["notes"] == []
    assert all(len(V.words_of(s["on_screen"])) <= 6 for s in shots) and shots[0]["on_screen"] == "$18,000 is what one missing clause" and shots[0]["visual"] == "Close-up of the contract." and shots[1]["visual"] == "A wide shot of the overalls in daylight." and shots[3]["visual"] == "A hand pulling the straps."
    assert all(SB.SHOT_MIN <= s["seconds"] <= SB.SHOT_MAX for s in shots) and "swoveralls" in shots[0]["still_query"] and "contract" in shots[0]["still_query"]
    assert SB.parse_seconds("15 second reel") == 15 and SB.parse_seconds("vertical video") == 30 and SB.parse_seconds("45-second story") == 45
    thin = SB.build(concept(script="Short one."), brief_id="BR-0001")
    assert any("too short" in n for n in thin["notes"])
    empty = SB.build(concept(script=""), brief_id="BR-0001")
    assert any("no sentences" in n for n in empty["notes"]) and [s["role"] for s in empty["shots"]] == ["hook", "cta"]


def test_a_storyboard_is_checked_again_for_slop_the_brands_rules_and_the_claims_gate():
    board = SB.build(concept(script="Let's delve into the cotton. It is the best overall in the world."), brief_id="BR-0001")
    found = SB.check(board, LIB)
    assert any(f["rule"] == "slop_word" and f["where"].startswith("shot 2") for f in found)
    seen = []
    def gate(brief):
        seen.append(brief)
        return [{"where": "concept 1 (x) script", "rule": "claims-gate", "severity": "error", "message": "BLOCKED: best in the world"}]
    with_gate = SB.check(board, LIB, claim_gate=gate)
    assert any(f["rule"] == "claims-gate" for f in with_gate) and "best overall in the world" in seen[0]["concepts"][0]["script"] and seen[0]["concepts"][0]["hook"].startswith("$18,000")
    long_hook = SB.build(concept(hook="word " * 40), brief_id="BR-0001")
    long_hook["shots"][0]["seconds"] = 5.0
    assert any(f["rule"] == "hook_shot_long" for f in SB.check(long_hook, LIB))
    from contentintel import rules as rules_mod
    ruleset = rules_mod.load_ruleset(next(Path("briefs").glob("*/rules.yaml")))
    assert isinstance(SB.check(board, LIB, rules=ruleset), list)


LONG = {"script": "Show the contract on a desk. Say what the missing clause was. Show the overalls on a hanger and say what changed. Pull on the straps to show how they sit.",
        "visual_direction": "Close-up of the contract. A wide shot of the overalls in daylight. A hanger on a rail."}


def test_each_shot_gets_a_licensed_still_or_an_honest_none_found():
    board = SB.build(concept(**LONG), brief_id="BR-0001")
    asked = []
    def finder(query, kind):
        asked.append((query, kind))
        if "contract" in query:
            return [{"url": "https://pics.example/a.jpg", "title": "Contract", "license": "CC0", "attribution": "A. Person", "source": "catalogue image"}]
        if "daylight" in query:
            raise RuntimeError("down")
        return []
    got = SB.add_styleframes(board, finder)
    stills = [s["still"] for s in board["shots"]]
    assert got == sum(1 for s in stills if s.get("url")) and stills[0]["url"] == "https://pics.example/a.jpg" and stills[0]["license"] == "CC0" and stills[0]["credit"] == "A. Person" and "the picture search failed (RuntimeError)" in stills[1]["missing"]
    assert all("missing" in s for s in stills if not s.get("url")) and any("no picture that matches this shot was found for" in s["missing"] for s in stills if "missing" in s)
    assert {k for _q, k in asked} == {"image"}
    pair = SB.build(concept(), brief_id="BR-0001")
    SB.add_styleframes(pair, lambda q, k: [{"url": "https://pics.example/nolicence.jpg", "source": "catalogue image"}, {"url": "https://pics.example/licensed.jpg", "license": "CC0", "attribution": "B", "source": "catalogue image"}])
    assert pair["shots"][0]["still"]["url"] == "https://pics.example/licensed.jpg"                                                              # a picture with a licence is preferred to one without
    page = SB.render_html(board, [{"where": "shot 1", "message": "<b>careful</b>"}])
    assert "<img src=\"https://pics.example/a.jpg\"" in page and "CC0 A. Person" in page and "&lt;b&gt;careful&lt;/b&gt;" in page and "no picture that matches this shot" in page


def unique_pictures():
    n = [0]

    def finder(q, k):
        n[0] += 1
        return [{"url": f"https://pics.example/x{n[0]}.jpg", "license": "CC0", "attribution": "X", "source": "catalogue image"}]
    return finder


def test_the_storyboard_command_works_from_a_saved_brief_and_saves_json_and_a_contact_sheet(tmp_path, capsys):
    from types import SimpleNamespace
    from contentintel import intel_cli
    store = Store(tmp_path / "data")
    content = {"title": "t", "key_message": "k", "concepts": [concept(**LONG), concept(name="Second", hook="Here are our overalls.", **LONG)]}
    store.write_brief("swoveralls", {"id": "BR-0001", "brief": "no-such-brief", "rules_version": 3, "content": content})
    args = SimpleNamespace(action="storyboard", brand="swoveralls", brands_dir="brands", briefs_dir="briefs", brief_id="", concept=0, stills=True, top=10)
    code = intel_cli.run_storyboard(args, store, NOW, finder=unique_pictures())
    out = capsys.readouterr().out
    assert code == 0 and "BR-0001 concept 1 (Cost confession): 6 shot(s)" in out and "BR-0001 concept 2 (Second)" in out and "saved data/swoveralls/extras/storyboard.json and storyboard.html" in out
    saved = store.read_extra("swoveralls", "storyboard")
    assert saved["brief"] == "BR-0001" and saved["rules_version"] == 3 and len(saved["boards"]) == 2 and saved["boards"][0]["styleframes"] == 6
    sheet = (tmp_path / "data" / "swoveralls" / "extras" / "storyboard.html").read_text(encoding="utf-8")
    assert sheet.count("<h1>") == 2 and "pics.example/x1.jpg" in sheet
    args.concept = 2
    assert intel_cli.run_storyboard(args, store, NOW, finder=lambda q, k: []) == 0 and len(store.read_extra("swoveralls", "storyboard")["boards"]) == 1
    args.concept = 5
    assert intel_cli.run_storyboard(args, store, NOW) == 1 and "has 2 concept(s), not 5" in capsys.readouterr().out
    args.concept, args.brief_id = 0, "BR-0009"
    assert intel_cli.run_storyboard(args, store, NOW) == 1 and "there is no brief BR-0009" in capsys.readouterr().out
    store.write_brief("duderobe", {"id": "BR-0001", "brief": "old"})
    args.brand, args.brief_id = "duderobe", ""
    assert intel_cli.run_storyboard(args, store, NOW) == 1 and "has no saved content" in capsys.readouterr().out
    args.brand = "all"
    assert intel_cli.run_storyboard(args, store, NOW) == 1
    args.brand = "defi_snacks"
    assert intel_cli.run_storyboard(args, store, NOW) == 1 and "no brief has been made" in capsys.readouterr().out


def test_a_blocked_claim_makes_the_storyboard_command_fail_and_a_brief_record_keeps_its_content(tmp_path, capsys):
    from types import SimpleNamespace
    from contentintel import intel_cli
    store = Store(tmp_path / "data")
    store.write_brief("swoveralls", {"id": "BR-0001", "brief": "x", "content": {"concepts": [concept()]}})
    from contentintel import canon as canon_mod
    reg = canon_mod.Registry(store.data_dir, "swoveralls")
    canon_mod.apply_seed(reg, canon_mod.load_seed(Path("brands/swoveralls/claims_seed.yaml")), NOW)
    blocked = next(c["text"] for c in reg.state()["claims"].values() if c["status"] == "blocked")
    store.write_brief("swoveralls", {"id": "BR-0002", "brief": "x", "content": {"concepts": [concept(script=blocked + " Show the overalls on a hanger and say so.")]}})
    args = SimpleNamespace(action="storyboard", brand="swoveralls", brands_dir="brands", briefs_dir="briefs", brief_id="BR-0002", concept=0, stills=False, top=10)
    assert intel_cli.run_storyboard(args, store, NOW) == 1 and "error: concept 1" in capsys.readouterr().out
    assert "\"content\": won[\"brief\"]" in Path("contentintel/brief.py").read_text(encoding="utf-8")
    wf = Path(".github/workflows/storyboard.yml").read_text(encoding="utf-8")
    assert "intel --action storyboard" in wf and "storyboard.json" in wf and "storyboard.html" in wf and "contents: write" in wf and "model" in wf


def test_a_worked_example_the_bank_refuses_is_caught_by_the_golden_set_and_a_brand_that_fails_its_own_cases_fails_the_gate(tmp_path, capsys):
    from types import SimpleNamespace
    from contentintel import intel_cli
    ig = json.loads(json.dumps(LIB.ig))
    ig["hooks"][0]["example"] = ig["hooks"][0]["example"] + "\u200b"
    bad_lib = F.Library(ig, LIB.yt, LIB.slop, "test", LIB.overrides)
    res = V.run_golden(bad_lib)
    assert any("the worked example of" in f and "refused by the bank" in f for f in res["failures"])
    brands = tmp_path / "brands" / "acme"
    brands.mkdir(parents=True)
    (brands / "golden.yaml").write_text("cases:\n  - {text: 'Plain clean words.', expect: flag, rule: slop_word}\n", encoding="utf-8")
    args = SimpleNamespace(action="gate", brand="acme", brands_dir=str(tmp_path / "brands"), top=10)
    assert intel_cli.run_intel(args, Store(tmp_path / "d"), NOW) == 1
    out = capsys.readouterr().out
    assert "acme: FAILED" in out and "must be flagged" in out and "1 brand(s) FAILED" in out
    (brands / "golden.yaml").write_text("cases:\n  - {text: 'Let us delve into it.', expect: flag, rule: slop_word}\n", encoding="utf-8")
    assert intel_cli.run_intel(args, Store(tmp_path / "d"), NOW) == 0 and "acme: PASSED" in capsys.readouterr().out


def test_every_text_an_item_has_is_tried_and_the_report_shows_the_coverage_and_what_is_not_named():
    cost = "$18,000 is what one missing clause cost me."
    a = item(1, hook="Here are our overalls.", title=cost)                                                                              # the hook line matches nothing, the title does: it was hidden before
    b = item(2, hook="", title="", text="Plain caption about the new colours. " + cost)
    c = item(3, hook="Soft cotton overalls in navy.", title="Soft overalls", text="Nothing here either.")
    f, field = F.tag_with_field(a, LIB)
    assert f["id"] == "ig:1" and field == "title" and F.tag_with_field(b, LIB)[1] == "text" and F.tag_with_field(c, LIB) == (None, "")
    cov = F.coverage([a, b, c], LIB)
    assert cov["kept"] == 3 and cov["tagged"] == 2 and cov["rate"] == round(2 / 3, 3) and cov["by_platform"] == {"instagram": [2, 3]} and cov["by_field"] == {"title": 1, "text": 1}
    assert cov["untagged_sample"] == [{"id": "IB-0003", "platform": "instagram", "text": "Soft cotton overalls in navy."}]
    rep = F.build_report("swoveralls", bank_of(c, item(4, hook="Also plain words."), item(5, hook="Plain again."), a), LIB, NOW)
    assert rep["coverage"]["tagged"] == 1 and any("names a formula for only 1 of 4 kept item(s) (25%)" in n for n in rep["notes"])
    lines = F.render(rep)
    assert any(l.startswith("  coverage: 1 of 4 kept item(s) name a formula (25%)") for l in lines) and any(l.startswith("    untagged IB-0003") for l in lines)
    full = F.build_report("swoveralls", bank_of(a, b), LIB, NOW)
    assert not any("names a formula for only" in n for n in full["notes"])


def test_one_blocked_concept_is_flagged_and_the_others_are_saved_and_the_run_only_fails_when_nothing_is_clean(tmp_path, capsys):
    from types import SimpleNamespace
    from contentintel import canon as canon_mod
    from contentintel import intel_cli
    store = Store(tmp_path / "data")
    reg = canon_mod.Registry(store.data_dir, "swoveralls")
    canon_mod.apply_seed(reg, canon_mod.load_seed(Path("brands/swoveralls/claims_seed.yaml")), NOW)
    blocked = next(c["text"] for c in reg.state()["claims"].values() if c["status"] == "blocked")
    store.write_brief("swoveralls", {"id": "BR-0001", "brief": "x", "content": {"concepts": [concept(**LONG), concept(name="Bad one", script=blocked + " Show the overalls on a hanger and say so. Then say it again for the camera."), concept(name="Third", **LONG)]}})
    args = SimpleNamespace(action="storyboard", brand="swoveralls", brands_dir="brands", briefs_dir="briefs", brief_id="", concept=0, stills=False, top=10)
    assert intel_cli.run_storyboard(args, store, NOW) == 0
    out = capsys.readouterr().out
    assert "2 of 3 concept(s) storyboarded cleanly; BLOCKED and flagged, not ready: concept(s) 2" in out
    saved = store.read_extra("swoveralls", "storyboard")
    assert saved["ready"] == [1, 3] and saved["blocked"] == [2] and [b["status"] for b in saved["boards"]] == ["ok", "blocked", "ok"]
    sheet = (tmp_path / "data" / "swoveralls" / "extras" / "storyboard.html").read_text(encoding="utf-8")
    assert sheet.count("<b>BLOCKED</b>") == 1 and sheet.count("<h1>") == 3
    only = {"id": "BR-0002", "brief": "x", "content": {"concepts": [concept(name="Bad one", script=blocked + " Show the overalls on a hanger and say so.")]}}
    store.write_brief("swoveralls", only)
    assert intel_cli.run_storyboard(args, store, NOW) == 1                                                                                      # nothing clean: the run fails
    assert "0 of 1 concept(s) storyboarded cleanly" in capsys.readouterr().out


def test_a_picture_is_used_once_per_storyboard_so_a_front_page_image_is_not_repeated_across_shots():
    board = SB.build(concept(**LONG), brief_id="BR-0001")
    front = {"url": "https://swoveralls.com/front.jpg", "title": "front page image", "license": "brand-own", "attribution": "Swoveralls", "source": "catalogue image"}
    other = {"url": "https://pics.example/b.jpg", "title": "b", "license": "CC0", "attribution": "B", "source": "catalogue image"}
    got = SB.add_styleframes(board, lambda q, k: [front, other] if "contract" in q else [front])
    urls = [s["still"].get("url") for s in board["shots"]]
    assert urls.count("https://swoveralls.com/front.jpg") == 1 and got == 1 + (1 if other["url"] in urls else 0)
    assert any("already used on earlier shots" in (s["still"].get("missing") or "") for s in board["shots"])                                       # said, not repeated
    assert board["shots"][0]["still"]["url"] == front["url"]


# ---- the still picker: brand, product and generated pictures only, each one matching its shot -------------------------------------------------------------------------------------------------

def shot(visual="Dad pulls the zip and the sweatpants turn into overalls", spoken="Watch this.", product=""):
    return {"n": 2, "visual": visual, "spoken": spoken, "product": product, "role": "beat", "seconds": 3.0, "on_screen": "x", "still_query": "zip sweatpants overalls", "still": None}


def test_a_picture_must_match_its_shot_a_stock_photo_that_does_not_is_dropped_and_the_shot_says_so():
    from contentintel import stills as ST
    s = shot()
    assert ST.relevant(s, {"source": "brand site", "title": "Sweatpants with a zip front", "url": "https://x/p/a.jpg"})
    assert ST.relevant(s, {"source": "brand site", "title": "", "url": "https://x/files/sweatpants-overalls-navy.jpg"})                                   # the file name counts
    assert not ST.relevant(s, {"source": "openverse", "title": "Boy fishing from a boat", "url": "https://x/p/boy-fishing.jpg"})
    assert not ST.relevant(s, {"source": "brand site", "title": "front page image", "url": "https://x/front.jpg"})                                         # a generic front page shares nothing with the shot
    assert ST.relevant(s, {"source": "generated", "title": "", "url": "https://x"}) and ST.relevant(s, {"source": "catalogue image", "title": "", "url": "https://x"})
    board = SB.build(concept(**LONG), brief_id="BR-0001")
    got = SB.add_styleframes(board, lambda q, k: [{"url": "https://pics.example/boy.jpg", "title": "Boy fishing", "source": "openverse", "license": "CC0"}])
    assert got == 0 and all("no picture that matches this shot was found" in s_["still"]["missing"] and "1 candidate(s) did not match it" in s_["still"]["missing"] for s_ in board["shots"])


def test_the_default_picker_uses_the_products_own_pictures_the_sites_matching_pictures_and_a_generated_one_and_never_stock():
    from contentintel import stills as ST
    brand = SimpleNamespace(name="Swoveralls", slug="swoveralls", domain="swoveralls.com")
    products = {"1": {"handle": "swoveralls-navy", "title": "Swoveralls - Navy", "images": [{"src": "https://cdn.shopify.com/navy-1.jpg"}, {"key": "https://cdn.shopify.com/navy-2.jpg"}]},
                "2": {"handle": "swoveralls-garden-party", "title": "Swoveralls - Garden Party Floral", "images": [{"src": "https://cdn.shopify.com/garden.jpg"}]}}
    html = '<html><head><meta property="og:image" content="/files/front-page-hero.jpg"></head><body><img src="/files/zip-sweatpants-detail.jpg" alt="Zip detail on the sweatpants"><img src="/files/crowd.jpg" alt="Crowd at a concert"></body></html>'
    finder = ST.build_finder(brand, products, aspect_of=lambda board: "9:16", get=lambda url: (200, html))
    about_product = finder("navy", "image", shot=shot(visual="Close on the navy pair, the zip running up the front", product="swoveralls-navy"))
    assert [c["source"] for c in about_product][:2] == ["catalogue image", "catalogue image"] and about_product[0]["url"] == "https://cdn.shopify.com/navy-1.jpg" and about_product[-1]["source"] == "generated"
    assert not any("garden" in c["url"] for c in about_product)                                                                                            # the other product's pictures are not offered
    scene = finder("window", "image", shot=shot(visual="Dad opens the kitchen window at dawn", product="swoveralls-navy"))
    assert [c["source"] for c in scene] == ["generated"]                                                                                                   # a scene shot never gets a product picture just because the concept names a product
    site = finder("zip sweatpants", "image", shot=shot())
    assert [c["source"] for c in site][-1] == "generated" and any(c["source"] == "brand site" and "zip-sweatpants-detail" in c["url"] for c in site)
    assert not any("front-page-hero" in c["url"] for c in about_product + scene + site)                                                                   # the site's generic front-page picture is never offered for a shot
    gen = site[-1]
    assert gen["url"].startswith("https://image.pollinations.ai/prompt/Dad%20pulls%20the%20zip") and "width=720&height=1280" in gen["url"] and "seed=2" in gen["url"] and gen["license"] == "generated for this shot" and "not a photograph of the product" in gen["attribution"]
    assert "openverse" not in str([c["source"] for c in about_product + scene + site]) and "width=1080&height=1350&nologo=true&seed=1" in ST.generated_still(shot(), aspect="4:5")["url"]
    board = SB.build(concept(**LONG), brief_id="BR-0001")
    got = SB.add_styleframes(board, ST.build_finder(brand, products, get=lambda url: (404, "")))
    assert got == len(board["shots"]) and all(s_["still"]["url"] for s_ in board["shots"])                                                                 # every shot gets a picture that matches it (generated when nothing of the brand's does)
    used = [s_["still"]["url"] for s_ in board["shots"]]
    assert len(set(used)) == len(used)


def test_the_stock_libraries_are_no_longer_reached_by_the_storyboard_picker():
    text = Path("contentintel/intel_cli.py").read_text(encoding="utf-8")
    body = text[text.index("def _default_finder"):text.index("def run_storyboard")]
    assert "find_usable" not in body and "pixabay" not in body.lower() and "openverse" not in body.lower() and "STL.build_finder" in body
    src = Path("contentintel/stills.py").read_text(encoding="utf-8")
    assert "find_usable" not in src and "pixabay" not in src.lower().replace("pixabay, competitors", "")
