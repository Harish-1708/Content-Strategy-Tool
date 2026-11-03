"""The shared evidence record: one versioned shape for everything the tool collects.

An ad, a post, a search result and a research report all become the same kind of record, so every later
stage (the Idea Bank, the competitor observatory, opportunity scoring, briefs, the studio, learning from
results) reads one thing and can always say where a fact came from, how it was reached, how complete it
is and how old it is.

Records are *projected* from what the tool already saves; nothing stored is changed. A collected batch
carries one small `provenance` block (route, access tier, access type, source health when it was
captured); the full per-item record is built from it on demand.

The honesty rules are checked, not just written down:
  * a search snippet is labelled snippet_only, never as page content;
  * an item known only from links is labelled links_only;
  * a cover picture is labelled cover_only and is never a video;
  * a proxy (days running, variant count) is labelled a proxy, never a measurement;
  * old data is labelled stale, with its age.
"""
import hashlib
import json
import re

from . import sources as sources_mod
from .health import age_days
from .safety import clean_text

SCHEMA_VERSION = 1
COMPLETENESS = ("observed", "partial", "snippet_only", "links_only", "cover_only", "unknown")
MEDIA_STATUS = ("linked", "saved", "cover_only", "none", "gone", "blocked", "failed")
METRIC_KINDS = ("measured", "proxy")
CONFIDENCE = ("high", "medium", "low")
REVIEW_STATUS = ("unreviewed", "reviewed", "approved", "excluded")

# what a good reading of each source normally includes; an item missing some of these is "partial"
EXPECTED = {
    "meta_ads": ("title", "text", "url", "posted_at", "media_url", "days_running"),
    "tiktok": ("text", "url", "posted_at", "media_url", "likes", "views"),
    "instagram": ("text", "url", "posted_at", "media_url", "likes"),
    "deep_research": ("title", "text", "url"),
    "web_search": ("title", "url", "snippet"),
}

# (field on a collected item, metric name, unit, kind)
_ITEM_METRICS = (("likes", "likes", "count", "measured"), ("comments", "comments", "count", "measured"),
                 ("shares", "shares", "count", "measured"), ("views", "views", "count", "measured"),
                 ("saves", "saves", "count", "measured"), ("reach", "reach", "count", "measured"),
                 ("days_running", "days_running", "days", "proxy"), ("variants", "variant_count", "count", "proxy"),
                 ("page_likes", "page_followers", "count", "measured"))
_PLATFORM = {"meta_ads": "meta", "tiktok": "tiktok", "instagram": "instagram", "deep_research": "gemini", "web_search": "web"}
_VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif")
_NOTE = "third-party content, kept for reference and analysis; never reproduced in client work"


def _short_hash(*parts, n=16):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:n]


def canonical(url):
    """The address without tracking codes and fragments, lower-cased where that is safe (what two sightings are compared by)."""
    if not str(url or "").strip():
        return ""
    from .research import canonical_url          # local import: research imports the collectors
    try:
        return canonical_url(str(url))
    except ValueError:
        return str(url).strip()


def content_hash(title, text):
    body = " ".join(clean_text(f"{title or ''} {text or ''}").lower().split())
    return _short_hash(body, n=20) if len(body) >= 20 else ""


def evidence_id(source, key):
    return "ev1-" + _short_hash(source, key)


def _media_kind(url):
    low = str(url or "").lower().split("?")[0]
    return "video" if low.endswith(_VIDEO_EXT) else "image" if low.endswith(_IMAGE_EXT) else "unknown"


def provenance(registry, source, route, now, run="", health_status=""):
    """The small block saved with each collected batch. Never raises: an undeclared route is recorded as unknown, and validation says so."""
    first = str(route).split("+")[0]
    try:
        rt = registry.route(source, first)
        tier, access = rt.tier, rt.access_type
    except sources_mod.SourcesError:
        tier, access = "unknown", "unknown"
    return {"schema": SCHEMA_VERSION, "route": route, "access_tier": tier, "access_type": access,
            "collector": f"{source}.{route}", "captured_at": now, "run": run, "source_health": health_status}


def _blank_analysis():
    return {"formula": "", "structure_tags": [], "scores": {}, "confidence": None, "evidence_refs": [], "model": "", "provider": "",
            "prompt_hash": "", "rule_version": "", "validators": [], "human_decision": ""}


def _metrics(source, item, captured, platform):
    out = []
    for field, name, unit, kind in _ITEM_METRICS:
        value = item.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        out.append({"name": name, "value": value, "unit": unit, "denominator": None, "window": "cumulative at capture",
                    "captured_at": captured, "platform": platform, "source": source, "kind": kind})
    return out


def _stale(registry, source, captured, now):
    age = age_days(captured, now)
    try:
        limit = registry.source(source).stale_after_days
    except sources_mod.SourcesError:
        limit = registry.stale_after_days
    return (age is not None and age > limit), (None if age is None else round(age, 1))


def _limitations(registry, source, route, record):
    notes = []
    try:
        notes.extend(registry.source(source).limitations[:2])
    except sources_mod.SourcesError:
        pass
    if record.get("partial"):
        notes.append("the run stopped before every page was read")
    if "+" in str(route):
        notes.append("merged from two routes")
    return notes


def _assemble(*, source, platform, item_id, key_url, url, title, text, creator, handle_context, route, tier, access_type, captured,
              first_seen, last_seen, times_seen, metrics, media_url, observed, missing, completeness, limitations, health_status,
              found_via, registry, now):
    stale, age = _stale(registry, source, captured, now)
    media = []
    if media_url:
        kind = _media_kind(media_url)
        media.append({"kind": kind, "url": media_url, "status": "cover_only" if completeness == "cover_only" else "linked"})
    confidence = {"site_data": "high", "feed": "high", "page_links": "low"}.get(access_type, "medium")
    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": evidence_id(source, canonical(key_url) or item_id),
        "origin": {"source": source, "platform": platform, "canonical_url": canonical(key_url), "url": url, "collector": f"{source}.{route}",
                   "route": route, "access_tier": tier, "access_type": access_type, "captured_at": captured,
                   "account": handle_context, "found_via": found_via},
        "identity": {"item_id": item_id, "content_hash": content_hash(title, text), "url_hash": _short_hash(canonical(key_url)) if canonical(key_url) else "",
                     "creator": creator, "parent_id": "", "variant_of": "", "duplicate_cluster": "", "first_seen": first_seen,
                     "last_seen": last_seen, "times_seen": times_seen},
        "content": {"title": title, "text": text, "transcript": "", "claims": [], "media": media,
                    "media_status": media[0]["status"] if media else "none", "language": "", "format": "", "market": ""},
        "quality": {"completeness": {"status": completeness, "observed": observed, "missing": missing}, "stale": stale, "age_days": age,
                    "parse_confidence": confidence, "source_health": health_status, "limitations": limitations},
        "metrics": metrics,
        "analysis": _blank_analysis(),
        "governance": {"retention_class": "research", "data_class": "public", "provenance": f"{platform} via {route}",
                       "licence_note": _NOTE, "review_status": "unreviewed", "downstream_restrictions": []},
    }


# ---- collected ads, posts and reports --------------------------------------------------------------------------------------------------------

def _sightings(records):
    """item_id -> (first_seen, last_seen, times_seen) over every saved batch."""
    seen = {}
    for rec in records:
        for item in rec.get("items", []):
            iid = item.get("item_id")
            if not iid:
                continue
            first, last, n = seen.get(iid, (rec["at"], rec["at"], 0))
            seen[iid] = (min(first, rec["at"]), max(last, rec["at"]), n + 1)
    return seen


def evidence_from_collected(records, registry, now):
    """One evidence record per distinct item across a brand's saved collection batches (newest sighting wins)."""
    seen = _sightings(records)
    latest = {}
    for rec in sorted(records, key=lambda r: r.get("at", "")):
        for item in rec.get("items", []):
            iid = item.get("item_id")
            if not iid:
                continue
            merged = dict(latest[iid][1]) if iid in latest else {}
            merged.update({k: v for k, v in item.items() if v not in (None, "")})
            latest[iid] = (rec, merged)
    out = []
    for iid, (rec, item) in latest.items():
        source = item.get("source") or rec.get("source") or "unknown"
        prov = rec.get("provenance") or {}
        route = prov.get("route") or rec.get("via") or "unknown"
        links_only = not any(item.get(k) not in (None, "") for k in ("text", "title", "likes", "comments", "views", "days_running", "posted_at"))
        tier = prov.get("access_tier") or _declared(registry, source, route, "tier")
        access = "page_links" if links_only and source != "deep_research" else (prov.get("access_type") or _declared(registry, source, route, "access_type"))
        expected = EXPECTED.get(source, ())
        observed = [f for f in expected if item.get(f) not in (None, "")]
        missing = [f for f in expected if item.get(f) in (None, "")]
        completeness = "links_only" if access == "page_links" else ("observed" if expected and not missing else "partial" if expected else "unknown")
        first, last, n = seen[iid]
        out.append(_assemble(
            source=source, platform=_PLATFORM.get(source, source), item_id=iid, key_url=item.get("url") or "", url=item.get("url") or "",
            title=clean_text(item.get("title") or ""), text=clean_text(item.get("text") or ""), creator=item.get("handle") or "",
            handle_context=item.get("handle") or "", route=route, tier=tier, access_type=access, captured=prov.get("captured_at") or rec.get("at", ""),
            first_seen=first, last_seen=last, times_seen=n, metrics=_metrics(source, item, rec.get("at", ""), _PLATFORM.get(source, source)),
            media_url=item.get("media_url") or "", observed=observed, missing=missing, completeness=completeness,
            limitations=_limitations(registry, source, route, rec), health_status=prov.get("source_health", ""),
            found_via=str(item.get("found_via") or rec.get("seeds") or ""), registry=registry, now=now))
    return out


def _declared(registry, source, route, field):
    try:
        return getattr(registry.route(source, str(route).split("+")[0]), field)
    except sources_mod.SourcesError:
        return "unknown"


# ---- search results ----------------------------------------------------------------------------------------------------------------------------

def evidence_from_searches(records, registry, now):
    """One evidence record per distinct page that appeared in a brand's saved searches. Always snippet_only: a result is never the page."""
    pages = {}
    for rec in sorted(records, key=lambda r: r.get("at", "")):
        provider = rec.get("provider")
        if not provider:
            continue
        for res in rec.get("results", []):
            url = res.get("url") or ""
            key = canonical(url)
            if not key:
                continue
            page = pages.setdefault(key, {"first": rec["at"], "last": rec["at"], "n": 0})
            page.update({"last": rec["at"], "res": res, "provider": provider, "query": rec.get("query", ""), "at": rec["at"], "kind": rec.get("kind", "")})
            page["n"] += 1
    out = []
    for key, page in pages.items():
        res = page["res"]
        observed = [f for f in ("title", "url", "snippet") if res.get(f)]
        missing = [f for f in ("title", "url", "snippet") if not res.get(f)]
        item = _assemble(
            source="web_search", platform="web", item_id=f"web_search:{_short_hash(key, n=12)}", key_url=res.get("url") or "", url=res.get("url") or "",
            title=clean_text(res.get("title") or ""), text=clean_text(res.get("snippet") or ""), creator="", handle_context="",
            route=page["provider"], tier=_declared(registry, "web_search", page["provider"], "tier"),
            access_type="search_snippet", captured=page["at"], first_seen=page["first"], last_seen=page["last"], times_seen=page["n"],
            metrics=[], media_url="", observed=observed, missing=missing + ["page_text"], completeness="snippet_only",
            limitations=_limitations(registry, "web_search", page["provider"], {}), health_status="", found_via=page["query"],
            registry=registry, now=now)
        item["origin"]["published"] = res.get("date") or ""
        out.append(item)
    return out


# ---- duplicates ------------------------------------------------------------------------------------------------------------------------------

def assign_clusters(records):
    """Mark items that are the same thing (same address, or the same wording) with one duplicate cluster id (the smallest evidence id)."""
    parent = {r["evidence_id"]: r["evidence_id"] for r in records}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_key = {}
    for r in records:
        for key in (("url", r["identity"]["url_hash"]), ("text", r["identity"]["content_hash"])):
            if key[1]:
                if key in by_key:
                    union(by_key[key], r["evidence_id"])
                else:
                    by_key[key] = r["evidence_id"]
    roots = {}
    for r in records:
        roots.setdefault(find(r["evidence_id"]), []).append(r)
    for root, group in roots.items():
        for r in group:
            r["identity"]["duplicate_cluster"] = root if len(group) > 1 else ""
    return records


def build_all(store, slug, registry, now):
    """Every evidence record for a brand: collected items and search results, with duplicate clusters assigned."""
    records = evidence_from_collected(store.read_collected(slug), registry, now) + evidence_from_searches(store.read_searches(slug), registry, now)
    return assign_clusters(records)


# ---- validation ------------------------------------------------------------------------------------------------------------------------------

_SECTIONS = ("origin", "identity", "content", "quality", "metrics", "analysis", "governance")


def validate_evidence(rec, registry=None, now=None):
    """Problems with one evidence record; an empty list means it keeps the contract and its honesty rules."""
    problems = []
    if not isinstance(rec, dict):
        return ["the record is not an object"]
    if rec.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version is {rec.get('schema_version')!r}, not {SCHEMA_VERSION}")
    if not re.fullmatch(r"ev1-[0-9a-f]{16}", str(rec.get("evidence_id", ""))):
        problems.append("evidence_id is missing or malformed")
    for name in _SECTIONS:
        if name not in rec:
            problems.append(f"missing section '{name}'")
    if problems:
        return problems
    origin, ident, content, quality = rec["origin"], rec["identity"], rec["content"], rec["quality"]
    for key in ("source", "platform", "route", "access_tier", "access_type", "captured_at"):
        if not origin.get(key):
            problems.append(f"origin.{key} is empty")
    if origin.get("access_tier") not in sources_mod.TIERS:
        problems.append(f"origin.access_tier {origin.get('access_tier')!r} is not a known tier")
    if origin.get("access_type") not in sources_mod.ACCESS_TYPES:
        problems.append(f"origin.access_type {origin.get('access_type')!r} is not a known access type")
    for key in ("item_id", "first_seen", "last_seen"):
        if not ident.get(key):
            problems.append(f"identity.{key} is empty")
    if ident.get("first_seen", "") > ident.get("last_seen", ""):
        problems.append("identity.first_seen is after last_seen")
    status = quality.get("completeness", {}).get("status")
    if status not in COMPLETENESS:
        problems.append(f"quality.completeness.status {status!r} is not allowed")
    access = origin.get("access_type")
    if access == "search_snippet" and status != "snippet_only":
        problems.append("a search snippet must be labelled snippet_only, never as page content")
    if access == "page_links" and status != "links_only":
        problems.append("an item known only from links must be labelled links_only")
    if content.get("media_status") not in MEDIA_STATUS:
        problems.append(f"content.media_status {content.get('media_status')!r} is not allowed")
    for m in content.get("media", []):
        if m.get("status") not in MEDIA_STATUS:
            problems.append(f"a media entry has status {m.get('status')!r}")
        if m.get("status") == "cover_only" and m.get("kind") != "image":
            problems.append("a cover picture must be an image, never a video")
        if m.get("status") == "saved" and m.get("kind") == "unknown":
            problems.append("saved media must say whether it is a picture or a video")
    if quality.get("parse_confidence") not in CONFIDENCE:
        problems.append("quality.parse_confidence must be high, medium or low")
    if not isinstance(rec["metrics"], list):
        problems.append("metrics must be a list")
    else:
        for m in rec["metrics"]:
            if isinstance(m.get("value"), bool) or not isinstance(m.get("value"), (int, float)):
                problems.append(f"metric {m.get('name')!r} has no numeric value")
            for key in ("name", "unit", "window", "captured_at", "platform", "source"):
                if not m.get(key):
                    problems.append(f"metric {m.get('name')!r} lacks {key}")
            if "denominator" not in m:
                problems.append(f"metric {m.get('name')!r} lacks a denominator (null is fine for plain counts)")
            if m.get("kind") not in METRIC_KINDS:
                problems.append(f"metric {m.get('name')!r} must be measured or proxy")
            if m.get("name") in ("days_running", "variant_count") and m.get("kind") != "proxy":
                problems.append(f"{m.get('name')} is a proxy and must be labelled one")
            if m.get("unit") in ("percent", "rate", "ratio") and m.get("denominator") in (None, ""):
                problems.append(f"metric {m.get('name')!r} is a rate and needs its denominator")
    if rec["governance"].get("review_status") not in REVIEW_STATUS:
        problems.append("governance.review_status is not allowed")
    if registry is not None and now is not None and origin.get("captured_at"):
        stale, _ = _stale(registry, origin.get("source", ""), origin["captured_at"], now)
        if stale != quality.get("stale"):
            problems.append("quality.stale does not match the age of the data")
    return problems


def summary(records):
    """Counts for the log: by source, access tier, access type, completeness, staleness and duplicates."""
    def count(getter):
        out = {}
        for r in records:
            k = getter(r)
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items()))
    return {"total": len(records), "by_source": count(lambda r: r["origin"]["source"]), "by_tier": count(lambda r: r["origin"]["access_tier"]),
            "by_access_type": count(lambda r: r["origin"]["access_type"]), "by_completeness": count(lambda r: r["quality"]["completeness"]["status"]),
            "stale": sum(1 for r in records if r["quality"]["stale"]),
            "in_duplicate_clusters": sum(1 for r in records if r["identity"]["duplicate_cluster"])}


def export_jsonl(records):
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)


# ---- the contract as a JSON Schema, for components that are not Python (the studio, the editor) -----------------------------------------------

_STR = {"type": "string"}


def _enum(values):
    return {"type": "string", "enum": list(values)}


EVIDENCE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "evidence.v1.schema.json",
    "title": "Evidence record, version 1",
    "type": "object",
    "required": ["schema_version", "evidence_id", *_SECTIONS],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "evidence_id": {"type": "string", "pattern": "^ev1-[0-9a-f]{16}$"},
        "origin": {"type": "object", "required": ["source", "platform", "route", "access_tier", "access_type", "captured_at"],
                   "properties": {"source": _STR, "platform": _STR, "route": _STR, "canonical_url": _STR, "url": _STR, "collector": _STR,
                                  "access_tier": _enum(sources_mod.TIERS), "access_type": _enum(sources_mod.ACCESS_TYPES), "captured_at": _STR}},
        "identity": {"type": "object", "required": ["item_id", "content_hash", "url_hash", "duplicate_cluster", "first_seen", "last_seen", "times_seen"],
                     "properties": {"item_id": _STR, "content_hash": _STR, "url_hash": _STR, "duplicate_cluster": _STR,
                                    "first_seen": _STR, "last_seen": _STR, "times_seen": {"type": "integer", "minimum": 1}}},
        "content": {"type": "object", "required": ["title", "text", "media", "media_status"],
                    "properties": {"title": _STR, "text": _STR, "transcript": _STR, "claims": {"type": "array"},
                                   "media_status": _enum(MEDIA_STATUS),
                                   "media": {"type": "array", "items": {"type": "object", "required": ["kind", "url", "status"],
                                                                        "properties": {"kind": _enum(("image", "video", "unknown")), "url": _STR,
                                                                                       "status": _enum(MEDIA_STATUS)}}}}},
        "quality": {"type": "object", "required": ["completeness", "stale", "age_days", "parse_confidence", "limitations"],
                    "properties": {"completeness": {"type": "object", "required": ["status", "observed", "missing"],
                                                    "properties": {"status": _enum(COMPLETENESS), "observed": {"type": "array", "items": _STR},
                                                                   "missing": {"type": "array", "items": _STR}}},
                                   "stale": {"type": "boolean"}, "age_days": {"type": ["number", "null"]},
                                   "parse_confidence": _enum(CONFIDENCE), "limitations": {"type": "array", "items": _STR}}},
        "metrics": {"type": "array", "items": {"type": "object", "required": ["name", "value", "unit", "denominator", "window", "captured_at", "platform", "source", "kind"],
                                               "properties": {"name": _STR, "value": {"type": "number"}, "unit": _STR,
                                                              "denominator": {"type": ["number", "string", "null"]}, "window": _STR,
                                                              "captured_at": _STR, "platform": _STR, "source": _STR, "kind": _enum(METRIC_KINDS)}}},
        "analysis": {"type": "object"},
        "governance": {"type": "object", "required": ["retention_class", "data_class", "review_status"],
                       "properties": {"data_class": _enum(("public", "internal", "restricted")), "review_status": _enum(REVIEW_STATUS)}},
    },
}
