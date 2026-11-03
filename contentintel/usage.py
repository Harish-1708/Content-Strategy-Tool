"""Usage and budget tracking for AI calls: requests, tokens, latency and cost, per day and month, per provider and lane.

Stored in data/_shared/ai_usage.json. Paid use is refused unless a budget above zero has been set, and a daily or
monthly cap stops further calls. Free providers have request and token limits instead.
"""
import json
from pathlib import Path


def _cost(provider, tokens_in, tokens_out, model=None):
    if not provider.paid:
        return 0.0
    p = provider.price_for(model)
    return round((tokens_in * p.get("input", 0) + tokens_out * p.get("output", 0)) / 1_000_000, 6)


class UsageLedger:
    def __init__(self, path):
        self.path = Path(path)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except ValueError:
            data = {}
        self.data = data if isinstance(data, dict) and isinstance(data.get("days"), dict) else {"days": {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _bucket(self, day, provider):
        return self.data["days"].setdefault(day, {}).setdefault(provider, {"requests": 0, "failures": 0, "tokens_in": 0, "tokens_out": 0,
                                                                          "cost_usd": 0.0, "latency_ms": 0, "by_lane": {}})

    def record(self, provider, lane, now, *, ok, tokens_in=0, tokens_out=0, latency_ms=0, model=None):
        b = self._bucket(now[:10], provider.id)
        cost = _cost(provider, tokens_in, tokens_out, model)
        b["requests"] += 1
        b["failures"] += 0 if ok else 1
        b["tokens_in"] += int(tokens_in)
        b["tokens_out"] += int(tokens_out)
        b["cost_usd"] = round(b["cost_usd"] + cost, 6)
        b["latency_ms"] += int(latency_ms)
        lane_b = b["by_lane"].setdefault(lane, {"requests": 0, "tokens": 0, "cost_usd": 0.0})
        lane_b["requests"] += 1
        lane_b["tokens"] += int(tokens_in) + int(tokens_out)
        lane_b["cost_usd"] = round(lane_b["cost_usd"] + cost, 6)
        return cost

    def day(self, provider_id, now):
        return self.data["days"].get(now[:10], {}).get(provider_id, {"requests": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})

    def spend(self, now, *, month=False):
        total = 0.0
        for day, providers in self.data["days"].items():
            if (day[:7] == now[:7]) if month else (day == now[:10]):
                total += sum(b["cost_usd"] for b in providers.values())
        return round(total, 6)

    def worst_case_cost(self, provider, model, est_in, max_out):
        return _cost(provider, est_in, max_out, model)

    def allows(self, provider, registry, now, est_tokens=1000, *, model=None, est_in=0, max_out=0):
        """(ok, reason): does today's usage leave room for one more call. For a paid call the worst case of that call is counted
        before it is made, so the spending cap is never crossed by the call itself."""
        d = self.day(provider.id, now)
        lim = provider.limits
        if lim.get("requests_per_day") is not None and d["requests"] + 1 > lim["requests_per_day"]:
            return False, "daily request limit reached"
        if lim.get("tokens_per_day") is not None and d["tokens_in"] + d["tokens_out"] + est_tokens > lim["tokens_per_day"]:
            return False, "daily token limit reached"
        if provider.paid:
            if registry.daily_usd <= 0 and registry.monthly_usd <= 0:
                return False, "paid use is not approved (the spending budget is zero)"
            worst = self.worst_case_cost(provider, model, est_in, max_out)
            if registry.daily_usd and self.spend(now) + worst > registry.daily_usd:
                return False, "daily spending limit would be exceeded"
            if registry.monthly_usd and self.spend(now, month=True) + worst > registry.monthly_usd:
                return False, "monthly spending limit would be exceeded"
        return True, ""

    def summary(self, now):
        rows = []
        for pid, b in sorted(self.data["days"].get(now[:10], {}).items()):
            rows.append({"provider": pid, "requests": b["requests"], "failures": b["failures"], "tokens": b["tokens_in"] + b["tokens_out"],
                         "cost_usd": b["cost_usd"], "avg_latency_ms": round(b["latency_ms"] / b["requests"]) if b["requests"] else 0})
        return rows
