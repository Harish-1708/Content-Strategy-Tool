"""Batch 3: the OpenMontage patterns, forecasting (TimesFM), MoneyPrinterTurbo (off, no publishing), the Concat gate and the storyboard stage."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from contentintel import batch3_cli as B3
from contentintel import cli
from contentintel import concat_gate as CG
from contentintel import forecast as FC
from contentintel import mpt as MPT
from contentintel import openmontage as OM
from contentintel import stages as ST
from contentintel.store import Store

NOW = "2026-11-19T12:00:00Z"


# ---- OpenMontage: scored choice, manifest, gates, stock corpus --------------------------------------------------------------------------------------------
def test_the_scored_choice_weighs_quality_headroom_speed_and_cost_and_never_picks_an_unhealthy_provider():
    a = {"name": "a", "quality": 1.0, "headroom": 0.1, "latency_s": 5, "cost_usd": 0}
    b = {"name": "b", "quality": 0.9, "headroom": 1.0, "latency_s": 5, "cost_usd": 0}
    best, ranking = OM.choose([a, b])
    assert best["name"] == "b" and [r["name"] for r in ranking] == ["b", "a"]                                                   # a bit less strong but far more headroom wins
    assert OM.choose([a, {**b, "healthy": False}])[0]["name"] == "a" and OM.choose([{**a, "healthy": False}])[0] is None
    assert OM.score({"quality": 1, "headroom": 1, "latency_s": 0, "cost_usd": 0}) == 1.0 and OM.score({"quality": 5, "headroom": -3, "latency_s": 999, "cost_usd": 9}) == 0.62
    tie = OM.choose([{"name": "x", "quality": 0.5}, {"name": "y", "quality": 0.5}])[1]
    assert [r["name"] for r in tie] == ["x", "y"]                                                                                # ties keep the measured order


def test_the_manifest_records_each_step_and_survives_a_run_that_dies(tmp_path):
    path = tmp_path / "m" / "manifest.json"
    m = OM.Manifest("run1", path, NOW)
    s1 = m.start("render", "tool", {"a": 1}, provider="p1")
    assert json.loads(path.read_text())["steps"][0]["status"] == "running" and s1["input_hash"] and len(s1["input_hash"]) == 16          # written before the step ends
    m.finish(s1, output="out.mp4", gate=[], cost_usd=0.01234567, ok=True)
    s2 = m.start("check", "gate")
    m.finish(s2, ok=False, note="bad")
    data = m.close(NOW)
    assert data["status"] == "stopped" and data["steps"][0]["cost_usd"] == 0.012346 and data["cost_usd"] == 0.012346 and data["steps"][1]["status"] == "failed" and "t0" not in data["steps"][0]
    assert OM.Manifest("r", None).data["status"] == "running" and OM.Manifest("r2", None).close()["status"] == "complete"


def test_the_quality_gate_checks_each_limit_and_a_check_that_was_not_measured_fails():
    ok, fails = OM.quality_gate({"duration_s": 20, "width": 1080, "height": 1920, "bytes": 2_000_000, "has_audio": True, "text_chars": 40},
                                {"min_duration_s": 5, "max_duration_s": 60, "width": 1080, "height": 1920, "min_bytes": 1000, "max_bytes": 5_000_000, "has_audio": True, "min_text_chars": 10})
    assert ok and fails == []
    ok, fails = OM.quality_gate({"duration_s": 2, "width": 720, "height": 1280, "bytes": 9, "has_audio": False}, {"min_duration_s": 5, "width": 1080, "min_bytes": 100, "has_audio": True, "max_duration_s": 1, "height": 1, "max_bytes": 1, "min_text_chars": 1, "mystery": 1})
    assert not ok and len(fails) == 9 and any("not a known check" in f for f in fails) and any("text_chars was not measured" in f for f in fails)


def test_probe_video_reads_ffprobe_json_and_reports_a_failure():
    info = {"streams": [{"codec_type": "video", "width": 1080, "height": 1920}, {"codec_type": "audio"}], "format": {"duration": "4.5", "size": "1234"}}
    got = OM.probe_video("x.mp4", runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr=""))
    assert got == {"duration_s": 4.5, "width": 1080, "height": 1920, "bytes": 1234, "has_audio": True}
    with pytest.raises(RuntimeError):
        OM.probe_video("x.mp4", runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="no such file"))


def fake_embedders():
    words = ["overalls", "pockets", "bridge", "snack"]
    vec = lambda text: [float(w in str(text).lower()) + 0.01 for w in words]                  # noqa: E731
    return vec, (lambda data: vec(data.decode()))


def test_the_clip_corpus_indexes_what_pictures_show_and_finds_them_by_meaning_and_skips_what_cannot_be_read(tmp_path):
    embed_text, embed_image = fake_embedders()
    corpus = OM.StockCorpus(tmp_path / "c.json")
    assets = [{"url": "https://i/a.jpg", "title": "Navy overalls", "license": "by", "attribution": "x"}, {"url": "https://i/b.jpg", "title": "A bridge", "license": "cc0"}, {"url": "https://i/bad.jpg", "title": "broken"}]
    pictures = {"https://i/a.jpg": b"overalls with pockets", "https://i/b.jpg": b"a bridge over water"}

    def fetch(url):
        return pictures[url]
    done, skipped = OM.index_assets(assets, corpus, embed_image, fetch)
    assert done == 2 and skipped[0][0] == "broken" and "KeyError" in skipped[0][1]
    assert OM.index_assets(assets[:2], corpus, embed_image, fetch)[0] == 0                                                       # already indexed: not done twice
    corpus.save()
    again = OM.StockCorpus(tmp_path / "c.json")
    found = again.search(embed_text("overalls pockets close-up"))
    assert [f["title"] for f in found] == ["Navy overalls"] and found[0]["score"] > 0.9 and "vector" not in found[0]
    assert again.search(embed_text("zebra crossing"), floor=0.9) == [] and OM.cosine([0, 0], [1, 1]) == 0.0
    (tmp_path / "bad.json").write_text("not json")
    assert OM.StockCorpus(tmp_path / "bad.json").items == []
    with pytest.raises(ImportError):
        import sys
        sys.modules["sentence_transformers"] = None
        try:
            OM.clip_embedders()
        finally:
            sys.modules.pop("sentence_transformers", None)


# ---- forecasting ----------------------------------------------------------------------------------------------------------------------------------------
def test_a_short_series_is_not_forecast_and_the_naive_baseline_repeats_the_last_value_or_season():
    assert FC.forecast([1, 2, 3], 4)["method"] == "none" and "not enough history" in FC.forecast([1, 2, 3], 4)["note"]
    series = [float(i % 4) for i in range(16)]
    assert FC.forecast(series, 4)["values"] == [3.0] * 4 and FC.naive(series, 6, season=4) == [0.0, 1.0, 2.0, 3.0, 0.0, 1.0]
    f = FC.forecast(series, 2, model_fn=lambda s, h: [9.0] * h)
    assert f["method"] == "timesfm" and f["values"] == [9.0, 9.0]
    broken = FC.forecast(series, 2, model_fn=lambda s, h: 1 / 0)
    assert broken["method"] == "naive" and "the model failed (ZeroDivisionError)" in broken["note"]


def test_the_backtest_trusts_a_model_only_when_it_beats_the_baseline_on_enough_series_within_the_time_limit():
    trend = [[float(i) * k for i in range(20)] for k in range(1, 8)]
    good = lambda s, h: [s[-1] + (s[-1] - s[-2]) * (i + 1) for i in range(h)]                       # noqa: E731
    bad = lambda s, h: [0.0] * h                                                                     # noqa: E731
    assert FC.backtest(trend, good)["gate"] == "PASS" and FC.backtest(trend, good)["share"] == 1.0
    assert FC.backtest(trend, bad)["gate"] == "FAIL" and FC.backtest(trend[:3], good)["gate"] == "NOT ENOUGH SERIES"
    assert FC.backtest(trend, lambda s, h: 1 / 0)["model_wins"] == 0 and FC.backtest([[1, 2, 3]], good)["tested"] == 0
    ticks = iter(range(0, 10_000, 400))
    assert FC.backtest(trend, good, clock=lambda: next(ticks))["gate"] != "PASS"                  # a backtest that takes too long does not pass


def test_anomalies_momentum_and_timeliness():
    flat = [10.0, 11.0] * 15
    spiky = flat[:25] + [60.0] + flat[26:]
    found = FC.anomalies(spiky)
    assert [a["index"] for a in found][0] == 25 and found[0]["value"] == 60.0 and found[0]["expected"] == 10.0 and FC.anomalies(flat) == []
    assert FC.momentum([1, 1, 1, 1, 5, 5, 5, 5])["label"] == "accelerating" and FC.momentum([5, 5, 5, 5, 1, 1, 1, 1])["label"] == "fading" and FC.momentum([5, 5, 5, 5, 5, 5, 5, 5])["label"] == "steady"
    assert FC.momentum([1, 2, 3])["label"] == "unknown" and FC.momentum([0, 0, 0, 0, 3, 3, 3, 3])["change"] == 1.0
    signals = {"overalls audience voice": FC.momentum([1] * 4 + [5] * 4), "snack ads": FC.momentum([5] * 4 + [1] * 4)}
    ideas = [{"id": "IB-1", "title": "Overalls pocket test", "topic": ""}, {"id": "IB-2", "title": "Snack unboxing", "topic": "snack"}, {"id": "IB-3", "title": "Weather", "topic": "rain"}]
    out = {t["id"]: t for t in FC.timeliness(ideas, signals)}
    assert out["IB-1"]["timeliness"] == "timely" and out["IB-2"]["timeliness"] == "fading" and out["IB-3"]["timeliness"] == "unknown" and out["IB-3"]["signal"] == ""
    assert [t["id"] for t in FC.timeliness(ideas, signals)] == ["IB-1", "IB-3", "IB-2"]               # timely first, fading last


def test_weekly_counts_fill_empty_weeks_and_the_brand_series_come_from_what_was_really_collected(tmp_path):
    assert FC.weekly_counts(["2026-11-02", "2026-11-03", "2026-11-23", "bad"]) == [2, 0, 0, 1] and FC.weekly_counts([]) == []
    store = Store(tmp_path / "data")
    for n in range(14):
        store.append_collected("swoveralls", {"at": f"2026-0{1 + n // 4}-{1 + (n % 4) * 7:02d}T00:00:00Z", "source": "meta_ads", "items": [{}, {}]})
    store.write_extra("swoveralls", "voice", {"items": [{"posted": "2026-02-01"}, {"posted": "2026-02-20"}]})
    store.write_extra("swoveralls", "observatory_series", {"price": [1, 2, 3], "bad": ["x"]})
    series = FC.series_for_brand(store, "swoveralls")
    assert "found by meta_ads" in series and "audience voice" in series and series["observatory price"] == [1.0, 2.0, 3.0] and "observatory bad" not in series
    analysis = FC.analyse_brand(store, "swoveralls", ideas=[{"id": "IB-1", "title": "meta ads test", "topic": ""}])
    assert analysis["method"] == "naive" and "found by meta_ads" in analysis["series"] and analysis["series"]["audience voice"]["forecast"]["method"] == "none"
    lines = FC.format_report("swoveralls", analysis)
    assert lines[0].endswith("no backtest was run, so the model is NOT trusted over the baseline") and any("not enough history" in line for line in lines)
    assert "no series yet" in FC.format_report("x", {"method": "naive", "series": {}, "timeliness": []})[1]


def args_for(tmp_path, *extra):
    return cli.build_parser().parse_args(["batch3", "--data-dir", str(tmp_path / "data"), "--brands-dir", "brands", *extra])


def test_the_forecast_command_uses_the_model_only_after_the_backtest_and_says_when_it_is_missing(tmp_path, capsys):
    store = Store(tmp_path / "data")
    for n in range(24):
        store.append_collected("swoveralls", {"at": f"2026-{1 + n // 4:02d}-{1 + (n % 4) * 7:02d}T00:00:00Z", "source": "tiktok", "items": [{}] * (n + 1)})
    B3.run_forecast(args_for(tmp_path, "--action", "forecast", "--brand", "swoveralls"), store, NOW, model_loader=lambda: (_ for _ in ()).throw(ImportError("pip install timesfm")))
    out = capsys.readouterr().out
    assert "TimesFM is not on this machine (pip install timesfm)" in out and "method naive" in out
    saved = store.read_extra("swoveralls", "forecasts")
    assert saved["method"] == "naive" and saved["at"] == NOW and saved["backtest"] is None
    B3.run_forecast(args_for(tmp_path, "--action", "forecast", "--brand", "swoveralls"), store, NOW, model_loader=lambda: (lambda s, h: [0.0] * h))
    assert "did NOT pass the backtest (NOT ENOUGH SERIES" in capsys.readouterr().out and store.read_extra("swoveralls", "forecasts")["method"] == "naive"
    B3.run_forecast(args_for(tmp_path, "--action", "forecast", "--brand", "swoveralls", "--no-backtest"), store, NOW, model_loader=lambda: (lambda s, h: [7.0] * h))
    assert "WITHOUT having been shown to beat the baseline" in capsys.readouterr().out and store.read_extra("swoveralls", "forecasts")["method"] == "timesfm"


# ---- MoneyPrinterTurbo: off, executable briefs, hook A/B, no publishing -------------------------------------------------------------------------------------
BRIEF = {"concepts": [{"name": "Soft pockets", "script": "Deep pockets change everything. They hold your phone and your keys. Order yours today.", "visual_direction": "close shots of navy overalls pockets holding a phone"},
                      {"name": "Weekend", "script": "Weekend ready in one piece. No more outfits to plan.", "visual_direction": "a morning routine"}, {"name": "No script"}]}


def test_mpt_is_off_until_the_owner_says_so_and_a_host_is_set_and_it_has_no_way_to_publish(tmp_path):
    cfg = MPT.load_config(str(tmp_path / "missing.yaml"))
    assert cfg["enabled"] is False
    with pytest.raises(MPT.MptOff, match="OFF"):
        MPT.ensure_on(cfg, {"MPT_URL": "https://h"})
    on = {**cfg, "enabled": True}
    with pytest.raises(MPT.MptOff, match="MPT_URL"):
        MPT.ensure_on(on, {})
    with pytest.raises(MPT.MptOff):
        MPT.ensure_on(on, {"MPT_URL": "http://insecure"})
    assert MPT.ensure_on(on, {"MPT_URL": "https://mpt.example.run.app/"}) == "https://mpt.example.run.app"
    assert not [n for n in dir(MPT) if any(w in n.lower() for w in ("publish", "upload", "post_to", "tiktok", "instagram"))] and "publish" not in " ".join(MPT.DEFAULTS)
    shipped = MPT.load_config("mpt.yaml")
    assert shipped["enabled"] is False
    (tmp_path / "x.yaml").write_text("enabled: true\npublishing: true\n")
    assert MPT.load_config(str(tmp_path / "x.yaml"))["enabled"] is True and "publishing" not in MPT.load_config(str(tmp_path / "x.yaml"))                 # `publishing: true` is ignored
    (tmp_path / "y.yaml").write_text("enabld: true\n")
    with pytest.raises(ValueError, match="unknown setting"):
        MPT.load_config(str(tmp_path / "y.yaml"))


def test_only_a_ready_brief_with_no_error_becomes_jobs_and_each_concept_with_a_script_is_one_draft():
    cfg = MPT.load_config("mpt.yaml")
    jobs = MPT.jobs_from_brief(BRIEF, cfg, status="ready", violations=[{"severity": "warning", "message": "check this"}], brand="swoveralls")
    assert [label for label, _p in jobs] == ["concept 1", "concept 2"] and jobs[0][1]["video_subject"] == "Soft pockets" and jobs[0][1]["video_aspect"] == "9:16" and "pockets" in jobs[0][1]["video_terms"]
    for status, violations in (("needs review", []), ("ready", [{"severity": "error", "where": "concept 1", "message": "a blocked claim"}])):
        with pytest.raises(MPT.MptError, match="not executable"):
            MPT.jobs_from_brief(BRIEF, cfg, status=status, violations=violations)
    with pytest.raises(MPT.MptError, match="has no script"):
        MPT.params_for({"name": "x"}, cfg)


def test_hook_variants_replace_only_the_opening_line_and_are_labelled_a_b_c():
    cfg = {**MPT.load_config("mpt.yaml"), "max_hooks": 3}
    jobs = MPT.hook_variants(BRIEF["concepts"][0], ["Stop buying overalls without pockets.", "  ", "Pockets are the whole point.", "Fourth hook is cut"], cfg, "swoveralls")
    assert [label[0] for label, _p in jobs] == ["A", "B", "C"] and len(jobs) == 3
    assert jobs[0][1]["video_script"].startswith("Deep pockets change everything.") and jobs[1][1]["video_script"] == "Stop buying overalls without pockets. They hold your phone and your keys. Order yours today."
    assert jobs[2][1]["video_script"].startswith("Pockets are the whole point. They hold")


class Http:
    def __init__(self, states):
        self.states, self.calls = list(states), []

    def __call__(self, method, url, body=None, timeout=60):
        self.calls.append((method, url, body))
        if method == "POST":
            return 200, {"data": {"task_id": f"task{len(self.calls)}0000"}}
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return 200, {"data": {"state": state, "progress": 50 if state != 1 else 100, "videos": ["https://mpt/v.mp4"] if state == 1 else []}}


def test_the_mpt_client_creates_waits_and_reports_a_failure_or_a_timeout():
    ticks = iter(range(0, 10_000, 100))
    client = MPT.Client("https://mpt", http=Http([4, 4, 1]), sleep=lambda s: None, clock=lambda: next(ticks))
    task = client.create({"video_subject": "x"})
    assert client.wait(task, 900, 1)["videos"] == ["https://mpt/v.mp4"]
    with pytest.raises(MPT.MptError, match="did not finish within 5 seconds"):
        MPT.Client("https://mpt", http=Http([4]), sleep=lambda s: None, clock=lambda: next(ticks)).wait("t", 5, 1)
    with pytest.raises(MPT.MptError, match="failed the job"):
        MPT.Client("https://mpt", http=Http([-1]), sleep=lambda s: None).wait("t", 5, 1)
    with pytest.raises(MPT.MptError, match="did not accept the job"):
        MPT.Client("https://mpt", http=lambda *a, **k: (500, {})).create({})


def test_the_mpt_command_is_off_by_default_renders_from_the_latest_ready_brief_and_records_a_manifest(tmp_path, capsys):
    store = Store(tmp_path / "data")
    store.write_extra("swoveralls", "last_brief", {"id": "B-1", "title": "t", "status": "ready", "violations": [], "brief": BRIEF})
    cfgfile = tmp_path / "mpt.yaml"
    cfgfile.write_text("enabled: false\n")
    args = args_for(tmp_path, "--action", "mpt-drafts", "--brand", "swoveralls", "--mpt-config", str(cfgfile))
    assert B3.run_mpt(args, store, NOW, environ={"MPT_URL": "https://mpt"}) == 0 and "MoneyPrinterTurbo is OFF" in capsys.readouterr().out
    B3.run_mpt(args_for(tmp_path, "--action", "mpt-status", "--mpt-config", str(cfgfile)), store, NOW, environ={})
    assert "MoneyPrinterTurbo is OFF; host NOT set; stock footage from pixabay" in capsys.readouterr().out
    cfgfile.write_text("enabled: true\npoll_seconds: 1\n")
    http = Http([1])
    probe = lambda p: {"duration_s": 12, "width": 1080, "height": 1920, "bytes": 900_000, "has_audio": True}       # noqa: E731
    code = B3.run_mpt(args, store, NOW, environ={"MPT_URL": "https://mpt"}, http=http, fetch=lambda u: b"V" * 10, probe=probe, sleep=lambda s: None)
    out = capsys.readouterr().out
    assert code == 0 and out.count("done, passed the quality gate") == 2
    manifest = json.loads((tmp_path / "data" / "swoveralls" / "extras" / "mpt_manifest.json").read_text())
    assert manifest["status"] == "complete" and [s["step"] for s in manifest["steps"]] == ["concept 1", "concept 2"] and all(s["gate"] == [] for s in manifest["steps"])
    drafts = store.read_extra("swoveralls", "mpt_drafts")["drafts"]
    assert len(drafts) == 2 and drafts[0]["measured"]["width"] == 1080 and not any(c[0] == "POST" and "publish" in c[1] for c in http.calls)
    code2 = B3.run_mpt(args, store, NOW, environ={"MPT_URL": "https://mpt"}, http=Http([1]), fetch=lambda u: b"V", probe=lambda p: {"duration_s": 1, "width": 100, "height": 100, "bytes": 1, "has_audio": False}, sleep=lambda s: None)
    assert code2 == 1 and "FAILED the quality gate" in capsys.readouterr().out
    store.write_extra("swoveralls", "last_brief", {"status": "needs review", "violations": [], "brief": BRIEF})
    assert B3.run_mpt(args, store, NOW, environ={"MPT_URL": "https://mpt"}, http=Http([1])) == 1 and "not executable" in capsys.readouterr().out
    hooks = args_for(tmp_path, "--action", "mpt-hooks", "--brand", "swoveralls", "--mpt-config", str(cfgfile), "--hooks", "First hook.|Second hook.", "--concept", "9")
    assert B3.run_mpt(hooks, store, NOW, environ={"MPT_URL": "https://mpt"}, http=Http([1])) == 1 and "the brief has no concept 9" in capsys.readouterr().out


# ---- Concat on GitHub ----------------------------------------------------------------------------------------------------------------------------------------
def ff_runner(fail_render=False, calls=None):
    def run(argv, capture_output=True, text=True, timeout=60):
        if calls is not None:
            calls.append(list(argv))
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(b"x")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[0] == "concat" and "--version" in argv:
            return SimpleNamespace(returncode=0, stdout="concat 0.2.7\n", stderr="")
        if argv[0] == "concat" and "--out" not in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="usage: concat [OPTIONS]")
        if argv[0] == "concat":
            if fail_render:
                return SimpleNamespace(returncode=2, stdout="", stderr="no display")
            Path(argv[argv.index("--out") + 1]).write_bytes(b"render")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise OSError("not installed")
    return run


def test_the_concat_gate_is_not_run_until_a_command_is_set_and_then_needs_two_of_three(tmp_path):
    cfg = CG.load_config("concat.yaml")
    assert cfg["render_command"] == "" and cfg["gate"] == {"width": 1080, "height": 1920, "max_seconds": 300, "runs": 3, "passes_needed": 2, "has_audio": True}
    res = CG.run_gate(cfg, tmp_path / "w1", runner=ff_runner())
    assert res["verdict"] == "NOT RUN" and "set it from the discover step" in res["reason"]
    cfg = {**cfg, "render_command": "concat render {inputs} --out {output}"}
    good = {"duration_s": 6, "width": 1080, "height": 1920, "bytes": 5000, "has_audio": True}
    calls = []
    res = CG.run_gate(cfg, tmp_path / "w2", runner=ff_runner(calls=calls), probe=lambda p: good)
    assert res["verdict"] == "GO" and len(res["runs"]) == 3 and all(r["passed"] for r in res["runs"])
    render = next(c for c in calls if c[0] == "concat")
    assert render[:2] == ["concat", "render"] and len([a for a in render if a.endswith(".mp4")]) == 4 and render[-2] == "--out"            # the three clips, then the output
    seq = iter([good, {**good, "width": 720}, good])
    assert CG.run_gate(cfg, tmp_path / "w3", runner=ff_runner(), probe=lambda p: next(seq))["verdict"] == "GO"                       # 2 of 3 is enough
    seq2 = iter([{**good, "width": 720}, {**good, "has_audio": False}, good])
    assert CG.run_gate(cfg, tmp_path / "w4", runner=ff_runner(), probe=lambda p: next(seq2))["verdict"] == "NO-GO"
    failed = CG.run_gate(cfg, tmp_path / "w5", runner=ff_runner(fail_render=True), probe=lambda p: good)
    assert failed["verdict"] == "NO-GO" and "the render exited 2: no display" in failed["runs"][0]["failures"][0]
    slow = iter(range(0, 100_000, 400))
    assert CG.run_gate(cfg, tmp_path / "w6", runner=ff_runner(), probe=lambda p: good, clock=lambda: next(slow))["verdict"] == "NO-GO"      # over the 300 s limit
    broken = lambda argv, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom")                                                      # noqa: E731
    assert CG.run_gate(cfg, tmp_path / "w7", runner=broken)["reason"].startswith("ffmpeg could not make the test clips")


def test_discover_prints_what_the_installed_concat_says_and_survives_a_missing_binary(tmp_path, capsys):
    cfg = CG.load_config("concat.yaml")
    found = CG.discover(cfg, ff_runner())
    assert found[0] == ("concat --version", 0, ["concat 0.2.7"]) and found[1][1] == 1
    args = args_for(tmp_path, "--action", "concat-discover", "--force-dropped")
    assert B3.run_concat(args, Store(tmp_path / "data"), NOW, runner=ff_runner()) == 0
    out = capsys.readouterr().out
    assert "concat: $ concat --version  (exit 0)" in out and "concat:   concat 0.2.7" in out and "gate NOT RUN" in out
    assert B3.run_concat(args_for(tmp_path, "--action", "concat-gate", "--force-dropped"), Store(tmp_path / "data"), NOW, runner=ff_runner()) == 0
    assert json.loads((tmp_path / "data" / "_shared" / "concat_gate.json").read_text())["verdict"] == "NOT RUN"
    bad = tmp_path / "bad.yaml"
    bad.write_text("gate: {width: 1080, height: 1920, max_seconds: 300, runs: 2, passes_needed: 3}\n")
    with pytest.raises(ValueError, match="passes_needed"):
        CG.load_config(str(bad))
    bad.write_text("gate: {width: 0}\n")
    with pytest.raises(ValueError, match="gate.width"):
        CG.load_config(str(bad))


# ---- the storyboard stage (ArcReel pattern) -----------------------------------------------------------------------------------------------------------------
def test_the_storyboard_stage_reads_only_a_ready_brief_and_has_its_own_lane(tmp_path):
    store = Store(tmp_path / "data")
    assert ST.gather(store, "swoveralls", ("brief",))["brief"] == ""
    store.write_extra("swoveralls", "last_brief", {"status": "needs review", "brief": BRIEF})
    assert ST.gather(store, "swoveralls", ("brief",))["brief"] == ""                                                                  # a brief that needs review is never storyboarded
    store.write_extra("swoveralls", "last_brief", {"status": "ready", "brief": BRIEF})
    text = ST.gather(store, "swoveralls", ("brief",))["brief"]
    assert text.startswith("1. Soft pockets: Deep pockets change everything.") and "visual: close shots of navy overalls pockets" in text
    stage = ST.STAGES["storyboard"]
    assert stage["lane"] == "storyboard_json" and stage["job"] == "storyboard" and ST.MIN_ITEMS["shot_list"] == 6 and "storyboard_json" in ST.LANES


def test_the_latest_brief_is_kept_for_the_storyboard_and_the_draft_video_step():
    text = Path("contentintel/brief.py").read_text(encoding="utf-8")
    assert 'store.write_extra(brand.slug, "last_brief"' in text and '"status": record["status"]' in text


def test_the_private_host_gets_the_identity_token_and_the_deploy_workflow_is_off_and_private():
    seen = {}

    class Resp:
        status = 200

        def read(self):
            return b'{"data": {}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    import urllib.request as ur
    original = ur.urlopen
    ur.urlopen = lambda req, timeout=0: seen.update(headers=dict(req.header_items())) or Resp()
    try:
        MPT.default_http("GET", "https://mpt/api", None, 5, token="abc")
        assert seen["headers"]["Authorization"] == "Bearer abc"
        MPT.default_http("GET", "https://mpt/api", None, 5)
        assert "Authorization" not in seen["headers"]
    finally:
        ur.urlopen = original
    text = Path(".github/workflows/mpt-deploy.yml").read_text(encoding="utf-8")
    assert "if: inputs.host_ready == 'yes'" in text and "--no-allow-unauthenticated" in text and "--allow-unauthenticated" not in text.replace("--no-allow-unauthenticated", "")
    assert 'default: "no"' in text


def test_the_concat_workflow_is_a_notice_only_because_concat_is_dropped_and_the_batch3_workflow_saves_only_its_own_files():
    concat = Path(".github/workflows/concat-gate.yml").read_text(encoding="utf-8")
    assert "Concat is DROPPED" in concat and "xvfb" not in concat and "gh release download" not in concat and "apt-get" not in concat and "git push" not in concat
    b3 = Path(".github/workflows/batch3-tools.yml").read_text(encoding="utf-8")
    assert 'pip install "timesfm[torch]>=3.0"' in b3 and "sentence-transformers" in b3 and "data/*/extras/forecasts.json data/*/extras/stock_corpus.json" in b3
    stage = Path(".github/workflows/stage-tools.yml").read_text(encoding="utf-8")
    assert "lightpanda-x86_64-linux" in stage and "obscura-x86_64-linux.tar.gz" in stage and "microsoft/VibeASR.cpp" in stage and "vibeasr-lm-i2_s-embed-q6_k.gguf" in stage and "asr_infer" in stage and "npm install -g agent-browser" in stage
    import yaml
    bake = yaml.safe_load(Path("bakeoff.yaml").read_text(encoding="utf-8"))["engines"]
    assert bake["lightpanda"]["command"] == "lightpanda fetch --dump html --log-level warn {url}" and bake["obscura"]["command"] == "obscura fetch {url} --dump html" and "stealth" not in bake["obscura"]["command"]


def test_timesfm_is_version_3_with_no_fallback_to_an_older_one():
    import sys
    text = Path("contentintel/forecast.py").read_text(encoding="utf-8")
    assert FC.TIMESFM_CHECKPOINT == "google/timesfm-3.0-pytorch" and "from timesfm3 import TimesFM3Forecaster" in text and "TimesFM3Forecaster.from_pretrained(TIMESFM_CHECKPOINT" in text and "TimesFM_3_0" not in text and "timesfm-2" not in text
    sys.modules["timesfm3"] = None
    try:
        with pytest.raises(ImportError, match="no fallback to an older version"):
            FC.load_timesfm()
    finally:
        sys.modules.pop("timesfm3", None)


def test_the_scored_ranking_has_seven_named_dimensions_that_add_up_to_one_and_each_one_moves_the_score():
    assert OM.DIMENSIONS == ("quality", "headroom", "reliability", "speed", "cost", "capability", "terms") and set(OM.WEIGHTS) == set(OM.DIMENSIONS) and abs(sum(OM.WEIGHTS.values()) - 1.0) < 1e-9
    base = {"name": "p", "quality": 0.5, "headroom": 0.5, "reliability": 0.5, "latency_s": 30, "cost_usd": 0.05, "capability": 0.5, "terms": 0.5}
    start = OM.score(base)
    better = {"quality": 1.0, "headroom": 1.0, "reliability": 1.0, "latency_s": 0, "cost_usd": 0, "capability": 1.0, "terms": 1.0}
    for key, value in better.items():
        assert OM.score({**base, key: value}) > start, key                                                                         # every dimension counts
    assert OM.parts(base)["speed"] == 0.5 and OM.parts(base)["cost"] == 0.5 and OM.parts({"name": "x"})["reliability"] == 1.0 and OM.choose([base])[1][0]["parts"]["terms"] == 0.5


def test_the_pricing_catalogue_lists_every_approved_model_with_its_kind_price_limits_and_card_status(tmp_path):
    from contentintel.ai import pricing as PR
    from contentintel.ai import registry as R
    reg = R.load_registry("ai_providers.yaml")
    rows = PR.catalogue(reg)
    free = {(r["provider"], r["model"]) for r in rows if r["kind"] == "free" and r["enabled"]}
    assert ("groq", "openai/gpt-oss-120b") in free and all(r["input_per_m"] == 0 and r["output_per_m"] == 0 for r in rows if r["kind"] == "free")
    claude = [r for r in rows if r["provider"] == "claude"]
    assert claude and all(r["kind"] == "paid" and r["input_per_m"] > 0 for r in claude)
    nv = next(r for r in rows if r["provider"] == "nvidia")
    assert nv["limits"] == {"requests_per_minute": 40} and "no card" in nv["card"]
    hf = [r for r in rows if r["provider"] == "huggingface"]
    assert hf and not any(r["enabled"] for r in hf) and all("card required" in r["card"] for r in hf) and hf[0]["model"] == "(no model approved)"        # Hugging Face is switched OFF, not just recorded
    text = "\n".join(PR.lines(reg))
    assert text.startswith("Pricing catalogue: ") and "paid: claude /" in text and "huggingface" not in text


def test_reliability_and_speed_come_from_todays_ledger_and_a_failing_provider_gives_way_in_the_ranking(tmp_path):
    from contentintel.ai import usage as U
    led = U.UsageLedger(tmp_path / "u.json")
    led.data = {"days": {NOW[:10]: {"p": {"requests": 10, "failures": 4, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_ms": 20000, "by_lane": {}}, "q": {"requests": 2, "failures": 2, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "latency_ms": 0, "by_lane": {}}}}}
    assert led.reliability("p", NOW) == 0.6 and led.reliability("q", NOW) == 1.0 and led.reliability("none", NOW) == 1.0 and led.avg_latency_s("p", NOW) == 2.0 and led.avg_latency_s("none", NOW) == 0.0


HELP = ["Concat 0.2.7", "", "Usage: concat [COMMAND]", "", "Commands:", "  render   Render a project or clips to a video file", "  mcp      Start the MCP server", "  version  Print the version", "", "Options:", "  -h, --help"]
SUB_HELP = ["Usage: concat render [OPTIONS] <INPUTS>...", "  -o, --output <FILE>   where to write the video"]


def test_the_render_command_is_inferred_from_concats_own_help_and_the_gate_runs_in_the_same_run(tmp_path, capsys):
    discovered = [("concat --version", 0, ["concat 0.2.7"]), ("concat --help", 0, HELP), ("concat render --help", 0, SUB_HELP), ("concat export --help", 1, ["unknown command"])]
    assert CG._subcommands(HELP) == ["render", "mcp", "version"]
    cands = CG.candidates(discovered)
    assert cands[0] == "concat render {inputs} --output {output}" or "concat render {inputs} -o {output}" in cands
    assert all(c.startswith("concat ") and "{inputs}" in c and "{output}" in c for c in cands) and len(cands) <= 8
    cfg = CG.load_config("concat.yaml")
    good = {"duration_s": 6, "width": 1080, "height": 1920, "bytes": 5000, "has_audio": True}
    state = {"calls": []}

    def runner(argv, capture_output=True, text=True, timeout=60):
        state["calls"].append(list(argv))
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(b"x")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["concat", "--help"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(HELP), stderr="")
        if argv[:3] == ["concat", "render", "--help"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(SUB_HELP), stderr="")
        if argv[0] == "concat" and "-o" in argv:                                                                                        # only the -o form works in this fake
            Path(argv[argv.index("-o") + 1]).write_bytes(b"render")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[0] == "concat" and "--output" in argv:
            return SimpleNamespace(returncode=2, stdout="", stderr="unexpected argument")
        return SimpleNamespace(returncode=1, stdout="", stderr="no")
    result, found, tried = CG.run_auto(cfg, tmp_path / "w", runner=runner, probe=lambda p: good)
    assert result["verdict"] == "GO" and "inferred from Concat's help: concat render {inputs} -o {output}" in result["reason"] and len(found) == 4 and tried[-1] == "concat render {inputs} -o {output}"
    assert any(c[:2] == ["concat", "render"] and "-o" in c for c in state["calls"]) and tried[0] != tried[-1]                           # the first guess failed, the second ran
    def never(argv, **k):
        if argv[0] == "ffmpeg":
            Path(argv[-1]).write_bytes(b"x")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="nope")
    nope, _f, tried2 = CG.run_auto(cfg, tmp_path / "w2", runner=never, probe=lambda p: good)
    assert nope["verdict"] == "NO-GO" and "none of the" in nope["reason"] and tried2
    manual = {**cfg, "render_command": "concat render {inputs} -o {output}"}
    res3, _f3, tried3 = CG.run_auto(manual, tmp_path / "w3", runner=runner, probe=lambda p: good)
    assert res3["verdict"] == "GO" and tried3 == ["concat render {inputs} -o {output}"]                                                  # a manual command is used as it is
    args = args_for(tmp_path, "--action", "concat-auto", "--force-dropped")
    assert B3.run_concat(args, Store(tmp_path / "data"), NOW, runner=runner, probe=lambda p: good) == 0
    out = capsys.readouterr().out
    assert "gate GO" in out and "render command(s) tried" in out and json.loads((tmp_path / "data" / "_shared" / "concat_gate.json").read_text())["verdict"] == "GO"
    assert "DROPPED" in Path(".github/workflows/concat-gate.yml").read_text(encoding="utf-8")


def test_stock_footage_comes_from_pixabay_while_pexels_is_paused_and_from_pexels_when_a_key_exists_and_reaches_every_job():
    cfg = MPT.load_config("mpt.yaml")
    assert cfg["stock_source"] == "auto" and MPT.stock_source(cfg, {}) == "pixabay" and MPT.stock_source(cfg, {"PIXABAY_API_KEY": "k"}) == "pixabay" and MPT.stock_source(cfg, {"PEXELS_API_KEY": "k"}) == "pexels"
    assert MPT.stock_source({**cfg, "stock_source": "pexels"}, {}) == "pexels" and MPT.stock_source({**cfg, "stock_source": "pixabay"}, {"PEXELS_API_KEY": "k"}) == "pixabay"
    jobs = MPT.jobs_from_brief(BRIEF, cfg, status="ready", brand="x", source="pixabay")
    assert all(p["video_source"] == "pixabay" for _l, p in jobs)
    hooks = MPT.hook_variants(BRIEF["concepts"][0], ["Another hook."], cfg, "x", source="pexels")
    assert all(p["video_source"] == "pexels" for _l, p in hooks) and MPT.params_for(BRIEF["concepts"][0], cfg)["video_source"] == "pixabay"          # Pixabay is the default


def test_the_loader_uses_the_published_3_0_entry_point_and_names_the_installed_version_when_the_import_fails(monkeypatch):
    import sys
    import types
    calls = {}

    class Out:
        forecast = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    class Forecaster:
        @classmethod
        def from_pretrained(cls, name, device=None, **kw):
            calls["load"] = (name, device)
            return cls()

        def predict(self, context, horizon):
            calls["predict"] = (len(context), horizon)
            return Out()
    fake = types.ModuleType("timesfm3")
    fake.TimesFM3Forecaster = Forecaster
    monkeypatch.setitem(sys.modules, "timesfm3", fake)
    fake_numpy = types.ModuleType("numpy")                                                                                     # the Tests runner has no numpy (Tests #1267 and Sabotage #1268 failed on it): the test supplies its own
    fake_numpy.float32 = float
    fake_numpy.asarray = lambda values, dtype=None: list(values)
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    monkeypatch.setitem(sys.modules, "numpy", None)                                                                            # numpy genuinely unimportable while the model loads
    run = FC.load_timesfm()                                                                                                    # loading needs neither numpy nor torch
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)
    assert run([1, 2, 3, 4, 5, 6, 7, 8], 4) == [1.0, 2.0, 3.0, 4.0] and calls == {"load": ("google/timesfm-3.0-pytorch", "cpu"), "predict": (8, 4)}
    monkeypatch.delitem(sys.modules, "numpy")                                                                                  # and a model that is loaded can be asked for a forecast only where numpy exists
    assert "import numpy as np" in Path("contentintel/forecast.py").read_text(encoding="utf-8").split("def run(series, horizon):")[1].split("return first_forecast")[0]
    monkeypatch.setitem(sys.modules, "timesfm3", None)
    monkeypatch.setattr(FC, "installed_version", lambda: "1.3.0")
    with pytest.raises(ImportError, match=r"installed timesfm: 1\.3\.0.*timesfm\[torch\]>=3\.0"):
        FC.load_timesfm()
    wf = Path(".github/workflows/batch3-tools.yml").read_text(encoding="utf-8")
    assert 'pip install "timesfm[torch]>=3.0"' in wf and "pip show timesfm" in wf
