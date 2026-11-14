"""The router: runs one task through a lane's fixed chain, under the data-class rules, with checks, fallback and recovery.

Order of play for every call:
  1. the lane must be allowed to carry this data class (otherwise refused before anything is sent);
  2. each route in the chain is checked by configuration (enabled, approved for this class, experimental allowed), by credentials
     (a client exists), by health (not resting) and by budget (limits and spending);
  3. the first eligible route is called; its answer must pass the lane's output checks;
  4. a failure or an unusable answer is recorded, the route may rest, and the next route is tried;
  5. if the chain gives nothing and the lane allows it, Claude answers (only where Claude may receive this class);
  6. if nothing answers, the task is deferred (a safe outcome, never a guess);
  7. important output waits for a person.

Each provider is its own source for health: a rate limit on one provider rests that provider, and the next provider is tried.
"""
import hashlib
import json
import re

from .. import contract
from .. import safety
from . import data_class as dc
from . import validate
from .pacing import Pacer

CLAUDE = "claude"
PACED = object()                         # a route that would have to wait for its provider's own limit: passed over, tried again only if nothing else answers
TOO_BIG = re.compile(r"max[_ ]?(output[_ ]?)?tokens|maximum|too (large|long|many)|exceed|context length|out of range", re.I)
GONE = re.compile(r"model_not_found|not found or removed upstream|end of life|was retired|has been retired|no longer available|decommission|has been deprecated|Function '[^']*': Not found for account", re.I)
CREDITS = re.compile(r"out_of_credits|add usage credits|usage credits|insufficient[_ ](credits|funds|balance)|payment required|credit balance", re.I)
CREDITS_REST = 86400                     # a model that answers 'out of credits' is not asked again for a day. ONLY that model: a 402 on one model does not prove the provider's other models are out of credits, and only trying them tells us (owner, 14 Nov 2026)
RESET = re.compile(r"soonest (?:cooldown )?reset ~\s*(\d+)\s*([smh])", re.I)


def reset_seconds(message):
    """Seconds from the gateway's own 'Soonest cooldown reset ~13s' (or ~2m, ~24h), or None. It is how long the gateway holds the provider's key on cooldown after a refusal."""
    m = RESET.search(str(message or ""))
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2).lower()] if m else None



GONE_REST = 86400                        # a withdrawn model is not asked again for a day (and only that model: its provider's other models keep working)
MIN_CAP = 1024                           # the smallest cap tried when a provider rejects a large one


def _stable_bucket(lane, item_ids, text, n):
    """Which of n buckets this output falls in. Stable across runs (Python's hash() is not), so sampling for review is reproducible."""
    digest = hashlib.sha256(json.dumps([lane, list(item_ids), text], ensure_ascii=False).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % n


def build_prompt(instructions, context=()):
    """The instructions, then each collected text fenced as untrusted data, then the reminder that it is data and not instructions."""
    blocks = [safety.wrap_untrusted(text, label) for label, text in context]
    return "\n\n".join([instructions.strip(), *blocks, safety.GUARD] if blocks else [instructions.strip()])


class Router:
    def __init__(self, registry, lanes, usage, health, clients, review, *, coordinator_client=None, clock=None, pool=(), pacer=None):
        self.registry, self.lanes, self.usage, self.health = registry, lanes, usage, health
        self.clients, self.review = clients, review
        self.claude = coordinator_client
        self.pool = list(pool)                                     # the shared pool: every approved model, available to every lane that draws on it
        self.pacer = pacer or Pacer(getattr(registry, "pacing", None))
        self.learned_caps = {}                                     # (provider, model) -> the largest max_tokens it has been seen to accept this run

    # ---- planning (no calls) ------------------------------------------------------------------------------------------------------

    def explain(self, lane_name, data_class, now):
        """For each route of the lane: whether it would be used now, and if not why. Makes no call and records nothing."""
        lane = self.lanes[lane_name]
        rows = []
        own = {(r["provider"], r["model"]) for r in lane.chain}
        for r in self._routes(lane):
            ok, why = self._eligible(lane, r, data_class, now)
            if ok and r["provider"] in self.registry.providers:
                wait = self.pacer.wait_for(self.registry.provider(r["provider"]))
                if wait > self.pacer.fast_wait:
                    why = f"paced: the provider's own limit leaves no free slot for {wait:.0f}s, so another route is tried first"
            source = "lane" if (r["provider"], r["model"]) in own else ("claude" if r["provider"] == CLAUDE else "pool")
            rows.append({"provider": r["provider"], "model": r["model"], "eligible": ok, "reason": why, "source": source})
        return rows

    def _routes(self, lane):
        chain = list(lane.chain)
        if lane.use_pool:
            have = {(r["provider"], r["model"]) for r in chain}
            chain += [r for r in self.pool if (r["provider"], r["model"]) not in have]
        if lane.claude_fallback and not any(r["provider"] == CLAUDE for r in chain):
            chain.append({"provider": CLAUDE, "model": lane.claude_model, "gateway_model": lane.claude_model})
        return chain

    def _eligible(self, lane, route, data_class, now, est_in=2000, ignore_rests=False):
        """ignore_rests: a live CHECK tries every route, even one that an earlier run left resting, because finding out which ones work is its whole purpose."""
        pid = route["provider"]
        ok, why = self.registry.eligibility(pid, data_class, model=route["model"])
        if not ok:
            return False, why
        if pid not in self.clients:
            return False, "no credentials or client available on this machine"
        rest = None if ignore_rests else self.health.is_paused(f"ai:{pid}", "gateway", now)
        if rest:
            return False, rest["message"]
        gone = None if ignore_rests else self.health.is_paused(f"ai:{pid}", "model:" + route["model"], now)
        if gone:
            return False, "this model was reported withdrawn by its provider: " + str(gone["message"])[:160]
        ok, why = self.usage.allows(self.registry.provider(pid), self.registry, now, model=route["model"], est_in=est_in, max_out=lane.max_tokens)
        return (True, "") if ok else (False, why)

    # ---- running --------------------------------------------------------------------------------------------------------------------

    def run(self, lane_name, *, data_class, instructions, context=(), now, system="", item_ids=()):
        lane = self.lanes[lane_name]
        dc.validate(data_class)
        base = {"lane": lane_name, "data_class": data_class, "attempts": [], "skipped": [], "route": None, "output": None, "text": "", "review_id": ""}
        if not dc.within(lane.max_data_class, data_class):
            return {**base, "status": "refused", "reason": f"the {lane_name} lane may carry {lane.max_data_class} data at most; this is {data_class}"}
        prompt = build_prompt(instructions, context)
        paced = []
        for route in self._routes(lane):
            ok, why = self._eligible(lane, route, data_class, now, est_in=max(1, len(prompt) // 3))
            if not ok:
                base["skipped"].append({"provider": route["provider"], "reason": why})
                continue
            result = self._try(lane, route, system, prompt, now, base, patient=False)
            if result is PACED:
                paced.append(route)                                    # its provider's own limit has no slot soon: the next provider is tried first, so load is spread
                continue
            if result is not None:
                return self._finish(lane, route, result, data_class, item_ids, now, base)
        for route in paced:                                            # nothing else answered: now wait for a free slot, as long as the provider's own limit needs
            result = self._try(lane, route, system, prompt, now, base, patient=True)
            if result is PACED:
                base["skipped"].append({"provider": route["provider"], "reason": "paced: no free slot within the longest wait"})
                continue
            if result is not None:
                return self._finish(lane, route, result, data_class, item_ids, now, base)
        return {**base, "status": "deferred", "reason": "no route produced a usable answer; the task is deferred, nothing was guessed"}

    def run_route(self, lane_name, route, *, instructions, context=(), now, system="", ignore_rests=False):
        """One specific route, for evaluation and for the live check. The same eligibility, checks and recording apply (a live check passes ignore_rests so that every route is tried). Public data only."""
        lane = self.lanes[lane_name]
        base = {"lane": lane_name, "data_class": dc.PUBLIC, "attempts": [], "skipped": [], "route": None, "output": None, "text": "", "review_id": ""}
        ok, why = self._eligible(lane, route, dc.PUBLIC, now, ignore_rests=ignore_rests)
        if not ok:
            return {**base, "status": "skipped", "reason": why}
        result = self._try(lane, route, system, build_prompt(instructions, context), now, base, patient=True)
        if result is PACED:
            return {**base, "status": "skipped", "reason": "paced: no free slot at this provider within the longest wait"}
        if result is None:
            return {**base, "status": "failed", "reason": base["attempts"][-1]["message"]}
        return {**base, "status": "ok", "route": {"provider": route["provider"], "model": route["model"]}, "output": result[0], "text": result[1].text,
                "completion": result[1]}

    def _try(self, lane, route, system, prompt, now, base, patient=True):
        pid = route["provider"]
        provider = self.registry.provider(pid)
        client = self.clients[pid]
        source = f"ai:{pid}"
        attempt = {"provider": pid, "model": route["model"]}
        got, _ = self.pacer.acquire(provider, self.pacer.max_wait if patient else self.pacer.fast_wait)
        if not got:
            return PACED
        held = 1                                                       # slots reserved at this provider: a retry with a smaller cap is another request and takes its own
        cap = min(lane.max_tokens, self.learned_caps.get((pid, route["model"]), lane.max_tokens))
        try:
            while True:
                try:
                    comp = client.complete(route, system, prompt, cap, expect_platform=provider.gateway_platform if pid != CLAUDE else None, lane=lane.name)
                    break
                except Exception as exc:       # every failure of every client is classified; none can end the run
                    err = contract.classify_exception(exc)
                    if err["code"] != contract.RATE_LIMITED and cap > MIN_CAP and TOO_BIG.search(str(err["message"])):
                        cap = max(MIN_CAP, cap // 2)                      # the provider refused this much output: ask for less, remember it, and ask again
                        self.learned_caps[(pid, route["model"])] = cap
                        got, _ = self.pacer.acquire(provider, self.pacer.max_wait)
                        if not got:
                            return PACED
                        held += 1
                        continue
                    message = str(err["message"])
                    credits = bool(CREDITS.search(message))
                    wait = err.get("retry_after_seconds") or reset_seconds(message)                  # the provider's Retry-After, or the gateway's own 'Soonest reset ~13s'
                    short = wait if (wait and wait <= self.pacer.max_wait and (err["code"] == contract.RATE_LIMITED or credits)) else None
                    if short:
                        self.pacer.rate_limited(provider, short)                                   # a short wait is waited for (then the provider's next model is tried): the full allowance is used
                    attempt.update({"result": err["code"], "message": err["message"]})
                    base["attempts"].append(attempt)
                    self.usage.record(provider, lane.name, now, ok=False, model=route["model"])
                    if err["code"] in (contract.UPSTREAM_BROKEN, contract.NOT_FOUND) and GONE.search(str(err["message"])):
                        self.health.observe(source, "model:" + route["model"], ok=False, code=err["code"], message=err["message"], now=now, rest_seconds=GONE_REST)      # that model only: the provider is not rested
                    elif credits or (err["code"] == contract.RATE_LIMITED and wait and not short):
                        self.health.observe(source, "model:" + route["model"], ok=False, code=err["code"], message=err["message"], now=now, rest_seconds=CREDITS_REST if credits else int(wait))      # THIS model only: the provider's other models are tried
                    else:
                        self.health.observe(source, "gateway", ok=False, code=err["code"], message=err["message"], now=now, retry_after=short or err.get("retry_after_seconds"))
                    return None
        finally:
            for _ in range(held):
                self.pacer.release(provider)
        try:
            output = validate.check_output(comp.text, lane)
        except validate.OutputProblem as problem:
            attempt.update({"result": "INVALID_OUTPUT", "message": str(problem)})
            base["attempts"].append(attempt)
            self.usage.record(provider, lane.name, now, ok=False, tokens_in=comp.tokens_in, tokens_out=comp.tokens_out, latency_ms=comp.latency_ms, model=route["model"])
            self.health.observe(source, "gateway", ok=False, code=contract.UPSTREAM_BROKEN, message=f"unusable answer: {problem}", now=now)
            return None
        attempt.update({"result": "ok", "tokens_in": comp.tokens_in, "tokens_out": comp.tokens_out, "latency_ms": comp.latency_ms})
        base["attempts"].append(attempt)
        self.usage.record(provider, lane.name, now, ok=True, tokens_in=comp.tokens_in, tokens_out=comp.tokens_out, latency_ms=comp.latency_ms, model=route["model"])
        self.health.observe(source, "gateway", ok=True, items=1, now=now, elapsed_ms=comp.latency_ms)
        return output, comp

    def _finish(self, lane, route, result, data_class, item_ids, now, base):
        output, comp = result
        reasons = []
        if lane.review == "required":
            reasons.append("this lane requires human review")
        if lane.review_sample_every and _stable_bucket(lane.name, item_ids, comp.text, lane.review_sample_every) == 0:
            reasons.append("sampled for review")
        out = {**base, "route": {"provider": route["provider"], "model": route["model"]}, "output": output, "text": comp.text}
        if reasons:
            rid = self.review.add(lane=lane.name, now=now, output=output, route=out["route"], item_ids=item_ids, data_class=data_class, reason="; ".join(reasons))
            return {**out, "status": "needs_review", "review_id": rid, "reason": "; ".join(reasons)}
        return {**out, "status": "ok"}
