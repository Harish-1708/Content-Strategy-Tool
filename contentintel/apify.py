"""Apify: ready-made scrapers run on Apify's servers. An optional extra, off unless switched on in
research.yaml (collectors.apify.mode). Needs APIFY_TOKEN.

Apify's synchronous endpoint answers 201 Created (not 200) with the dataset items when the run succeeded, 400 with an
error type when the request is refused, and 408 when the run did not finish in 300 seconds."""
import re

from .providers import AuthError, Provider, ProviderConfigError, ProviderError, QuotaError


class Apify(Provider):
    name = "apify"
    label = "Apify"
    env_var = "APIFY_TOKEN"
    base = "https://api.apify.com/v2"

    def run(self, actor, payload, *, max_items, max_cost_usd, timeout=280):
        """Run an actor and wait for its items (Apify allows up to 300 seconds)."""
        url = f"{self.base}/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
        params = {"timeout": timeout, "maxItems": max_items, "maxTotalChargeUsd": max_cost_usd}
        try:
            body = self._call("POST", url, params=params, json_body=payload, timeout=timeout + 30, ok=(200, 201), stop=(408,),
                              headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        except ProviderError as exc:
            if "max-total-charge-usd-below-minimum" in str(exc):
                found = re.search(r"minimum of \$([0-9]+(?:\.[0-9]+)?)", str(exc))
                minimum = f"${found.group(1)}" if found else "a higher amount"
                raise ProviderConfigError(
                    f"{self.label}: the cost ceiling for this run (${max_cost_usd:g}) is below the minimum this actor accepts ({minimum}). "
                    f"Nothing was run and nothing was spent. Using this actor needs a ceiling of at least {minimum} per run (that is the most one run can cost); "
                    "raise it only if you approve that spending") from None
            raise
        if not isinstance(body, list):
            raise ProviderError(f"{self.label}: expected a list of items, got {type(body).__name__}")
        return body[:max_items]


    def usage(self):
        """What Apify itself says about this key's month: {'used': dollars spent, 'limit': dollars allowed, 'ends': the day the cycle ends} from its account limits endpoint, or None when it cannot be read (the platform then
        relies on its item counters alone). Nothing carries over: Apify's allowance is per monthly cycle, and what is not spent lapses."""
        self.usage_error = ""
        try:
            body = self._call("GET", f"{self.base}/users/me/limits", headers={"Authorization": f"Bearer {self.api_key}"}, ok=(200,))
        except ProviderError as exc:
            self.usage_error = f"{exc.__class__.__name__}: {self._clean(str(exc))[:160]}"
            return None
        data = body.get("data", body) if isinstance(body, dict) else {}
        used, limit = (data.get("current") or {}).get("monthlyUsageUsd"), (data.get("limits") or {}).get("maxMonthlyUsageUsd")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (used, limit)):
            self.usage_error = "Apify answered, but its reply has no monthlyUsageUsd / maxMonthlyUsageUsd numbers (fields seen: " + ", ".join(sorted(str(k) for k in (data if isinstance(data, dict) else {})))[:120] + ")"
            return None
        return {"used": float(used), "limit": float(limit), "ends": str((data.get("monthlyUsageCycle") or {}).get("endAt") or "")[:10]}


KEY_NAMES = ("APIFY_TOKEN", "APIFY_TOKEN_2", "APIFY_TOKEN_3", "APIFY_TOKEN_4")


class ApifyPool:
    """Several Apify keys, tried in order, for FAILOVER only: when a key is rejected (401/403) or has no credit left (402/429), the next key answers and the first is not asked again in this run. It is not a way to
    multiply the free allowance: the item allowances in research.yaml and the voice lane are counted for Apify as a whole, whichever key served a call. A refusal that another key cannot cure (our own cost ceiling,
    a timeout, a failed actor) is raised at once. `events` says, in words, which key was dropped and why (never the key)."""
    name = "apify"
    label = "Apify"
    env_var = "APIFY_TOKEN"

    def __init__(self, tokens):
        self.clients = [Apify(t) for t in tokens]
        self.dropped = {}
        self.events = []

    def usages(self):
        """One usage reading per key (None where unreadable), in key order."""
        return [c.usage() if hasattr(c, "usage") else None for c in self.clients]

    def run(self, actor, payload, *, max_items, max_cost_usd, timeout=280):
        last = None
        for i, client in enumerate(self.clients):
            if i in self.dropped:
                continue
            reading = client.usage() if hasattr(client, "usage") else None
            if reading is not None and reading["limit"] - reading["used"] < max_cost_usd:        # a key with less left than this call's ceiling is not asked: no wasted refusal, no overspend
                self.dropped[i] = "OutOfBudget"
                self.events.append(f"Apify key {i + 1} of {len(self.clients)} has ${max(0.0, reading['limit'] - reading['used']):.2f} left, less than this call's ${max_cost_usd:g} ceiling; the next key is used")
                continue
            try:
                return client.run(actor, payload, max_items=max_items, max_cost_usd=max_cost_usd, **({} if timeout == 280 else {"timeout": timeout}))
            except (AuthError, QuotaError) as exc:
                self.dropped[i] = exc.__class__.__name__
                nxt = next((j for j in range(len(self.clients)) if j not in self.dropped), None)
                self.events.append(f"Apify key {i + 1} of {len(self.clients)} was refused ({exc.__class__.__name__}); " + (f"key {nxt + 1} answers instead" if nxt is not None else "no other key is left"))
                last = exc
        if last is not None:
            raise type(last)(f"{self.label}: every key was refused ({'; '.join(self.events)})") from None
        raise AuthError(f"{self.label}: no key is available")


def build_apify(environ):
    """None with no key; the plain client with one; an ApifyPool with more (APIFY_TOKEN, APIFY_TOKEN_2, APIFY_TOKEN_3, APIFY_TOKEN_4: the same key twice counts once)."""
    tokens = list(dict.fromkeys(t for t in ((environ.get(n) or "").strip() for n in KEY_NAMES) if t))
    if not tokens:
        return None
    return Apify(tokens[0]) if len(tokens) == 1 else ApifyPool(tokens)


def budget_report(client):
    """(lines to print, dollars left in all keys or None). The remaining Apify money in this monthly cycle, per key and in total, as Apify itself reports it; every month is its own budget and nothing is carried forward."""
    readings = client.usages() if hasattr(client, "usages") else [client.usage() if hasattr(client, "usage") else None]
    known = [r for r in readings if r is not None]
    lines = []
    clients = getattr(client, "clients", [client])
    for n, r in enumerate(readings, 1):
        why = getattr(clients[n - 1], "usage_error", "") if n - 1 < len(clients) else ""
        lines.append(f"  key {n}: " + (f"${r['used']:.2f} of ${r['limit']:.2f} used" + (f", cycle ends {r['ends']}" if r["ends"] else "") if r is not None else "usage could not be read" + (f" ({why})" if why else "")))
    if not known:
        return ["Apify budget: Apify did not report the money left, so only the item counters guard the spending (the item allowances still apply):"] + lines, None
    left = sum(max(0.0, r["limit"] - r["used"]) for r in known)
    return [f"Apify budget this cycle (each key's allowance is per monthly cycle and nothing carries forward): ${left:.2f} left of ${sum(r['limit'] for r in known):.2f} across {len(known)} key(s)"] + lines, left
