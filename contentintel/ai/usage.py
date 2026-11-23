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

    def lane_today(self, provider_id, lane, now):
        """What ONE job (lane) has used of one provider today: {requests, tokens}."""
        b = self.data["days"].get(now[:10], {}).get(provider_id, {}).get("by_lane", {}).get(lane, {})
        return {"requests": b.get("requests", 0), "tokens": b.get("tokens", 0)}

    def load(self, provider, now):
        """How much of its daily allowance a provider has used today, 0 to 1 and more: requests over requests_per_day, or tokens over tokens_per_day, whichever the provider limits; a provider with no stated
        daily limit is measured against a soft 1000 requests so it still takes its turn."""
        d, lim = self.day(provider.id, now), provider.limits
        fractions = []
        if lim.get("requests_per_day"):
            fractions.append(d["requests"] / lim["requests_per_day"])
        if lim.get("tokens_per_day"):
            fractions.append((d["tokens_in"] + d["tokens_out"]) / lim["tokens_per_day"])
        return max(fractions) if fractions else d["requests"] / 1000

    def reliability(self, provider_id, now):
        """1 minus the share of today's calls that failed; a provider with fewer than 5 calls today is given the benefit of the doubt (1.0)."""
        d = self.data["days"].get(now[:10], {}).get(provider_id, {})
        n = d.get("requests", 0)
        return 1.0 if n < 5 else round(1.0 - d.get("failures", 0) / n, 3)

    def avg_latency_s(self, provider_id, now):
        d = self.data["days"].get(now[:10], {}).get(provider_id, {})
        return round(d.get("latency_ms", 0) / d["requests"] / 1000, 2) if d.get("requests") else 0.0

    def lane_share_ok(self, provider, lane, now, share, near=0.8):
        """(ok, reason): one job must not use up what the other jobs also draw on. Only when the provider is NEAR its daily limit (80% used) and this job has used more than `share` of what was used today is the
        job held back and the rest kept for the others; while the provider has room, or this job is not the one using it up, nothing is withheld."""
        d = self.day(provider.id, now)
        if d["requests"] <= 0 or self.load(provider, now) < near:
            return True, ""
        used = self.lane_today(provider.id, lane, now)["requests"]
        if used / d["requests"] > share:
            return False, f"this provider is near its daily limit and this job has used {int(used * 100 / d['requests'])}% of today's calls to it (more than {int(share * 100)}%): the rest is kept for the other jobs"
        return True, ""

    def billing(self, now):
        """The billing ledger: for today and this month, per provider and per job, the requests, tokens and cost (free providers cost 0; Claude and any paid provider cost what their price says)."""
        out = {"day": now[:10], "month": now[:7], "today": {}, "month_totals": {}}
        for day, providers in self.data["days"].items():
            for pid, b in providers.items():
                targets = []
                if day == now[:10]:
                    targets.append(out["today"])
                if day[:7] == now[:7]:
                    targets.append(out["month_totals"])
                for target in targets:
                    row = target.setdefault(pid, {"requests": 0, "failures": 0, "tokens": 0, "cost_usd": 0.0, "lanes": {}})
                    row["requests"] += b["requests"]
                    row["failures"] += b["failures"]
                    row["tokens"] += b["tokens_in"] + b["tokens_out"]
                    row["cost_usd"] = round(row["cost_usd"] + b["cost_usd"], 6)
                    for lane, lb in b.get("by_lane", {}).items():
                        l = row["lanes"].setdefault(lane, {"requests": 0, "tokens": 0, "cost_usd": 0.0})
                        l["requests"] += lb["requests"]
                        l["tokens"] += lb["tokens"]
                        l["cost_usd"] = round(l["cost_usd"] + lb["cost_usd"], 6)
        return out

    def month_requests(self, provider_id, now):
        """Calls made to one provider so far this month, from the ledger (a provider whose free key allows only so many calls a month)."""
        return sum(providers.get(provider_id, {}).get("requests", 0) for day, providers in self.data["days"].items() if day[:7] == now[:7])

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
        if lim.get("requests_per_month") is not None and self.month_requests(provider.id, now) + 1 > lim["requests_per_month"]:
            return False, "monthly request limit reached (the provider's own cap)"
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


def billing_lines(report, registry=None):
    """The billing ledger as lines: today and this month per provider with each job's share of it, and each provider's use of its own daily allowance."""
    lines = []
    for title, key in (("today", "today"), ("this month", "month_totals")):
        rows = report[key]
        lines.append(f"Billing ledger, {title} ({report['day'] if key == 'today' else report['month']}): " + (f"{sum(r['requests'] for r in rows.values())} request(s), ${sum(r['cost_usd'] for r in rows.values()):.4f}" if rows else "no calls"))
        for pid, r in sorted(rows.items(), key=lambda kv: -kv[1]["requests"]):
            cap = ""
            if registry is not None and key == "today" and pid in registry.providers:
                lim = registry.provider(pid).limits
                if lim.get("requests_per_day"):
                    cap = f" of {lim['requests_per_day']} a day ({r['requests'] * 100 // lim['requests_per_day']}%)"
            jobs = ", ".join(f"{lane} {lb['requests']}" for lane, lb in sorted(r["lanes"].items(), key=lambda kv: -kv[1]["requests"])[:5])
            lines.append(f"  {pid}: {r['requests']} request(s){cap}, {r['tokens']} tokens, ${r['cost_usd']:.4f}" + (f" | by job: {jobs}" if jobs else ""))
    return lines
