"""Batch 2 stages 4, 6, 7, 8, 9: one engine, proposals only, every section through the claims gate, a lane and a retest probe for each stage."""
from pathlib import Path

import yaml

from contentintel import canon as C
from contentintel import skills as SK
from contentintel import stages as ST
from contentintel.store import Store

AT = "2026-11-18T09:00:00Z"
QUOTE_A = "Swoveralls won the 2026 Probe Design Award, the awarding body said on 3 March."


def seeded(tmp_path):
    reg = C.Registry(tmp_path / "data", "swoveralls")
    C.apply_seed(reg, C.load_seed(Path("brands/swoveralls/claims_seed.yaml")), AT)
    return reg


def cfg_for(tmp_path, waiting=()):
    root = tmp_path / "skills"
    skills = {}
    for key, stage in ST.STAGES.items():
        for section, path, _task in stage["sections"]:
            d = root / path
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(f"---\nname: {section}\n---\nMethod for {section}.\n", encoding="utf-8")
            skills[path] = {"n": len(skills) + 1, "verdict": "useful", "state": "waiting_for_access" if path in waiting else "ready", "auto_run": True, **({"needs": "GA4 exports"} if path in waiting else {})}
    return {"repo": {"max_skill_chars": 60000}, "skills": skills, "runtime_files": []}, root


class Model:
    def __init__(self, answers):
        self.answers, self.prompts, self.jobs = list(answers), [], []

    def for_job(self, job):
        self.jobs.append(job)
        return self

    def json_task(self, system, user, max_tokens=None):
        self.prompts.append(user)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def store_with(tmp_path):
    store = Store(tmp_path / "data")
    store.append_collected("swoveralls", {"platform": "tiktok", "caption": "Workwear that moves with you, now in navy", "source": "competitor"})
    store.append_collected("swoveralls", {"platform": "instagram", "caption": "Deep pockets, big comfort"})
    store.write_extra("swoveralls", "voice", {"items": []})
    return store


def run(key, tmp_path, answers, **kw):
    cfg, root = cfg_for(tmp_path, waiting=kw.pop("waiting", ()))
    model = Model(answers)
    store = kw.pop("store", None) or store_with(tmp_path)
    rep = ST.run_stage(key, kw.pop("reg", None) or seeded(tmp_path), cfg, root, model, slug="swoveralls", name="Swoveralls", brand_text=kw.pop("brand_text", "Brand name: Swoveralls."), story_text="Story.", store=store,
                       now=AT, verify_connection=False, retry_wait=0, **kw)
    return rep, model, store


GOOD = {"text": "Swoveralls could answer the workwear ads with a soft-comfort angle built on the deep pockets people mention. " * 3, "needs_input": ["named competitors"], "claims_used": []}


def test_a_stage_proposes_each_section_through_the_gate_and_saves_only_proposals(tmp_path):
    rep, model, store = run("competitor_ideas", tmp_path, [GOOD])
    assert [(r["section"], r["result"]) for r in rep] == [("idea_candidates", "proposed")] and model.jobs == ["competitor_ideas"]            # asked the stage's own lane
    assert "Workwear that moves with you" in model.prompts[0] and "DO NOT STATE" in model.prompts[0] and "Method for idea_candidates." in model.prompts[0]
    saved = store.read_extra("swoveralls", "stage_competitor_ideas")
    assert saved["runs"][0]["proposals"]["idea_candidates"]["gate"] == "OK" and "PROPOSALS only" in saved["note"]
    export = (tmp_path / "data" / "swoveralls" / "catalogue" / "stage_competitor_ideas.md").read_text(encoding="utf-8")
    assert "Proposals only" in export and "soft-comfort angle" in export
    rep2, _m, _s = run("competitor_ideas", tmp_path, [GOOD], store=store)
    assert len(store.read_extra("swoveralls", "stage_competitor_ideas")["runs"]) == 2                                                          # a new run is added, nothing replaced


def test_every_stage_has_its_own_lane_probe_and_job_and_the_probe_asks_that_stages_own_task():
    from contentintel import services as SV
    from contentintel.ai import lanes as L, ranking as RK, registry as R
    lanes = L.load_lanes("ai_lanes.yaml", R.load_registry("ai_providers.yaml"))
    probes = ST.probes()
    for key, stage in ST.STAGES.items():
        assert SV.JOB_LANES[stage["job"]] == stage["lane"] and stage["lane"] in lanes and stage["lane"] in RK.JOBS and stage["lane"] in probes, key
        system, user, fn = probes[stage["lane"]][0]
        assert stage["sections"][0][2] in user and fn({"text": "Probe Brand " * 30, "needs_input": ["x"]}) == 1.0 and fn({}) == 0.0
    assert ST.probe_score({"text": "Probe Brand won an award " * 20, "needs_input": []}) < 1.0                                                  # a blocked kind of claim costs points


def test_a_section_that_repeats_a_blocked_claim_is_corrected_once_then_rejected_without_failing_the_stage(tmp_path):
    bad = {"text": "Swoveralls won several awards for its design.", "needs_input": []}
    rep, model, store = run("seo_lens", tmp_path, [bad, bad, GOOD])
    results = {r["section"]: r["result"] for r in rep}
    assert results["keyword_research"] == "rejected" and results["content_gaps"] == "proposed" and "YOUR PREVIOUS ANSWER WAS REFUSED" in model.prompts[1]
    saved = store.read_extra("swoveralls", "stage_seo_lens")["runs"][0]["proposals"]
    assert list(saved) == ["content_gaps"]                                                                                                    # the rejected text is never saved
    assert any(r["section"] == "numbers" and r["result"] == "needs_input" and "Search Console" in r["detail"] for r in rep)                 # the store-number skills stay NEEDS_INPUT


def test_the_verifier_is_asked_before_the_correction_and_a_verified_claim_lets_the_section_through(tmp_path):
    bad = {"text": "Swoveralls won several awards for its design.", "needs_input": []}
    reg_holder = {}
    reg = seeded(tmp_path)

    def verifier(ids):
        reg_holder["ids"] = ids
        reg.record_evidence(ids[0], [{"url": "https://a.com/x", "domain": "a.com", "quote": QUOTE_A, "close": True, "risk": "low", "medium": "page"}], [], True, AT)
        return ids
    rep, model, _s = run("voice_creative", tmp_path, [bad, GOOD], verifier=verifier, reg=reg)
    assert rep[0]["result"] == "proposed" and reg_holder["ids"] and not any("YOUR PREVIOUS ANSWER WAS REFUSED" in p for p in model.prompts)          # verified first: no correction round


def test_a_stage_with_no_material_refuses_politely_a_waiting_skill_is_needs_input_and_a_failing_model_is_reported(tmp_path):
    empty = Store(tmp_path / "empty")
    rep, _m, _s = run("listen_learn", tmp_path, [GOOD], store=empty, brand_text="")
    assert {r["result"] for r in rep} == {"needs_input"} and "no voice yet and no brand facts" in rep[0]["detail"]
    waiting_path = ST.STAGES["creator_chain"]["sections"][0][1]
    rep2, _m2, _s2 = run("creator_chain", tmp_path, [GOOD], waiting=(waiting_path,))
    first = next(r for r in rep2 if r["section"] == "creator_shortlist")
    assert first["result"] in ("needs_input", "refused") and next(r for r in rep2 if r["section"] == "creator_brief")["result"] == "proposed"
    rep3, _m3, _s3 = run("competitor_ideas", tmp_path, [RuntimeError("down"), RuntimeError("down")])
    assert rep3[0]["result"] == "failed" and "attempt 2 of 2" in rep3[0]["detail"]
    cfg, root = cfg_for(tmp_path)
    none = ST.run_stage("competitor_ideas", seeded(tmp_path), cfg, root, None, slug="swoveralls", name="x", brand_text="b", story_text="", store=store_with(tmp_path), now=AT, verify_connection=False)
    assert none[0]["result"] == "refused" and "no AI client" in none[0]["detail"]
    unknown = ST.run_stage("nope", seeded(tmp_path), cfg, root, Model([]), slug="swoveralls", name="x", brand_text="b", story_text="", store=store_with(tmp_path), now=AT, verify_connection=False)
    assert unknown[0]["result"] == "refused" and "not a stage" in unknown[0]["detail"]


def test_the_call_limit_stops_the_stage_and_says_the_next_run_continues(tmp_path):
    rep, model, _s = run("creator_chain", tmp_path, [GOOD, GOOD], max_calls=1)
    assert [r["result"] for r in rep if r["section"] != "numbers"] == ["proposed", "skipped"] and "limit of 1 model call" in rep[1]["detail"] and len(model.prompts) == 1


def test_the_stage_inputs_read_the_real_collected_and_voice_records_and_cap_their_size(tmp_path):
    store = store_with(tmp_path)
    for i in range(400):
        store.append_collected("swoveralls", {"caption": f"post number {i} " + "word " * 80})
    text = ST.gather(store, "swoveralls", ("collected",))["collected"]
    assert len(text) <= ST.INPUT_CAP + 400 and "post number 200" in text and "post number 0 " not in text and text.splitlines()[0].startswith("1. [")          # the newest 200 records, cut at the cap
    assert ST.gather(store, "swoveralls", ("voice",))["voice"] == ""


def test_the_stage_command_runs_a_stage_through_the_clients_saves_and_exits_zero_even_when_a_section_is_rejected(tmp_path, capsys, monkeypatch):
    from contentintel import batch2_cli as B2
    from contentintel import cli
    cfg, root = cfg_for(tmp_path)
    monkeypatch.setattr(SK, "load_config", lambda p: cfg)
    monkeypatch.setattr(SK, "checkout_dir", lambda c, *a, **k: root)
    monkeypatch.setattr(SK, "verify", lambda *a, **k: {"ok": True, "problems": []})
    bad = {"text": "Swoveralls won several awards for its design.", "needs_input": []}
    args = cli.build_parser().parse_args(["canon", "--data-dir", str(tmp_path / "data"), "--brand", "swoveralls", "--skills-config", "x.yaml", "--action", "stage", "--stage", "seo_lens"])
    seeded(tmp_path)
    store = cli.Store(args.data_dir)
    store.append_collected("swoveralls", {"caption": "People ask for soft overalls"})
    code = B2.run_canon(args, store, AT, clients=lambda: (Model([bad, bad, GOOD]), None, None))
    out = capsys.readouterr().out
    assert code == 0 and "seo_lens keyword_research: rejected" in out and "1 section(s) PROPOSED" in out and "1 REJECTED by the gate and recorded" in out
    args2 = cli.build_parser().parse_args(["canon", "--data-dir", str(tmp_path / "data"), "--brand", "swoveralls", "--skills-config", "x.yaml", "--action", "stage"])
    assert B2.run_canon(args2, store, AT, clients=lambda: (None, None, None)) == 2 and "stage needs --stage" in capsys.readouterr().out


def test_the_skills_workflow_has_the_verify_claims_and_stage_actions_and_saves_the_stage_files():
    wf = yaml.safe_load(Path(".github/workflows/skills.yml").read_text(encoding="utf-8"))
    inputs = wf[True]["workflow_dispatch"]["inputs"]
    assert {"verify-claims", "stage"} <= set(inputs["action"]["options"]) and inputs["stage"]["options"] == list(ST.STAGES)
    text = Path(".github/workflows/skills.yml").read_text(encoding="utf-8")
    assert 'verify-claims) python -m contentintel canon --action verify-claims' in text and 'stage) python -m contentintel canon --action stage --stage "$STAGE"' in text
    steps = {s.get("name"): s for s in wf["jobs"]["skills"]["steps"] if s.get("name")}
    for name in ("Set up the free provider pool", "Save the data"):
        assert "verify-claims" in steps[name]["if"] and "inputs.action == 'stage'" in steps[name]["if"]
    assert "data/*/extras/stage_*.json" in steps["Save the data"]["run"] and "data/*/catalogue/stage_*.md" in steps["Save the data"]["run"]
    assert "inputs.action == 'stage'" in steps["Check out the skills repository at the pinned commit"]["if"]


def test_the_brief_is_written_through_its_own_ranked_lane_and_any_other_client_is_left_as_it_is():
    from contentintel import cli
    class Routed:
        def for_job(self, job):
            return ("lane for", job)
    assert cli._job_client(Routed(), "brief") == ("lane for", "brief")
    plain = object()
    assert cli._job_client(plain, "brief") is plain


def test_the_collect_workflow_retries_the_ffmpeg_and_the_browser_downloads_three_times_before_it_fails_the_run():
    text = Path(".github/workflows/collect.yml").read_text(encoding="utf-8")
    assert text.count("for attempt in 1 2 3; do") == 2                                                               # one network blip never fails the whole run
    browser = text[text.index("pip install -r requirements-browser.txt"):]
    assert browser.lstrip().split("\n")[1].strip() == "for attempt in 1 2 3; do"
    assert "ffmpeg or ffprobe is missing after 3 attempts: failing this run" in text
