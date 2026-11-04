"""The AI Gateway Health tab: one row per provider that is switched on or has been used, showing free quota used, failures, last success, any rest period,
and (for Claude) spending against the caps. It is the same on every brand's sheet, like Collector Health."""
TAB = "AI Gateway Health"
COLUMNS = ["provider", "label", "status", "cost", "data_classes", "approved_models", "requests_today", "daily_limit", "left_today", "requests_this_month", "failures_this_month",
           "tokens_today", "spend_this_month_usd", "last_success", "last_error", "resting_until", "terms", "note"]


def _month(usage, pid, now):
    req = fail = 0
    spend = 0.0
    for day, providers in usage.data["days"].items():
        if day[:7] == now[:7] and pid in providers:
            b = providers[pid]
            req, fail, spend = req + b["requests"], fail + b["failures"], spend + b["cost_usd"]
    return req, fail, round(spend, 4)


def rows(registry, usage, health_state, now):
    """{provider id: row}. Order: Claude first, then the rest by name."""
    state = (health_state or {}).get("routes", {})
    out = {}
    for pid, p in sorted(registry.providers.items(), key=lambda kv: (kv[0] != "claude", kv[0])):
        req_m, fail_m, spend_m = _month(usage, pid, now)
        if not p.enabled and not req_m:
            continue
        today = usage.day(pid, now)
        entry = state.get(f"ai:{pid}|gateway") or state.get(f"{pid}|api") or {}
        pause = entry.get("pause") or {}
        resting = pause.get("until") if pause.get("until", "") > now else ""
        daily = p.limits.get("requests_per_day")
        note = []
        if pid == "claude":
            note.append(f"spent ${usage.spend(now):.2f} of ${registry.daily_usd:.2f} today and ${usage.spend(now, month=True):.2f} of ${registry.monthly_usd:.2f} this month; a call is refused before it is made if it would cross a cap")
        elif p.paid:
            note.append("paid provider: spending counts against the caps")
        else:
            note.append("free plan: when the quota runs out this provider stops until it renews; nothing is bought or upgraded")
        if p.direct:
            note.append("called directly, not through the gateway")
        if resting:
            note.append("resting: " + str(pause.get("message", ""))[:120])
        out[pid] = {"provider": pid, "label": p.label, "status": "resting" if resting else ("ready" if p.enabled else "off"), "cost": "paid, capped" if p.paid else "free",
                    "data_classes": ", ".join(p.approved_classes) or "none", "approved_models": sum(1 for m in p.models if m.approved), "requests_today": today["requests"],
                    "daily_limit": daily, "left_today": (max(0, daily - today["requests"]) if daily is not None else None), "requests_this_month": req_m, "failures_this_month": fail_m,
                    "tokens_today": today["tokens_in"] + today["tokens_out"], "spend_this_month_usd": spend_m, "last_success": entry.get("last_success") or "",
                    "last_error": ((entry.get("last_error") or {}).get("code", "") + (" at " + (entry.get("last_error") or {}).get("at", "")[:16] if entry.get("last_error") else "")).strip(),
                    "resting_until": resting, "terms": p.terms.get("status", ""), "note": "; ".join(note)}
    return out


def tab(registry, usage, health_state, now):
    """The tab appears once the AI layer has actually been used (a call recorded, or a provider's health seen), like Collector Health: no activity, no tab."""
    ever_used = bool(usage.data["days"]) or any(k.startswith("ai:") for k in (health_state or {}).get("routes", {}))
    if not ever_used:
        return None
    data = rows(registry, usage, health_state, now)
    return {"title": TAB, "columns": COLUMNS, "rows_by_key": data, "key_col": "provider", "starter_manual": []} if data else None

