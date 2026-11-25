"""The two-tier discovery seeds filter (seed_judge.py): priority by the brand's own terms, a pool judge for the rest, within the shared limits."""
from pathlib import Path
from types import SimpleNamespace

from contentintel import research as R
from contentintel import research_tools as RT
from contentintel import seed_judge as SJ
from contentintel.store import Store

NOW = "2026-11-25T08:00:00Z"
TERMS = ["Probe Brand", "soft organic cotton overalls", "loungewear for adults"]


def item(title, **kw):
    return {"title": title, "url": kw.pop("url", ""), **kw}


class Judge:
    """A scripted pool model: says yes to a topic that contains 'cozy', no to the rest; can answer partly, or fail."""
    def __init__(self, partial=False, boom=False):
        self.partial, self.boom, self.calls = partial, boom, []

    def json_task(self, system, user, max_tokens=None):
        self.calls.append((system, user, max_tokens))
        if self.boom:
            raise RuntimeError("the pool is down")
        rows = [(int(line.split(".")[0]), line.split(". ", 1)[1]) for line in user.split("Topics:\n")[1].splitlines()]
        rows = rows[:1] if self.partial else rows
        return {"verdicts": [{"i": i, "could_help": "cozy" in text.lower()} for i, text in rows]}


def go(items, judge, cache=None, calls=None, usage=None, terms=TERMS):
    asked = []
    out = SJ.run("probe", items, terms, RT._lane_ok, lambda: asked.append(1) or judge, {} if cache is None else cache, NOW, calls_left=calls if calls is not None else [SJ.CALLS_PER_RUN], usage=usage)
    return out, asked


def test_an_item_that_shares_a_word_with_the_brands_own_terms_is_priority_and_no_model_is_asked():
    j = Judge()
    out, asked = go([item("Why loungewear is trending"), item("Soft overalls for winter")], j)
    assert [i["title"] for i in out["priority"]] == ["Why loungewear is trending", "Soft overalls for winter"] and out["judged"] == [] and out["calls"] == 0 and asked == [] and j.calls == []


def test_the_rest_is_judged_by_a_pool_model_yes_joins_the_list_no_is_dropped_and_the_brand_is_all_the_judge_is_told():
    j = Judge()
    out, _ = go([item("Cozy blanket sets everyone wants"), item("Central bank raises rates"), item("Why loungewear is trending")], j)
    assert [i["title"] for i in out["priority"]] == ["Why loungewear is trending"] and [i["title"] for i in out["judged"]] == ["Cozy blanket sets everyone wants"] and out["dropped"] == 1 and out["calls"] == 1
    system, user, max_tokens = j.calls[0]
    assert system == SJ.SYSTEM and "Brand: probe. Its own terms" in user and "soft organic cotton overalls" in user and "0. Cozy blanket sets everyone wants" in user and "Why loungewear" not in user and max_tokens == 600
    assert not any(w in SJ.SYSTEM.lower() for w in ("overall", "loungewear", "cotton", "snack", "robe"))                                       # no brand word is written into the judge's instructions


def test_an_answer_that_misses_a_topic_never_counts_as_yes_and_is_not_remembered():
    cache = {}
    out, _ = go([item("Cozy a"), item("Cozy b"), item("Cozy c")], Judge(partial=True), cache)
    assert [i["title"] for i in out["judged"]] == ["Cozy a"] and out["unjudged"] == 2 and out["dropped"] == 0 and set(cache) == {"cozy a"}          # the two it did not answer are asked again next time
    assert SJ.parse({"verdicts": [{"i": 0, "could_help": "yes"}, {"i": 9, "could_help": True}, {"i": 1, "could_help": False}, "junk"]}, 3) == {1: False} and SJ.parse("x", 3) == {} and SJ.parse(None, 3) == {}


def test_the_judge_is_asked_in_small_batches_within_a_run_limit_and_a_month_limit_and_never_twice_for_the_same_topic():
    items = [item(f"Cozy topic number {i}") for i in range(30)]
    j, cache, calls = Judge(), {}, [SJ.CALLS_PER_RUN]
    ledger = R.Ledger({}, "2026-11")
    out, _ = go(items, j, cache, calls, ledger)
    assert out["calls"] == 2 and len(j.calls) == 2 and len(out["judged"]) == 24 and out["unjudged"] == 6 and "run's limit of 2" in out["why"] and calls == [0]          # 12 a call, two calls: 24 judged, 6 left
    assert ledger.month_used(SJ.COUNTER) == 2 and len(cache) == 24
    again, asked = go(items[:24], j, cache, [SJ.CALLS_PER_RUN], ledger)
    assert again["calls"] == 0 and again["cached"] == 24 and len(again["judged"]) == 24 and asked == [] and len(j.calls) == 2                       # remembered: no call at all
    full = R.Ledger({}, "2026-11")
    full.charge(SJ.COUNTER, SJ.MONTH_CALL_CAP)
    capped, asked2 = go([item("Cozy fresh topic")], Judge(), {}, [2], full)
    assert capped["unjudged"] == 1 and "month's limit of 60" in capped["why"] and asked2 == []
    shared = [1]
    first, _ = go([item("Cozy x")], Judge(), {}, shared)
    second, asked3 = go([item("Cozy y")], Judge(), {}, shared)
    assert first["calls"] == 1 and second["calls"] == 0 and "run's limit" in second["why"] and asked3 == []                                                       # the run's count is shared by every topic


def test_a_pool_that_cannot_answer_leaves_the_topics_unjudged_with_no_claude_and_nothing_remembered():
    cache = {}
    out, _ = go([item("Cozy a"), item("Cozy b")], Judge(boom=True), cache)
    assert out["unjudged"] == 2 and out["judged"] == [] and cache == {} and "the pool had no answer (RuntimeError: the pool is down)" in out["why"]
    none, _ = go([item("Cozy a")], None, {})
    assert none["unjudged"] == 1 and "no pool model is available" in none["why"]
    from contentintel import cli
    try:
        cli._NoClaude().json_task("s", "u")
        raise AssertionError("the seed judge's inner client must never answer")
    except RuntimeError as exc:
        assert "never falls back to Claude" in str(exc)


def test_a_remembered_verdict_expires_after_thirty_days_and_the_cache_keeps_the_newest_300(tmp_path):
    store = Store(tmp_path / "data")
    SJ.save_cache(store, "probe", {"old topic": {"yes": True, "at": "2026-10-01"}, "new topic": {"yes": False, "at": "2026-11-20"}, "bad": {"at": "2026-11-20"}})
    loaded = SJ.load_cache(store, "probe", NOW)
    assert set(loaded) == {"new topic"}
    big = {f"t{i}": {"yes": True, "at": f"2026-11-{1 + i % 24:02d}"} for i in range(400)}
    SJ.save_cache(store, "probe", big)
    assert len(store.read_extra("probe", "seed_judgments")["verdicts"]) == 300


def test_the_judge_has_its_own_ranked_lane_job_probe_and_the_cli_routes_it():
    import yaml
    from contentintel import services
    from contentintel.ai import ranking
    lanes = yaml.safe_load(Path("ai_lanes.yaml").read_text(encoding="utf-8"))["lanes"]
    lane = lanes["seed_judge_json"]
    assert len(lane["chain"]) >= 3 and "no fallback to Claude" in lane["description"] and "Discovery seeds, tier 2" in lane["description"] and "seed_judge_json" in ranking.JOBS and services.JOB_LANES["seed_judge"] == "seed_judge_json"
    system, user, score = ranking.probes()["seed_judge_json"][0]
    assert system == SJ.SYSTEM and "Probe Brand" in user
    good = {"verdicts": [{"i": 0, "could_help": True}, {"i": 1, "could_help": False}, {"i": 2, "could_help": False}]}
    assert score(good) == 1.0 and score({"verdicts": [{"i": 0, "could_help": False}, {"i": 1, "could_help": True}, {"i": 2, "could_help": True}]}) == 0.0 and score({}) == 0.0
    from contentintel import cli
    assert cli._seed_judge_client(SimpleNamespace(), None) is None or True
    text = Path("contentintel/cli.py").read_text(encoding="utf-8")
    assert '_routed(_NoClaude(), "autoseeds", args, store)' in text and "judge=lambda: _seed_judge_client(args, store)" in text
