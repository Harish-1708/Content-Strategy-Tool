"""What the observatory found, written for people: a change report per brand, alerts with a suggested next step, and rows for the sheet.

Two honesty rules show up in every output: old readings are labelled stale with their age, and every statement is a plain observation
('the price went from X to Y'), never a claim about why it happened or how well something sold.
"""
import json
from pathlib import Path

from .. import health
from . import judge as judge_mod
from . import shopify as S

EVENT_COLUMNS = ["event_id", "at", "competitor", "severity", "type", "summary", "url", "suggested_action"]
COMPETITOR_COLUMNS = ["site_id", "name", "domain", "status", "origin", "platform", "products", "collections", "on_sale_products", "events_7d", "instagram", "instagram_confidence", "match_sources", "request_controls", "tiktok", "youtube", "last_read", "stale", "note"]
CORPUS_COLUMNS = ["video_id", "channel", "title", "views", "median", "multiple", "views_per_day", "trend_multiple", "trend_basis", "formula", "format", "published", "url"]
TAB_CORPUS = "Public Corpus"
PRODUCT_COLUMNS = ["key", "competitor", "title", "price", "compare_at_price", "on_sale", "available", "collections", "published", "last_change", "url"]
TAB_EVENTS, TAB_COMPETITORS, TAB_PRODUCTS = "Observatory Events", "Competitors", "Competitor Products"

_ACTIONS = {
    "new_drop": "A rival launched several products together. Check what it means for your own launch calendar.",
    "product_new": "A rival added a product. Check whether you have, or want, something comparable.",
    "sale_started": "A rival started a sale. Check your offer and price positioning before its next push.",
    "discount_depth_change": "A rival changed how deep its discount is. Compare with your current offer.",
    "offer_change": "A rival changed its offer or banner. Compare with what you are running now.",
    "stock_out": "A rival sold out of a product. A short window to reach the people who wanted it.",
    "best_seller_entry": "A product entered a rival's best sellers. Worth looking at what it is and how it is presented.",
    "collection_new": "A rival opened a new collection page. Look at how it frames the products.",
    "collection_reorder": "A rival changed what it puts first. Look at what it now leads with.",
    "headline_change": "A rival changed its main headline. Look at the new message.",
    "blog_post_new": "A rival published a post. Read what angle it takes.",
    "new_landing_page": "A rival added a landing page. Look at what it is for.",
    "price_change": "A rival changed a price. Compare with your own price for the closest product.",
}


def suggested_action(ev):
    return _ACTIONS.get(ev["type"], "") if ev["severity"] in ("major", "notable") else ""


# ---- which sites a brand watches --------------------------------------------------------------------------------------------------------------

def _sites_file(store, slug):
    return store.dir(slug) / "observatory" / "sites.json"


def write_brand_sites(store, slug, entries, now):
    path = _sites_file(store, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated_at": now, "sites": entries}, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_brand_sites(store, slug):
    path = Path(store.dir(slug, create=False)) / "observatory" / "sites.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("sites", []) if path.is_file() else []
    except (OSError, ValueError):
        return []


def discovery_file(store, slug):
    return _sites_file(store, slug).with_name("discovery.json")


def read_discovery(store, slug):
    """The discovery book for a brand: {'updated_at', 'candidates': [...]}. Missing or damaged means empty."""
    path = Path(store.dir(slug, create=False)) / "observatory" / "discovery.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    return {"updated_at": data.get("updated_at", ""), "candidates": [c for c in data.get("candidates", []) if isinstance(c, dict) and c.get("key")]}


def write_discovery(store, slug, book, now):
    path = discovery_file(store, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"updated_at": now, "candidates": book["candidates"]}, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def controls_summary(adv):
    """The request controls switched on for a competitor, in words, for the sheet and the controls command. Secrets appear by name only; nothing here is a secret."""
    if not adv:
        return ""
    parts = []
    if adv.get("browser") or adv.get("stealth"):
        parts.append("browser" + (f" ({adv['locale']}, {adv['timezone']})" if adv.get("locale") and adv.get("timezone") else f" ({adv.get('locale') or adv.get('timezone')})" if (adv.get("locale") or adv.get("timezone")) else ""))
    for key, label in (("identity", "identity"), ("accept_language", "language"), ("session", "session"), ("proxy", "proxy"), ("impersonate", "TLS impersonation"), ("hide_canvas", "canvas change"),
                       ("block_webrtc", "WebRTC block"), ("humanize", "cursor simulation"), ("solve_challenges", "challenge solving"), ("disable_resources", "no images or fonts")):
        if adv.get(key):
            parts.append(label + (f" ({adv[key]})" if key == "identity" else ""))
    if adv.get("headers"):
        parts.append("headers: " + ", ".join(sorted(adv["headers"])))
    if adv.get("cookies_env"):
        parts.append(f"account cookies (secret {adv['cookies_env']})")
    if adv.get("delay"):
        parts.append(f"pace {adv['delay']:g}s")
    if adv.get("rest_hours"):
        parts.append(f"rests {adv['rest_hours']:g}h after a refusal")
    return "; ".join(parts)


def discovered_competitors(store, slug):
    """Competitors discovery has accepted (automatically or by a person) as competitor dicts the monitor can use."""
    from . import config as OC
    out = []
    for c in read_discovery(store, slug)["candidates"]:
        if c.get("status") != "monitoring":
            continue
        out.append({"id": OC.competitor_id(c["domain"]), "name": c.get("name") or c["domain"], "domain": OC.host_of(c["domain"]), "kind": "store", "pages": [], "blog": "", "feeds": [], "advanced": {}, "selectors": {},
                    "youtube": "", "instagram": c.get("instagram", ""), "tiktok": c.get("tiktok", ""), "origin": "discovery",
                    "ai_summary": judge_mod.summary(c), "match": {"instagram_confidence": c.get("instagram_confidence"), "sources": sorted({e["source"] for k in ("instagram_evidence", "domain_evidence") for e in c.get(k, [])})},
                    "monitor": {"shopify": "auto", "sitemap": True, "feeds": True, "pages": True}})
    return [c for c in out if c["domain"]]


# ---- events and the report --------------------------------------------------------------------------------------------------------------------

def recent_events(snaps, site_ids, now, days):
    out = []
    for sid in site_ids:
        for ev in snaps.read_events(sid):
            age = health.age_days(ev["at"], now)
            if age is not None and age <= days:
                out.append(ev)
    return sorted(out, key=lambda e: (e["at"], e["event_id"]), reverse=True)


def _where(ev):
    return ev.get("subject", {}).get("url", "")


def build_report(brand_name, sites, snaps, now, days=7, stale_after=14):
    """Markdown for one brand: alerts first, then each competitor with what changed, and how old every reading is."""
    from . import monitor
    lines = [f"# Competitor observatory: {brand_name}", "",
             f"Prepared {now[:10]}. Covers the last {days} days of public information read politely from {len(sites)} competitor site(s) "
             "(robots.txt obeyed). Everything below is an observation of what a site published, not a statement about why or how well it works.", ""]
    per_site = {s["id"]: recent_events(snaps, [s["id"]], now, days) for s in sites}
    alerts = [(s, e) for s in sites for e in per_site[s["id"]] if e["severity"] == "major"]
    lines += [f"## Alerts ({len(alerts)})", ""]
    if not alerts:
        lines += ["Nothing needs attention this week.", ""]
    for s, e in sorted(alerts, key=lambda t: t[1]["at"], reverse=True)[:25]:
        action = suggested_action(e)
        lines.append(f"- **{s['name']}**, {e['at'][:10]}: {e['summary']}" + (f" *Next step: {action}*" if action else ""))
    if alerts:
        lines.append("")
    lines += ["## Competitors", ""]
    for s in sites:
        evs = [e for e in per_site[s["id"]] if e["type"] not in ("baseline_recorded", "events_truncated")]
        lines += [f"### {s['name']} ({s['domain']})", ""]
        stale = [r for r in monitor.stale_report(snaps, s["id"], now, {}, stale_after) if r["stale"]]
        _, cat = snaps.latest(s["id"], "catalogue", complete_only=False)
        if cat:
            sm = S.summary(cat)
            lines.append(f"Catalogue: {sm['products']} products in {sm['collections']} collections, {sm['on_sale']} on sale, {sm['available']} in stock"
                         + ("" if sm["complete"] else " (the last reading was incomplete)") + ".")
        for r in stale:
            lines.append(f"- STALE: {r['kind']} was last confirmed {r['age_days']} days ago ({r['confirmed_at'][:10]}); newer changes may exist.")
        counts = {}
        for e in evs:
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        lines.append("Changes: " + (", ".join(f"{n} {t.replace('_', ' ')}" for t, n in sorted(counts.items(), key=lambda kv: -kv[1])) if counts else "none in this period") + ".")
        for e in [e for e in evs if e["severity"] in ("major", "notable")][:12]:
            lines.append(f"- {e['at'][:10]} [{e['severity']}] {e['summary']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---- sheet tabs -------------------------------------------------------------------------------------------------------------------------------

def sheet_tabs(brand, store, snaps, now, settings):
    """The tabs the observatory adds to a brand's sheet: [{title, columns, rows_by_key, key_col, starter_manual}]. Empty when nothing is watched."""
    from . import corpus as corpus_mod
    from . import monitor
    tabs = []
    stored = corpus_mod.load_corpus(store, brand.slug)
    if stored["videos"]:
        yt = settings["youtube"]
        res = corpus_mod.outliers(list(stored["videos"].values()), corpus_mod.load_formulas(), min_videos=yt["min_videos_for_median"], min_multiple=yt["outlier_min_multiple"])
        trend = corpus_mod.velocity(list(stored["videos"].values()), now, window_days=yt["trend_window_days"], min_hours=yt["trend_min_hours"], min_multiple=yt["trend_min_multiple"])
        speed = {t["video_id"]: t for t in trend["trending"]}
        limit = settings["corpus"]["max_items_in_sheet"]
        formulas = corpus_mod.load_formulas()
        rows = {}
        for r in res["outliers"][:limit]:
            t = speed.get(r["video_id"], {})
            rows[r["video_id"]] = {"video_id": r["video_id"], "channel": r["channel"], "title": r["title"], "views": r["views"], "median": r["median"], "multiple": r["multiple"],
                                   "views_per_day": t.get("per_day"), "trend_multiple": t.get("multiple"), "trend_basis": t.get("basis", ""), "formula": r["formula"], "format": r["shorts_class"],
                                   "published": (stored["videos"][r["video_id"]].get("published_at") or "")[:10], "url": r["url"]}
        for t in trend["trending"][:limit]:                                   # fast risers that are not outliers by total views yet
            if t["video_id"] not in rows and len(rows) < limit * 2:
                v = stored["videos"][t["video_id"]]
                rows[t["video_id"]] = {"video_id": t["video_id"], "channel": t["channel"], "title": t["title"], "views": t["views"], "median": None, "multiple": None, "views_per_day": t["per_day"],
                                       "trend_multiple": t["multiple"], "trend_basis": t["basis"], "formula": corpus_mod.classify(t["title"], formulas)[1], "format": t["shorts_class"],
                                       "published": (v.get("published_at") or "")[:10], "url": t["url"]}
        tabs.append({"title": TAB_CORPUS, "columns": CORPUS_COLUMNS, "rows_by_key": rows, "key_col": "video_id", "starter_manual": ["Notes"]})
    sites = read_brand_sites(store, brand.slug)
    cfg = settings["sheet"]
    comp_rows, event_rows, product_rows = {}, {}, {}
    monitored_domains = {s["domain"] for s in sites}
    for c in read_discovery(store, brand.slug)["candidates"]:           # found by discovery but not (yet) watched: shown so a person can decide, with the reasons
        if c.get("domain") and c["domain"] in monitored_domains:          # already shown as a watched site below
            continue
        decided = c.get("decided_by") == "person"
        waiting = c.get("status") == "monitoring"                       # chosen for monitoring, but no monitor run has finished for it yet: still listed, never dropped
        comp_rows["candidate:" + c["key"]] = {"site_id": "candidate:" + c["key"], "name": c["name"], "domain": c.get("domain") or "", "status": ("monitoring (waiting for its first reading)" if waiting else c["status"]) + (" (your decision)" if decided else ""),
                                              "origin": "discovery", "platform": "", "products": None, "collections": None, "on_sale_products": None, "events_7d": 0, "instagram": c.get("instagram", ""), "instagram_confidence": c.get("instagram_confidence"),
                                              "match_sources": ", ".join(sorted({e["source"] for k in ("instagram_evidence", "domain_evidence") for e in c.get(k, [])})), "tiktok": c.get("tiktok", ""), "youtube": "",
                                              "last_read": "", "stale": "", "note": f"score {c.get('score', 0)}: " + "; ".join(c.get("reasons", [])[:3]) + ("" if c.get("domain") else "; its website is not known yet")
                                                      + ((". " + judge_mod.summary(c)) if c.get("ai") else "")
                                                      + (f". Possible Instagram @{c['instagram_possible']['handle']} ({c['instagram_possible']['confidence']:.2f} sure, not recorded)" if c.get("instagram_possible") else "")}
    if not sites:
        return tabs + ([{"title": TAB_COMPETITORS, "columns": COMPETITOR_COLUMNS, "rows_by_key": comp_rows, "key_col": "site_id", "starter_manual": ["Decision", "Notes"]}] if comp_rows else [])
    all_events = []
    for s in sites:
        evs = recent_events(snaps, [s["id"]], now, 30)
        all_events += [(s, e) for e in evs if e["type"] != "events_truncated"]
        _, cat = snaps.latest(s["id"], "catalogue")
        stale = [r for r in monitor.stale_report(snaps, s["id"], now, settings) if r["stale"]]
        sm = S.summary(cat) if cat else {}
        recent7 = [e for e in evs if (health.age_days(e["at"], now) or 99) <= 7 and e["type"] != "baseline_recorded"]
        kinds = snaps.kinds(s["id"])
        comp_rows[s["id"]] = {"site_id": s["id"], "name": s["name"], "domain": s["domain"], "status": "monitoring" if kinds else "waiting for first reading", "origin": s.get("origin", ""),
                              "platform": "shopify" if cat else ("" if not kinds else "website"), "products": sm.get("products"), "collections": sm.get("collections"),
                              "on_sale_products": sm.get("on_sale"), "events_7d": len(recent7), "instagram": s.get("instagram", ""), "instagram_confidence": (s.get("match") or {}).get("instagram_confidence"),
                              "match_sources": ", ".join((s.get("match") or {}).get("sources", [])), "request_controls": s.get("request_controls") or controls_summary(s.get("advanced") or {}), "tiktok": s.get("tiktok", ""), "youtube": s.get("youtube", ""), "last_read": max((snaps.entries(s["id"], k)[-1]["confirmed_at"] for k in kinds), default=""),
                              "stale": ", ".join(r["kind"] for r in stale), "note": " ".join(x for x in ["" if (cat or {}).get("complete", True) else "the last catalogue reading was incomplete.", s.get("ai_summary", "")] if x)}
        if cat:
            changed = {}
            for e in snaps.read_events(s["id"]):
                sub = e.get("subject", {})
                if sub.get("kind") == "product" and sub.get("id") is not None:
                    changed[sub["id"]] = max(changed.get(sub["id"], ""), e["at"])
            homes = {}
            for coll in cat.get("collections", []):
                for pid in coll["members"]:
                    homes.setdefault(pid, []).append(coll["handle"])
            for p in cat["products"][:cfg["max_products_per_competitor"]]:
                key = f"{s['id']}:{p['id']}"
                product_rows[key] = {"key": key, "competitor": s["name"], "title": p["title"], "price": p["price_min"], "compare_at_price": p["compare_at_max"], "on_sale": p["on_sale"],
                                     "available": p["available"], "collections": ", ".join(homes.get(p["id"], [])[:6]), "published": (p.get("published_at") or "")[:10],
                                     "last_change": changed.get(p["id"], "")[:10], "url": p["url"]}
    for s, e in sorted(all_events, key=lambda t: t[1]["at"], reverse=True)[:cfg["max_events"]]:
        if e["type"] == "baseline_recorded":
            continue
        event_rows[e["event_id"]] = {"event_id": e["event_id"], "at": e["at"], "competitor": s["name"], "severity": e["severity"], "type": e["type"], "summary": e["summary"],
                                     "url": _where(e), "suggested_action": suggested_action(e)}
    return tabs + [{"title": TAB_COMPETITORS, "columns": COMPETITOR_COLUMNS, "rows_by_key": comp_rows, "key_col": "site_id", "starter_manual": ["Decision", "Notes"]},
            {"title": TAB_EVENTS, "columns": EVENT_COLUMNS, "rows_by_key": event_rows, "key_col": "event_id", "starter_manual": ["Notes"]},
            {"title": TAB_PRODUCTS, "columns": PRODUCT_COLUMNS, "rows_by_key": product_rows, "key_col": "key", "starter_manual": ["Notes"]}]



def apply_sheet_decisions(values, store, slug, now, save=True):
    """Read the Decision column of the Competitors tab (all values, header first) and record a person's choices on discovery candidates: monitor / keep, stop / reject, reset.
    Competitors listed in a brand file are controlled by that file, not by the sheet. Returns how many choices changed."""
    from . import discovery as discovery_mod
    if not values or "Decision" not in values[0]:
        return 0
    head = values[0]
    col, dom, sid = head.index("Decision"), head.index("domain") if "domain" in head else -1, head.index("site_id") if "site_id" in head else -1
    decisions = {}
    for row in values[1:]:
        word = (row[col] if col < len(row) else "").strip()
        if not word:
            continue
        ref = (row[sid] if 0 <= sid < len(row) else "")
        ref = ref.removeprefix("candidate:") if ref.startswith("candidate:") else ((row[dom] if 0 <= dom < len(row) else "") or ref)
        decisions[ref] = word
    if not decisions:
        return 0
    book = read_discovery(store, slug)
    changed = discovery_mod.apply_decisions(book, decisions, now)
    if changed and save:
        write_discovery(store, slug, book, now)
    return changed
