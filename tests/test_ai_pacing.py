"""Pacing: every call to a provider is sent at no more than the provider's OWN stated limits, once, in the shared piece (never a limit of ours), and Retry-After is honoured."""
import threading
import time

from contentintel import contract as K
from contentintel.ai import clients as C
from contentintel.ai import lanes as L
from contentintel.ai import pacing as P
from contentintel.ai import registry as R
from contentintel.ai import review as RV
from contentintel.ai import router as RT
from contentintel.ai import usage as U
from contentintel.health import HealthLedger

from contentintel.health import add_seconds
from ai_helpers import NOW, PROVIDERS, LANES, edited, write
from stage1_helpers import registry as sources_registry


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.t += seconds


def provider_with(tmp_path, **limits):
    data = edited(PROVIDERS, lambda d: d["providers"]["alpha"].update(limits=limits))
    p, _ = write(tmp_path, data, LANES)
    return R.load_registry(p).provider("alpha")


def pacer(clock, **cfg):
    return P.Pacer(cfg or None, clock=clock.now, sleep=clock.sleep)


# ---- the pacer itself ------------------------------------------------------------------------------------------------------------

def test_a_provider_with_no_stated_limit_is_never_made_to_wait(tmp_path):
    clock, prov = Clock(), provider_with(tmp_path)
    pc = pacer(clock)
    assert [pc.acquire(prov, 0) for _ in range(200)] == [(True, 0.0)] * 200 and clock.sleeps == [] and pc.waited == {} and pc.wait_for(prov) == 0.0


def test_the_per_minute_limit_is_kept_exactly_the_calls_go_at_once_up_to_the_limit_and_the_next_waits_for_the_oldest_to_leave_the_window(tmp_path):
    clock, prov = Clock(), provider_with(tmp_path, requests_per_minute=3)
    pc = pacer(clock)
    assert [pc.acquire(prov, 0)[0] for _ in range(3)] == [True, True, True] and clock.sleeps == []              # the whole allowance, with no wait: this is not a limit of ours
    assert pc.wait_for(prov) == 60.0 and pc.acquire(prov, 10) == (False, 60.0) and clock.sleeps == []          # too long to wait: the caller moves on; nothing is slept or reserved
    clock.t += 25
    assert pc.wait_for(prov) == 35.0
    assert pc.acquire(prov, 60) == (True, 35.0) and clock.sleeps == [35.0] and pc.waited == {"alpha": 35.0}      # it waits only as long as the provider's own window needs
    assert pc.wait_for(prov) == 0.0                                                                           # the first three left the window together, so all but the new call's slot is free
    assert pc.acquire(prov, 0)[0] and pc.acquire(prov, 0)[0] and pc.wait_for(prov) == 60.0                  # three calls in the last minute again: the limit is kept exactly, never exceeded
    clock.t += 100
    assert pc.wait_for(prov) == 0.0


def test_the_wait_is_for_the_oldest_call_in_the_window_so_a_staggered_allowance_comes_back_one_call_at_a_time(tmp_path):
    clock, prov = Clock(), provider_with(tmp_path, requests_per_minute=3)
    pc = pacer(clock)
    for _ in range(3):                                    # calls at 0s, 10s and 20s
        assert pc.acquire(prov, 0)[0]
        clock.t += 10
    assert clock.t == 1030.0 and pc.wait_for(prov) == 30.0                                             # the first call (0s) leaves at 60s: 30s from now, not 50s (when the newest leaves)
    clock.t += 30
    assert pc.wait_for(prov) == 0.0 and pc.acquire(prov, 0)[0] and pc.wait_for(prov) == 10.0           # one slot is back; then the call from 10s is the next to leave, 10s later
    clock.t += 10
    assert pc.acquire(prov, 0)[0] and pc.wait_for(prov) == 10.0


def test_exactly_the_limit_is_allowed_in_a_minute_and_one_more_is_not(tmp_path):
    clock, prov = Clock(), provider_with(tmp_path, requests_per_minute=5)
    pc = pacer(clock)
    assert [pc.acquire(prov, 0)[0] for _ in range(6)] == [True] * 5 + [False]


def test_the_per_hour_limit_is_kept_too_and_the_two_windows_work_together(tmp_path):
    clock, prov = Clock(), provider_with(tmp_path, requests_per_hour=2, requests_per_minute=5)
    pc = pacer(clock)
    assert pc.acquire(prov, 0)[0] and pc.acquire(prov, 0)[0]
    assert pc.wait_for(prov) == 3600.0 and pc.acquire(prov, 3599) == (False, 3600.0)
    clock.t += 1800
    assert pc.wait_for(prov) == 1800.0 and pc.acquire(prov, 1800) == (True, 1800.0) and clock.sleeps == [1800.0]
    both = provider_with(tmp_path, requests_per_hour=100, requests_per_minute=1)
    pc2 = pacer(Clock())
    assert pc2.acquire(both, 0)[0] and pc2.wait_for(both) == 60.0                                              # the tighter window binds


def test_a_concurrency_limit_allows_that_many_calls_at_once_and_no_more_until_one_is_done(tmp_path):
    clock, prov = Clock(), provider_with(tmp_path, concurrency=1)
    pc = pacer(clock)
    assert pc.acquire(prov, 0) == (True, 0.0)
    assert pc.acquire(prov, 0) == (False, P.POLL)                                                              # Ollama: one request at a time
    pc.release(prov)
    assert pc.acquire(prov, 0) == (True, 0.0)
    two = provider_with(tmp_path, concurrency=2)
    pc2 = pacer(Clock())
    assert [pc2.acquire(two, 0)[0] for _ in range(3)] == [True, True, False]
    pc.release(prov)
    pc.release(prov)
    pc.release(prov)
    assert pc.wait_for(prov) == 0.0                                                                            # releasing more than was taken never goes below zero


def test_a_waiting_call_gets_its_slot_when_the_busy_call_finishes():
    clock = Clock()
    prov = R.Provider(id="slow", label="Slow", enabled=True, experimental=False, paid=False, ceiling="public", override={}, approved_classes=("public",), credential_env=(), gateway_platform="slow",
                      terms={}, limits={"concurrency": 1}, price_per_million={}, models=())
    pc = pacer(clock)
    assert pc.acquire(prov, 0)[0]
    state = {"n": 0}
    real_sleep = clock.sleep

    def sleep_then_free(seconds):
        real_sleep(seconds)
        state["n"] += 1
        if state["n"] == 2:
            pc.release(prov)
    pc.sleep = sleep_then_free
    assert pc.acquire(prov, 5) == (True, 2 * P.POLL) and clock.sleeps == [P.POLL, P.POLL]


def test_retry_after_is_honoured_nothing_goes_to_that_provider_until_then(tmp_path):
    clock, prov = Clock(), provider_with(tmp_path)
    pc = pacer(clock)
    pc.rate_limited(prov, None)
    pc.rate_limited(prov, 0)
    assert pc.wait_for(prov) == 0.0                                                                            # no Retry-After given: nothing is invented
    pc.rate_limited(prov, 45)
    assert pc.wait_for(prov) == 45.0 and pc.acquire(prov, 30) == (False, 45.0)
    pc.rate_limited(prov, 10)
    assert pc.wait_for(prov) == 45.0                                                                           # a shorter Retry-After never shortens a longer one
    clock.t += 45
    assert pc.acquire(prov, 0) == (True, 0.0)


def test_concurrency_is_kept_across_real_threads(tmp_path):
    prov = provider_with(tmp_path, concurrency=2)
    pc = P.Pacer({"fast_wait_seconds": 2, "max_wait_seconds": 30})
    live, peak, lock = {"n": 0}, {"n": 0}, threading.Lock()

    def worker():
        ok, _ = pc.acquire(prov, 30)
        assert ok
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        time.sleep(0.05)
        with lock:
            live["n"] -= 1
        pc.release(prov)
    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak["n"] == 2 and live["n"] == 0


# ---- inside the router -----------------------------------------------------------------------------------------------------------

GOOD = '{"answer": "fine"}'


def router_for(tmp_path, clients, *, limits=None, pacing=None, lanes=None, pool=(), clock=None):
    def tune(d):
        d["providers"]["alpha"]["limits"] = {"requests_per_day": 1000, **(limits or {}).get("alpha", {})}
        d["providers"]["beta"]["limits"] = (limits or {}).get("beta", {})
        if pacing:
            d["pacing"] = pacing
    p, l = write(tmp_path, edited(PROVIDERS, tune), lanes or LANES)
    reg = R.load_registry(p)
    clock = clock or Clock()
    router = RT.Router(reg, L.load_lanes(l, reg), U.UsageLedger(tmp_path / "u.json"), HealthLedger({}, sources_registry(tmp_path)), clients, RV.ReviewQueue(tmp_path / "r.json"),
                       pool=pool, pacer=P.Pacer(reg.pacing, clock=clock.now, sleep=clock.sleep))
    return router, clock


def go(router, lane="pub", data_class="public", now=NOW):
    return router.run(lane, data_class=data_class, instructions="do it", now=now)


BETA_POOL = [{"provider": "beta", "model": "b-1", "gateway_model": "b-1"}]


def test_a_route_whose_provider_has_no_free_slot_is_passed_over_and_the_next_provider_answers_with_no_waiting(tmp_path):
    alpha, beta = C.MockClient([GOOD]), C.MockClient([GOOD])
    router, clock = router_for(tmp_path, {"alpha": alpha, "beta": beta}, limits={"alpha": {"requests_per_minute": 1}}, pool=BETA_POOL)
    first = go(router)
    assert first["status"] == "ok" and first["route"]["provider"] == "alpha"
    second = go(router)
    assert second["status"] == "ok" and second["route"]["provider"] == "beta" and clock.sleeps == [] and len(alpha.calls) == 1 and len(beta.calls) == 1      # load is spread, not queued


def test_when_no_provider_can_answer_sooner_the_call_waits_exactly_as_long_as_the_providers_own_limit_needs(tmp_path):
    alpha = C.MockClient([GOOD])
    router, clock = router_for(tmp_path, {"alpha": alpha}, limits={"alpha": {"requests_per_minute": 1}})
    assert go(router)["route"]["provider"] == "alpha" and clock.sleeps == []
    second = go(router)
    assert second["status"] == "ok" and second["route"]["provider"] == "alpha" and clock.sleeps == [60.0] and len(alpha.calls) == 2
    assert router.pacer.waited == {"alpha": 60.0}


def test_a_call_that_would_wait_longer_than_the_longest_wait_is_deferred_and_nothing_is_sent(tmp_path):
    alpha = C.MockClient([GOOD])
    router, clock = router_for(tmp_path, {"alpha": alpha}, limits={"alpha": {"requests_per_minute": 1}}, pacing={"fast_wait_seconds": 0, "max_wait_seconds": 10})
    go(router)
    res = go(router)
    assert res["status"] == "deferred" and clock.sleeps == [] and len(alpha.calls) == 1
    assert {"provider": "alpha", "reason": "paced: no free slot within the longest wait"} in res["skipped"]


def test_retry_after_from_a_429_is_honoured_by_the_whole_router(tmp_path):
    alpha = C.MockClient([K.Failure(K.RATE_LIMITED, "429 too many requests", retry_after=45), GOOD])
    beta = C.MockClient([GOOD])
    router, clock = router_for(tmp_path, {"alpha": alpha, "beta": beta}, pool=BETA_POOL)
    first = go(router)
    assert first["status"] == "ok" and first["route"]["provider"] == "beta" and first["attempts"][0]["result"] == K.RATE_LIMITED
    assert router.pacer.wait_for(router.registry.provider("alpha")) == 45.0
    second = go(router, now=add_seconds(NOW, 20))
    assert second["route"]["provider"] == "beta" and len(alpha.calls) == 1                                       # alpha is not asked again while its Retry-After runs
    clock.t += 45
    third = go(router, now=add_seconds(NOW, 50))
    assert third["route"]["provider"] == "alpha" and len(alpha.calls) == 2                                       # and it rests for exactly what it said (45s), not for the standard hour after a 429
    assert router.health.is_paused("ai:alpha", "gateway", add_seconds(NOW, 50)) is None


def test_a_slot_is_released_when_a_call_fails_so_the_next_call_is_not_blocked(tmp_path):
    alpha = C.MockClient([K.Failure(K.UPSTREAM_BROKEN, "boom"), GOOD])
    router, clock = router_for(tmp_path, {"alpha": alpha}, limits={"alpha": {"concurrency": 1}})
    assert go(router)["status"] == "deferred"
    assert router.pacer.wait_for(router.registry.provider("alpha")) == 0.0
    assert go(router, now=add_seconds(NOW, 300))["status"] == "ok" and clock.sleeps == []


def test_a_provider_that_refuses_a_large_answer_is_asked_for_less_remembered_and_every_ask_takes_its_own_slot(tmp_path):
    def picky(route, prompt, lane):
        raise K.Failure(K.UPSTREAM_BROKEN, "max_tokens is too large: the maximum is 2048") if sizes[-1] > 2048 else None
    sizes = []
    alpha = C.MockClient([GOOD])
    real = alpha.complete

    def complete(route, system, prompt, max_tokens, **kw):
        sizes.append(max_tokens)
        if max_tokens > 2048:
            raise K.Failure(K.UPSTREAM_BROKEN, "max_tokens is too large: the maximum is 2048")
        return real(route, system, prompt, max_tokens, **kw)
    alpha.complete = complete
    lanes = edited(LANES, lambda d: d["lanes"]["pub"].update(max_tokens=8000))
    router, clock = router_for(tmp_path, {"alpha": alpha}, lanes=lanes, limits={"alpha": {"requests_per_minute": 10}})
    res = go(router)
    assert res["status"] == "ok" and sizes == [8000, 4000, 2000] and router.learned_caps == {("alpha", "a-1"): 2000}
    assert len(router.pacer._sent["alpha"]) == 3 and router.pacer._busy["alpha"] == 0                          # three requests were made, three slots were taken and every one was released
    assert go(router)["status"] == "ok" and sizes[3:] == [2000]                                                  # next time it starts from what it learned
    assert len(alpha.calls) == 2


def test_a_cap_is_not_lowered_for_a_rate_limit_or_an_unrelated_error_and_never_below_the_floor(tmp_path):
    seen = []

    def make(error):
        client = C.MockClient([GOOD])

        def complete(route, system, prompt, max_tokens, **kw):
            seen.append(max_tokens)
            raise error
        client.complete = complete
        return client
    lanes = edited(LANES, lambda d: d["lanes"]["pub"].update(max_tokens=3000, claude_fallback=False))
    for error, expected in ((K.Failure(K.RATE_LIMITED, "429: the maximum requests per minute was exceeded", retry_after=1), [3000]),
                            (K.Failure(K.UPSTREAM_BROKEN, "the model is down"), [3000]),
                            (K.Failure(K.UPSTREAM_BROKEN, "maximum output exceeded"), [3000, 1500, 1024])):
        seen.clear()
        router, _ = router_for(tmp_path, {"alpha": make(error)}, lanes=lanes)
        assert go(router)["status"] == "deferred" and seen == expected, error.message


def test_an_experimental_route_is_not_a_guard_internal_data_goes_through_it_like_any_other_when_the_owner_has_approved_it(tmp_path):
    from ai_helpers import OWNER
    gamma = C.MockClient(["a fine answer"])
    pool = [{"provider": "gamma", "model": "g-1", "gateway_model": "g-1"}]
    lanes = edited(LANES, lambda d: d["lanes"].update(general={"max_data_class": "internal", "chain": [], "output": {"format": "text", "min_chars": 1}, "max_tokens": 50}))
    open_internal = edited(PROVIDERS, lambda d: d["providers"]["gamma"].update(owner_override=OWNER, approved_classes=["public", "internal"]))
    p, l = write(tmp_path, open_internal, lanes)
    reg = R.load_registry(p)
    assert reg.provider("gamma").experimental and reg.provider("gamma").effective_ceiling == "internal"           # the registry accepts an experimental route with an Internal ceiling
    router = RT.Router(reg, L.load_lanes(l, reg), U.UsageLedger(tmp_path / "u.json"), HealthLedger({}, sources_registry(tmp_path)), {"gamma": gamma}, RV.ReviewQueue(tmp_path / "r.json"), pool=pool)
    assert go(router, "general", "public")["route"]["provider"] == "gamma"
    assert go(router, "general", "internal")["route"]["provider"] == "gamma" and len(gamma.calls) == 2           # and Internal data reaches it: nothing in the code stops it
    (tmp_path / "plain").mkdir()
    plain = R.load_registry(write(tmp_path / "plain", PROVIDERS, lanes)[0])
    assert plain.eligibility("gamma", "internal", model="g-1") == (False, "not approved for internal data")      # without the owner's approval it is the approval that says no, not the word experimental


def test_a_check_waits_for_a_free_slot_instead_of_skipping_but_never_past_the_longest_wait(tmp_path):
    alpha = C.MockClient([GOOD])
    router, clock = router_for(tmp_path, {"alpha": alpha}, limits={"alpha": {"requests_per_minute": 1}})
    route = {"provider": "alpha", "model": "a-1", "gateway_model": "a-1"}
    assert router.run_route("pub", route, instructions="x", now=NOW)["status"] == "ok"
    assert router.run_route("pub", route, instructions="x", now=NOW)["status"] == "ok" and clock.sleeps == [60.0]
    tight, clock2 = router_for(tmp_path, {"alpha": C.MockClient([GOOD])}, limits={"alpha": {"requests_per_minute": 1}}, pacing={"fast_wait_seconds": 0, "max_wait_seconds": 10})
    tight.run_route("pub", route, instructions="x", now=NOW)
    res = tight.run_route("pub", route, instructions="x", now=NOW)
    assert res["status"] == "skipped" and res["reason"] == "paced: no free slot at this provider within the longest wait" and clock2.sleeps == []


def test_the_plan_shows_where_each_route_comes_from_and_when_a_provider_is_paced(tmp_path):
    router, clock = router_for(tmp_path, {"alpha": C.MockClient([GOOD]), "beta": C.MockClient([GOOD])}, limits={"alpha": {"requests_per_minute": 1}}, pool=BETA_POOL)
    rows = router.explain("pub", "public", NOW)
    assert [(r["provider"], r["source"]) for r in rows] == [("alpha", "lane"), ("beta", "pool"), ("claude", "claude")] and rows[0]["reason"] == ""
    go(router)
    rows = router.explain("pub", "public", NOW)
    assert rows[0]["eligible"] is True and rows[0]["reason"] == "paced: the provider's own limit leaves no free slot for 60s, so another route is tried first"
    assert go(router)["route"]["provider"] == "beta"


# ---- a provider's own monthly cap ---------------------------------------------------------------------------------------------------

def test_a_providers_own_monthly_call_cap_is_kept_from_the_ledger_and_starts_again_next_month(tmp_path):
    data = edited(PROVIDERS, lambda d: d["providers"]["alpha"].update(limits={"requests_per_month": 3}))
    p, _ = write(tmp_path, data, LANES)
    reg = R.load_registry(p)
    prov = reg.provider("alpha")
    led = U.UsageLedger(tmp_path / "u.json")
    for day in ("2026-11-02T10:00:00Z", "2026-11-09T10:00:00Z"):
        led.record(prov, "pub", day, ok=True)
    assert led.month_requests("alpha", NOW) == 2 and led.allows(prov, reg, "2026-11-13T10:00:00Z") == (True, "")
    led.record(prov, "pub", "2026-11-12T10:00:00Z", ok=False)
    assert led.allows(prov, reg, "2026-11-13T10:00:00Z") == (False, "monthly request limit reached (the provider's own cap)")
    assert led.allows(prov, reg, "2026-12-01T10:00:00Z") == (True, "") and led.month_requests("alpha", "2026-12-01T10:00:00Z") == 0 and led.month_requests("beta", NOW) == 0


def test_a_providers_own_retry_after_sets_how_long_it_rests_and_without_one_the_standard_rest_applies(tmp_path):
    health = HealthLedger({}, sources_registry(tmp_path))
    health.observe("ai:x", "gateway", ok=False, code=K.RATE_LIMITED, message="429", now=NOW, retry_after=45)
    assert health.is_paused("ai:x", "gateway", add_seconds(NOW, 44)) and not health.is_paused("ai:x", "gateway", add_seconds(NOW, 46))
    health.observe("ai:y", "gateway", ok=False, code=K.RATE_LIMITED, message="429", now=NOW)
    assert health.is_paused("ai:y", "gateway", add_seconds(NOW, 3500)) and not health.is_paused("ai:y", "gateway", add_seconds(NOW, 3700))        # no Retry-After given: the standard hour
    for i, odd in enumerate((0, -5, "30", True, None)):                                                         # each on its own provider, so an earlier longer rest cannot hide a wrong one
        health.observe(f"ai:z{i}", "gateway", ok=False, code=K.RATE_LIMITED, message="429", now=NOW, retry_after=odd)
        assert health.is_paused(f"ai:z{i}", "gateway", add_seconds(NOW, 3500)), odd                           # nonsense is ignored, never trusted: the standard hour applies
    health.observe("ai:w", "gateway", ok=False, code=K.UPSTREAM_BROKEN, message="boom", now=NOW, retry_after=5)
    assert health.is_paused("ai:w", "gateway", add_seconds(NOW, 100))                                          # a Retry-After only changes the rest after a rate limit
    health.observe("ai:x", "gateway", ok=False, code=K.RATE_LIMITED, message="429", now=NOW, retry_after=10)
    assert health.is_paused("ai:x", "gateway", add_seconds(NOW, 44))                                           # a shorter rest never shortens a longer one already set
