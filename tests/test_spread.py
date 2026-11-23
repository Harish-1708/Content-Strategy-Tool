"""Rate-limit-aware routing: the strong providers of a job take turns by how much of their own daily allowance they have used, one job cannot use up a provider the others need, and the billing ledger says who did what."""
from contentintel.ai import lanes as L
from contentintel.ai import registry as R
from contentintel.ai import router as RT
from contentintel.ai import usage as U

NOW = "2026-11-19T12:00:00Z"


class Health:
    def is_paused(self, source, route, now):
        return None


def stack(tmp_path):
    reg = R.load_registry("ai_providers.yaml")
    lanes = L.load_lanes("ai_lanes.yaml", reg)
    pool = L.load_pool("ai_lanes.yaml", reg)
    usage = U.UsageLedger(tmp_path / "usage.json")
    clients = {pid: object() for pid, p in reg.providers.items() if p.enabled}
    router = RT.Router(reg, lanes, usage, Health(), clients, None, pool=pool)
    return router, reg, lanes, usage


def providers_in_order(router, lane, now=NOW):
    seen = []
    for r in router._routes(lane, now):
        if r["provider"] not in seen:
            seen.append(r["provider"])
    return seen


def use(usage, reg, pid, n, lane="foundation_json", tokens=100):
    for _ in range(n):
        usage.record(reg.provider(pid), lane, NOW, ok=True, tokens_in=tokens, tokens_out=0)


def test_with_no_use_the_measured_order_is_kept_and_without_a_day_it_is_never_reordered(tmp_path):
    router, reg, lanes, usage = stack(tmp_path)
    lane = lanes["foundation_json"]
    measured = [r["provider"] for r in lane.chain if reg.provider(r["provider"]).enabled]                                       # a switched-off provider holds no slot
    assert providers_in_order(router, lane)[:3] == list(dict.fromkeys(measured))[:3]
    use(usage, reg, "groq", 300)
    assert [r["provider"] for r in router._routes(lane)][:len(lane.chain)] == [r["provider"] for r in lane.chain]               # no day given: the measured order exactly


def test_the_first_provider_gives_way_once_it_has_used_more_of_its_own_allowance_than_the_others_and_everything_after_the_tier_stays(tmp_path):
    router, reg, lanes, usage = stack(tmp_path)
    lane = lanes["foundation_json"]
    base = providers_in_order(router, lane)
    first = base[0]
    cap = reg.provider(first).limits.get("requests_per_day") or 1000
    use(usage, reg, first, int(cap * 0.35))
    spread = providers_in_order(router, lane)
    assert spread[0] != first and first in spread[1:4] and spread[4:] == base[4:] and spread[-1] == "claude" if "claude" in spread else spread[4:] == base[4:]
    lb = U.UsageLedger(tmp_path / "other.json")
    router.usage = lb
    use(lb, reg, first, 1)
    assert providers_in_order(router, lane)[0] == first                                                                          # a tiny difference does not reshuffle: the best still leads while the load is level


def test_the_work_is_spread_over_all_four_strong_providers_as_each_one_fills_up(tmp_path):
    router, reg, lanes, usage = stack(tmp_path)
    lane = lanes["foundation_json"]
    tier = providers_in_order(router, lane)[:4]
    chosen = []
    for _ in range(80):
        pid = providers_in_order(router, lane)[0]
        chosen.append(pid)
        cap = reg.provider(pid).limits.get("requests_per_day") or 1000
        use(usage, reg, pid, max(1, cap // 25))                                                                                  # each call uses about 4% of that provider's own day
    assert set(chosen) == set(tier) and max(chosen.count(p) for p in tier) <= 2 * min(chosen.count(p) for p in tier) + 20          # all four carried real work, none carried everything


def test_a_job_that_uses_up_a_nearly_full_provider_is_held_back_so_the_other_jobs_keep_some_of_it(tmp_path):
    router, reg, lanes, usage = stack(tmp_path)
    pid = "groq"
    cap = reg.provider(pid).limits.get("requests_per_day")
    assert cap
    use(usage, reg, pid, int(cap * 0.5), lane="foundation_json")
    use(usage, reg, pid, int(cap * 0.35), lane="story_review_json")
    assert usage.load(reg.provider(pid), NOW) >= 0.8
    ok, why = usage.lane_share_ok(reg.provider(pid), "foundation_json", NOW, RT.LANE_SHARE)
    other_ok, _ = usage.lane_share_ok(reg.provider(pid), "story_review_json", NOW, RT.LANE_SHARE)
    assert not ok and "near its daily limit" in why and other_ok                                                                  # the big user waits, the small one may continue
    lane = lanes["foundation_json"]
    route = next(r for r in router._routes(lane, NOW) if r["provider"] == pid)
    assert router._eligible(lane, route, "public", NOW) == (False, why)
    assert router._eligible(lanes["story_review_json"], next(r for r in router._routes(lanes["story_review_json"], NOW) if r["provider"] == pid), "public", NOW)[0] is True
    rows = {r["provider"]: r for r in router.explain("foundation_json", "public", NOW)}
    assert "kept for the other jobs" in rows[pid]["reason"]
    quiet = U.UsageLedger(tmp_path / "quiet.json")
    use(quiet, reg, pid, int(cap * 0.5))
    assert quiet.lane_share_ok(reg.provider(pid), "foundation_json", NOW, RT.LANE_SHARE)[0] is True                              # nobody else is using it and it still has room: nothing is withheld
    assert U.UsageLedger(tmp_path / "empty.json").lane_share_ok(reg.provider(pid), "foundation_json", NOW, RT.LANE_SHARE)[0] is True


def test_a_provider_with_a_token_limit_and_one_with_no_stated_limit_are_measured_too():
    class P:
        id = "p"
        limits = {"tokens_per_day": 1000}
    led = U.UsageLedger("/tmp/does-not-exist-usage.json")
    led.data = {"days": {NOW[:10]: {"p": {"requests": 2, "failures": 0, "tokens_in": 400, "tokens_out": 100, "cost_usd": 0.0, "latency_ms": 0, "by_lane": {}}}}}
    assert led.load(P(), NOW) == 0.5

    class Q:
        id = "p"
        limits = {}
    assert led.load(Q(), NOW) == 0.002 and led.load(Q(), "2026-11-20T00:00:00Z") == 0


def test_the_billing_ledger_totals_each_provider_and_job_for_today_and_the_month_and_prints_the_share_of_the_daily_limit(tmp_path):
    router, reg, lanes, usage = stack(tmp_path)
    use(usage, reg, "groq", 3, lane="foundation_json", tokens=200)
    use(usage, reg, "groq", 1, lane="claim_verify_json", tokens=50)
    usage.record(reg.provider("cohere"), "brief_json", "2026-11-05T10:00:00Z", ok=False, tokens_in=10, tokens_out=5)
    rep = usage.billing(NOW)
    assert rep["today"]["groq"]["requests"] == 4 and rep["today"]["groq"]["tokens"] == 650 and rep["today"]["groq"]["lanes"]["foundation_json"]["requests"] == 3 and "cohere" not in rep["today"]
    assert rep["month_totals"]["cohere"]["failures"] == 1 and rep["month_totals"]["groq"]["requests"] == 4 and rep["month_totals"]["groq"]["cost_usd"] == 0.0
    lines = U.billing_lines(rep, reg)
    assert lines[0] == "Billing ledger, today (2026-11-19): 4 request(s), $0.0000" and "groq: 4 request(s) of" in lines[1] and "by job: foundation_json 3, claim_verify_json 1" in lines[1]
    assert lines[2].startswith("Billing ledger, this month (2026-11): 5 request(s)")


def test_every_enabled_provider_leads_some_job_so_all_of_them_carry_real_work(tmp_path):
    router, reg, lanes, usage = stack(tmp_path)
    enabled = {pid for pid, p in reg.providers.items() if p.enabled and pid != "claude"}
    assert len(enabled) == 17
    led = {}
    for name, lane in lanes.items():
        for pid in providers_in_order(router, lane)[:4]:
            if pid in enabled:
                led.setdefault(pid, set()).add(name)
    assert set(led) == enabled, sorted(enabled - set(led))                                                                       # no provider is left out of every job's leading four
    first = {providers_in_order(router, lane)[0] for lane in lanes.values() if providers_in_order(router, lane)}
    assert len(first) >= 8                                                                                                       # and the leaders differ across the jobs
    for name in ("claim_verify_json", "storyboard_json"):
        assert not any(r["provider"] == "mistral" for r in lanes[name].chain)                                                    # a switched-off provider is never in a seeded list
