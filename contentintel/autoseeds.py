"""Research seeds that keep themselves up to date.

A factual profile of the brand is built from its catalogue every time (so it can never go stale), and
Claude turns that profile, plus the advertisers already seen, into what to search for: Meta Ad Library
keywords, hashtags and Deep Research questions, for direct rivals, neighbouring categories and
benchmark brands from any industry. The result is saved, and refreshed only when the catalogue changes
or it gets old. Seeds written by hand in a brand's research.yaml always come first.
"""
import collections
import hashlib
import json
import re
from datetime import datetime
from typing import NamedTuple

from .export import product_rows
from .providers import AuthError, ProviderError, QuotaError

SEEDS_VERSION = 2            # bumped when the saved seeds change shape, so older ones are rewritten once
SCOPES = ("direct", "adjacent", "cross_industry")
LIVE = ("active", "coming_soon", "pre_order")
_TS = "%Y-%m-%dT%H:%M:%SZ"


class AutoSeedError(Exception):
    """Claude's answer could not be used."""


class SeedResult(NamedTuple):
    seeds: dict          # or None when there is no catalogue yet
    note: str            # one line for the run report
    profile: str         # the profile text the seeds were (or would be) built from
    source: str          # claude | cache | profile-only | none


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _price(value):
    return "$" + (f"{value:.2f}".rstrip("0").rstrip(".") if value is not None else "?")


def brand_profile(brand, snapshot):
    """What the catalogue says about the brand right now: categories, price bands, materials, claims."""
    rows = [r for r in product_rows(snapshot.get("products", {})) if r["excluded"] != "TRUE" and r["status"] in LIVE]
    cats, materials, pockets, audiences, allergens = {}, collections.Counter(), set(), collections.Counter(), set()
    for r in rows:
        c = cats.setdefault(r["category"] or "other", {"count": 0, "families": collections.Counter(), "prices": [],
                                                       "limited": 0, "new": 0, "sale": 0})
        c["count"] += 1
        c["families"][r["family"]] += 1
        c["prices"] += [v for v in (_num(r["price_min"]), _num(r["price_max"])) if v is not None]
        flags = {f.strip() for f in r["flags"].split(";")}
        c["limited"] += "limited_edition" in flags
        c["new"] += "new" in flags
        c["sale"] += "on_sale" in flags
        for part in (p.strip() for p in r["materials"].split(";") if p.strip()):
            name = re.sub(r"\s*\d+(\.\d+)?%?$", "", part).strip().lower()
            materials["polyester" if name == "poly" else name] += 1
        pockets.update(p.strip() for p in r["pocket_claims"].split(";") if p.strip())
        audiences.update(a.strip() for a in r["audience_hints"].split(";") if a.strip())
        allergens.update(a.strip() for a in r["allergens"].split(";") if a.strip())
    return {
        "name": brand.name, "domain": brand.domain, "platform": brand.platform, "live_products": len(rows),
        "categories": {cat: {"products": c["count"], "families": [f for f, _ in c["families"].most_common(12)],
                             "price_low": min(c["prices"]) if c["prices"] else None,
                             "price_high": max(c["prices"]) if c["prices"] else None,
                             "limited_edition": c["limited"], "new": c["new"], "on_sale": c["sale"]}
                       for cat, c in sorted(cats.items())},
        "materials": [m for m, _ in materials.most_common(5)],
        "pocket_claims": sorted(pockets, key=lambda p: (not p.isdigit(), int(p) if p.isdigit() else 0, p)),
        "audience_hints": dict(sorted(audiences.items())), "allergens": sorted(allergens)}


def profile_text(profile):
    lines = [f"Brand: {profile['name']} ({profile['domain']}), a {profile['platform']} store with "
             f"{profile['live_products']} products on sale or coming soon."]
    for cat, c in profile["categories"].items():
        band = (_price(c["price_low"]) if c["price_low"] == c["price_high"] else f"{_price(c['price_low'])}-{_price(c['price_high'])}")
        extras = ", ".join(f"{n} {label}" for n, label in ((c["limited_edition"], "limited edition"), (c["new"], "new"),
                                                           (c["on_sale"], "on sale")) if n)
        lines.append(f"{cat}: {c['products']} products, {band}. Families: {', '.join(c['families'])}."
                     + (f" {extras}." if extras else ""))
    if profile["materials"]:
        lines.append("Materials seen: " + ", ".join(profile["materials"]) + ".")
    if profile["pocket_claims"]:
        lines.append("Pocket claims: " + ", ".join(profile["pocket_claims"]) + ".")
    if profile["audience_hints"]:
        lines.append("Audiences hinted: " + ", ".join(f"{a} ({n} products)" for a, n in profile["audience_hints"].items()) + ".")
    if profile["allergens"]:
        lines.append("Allergens mentioned: " + ", ".join(profile["allergens"]) + ".")
    return "\n".join(lines)


def profile_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def advertisers_seen(records, own_name, limit=15):
    """Who else turned up in the Meta ad searches: (name, ads found, longest-running ad in days)."""
    seen = {}
    for rec in records:
        if rec.get("source") != "meta_ads":
            continue
        for item in rec.get("items", []):
            name = (item.get("handle") or "").strip()
            if not name or name.lower() == own_name.lower():
                continue
            entry = seen.setdefault(name, {"ads": set(), "days": 0})
            entry["ads"].add(item["item_id"])
            entry["days"] = max(entry["days"], item.get("days_running") or 0)
    ranked = sorted(seen.items(), key=lambda kv: (-len(kv[1]["ads"]), -kv[1]["days"], kv[0].lower()))
    return [(name, len(e["ads"]), e["days"]) for name, e in ranked[:limit]]


SEED_SYSTEM = (
    "You plan competitor and inspiration research for a marketing agency that writes ad briefs. You are given a "
    "factual profile of one brand, built from its catalogue, and advertisers already seen. Reply with ONLY a JSON "
    "object, no prose, with these keys:\n"
    '  "context": 2-4 factual sentences a researcher needs about the brand (what it sells, price points, audience, '
    "what is distinctive). Use only facts in the profile.\n"
    '  "keywords": {"direct": [...], "adjacent": [...], "cross_industry": [...]} - short generic search phrases '
    "(1-4 words, no hashtags, NEVER a brand name) for the Meta Ad Library. direct = rival products selling the same "
    "thing; adjacent = neighbouring categories the same customer buys; cross_industry = ad angles or themes from "
    "OTHER industries whose creative is worth studying for craft (hooks, formats, storytelling).\n"
    '  "benchmarks": [...] - brand names, written exactly as the brand\'s Facebook page is named, whose advertising '
    "is worth following: direct rivals (the strongest first) and brands from ANY industry chosen for the quality of "
    "their advertising and not for product similarity. Names only; the tool finds each brand's own advertiser page, "
    "so never put a brand in keywords.\n"
    '  "tiktok_hashtags": [...] and "instagram_hashtags": [...] - hashtags without the # sign.\n'
    '  "questions": [...] - self-contained research questions for Gemini Deep Research. At least one must ask '
    "which ad hooks, formats and angles are performing best right now across the best consumer brands in any "
    "industry and how this brand could adapt them to its own product and voice.\n"
    "Do not invent facts about the brand. Prefer specific, searchable terms over generic ones.")


def _user_message(text, advertisers, settings):
    seen = "\n".join(f"- {n} ({c} ads found, longest running {d} days)" for n, c, d in advertisers) or "(none yet)"
    return (f"Brand profile:\n{text}\n\nAdvertisers already seen in Meta ad searches:\n{seen}\n\n"
            f"Limits: at most {settings.auto_max_keywords} keywords in total, {settings.auto_max_benchmarks} benchmarks, "
            f"{settings.auto_max_hashtags} hashtags per platform and {settings.auto_max_questions} questions.")


def _clean(values, limit, maxlen, strip=""):
    out, seen = [], set()
    if limit <= 0:
        return out
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split()).strip(strip).strip()
        if not text or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text[:maxlen])
        if len(out) >= limit:
            break
    return out


def parse_seeds(text, settings):
    """Claude's JSON reply -> tidy seeds within the limits. Raises AutoSeedError when it is unusable."""
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(), flags=re.I)
    start, end = body.find("{"), body.rfind("}")
    try:
        data = json.loads(body[start:end + 1]) if start >= 0 and end > start else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("keywords"), dict):
        raise AutoSeedError("Claude's answer was not the JSON the tool asked for")
    pools = {s: _clean(data["keywords"].get(s), 50, 60, "#@") for s in SCOPES}
    keywords, chosen = {s: [] for s in SCOPES}, 0
    while chosen < settings.auto_max_keywords and any(pools.values()):       # one from each scope in turn
        for scope in SCOPES:
            if pools[scope] and chosen < settings.auto_max_keywords:
                keywords[scope].append(pools[scope].pop(0))
                chosen += 1
    if not chosen:
        raise AutoSeedError("Claude's answer had no usable keywords")
    questions = [q for q in _clean(data.get("questions"), 50, 500) if len(q) >= 20][:settings.auto_max_questions]
    return {"context": " ".join(str(data.get("context") or "").split())[:600], "keywords": keywords,
            "benchmarks": _clean(data.get("benchmarks"), settings.auto_max_benchmarks, 60, "#@"),
            "tiktok_hashtags": _clean(data.get("tiktok_hashtags"), settings.auto_max_hashtags, 40, "#@"),
            "instagram_hashtags": _clean(data.get("instagram_hashtags"), settings.auto_max_hashtags, 40, "#@"),
            "questions": questions}


def fallback_seeds(brand, text):
    """With no Claude: the brand's own name and a fresh profile, so nothing is ever stale or empty."""
    tag = re.sub(r"[^a-z0-9]", "", brand.name.lower())
    return {"context": text, "keywords": {"direct": [brand.name], "adjacent": [], "cross_industry": []}, "benchmarks": [],
            "tiktok_hashtags": [tag] if tag else [], "instagram_hashtags": [tag] if tag else [], "questions": []}


def _age_days(generated_at, now):
    try:
        return (datetime.strptime(now, _TS) - datetime.strptime(generated_at, _TS)).total_seconds() / 86400
    except (TypeError, ValueError):
        return float("inf")


def ensure_auto_seeds(brand, store, claude, settings, ledger, now, dry_run=False, force=False):
    """The brand's auto seeds, regenerated only when the catalogue changed, they are old, or force is set."""
    snapshot = store.load_latest(brand.slug)
    if snapshot is None:
        return SeedResult(None, "", "", "none")
    text = profile_text(brand_profile(brand, snapshot))
    digest, cached = profile_hash(text), store.read_auto_seeds(brand.slug)
    if cached and cached.get("seeds") and not force and cached.get("profile_hash") == digest \
            and cached.get("version") == SEEDS_VERSION and _age_days(cached.get("generated_at"), now) < settings.auto_refresh_days:
        return SeedResult(cached["seeds"], f"reused (written {cached['generated_at'][:10]})", text, "cache")
    keep = (cached["seeds"], "cache") if cached and cached.get("seeds") else (fallback_seeds(brand, text), "profile-only")
    why = ("the catalogue changed" if cached and cached.get("profile_hash") != digest
           else "the seed format changed" if cached and cached.get("version") != SEEDS_VERSION
           else "they are old" if cached else "none existed yet")
    if dry_run:
        return SeedResult(keep[0], f"would be regenerated ({why}); a dry run does not call Claude", text, keep[1])
    if claude is None:
        return SeedResult(keep[0], f"need refreshing ({why}) but ANTHROPIC_API_KEY is not set; using {keep[1]} seeds", text, keep[1])
    if settings.claude_monthly_calls - ledger.month_used("claude_calls") < 1:
        return SeedResult(keep[0], f"need refreshing ({why}) but the monthly Claude allowance is used up; using {keep[1]} seeds", text, keep[1])
    try:
        raw = claude.complete(SEED_SYSTEM, _user_message(text, advertisers_seen(store.read_collected(brand.slug), brand.name), settings),
                              max_tokens=1600)
        seeds = parse_seeds(raw, settings)
    except (AuthError, QuotaError, ProviderError, AutoSeedError) as exc:
        return SeedResult(keep[0], f"could not be regenerated ({exc}); using {keep[1]} seeds", text, keep[1])
    ledger.charge("claude_calls", 1)
    store.save_usage(ledger.usage)
    store.write_auto_seeds(brand.slug, {"generated_at": now, "version": SEEDS_VERSION, "profile_hash": digest,
                                        "model": getattr(claude, "model", ""),
                                        "profile": text, "seeds": seeds})
    return SeedResult(seeds, f"regenerated ({why})", text, "claude")


def format_seeds(slug, result):
    lines = [f"=== {slug} ===", f"Auto seeds {result.note}" if result.note else "No catalogue yet: run Sync first.", ""]
    if result.seeds is None:
        return "\n".join(lines)
    s = result.seeds
    lines += ["Brand profile (built from the catalogue, so it is always current):", *("  " + l for l in result.profile.splitlines()), "",
              f"Seeds (source: {result.source}):"]
    for scope in SCOPES:
        if s["keywords"].get(scope):
            lines.append(f"  Meta ad keywords, {scope.replace('_', ' ')}: " + "; ".join(s["keywords"][scope]))
    if s.get("benchmarks"):
        lines.append("  Benchmark brands (followed by advertiser page): " + "; ".join(s["benchmarks"]))
    if s["tiktok_hashtags"]:
        lines.append("  TikTok hashtags: " + ", ".join("#" + h for h in s["tiktok_hashtags"]))
    if s["instagram_hashtags"]:
        lines.append("  Instagram hashtags: " + ", ".join("#" + h for h in s["instagram_hashtags"]))
    if s["context"] and result.source == "claude":
        lines.append("  Context for Deep Research: " + s["context"])
    lines += [f"  Deep Research question: {q}" for q in s["questions"]]
    return "\n".join(lines)
