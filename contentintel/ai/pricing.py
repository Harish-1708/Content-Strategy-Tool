"""The pricing catalogue (ArcReel's billing-ledger pattern): what every model the tool may call costs, and what it is allowed to use, in one table built from the registry itself, so the billing ledger can say not only
what was spent but what each route WOULD cost. Free providers cost 0 and are limited by their own allowance (a request or token limit); a paid provider has a price per million tokens in the registry; Claude is paid.
CARD records what each provider's own signup asks for (re-checked 2026-11-19): a provider that needs a card to be free is never wired as a free provider."""
CARD = {"huggingface": "card required: no free tier since 2026-10-07 (credits must be bought, or PRO at 9 dollars a month)", "ollama": "no card for the free plan; the free usage is a small monthly allowance for starter models (limits unpublished)"}
NO_CARD_CHECKED = ("xkiro", "anyapi", "requesty", "navy", "nvidia")            # confirmed no card on 2026-11-19


def card_status(provider):
    if provider.id in CARD:
        return CARD[provider.id]
    if provider.id in NO_CARD_CHECKED:
        return "no card (confirmed 2026-11-19)"
    return "paid (an Anthropic key and the spending cap)" if provider.paid else "no card needed (free tier; terms recorded in the registry)"


def catalogue(registry):
    """[{provider, model, enabled, kind: free|paid|retired, input_per_m, output_per_m, limits, card}] for every approved model of every provider that is switched on or has a price."""
    rows = []
    for pid, p in sorted(registry.providers.items()):
        models = [m for m in p.models if m.approved] or [None]                           # a provider with no approved model still gets its row, so its card status and switch are on record
        for m in models:
            price = (p.price_for(m.id) if m is not None else p.price_per_million) or {}
            rows.append({"provider": pid, "model": m.id if m is not None else ("(by model name)" if p.paid else "(no model approved)"), "enabled": bool(p.enabled), "kind": "paid" if p.paid else "free",
                         "input_per_m": float(price.get("input", 0) or 0), "output_per_m": float(price.get("output", 0) or 0), "limits": dict(p.limits), "card": card_status(p)})
    return rows


def lines(registry, only_enabled=True):
    rows = [r for r in catalogue(registry) if r["enabled"] or not only_enabled]
    free = [r for r in rows if r["kind"] == "free"]
    paid = [r for r in rows if r["kind"] == "paid"]
    out = [f"Pricing catalogue: {len(free)} free model(s) (cost 0, limited by their provider's own allowance) and {len(paid)} paid route(s)"]
    for r in paid:
        out.append(f"  paid: {r['provider']} / {r['model']}: ${r['input_per_m']:g} in, ${r['output_per_m']:g} out per million tokens")
    return out
