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

from .. import contract
from .. import safety
from . import data_class as dc
from . import validate

CLAUDE = "claude"


def _stable_bucket(lane, item_ids, text, n):
    """Which of n buckets this output falls in. Stable across runs (Python's hash() is not), so sampling for review is reproducible."""
    digest = hashlib.sha256(json.dumps([lane, list(item_ids), text], ensure_ascii=False).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % n


def build_prompt(instructions, context=()):
    """The instructions, then each collected text fenced as untrusted data, then the reminder that it is data and not instructions."""
    blocks = [safety.wrap_untrusted(text, label) for label, text in context]
    return "\n\n".join([instructions.strip(), *blocks, safety.GUARD] if blocks else [instructions.strip()])


class Router:
    def __init__(self, registry, lanes, usage, health, clients, review, *, coordinator_client=None, clock=None):
        self.registry, self.lanes, self.usage, self.health = registry, lanes, usage, health
        self.clients, self.review = clients, review
        self.claude = coordinator_client

    # ---- planning (no calls) ------------------------------------------------------------------------------------------------------

    def explain(self, lane_name, data_class, now):
        """For each route of the lane: whether it would be used now, and if not why. Makes no call and records nothing."""
        lane = self.lanes[lane_name]
        rows = []
        for r in self._routes(lane):
            ok, why = self._eligible(lane, r, data_class, now)
            rows.append({"provider": r["provider"], "model": r["model"], "eligible": ok, "reason": why})
        return rows

    def _routes(self, lane):
        chain = list(lane.chain)
        if lane.claude_fallback and not any(r["provider"] == CLAUDE for r in chain):
            chain.append({"provider": CLAUDE, "model": lane.claude_model, "gateway_model": lane.claude_model})
        return chain

    def _eligible(self, lane, route, data_class, now, est_in=2000):
        pid = route["provider"]
        ok, why = self.registry.eligibility(pid, data_class, experimental_ok=lane.allow_experimental, model=route["model"])
        if not ok:
            return False, why
        if pid not in self.clients:
            return False, "no credentials or client available on this machine"
        rest = self.health.is_paused(f"ai:{pid}", "gateway", now)
        if rest:
            return False, rest["message"]
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
        for route in self._routes(lane):
            ok, why = self._eligible(lane, route, data_class, now, est_in=max(1, len(prompt) // 3))
            if not ok:
                base["skipped"].append({"provider": route["provider"], "reason": why})
                continue
            result = self._try(lane, route, system, prompt, now, base)
            if result is not None:
                return self._finish(lane, route, result, data_class, item_ids, now, base)
        return {**base, "status": "deferred", "reason": "no route produced a usable answer; the task is deferred, nothing was guessed"}

    def run_route(self, lane_name, route, *, instructions, context=(), now, system=""):
        """One specific route, for evaluation. The same eligibility, checks and recording apply. Public data only."""
        lane = self.lanes[lane_name]
        base = {"lane": lane_name, "data_class": dc.PUBLIC, "attempts": [], "skipped": [], "route": None, "output": None, "text": "", "review_id": ""}
        ok, why = self._eligible(lane, route, dc.PUBLIC, now)
        if not ok:
            return {**base, "status": "skipped", "reason": why}
        result = self._try(lane, route, system, build_prompt(instructions, context), now, base)
        if result is None:
            return {**base, "status": "failed", "reason": base["attempts"][-1]["message"]}
        return {**base, "status": "ok", "route": {"provider": route["provider"], "model": route["model"]}, "output": result[0], "text": result[1].text,
                "completion": result[1]}

    def _try(self, lane, route, system, prompt, now, base):
        pid = route["provider"]
        provider = self.registry.provider(pid)
        client = self.clients[pid]
        source = f"ai:{pid}"
        attempt = {"provider": pid, "model": route["model"]}
        try:
            comp = client.complete(route, system, prompt, lane.max_tokens, expect_platform=provider.gateway_platform if pid != CLAUDE else None, lane=lane.name)
        except Exception as exc:       # every failure of every client is classified; none can end the run
            err = contract.classify_exception(exc)
            attempt.update({"result": err["code"], "message": err["message"]})
            base["attempts"].append(attempt)
            self.usage.record(provider, lane.name, now, ok=False, model=route["model"])
            self.health.observe(source, "gateway", ok=False, code=err["code"], message=err["message"], now=now)
            return None
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
