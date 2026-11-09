"""Automatic competitor discovery: who else is in this brand's market, found from everything the tool already collects.

Inputs, strongest first: advertisers whose ads Claude judged relevant in the Idea Bank; benchmark brands; advertisers with long-running or
many-variant ads; Instagram and TikTok accounts that keep appearing with real engagement; sites that keep appearing in research searches;
brands named in Deep Research reports. Each candidate gets a score out of 100 with the reasons written out, so a person can see why.

Rules that keep it safe:
  * Marketplaces, social networks, review and coupon sites and the brand's own site are never candidates.
  * A candidate without a web address is kept, flagged 'needs a website'; the tool will not guess one. An address is only accepted when a
    search result's name matches the candidate.
  * A person's decision (monitor, or stop) is permanent: automatic runs never change it.
  * Automatic monitoring needs a high score, a reachable site and room under the cap; everything else waits for a person.
"""
import re
from collections import defaultdict

from .. import benchmarks as BM
from .. import health
from . import config as OC
from . import judge

NOISE_DOMAINS = frozenset("""
facebook.com fb.com instagram.com threads.net tiktok.com youtube.com youtu.be x.com twitter.com pinterest.com reddit.com linkedin.com snapchat.com whatsapp.com
amazon.com amazon.co.uk amazon.ca ebay.com etsy.com walmart.com target.com costco.com aliexpress.com temu.com shein.com alibaba.com faire.com wayfair.com kroger.com
wikipedia.org wikimedia.org britannica.com quora.com medium.com substack.com blogspot.com wordpress.com tumblr.com
trustpilot.com yelp.com bbb.org sitejabber.com reviews.io judge.me glassdoor.com indeed.com crunchbase.com zoominfo.com pitchbook.com similarweb.com
retailmenot.com simplycodes.com honey.com couponfollow.com slickdeals.net groupon.com rakuten.com
google.com bing.com apple.com microsoft.com play.google.com apps.apple.com github.com gitlab.com notion.so
nytimes.com washingtonpost.com forbes.com businessinsider.com cnn.com bbc.com theguardian.com buzzfeed.com huffpost.com
shopify.com myshopify.com squarespace.com wix.com bigcommerce.com klaviyo.com mailchimp.com
""".split())
GENERIC_NAMES = frozenset({"facebook", "instagram", "tiktok", "youtube", "unknown", "sponsored", "ad library", "meta", "google"})
STATUSES = ("candidate", "monitoring", "rejected", "weak")
_STOP = frozenset("the a an and of for to in on by with from our your you we it is are be at as this that shop store official brand co inc llc ltd company".split())


def norm(text):
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def tokens(text):
    return [t for t in re.findall(r"[a-z0-9]+", str(text or "").lower()) if t not in _STOP and len(t) > 1]


def domain_of(url_or_host):
    host = OC.host_of(url_or_host)
    return host.removeprefix("www.")


def is_noise(domain, own=()):
    d = domain_of(domain)
    return (not d) or any(d == n or d.endswith("." + n) for n in NOISE_DOMAINS) or any(d == o or d.endswith("." + o) for o in own if o)


def name_matches(name, domain, title=""):
    """A search result belongs to a named brand when the name appears in the site's name, or the name's words fill the result's title."""
    n, label = norm(name), norm(domain_of(domain).split(".")[0])
    if n and label:
        if label == n or (len(n) >= 5 and n in label) or (len(label) >= 5 and label in n):
            return True
    words = tokens(name)
    head = " ".join(tokens(title)[:6])
    # a name that is one short word is too easily found in an unrelated title: the site's own name has to resemble it as well
    if len(words) < 2 and not (label and (label.startswith(n) or n.startswith(label)) and min(len(n), len(label)) >= 4 and abs(len(n) - len(label)) <= 3):
        return False
    return len(n) >= 4 and all(w in head for w in words)


def own_domains(brand):
    return {domain_of(getattr(brand, "domain", ""))} - {""}


def brand_keywords(store, brand):
    """Words that describe the brand's market: from its automatic research seeds and its product types."""
    words = set()

    def collect(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                collect(v, k)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, str) and ("keyword" in key or key in ("hashtags", "topics")):
                    words.update(tokens(v))
                elif isinstance(v, (dict, list)):
                    collect(v, key)
    try:
        collect(store.read_auto_seeds(brand.slug) or {})
        latest = store.load_latest(brand.slug) or {}
        for rec in list((latest.get("products") or {}).values())[:300]:
            words.update(tokens(rec.get("product_type") or rec.get("category") or ""))
    except Exception:
        pass
    return words - _STOP


# ---- finding candidates -----------------------------------------------------------------------------------------------------------------------

def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_entities(store, brand, now):
    """{key: entity} from everything collected for the brand. An entity may have no domain yet."""
    own = own_domains(brand)
    ents = {}

    def get(name, domain=""):
        d = domain_of(domain)
        key = norm(name) if name else d.split(".")[0]
        if not key or norm(name) in {norm(g) for g in GENERIC_NAMES} or (d and is_noise(d, own)):
            return None
        e = ents.setdefault(key, {"key": key, "name": str(name or d).strip()[:80], "domain": d, "sources": set(), "signals": defaultdict(float), "texts": [], "instagram": "", "tiktok": "",
                                  "first_seen": now, "last_seen": ""})
        if d and not e["domain"]:
            e["domain"] = d
        return e

    bank = (store.read_ideabank(brand.slug) or {}).get("items", {})
    for item in bank.values():
        handle = (item.get("handle") or "").strip()
        if not handle or item.get("kind") == "own" or item.get("own_brand"):
            continue
        e = get(handle)
        if not e:
            continue
        e["sources"].add(item.get("platform") or "idea_bank")
        rel = item.get("relevance")
        if item.get("state") == "kept" and isinstance(rel, (int, float)):
            e["signals"]["kept_items"] += 1
            e["signals"]["relevance_sum"] += rel
        elif item.get("state") == "removed" and isinstance(rel, (int, float)):
            e["signals"]["removed_items"] += 1
        e["texts"].append(f"{item.get('title', '')} {item.get('text', '')}"[:300])
        if item.get("platform") == "instagram":
            e["instagram"] = e["instagram"] or handle.lstrip("@")
        if item.get("platform") == "tiktok":
            e["tiktok"] = e["tiktok"] or handle.lstrip("@")
    for batch in store.read_collected(brand.slug):               # saved as batches: {at, source, via, items: [...]}
        for rec in (batch.get("items") if isinstance(batch.get("items"), list) else [batch]):
            handle = (rec.get("handle") or "").strip()
            src = rec.get("source") or batch.get("source", "")
            if not handle:
                continue
            e = get(handle)
            if not e:
                continue
            e["sources"].add(src)
            if src == "meta_ads":
                e["signals"]["ads"] += 1
                e["signals"]["max_days"] = max(e["signals"]["max_days"], _num(rec.get("days_running")))
                e["signals"]["max_variants"] = max(e["signals"]["max_variants"], _num(rec.get("variants")))
            elif src in ("instagram", "tiktok"):
                e["signals"]["posts"] += 1
                e["signals"]["engagement"] = max(e["signals"]["engagement"], _num(rec.get("engagement_pct")))
                if src == "instagram":
                    e["instagram"] = e["instagram"] or handle.lstrip("@")
                else:
                    e["tiktok"] = e["tiktok"] or handle.lstrip("@")
            e["texts"].append(f"{rec.get('title', '')} {rec.get('text', '')}"[:300])
            e["last_seen"] = max(e["last_seen"], str(rec.get("last_seen") or batch.get("at") or ""))
    for b in BM.clean_state(store.read_benchmarks(brand.slug) or {})["items"].values():
        for nm in {b.get("name"), b.get("page_name")}:
            e = get(nm) if nm else None
            if e:
                e["sources"].add("benchmarks")
                e["signals"]["benchmark"] = 1
    seen_domains = defaultdict(lambda: {"queries": set(), "titles": []})
    for rec in store.read_searches(brand.slug):
        for item in rec.get("results", []):
            d = domain_of(item.get("url", ""))
            if not d or is_noise(d, own) or int(item.get("rank") or 99) > 6:
                continue
            seen_domains[d]["queries"].add(rec.get("query", ""))
            seen_domains[d]["titles"].append(f"{item.get('title', '')} {item.get('snippet', '')}"[:300])
    for d, info in seen_domains.items():
        e = get(d.split(".")[0].replace("-", " "), d)
        if e:
            e["sources"].add("web_search")
            e["signals"]["search_queries"] = max(e["signals"]["search_queries"], len(info["queries"]))
            e["texts"].extend(info["titles"][:3])
    return {k: v for k, v in ents.items()}


# ---- scoring ----------------------------------------------------------------------------------------------------------------------------------

def score_entity(e, keywords):
    """(score 0-100, reasons) from the evidence, by a fixed rubric."""
    s, why = 0.0, []
    sig = e["signals"]
    if sig.get("kept_items"):
        avg = sig["relevance_sum"] / sig["kept_items"]
        pts = min(40, 25 + 5 * (sig["kept_items"] - 1)) + avg / 100 * 20
        s += pts
        why.append(f"{int(sig['kept_items'])} of its items were judged relevant for this brand (average relevance {avg:.0f})")
    elif sig.get("removed_items"):
        s -= 15
        why.append("its items were judged not relevant")
    if sig.get("benchmark"):
        s += 25
        why.append("it is one of the brand's benchmark names")
    if sig.get("max_days", 0) >= 90:
        s += 15
        why.append(f"an ad of theirs has run {int(sig['max_days'])} days (longevity is a proxy for what works, not a measurement)")
    elif sig.get("max_days", 0) >= 30:
        s += 10
        why.append(f"an ad of theirs has run {int(sig['max_days'])} days")
    if sig.get("max_variants", 0) >= 3:
        s += 5
        why.append(f"{int(sig['max_variants'])} variants of an ad were found")
    if sig.get("posts") and sig.get("engagement", 0) > 0:
        s += 10
        why.append("its social posts were collected with measurable engagement")
    if sig.get("search_queries", 0) >= 2:
        s += 15
        why.append(f"it appears for {int(sig['search_queries'])} different research searches")
    elif sig.get("search_queries", 0) == 1:
        s += 8
        why.append("it appears in a research search")
    real_sources = {x for x in e["sources"] if x}
    if len(real_sources) >= 2:
        s += 10
        why.append("it shows up in several places: " + ", ".join(sorted(real_sources)))
    if keywords:
        text_words = set(tokens(" ".join(e["texts"])))
        overlap = len(text_words & keywords) / max(1, min(len(keywords), 12))
        pts = min(20.0, overlap * 20)
        if pts >= 2:
            s += pts
            why.append(f"its wording overlaps the brand's market words ({', '.join(sorted(text_words & keywords)[:5])})")
    return int(max(0, min(100, round(s)))), why


# ---- the book: decisions persist --------------------------------------------------------------------------------------------------------------

def apply_decisions(book, decisions, now):
    """Record a person's choices from the sheet: {key or domain: 'monitor'|'stop'|'keep'|''}. A person's choice is never overridden afterwards."""
    changed = 0
    by_key = {c["key"]: c for c in book["candidates"]}
    by_domain = {c["domain"]: c for c in book["candidates"] if c.get("domain")}
    for ref, text in decisions.items():
        c = by_key.get(ref) or by_domain.get(domain_of(ref))
        word = str(text or "").strip().lower()
        if not c or word not in ("monitor", "keep", "stop", "reject", "reset"):
            continue
        if word == "reset":                           # hand the choice back to the rubric
            if c.get("decided_by") == "person":
                c.update(status="candidate", decided_by="", decided_at=now)
                changed += 1
            continue
        status = "monitoring" if word in ("monitor", "keep") else "rejected"
        if c["status"] != status or c.get("decided_by") != "person":
            c.update(status=status, decided_by="person", decided_at=now)
            changed += 1
    return changed


def merge(book, entities, scores, settings, now, *, probes=None, judged=None, monitored_elsewhere=0):
    """Fold fresh findings into the book. Returns the list of changes made, in words."""
    cfg = settings["discovery"]
    probes, judged = probes or {}, judged or {}
    notes = []
    by_key = {c["key"]: c for c in book["candidates"]}
    for key, e in entities.items():
        score, why = scores[key]
        c = by_key.get(key)
        if c is None:
            c = by_key[key] = {"key": key, "name": e["name"], "domain": e["domain"], "status": "weak", "decided_by": "", "first_seen": now, "sources": [], "reasons": [], "signals": {},
                               "instagram": "", "tiktok": "", "shopify": None, "shopify_note": "", "ai": None, "score": 0}
            book["candidates"].append(c)
            notes.append(f"found {e['name']} (score {score})")
        c.update(name=c["name"] or e["name"], score=score, reasons=why, sources=sorted(x for x in e["sources"] if x), last_seen=now,
                 signals={k: round(v, 1) for k, v in e["signals"].items()})
        if e["domain"] and not c["domain"]:
            c["domain"] = e["domain"]
        c["instagram"] = c["instagram"] or e["instagram"]
        c["tiktok"] = c["tiktok"] or e["tiktok"]
        for extra in ("instagram_confidence", "instagram_evidence", "instagram_possible", "instagram_checked_at", "domain_confidence", "domain_evidence"):
            if extra in e:                                   # who this is and how sure we are, kept with the sources that said so
                c[extra] = e[extra]
        if key in probes:
            c["shopify"], c["shopify_note"] = probes[key]
        if key in judged and (c.get("ai") or {}).get("review") != "approved":      # an approved verdict stands until a person approves a newer one
            c["ai"] = judged[key]
    watching = monitored_elsewhere + sum(1 for c in book["candidates"] if c["status"] == "monitoring")
    auto = sum(1 for c in book["candidates"] if c["status"] == "monitoring" and c.get("decided_by") == "auto")
    for c in sorted(book["candidates"], key=lambda c: -c["score"]):
        if c.get("decided_by") == "person":
            continue                                                  # a person's choice is final
        blocked = judge.blocks_auto_add(c)                          # a confident, not-rejected "not a competitor" from the AI judge (it can stop an addition, never cause one)
        if blocked and c["status"] != "monitoring":
            c["status"] = "weak"
        elif c["status"] == "monitoring" and c.get("decided_by") == "auto":
            continue                                                  # earlier automatic choices stand until a person changes them
        elif c["score"] < cfg["min_score"]:
            c["status"] = "weak"
        else:
            c["status"] = "candidate"
            if (cfg["auto_monitor_max"] and c["score"] >= cfg["auto_monitor_score"] and c["domain"] and c.get("shopify") is not None and not blocked
                    and auto < cfg["auto_monitor_max"] and watching < cfg["max_monitored"]):
                c.update(status="monitoring", decided_by="auto", decided_at=now)
                auto += 1
                watching += 1
                notes.append(f"{c['name']} is now monitored automatically (score {c['score']})")
    book["candidates"].sort(key=lambda c: (-c["score"], c["key"]))
    return notes


def rows_for_sheet(book):
    """Candidate rows for the Competitors tab (those not yet being watched through a file)."""
    out = {}
    for c in book["candidates"]:
        if c["status"] == "weak":
            continue
        out[c["key"]] = {"site_id": c["key"], "name": c["name"], "domain": c["domain"] or "(needs a website)", "status": c["status"], "origin": "discovery",
                         "platform": {True: "shopify", False: "website"}.get(c.get("shopify"), ""), "note": f"score {c['score']}: " + "; ".join(c["reasons"][:3])}
    return out


def needs_lookup(book, entities, settings, limit):
    """Names worth looking up a website for: scored, no domain yet, strongest first, at most `limit`."""
    known = {c["key"]: c for c in book["candidates"]}
    names = []
    for key, e in entities.items():
        have = known.get(key) or {}
        if e["domain"] or have.get("domain") or have.get("lookup_failed_at"):
            continue
        names.append((key, e["name"]))
    return names[:limit] if limit else []


def pick_domain(name, results, own=()):
    """The first search result that is a plausible site for the name, as (domain, title), else None."""
    for r in results:
        d = domain_of(r.get("url", ""))
        if d and not is_noise(d, own) and name_matches(name, d, r.get("title", "")):
            return d, r.get("title", "")
    return None


def age_label(ts, now):
    age = health.age_days(ts, now) if ts else None
    return "never" if age is None else f"{age:.0f} days ago"
