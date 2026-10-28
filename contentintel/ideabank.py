"""The Idea Bank: everything sourced that is worth a person's review, scored and summarised, waiting for a decision.

Every ad, social post or web result a run collects can become a row here with an id (IB-0001, IB-0002, ...). The tool fills in
the columns it owns (what it is, its numbers, a relevance score, a summary, how the brand could use it, any risk) and saves the
item's pictures and videos in the brand's Shared Drive folder, in a folder named like the row (IB-0001 - topic). The sheet row
holds the link to that folder. A person then fills in the Decision column (Use, Maybe or Skip); only rows marked Use go into a brief,
and the brief reads the saved files, so nothing has to be searched for again.

Nothing in the bank is ever deleted by the tool, and a row is never created twice for the same source item.
"""
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .collectors import collected_rows
from .drive import DriveError, folder_url, item_folder_name, safe_name
from .export import price_range
from .providers import ProviderError

KINDS = {"meta_ads": "ad", "instagram": "social post", "tiktok": "social post", "web": "web result"}
DECISION_USE = {"use", "yes", "y", "approved", "approve", "✓", "✔", "✅"}
DECISION_MAYBE = {"maybe", "?", "unsure"}
DECISION_SKIP = {"skip", "no", "n", "reject", "rejected", "pass"}
ASSET_TRIES = 3
TEXT_LIMIT = 500
SCORE_FIELDS = ("relevance", "topic", "summary", "how_to_use", "risk")


class IdeaBankError(Exception):
    pass


@dataclass(frozen=True)
class IdeaBankSettings:
    enabled: bool = True
    sources: tuple = ("meta_ads", "instagram", "tiktok")
    max_new_per_run: int = 60
    score_batch: int = 8
    max_scoring_calls: int = 12
    save_assets: bool = True
    save_videos: bool = True
    image_max_mb: int = 15
    video_max_mb: int = 80
    claude_monthly_calls: int = 300
    min_relevance: int = 60
    skip_own_brand: bool = True
    # planner (request mode)
    plan_max_keywords: int = 6
    plan_max_hashtags: int = 6
    plan_max_queries: int = 8
    plan_max_questions: int = 2
    plan_max_pages: int = 4
    # briefs
    brief_models: tuple = ("claude-sonnet-5-5",)
    brief_max_concepts: int = 8
    brief_max_calls: int = 8
    brief_images_per_item: int = 1
    extra: dict = field(default_factory=dict)


_IDEABANK_KEYS = {"enabled", "sources", "max_new_per_run", "score_batch", "max_scoring_calls", "save_assets", "save_videos",
                  "image_max_mb", "video_max_mb", "claude_monthly_calls", "min_relevance", "skip_own_brand"}
_PLANNER_KEYS = {"max_keywords", "max_hashtags", "max_queries", "max_questions", "max_pages"}
_BRIEF_KEYS = {"models", "max_concepts", "max_calls", "images_per_item"}
_SOURCES = ("meta_ads", "instagram", "tiktok")


def _whole(value, where, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise IdeaBankError(f"{where} must be a whole number of at least {minimum}, got {value!r}")
    return value


def _flag(value, where):
    if not isinstance(value, bool):
        raise IdeaBankError(f"{where} must be true or false, got {value!r}")
    return value


def _only(given, allowed, where):
    if not isinstance(given, dict):
        raise IdeaBankError(f"{where} must be name: value pairs")
    extra = sorted(set(given) - set(allowed))
    if extra:
        raise IdeaBankError(f"{where}: unknown {', '.join(map(repr, extra))} (allowed: {', '.join(sorted(allowed))})")
    return given


def load_settings(path="research.yaml"):
    """The ideabank, planner and brief sections of research.yaml (every one optional)."""
    data = {}
    path = Path(path)
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bank = _only(data.get("ideabank") or {}, _IDEABANK_KEYS, "research.yaml ideabank")
    planner = _only(data.get("planner") or {}, _PLANNER_KEYS, "research.yaml planner")
    brief = _only(data.get("brief") or {}, _BRIEF_KEYS, "research.yaml brief")
    base = IdeaBankSettings()
    kw = {}
    if "enabled" in bank:
        kw["enabled"] = _flag(bank["enabled"], "ideabank.enabled")
    if "sources" in bank:
        srcs = bank["sources"]
        if not isinstance(srcs, list) or not srcs or any(s not in _SOURCES for s in srcs):
            raise IdeaBankError(f"ideabank.sources must be a list of: {', '.join(_SOURCES)}")
        kw["sources"] = tuple(dict.fromkeys(srcs))
    for key, minimum in (("max_new_per_run", 1), ("score_batch", 1), ("max_scoring_calls", 0), ("image_max_mb", 1), ("video_max_mb", 1),
                         ("claude_monthly_calls", 0)):
        if key in bank:
            kw[key] = _whole(bank[key], f"ideabank.{key}", minimum)
    if "min_relevance" in bank:
        kw["min_relevance"] = _whole(bank["min_relevance"], "ideabank.min_relevance", 0)
        if kw["min_relevance"] > 100:
            raise IdeaBankError(f"ideabank.min_relevance must be between 0 and 100, got {bank['min_relevance']!r}")
    for key in ("save_assets", "save_videos", "skip_own_brand"):
        if key in bank:
            kw[key] = _flag(bank[key], f"ideabank.{key}")
    for key in _PLANNER_KEYS:
        if key in planner:
            kw[f"plan_{key}"] = _whole(planner[key], f"planner.{key}", 1)
    if "models" in brief:
        models = brief["models"]
        if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m.strip() for m in models):
            raise IdeaBankError("brief.models must be a list of model names, for example [claude-sonnet-5-5]")
        kw["brief_models"] = tuple(dict.fromkeys(m.strip() for m in models))
    for key, minimum in (("max_concepts", 1), ("max_calls", 1), ("images_per_item", 0)):
        if key in brief:
            kw[f"brief_{key}"] = _whole(brief[key], f"brief.{key}", minimum)
    return IdeaBankSettings(**{**base.__dict__, **kw})


# ---------------------------------------------------------------------------------------------------------------------
# a shared budget for Claude calls (scoring, planning, reviewing, briefs)
# ---------------------------------------------------------------------------------------------------------------------

class ClaudeBudget:
    """Claude calls made by these features in a calendar month, counted in data/_shared/claude_usage.json."""

    def __init__(self, store, now, monthly_cap):
        self.store, self.month, self.cap = store, now[:7], monthly_cap
        usage = store.read_claude_usage()
        self.used = int(usage.get(self.month, 0)) if isinstance(usage.get(self.month, 0), int) else 0

    @property
    def left(self):
        return max(0, self.cap - self.used)

    def take(self, n=1):
        """True and counted if `n` calls are still allowed this month; False (and nothing counted) otherwise."""
        if n > self.left:
            return False
        self.used += n
        usage = self.store.read_claude_usage()
        usage[self.month] = self.used
        self.store.write_claude_usage(usage)
        return True


# ---------------------------------------------------------------------------------------------------------------------
# intake
# ---------------------------------------------------------------------------------------------------------------------

def _clip(text, limit=TEXT_LIMIT):
    return " ".join(str(text or "").split())[:limit]


def _metrics(row):
    keys = ("likes", "comments", "shares", "views", "saves", "reach", "days_running", "variants", "engagement_pct")
    out = {k: row.get(k) for k in keys if row.get(k) not in (None, "")}
    if row.get("signal"):
        out["signal"] = row["signal"]
    return out


def _new_item(idea_id, source, source_item_id, row, now, request):
    return {"id": idea_id, "source_item_id": source_item_id, "kind": KINDS.get(source, "web result"), "platform": source,
            "handle": _clip(row.get("handle"), 80), "title": _clip(row.get("title"), 200), "text": _clip(row.get("text")),
            "url": row.get("url") or "", "posted_at": row.get("posted_at") or "", "metrics": _metrics(row),
            "media_url": row.get("media_url") or "", "page_id": row.get("page_id") or "", "region": (request or {}).get("region", ""),
            "request_id": (request or {}).get("id", ""), "occasion": (request or {}).get("occasion", ""), "added_at": now,
            "topic": "", "relevance": None, "score": None, "summary": "", "how_to_use": "", "risk": "", "scored_at": "", "model": "",
            "drive_folder_id": "", "drive_folder_url": "", "assets": [], "asset_note": "", "asset_tries": 0,
            "state": "new", "removed_reason": "", "duplicate_of": "", "removal_note": "", "fingerprint": ""}


def _interest(row):
    """A sort key: the items most worth a place first (a performance signal, then reach, then how long an ad ran)."""
    def num(key):
        value = row.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    return (1 if row.get("signal") else 0, num("views") + num("likes") * 10, num("days_running"))


def state_of(item):
    """'kept' (scored and relevant: shown and used), 'removed' (a copy, the brand's own, or not relevant), or 'new' (not scored yet)."""
    if item.get("state") in ("kept", "removed", "new"):
        return item["state"]
    return "kept" if item.get("score") is not None else "new"


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def fingerprint(item):
    """What makes two items the same idea, whatever their ids: an ad is its text (or headline, if the text is short), a web result is
    its address, a social post is its account plus its words. An item with nothing to compare is never merged with another."""
    body, head = _norm(item["text"]), _norm(item["title"])
    if item["kind"] == "ad":
        base = body if len(body) >= 20 else head
    elif item["kind"] == "web result":
        base = re.sub(r"^https?://(www\.)?", "", str(item["url"] or "").lower()).split("#")[0].split("?")[0].rstrip("/")
    else:
        base = f"{_norm(item['handle'])}|{body or head}" if (body or head) else ""
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16] if base else ""


def pick_original(items):
    """The item to keep out of copies of one idea: the longest-running ad, else the oldest post, else the first one added."""
    return min(items, key=lambda i: (-(i["metrics"].get("days_running") or 0), i["posted_at"] or "9999", i["id"]))


def normalise_bank(bank):
    """Give every item the fields newer code needs. Older banks had no state, page id or fingerprint."""
    for item in bank["items"].values():
        item["state"] = state_of(item)
        for key, default in (("page_id", ""), ("removed_reason", ""), ("duplicate_of", ""), ("removal_note", "")):
            item.setdefault(key, default)
        if not item.get("fingerprint"):
            item["fingerprint"] = fingerprint(item)
    return bank


def own_names(brand):
    """The spellings of the brand's own name: 'swoveralls', 'defi snacks' and the same without the space."""
    raw = {_norm(brand.name), _norm(brand.slug), _norm(str(brand.domain).split(".")[0])}
    return {n for n in raw | {n.replace(" ", "") for n in raw} if len(n) >= 4}


def own_reason(brand, own_pages, item):
    """Why this item is the brand's own content (so it is not an idea to take from someone else), or ''."""
    names = own_names(brand)
    handle = _norm(item["handle"]).replace(" ", "")
    if handle and any(n.replace(" ", "") in handle for n in names):
        return "it comes from the brand's own account"
    if item.get("page_id") and item["page_id"] in set(own_pages):
        return "it comes from the brand's own Meta page"
    domain = str(brand.domain or "").lower()
    if domain and domain in str(item["url"] or "").lower():
        return "it is a page on the brand's own site"
    blob = f" {_norm(item['title'])} {_norm(item['text'])} "
    if any(f" {n} " in blob for n in names):
        return "it names the brand itself"
    return ""


def own_page_ids(store, slug):
    state = store.read_benchmarks(slug) or {}
    return [p.get("page_id") for p in state.get("pages", []) if p.get("kind") == "own" and p.get("page_id")]


def intake_collected(bank, records, settings, now, request=None, since="", brand=None, own_pages=(), skipped=None):
    """Add the collected ads and posts that are not yet in the bank. Returns the new ids, newest sourced first.
    Items of the brand's own are left out, and so are copies: of an item already in the bank (whatever its state, so a removed copy
    never comes back) and of each other (the original is kept). `skipped` is filled with how many of each were left out."""
    skipped = skipped if skipped is not None else {}
    skipped.setdefault("own", 0)
    skipped.setdefault("duplicate", 0)
    skipped.setdefault("refreshed", 0)
    known = {item["source_item_id"] for item in bank["items"].values()}
    seen = {item["fingerprint"] for item in bank["items"].values() if item.get("fingerprint")}
    rows = collected_rows([r for r in records if r.get("at", "") >= since])
    by_source = {item["source_item_id"]: item for item in bank["items"].values()}
    for iid, row in rows.items():                # an ad collected again has a fresh picture link: use it for an item that has no file yet
        item = by_source.get(iid)
        if item and item["state"] != "removed" and not item["assets"] and row.get("media_url") and row["media_url"] != item["media_url"]:
            item.update({"media_url": row["media_url"], "asset_tries": 0, "asset_note": ""})
            skipped["refreshed"] += 1
    drafts = []
    for iid, row in rows.items():
        if row["source"] not in settings.sources or iid in known:
            continue
        draft = _new_item("", row["source"], iid, row, now, request)
        if brand is not None and settings.skip_own_brand and own_reason(brand, own_pages, draft):
            skipped["own"] += 1
            continue
        draft["fingerprint"] = fingerprint(draft)
        drafts.append((iid, row, draft))
    groups = {}
    for entry in drafts:
        groups.setdefault(entry[2]["fingerprint"] or f"unique:{entry[0]}", []).append(entry)
    fresh = []
    for fp, group in groups.items():
        skipped["duplicate"] += len(group) - 1
        if fp in seen:
            skipped["duplicate"] += 1
            continue
        fresh.append(min(group, key=lambda e: (-(e[2]["metrics"].get("days_running") or 0), e[2]["posted_at"] or "9999")))
    fresh.sort(key=lambda e: (e[1].get("last_seen") or "", _interest(e[1])), reverse=True)
    new = []
    for iid, row, draft in fresh[:settings.max_new_per_run]:
        idea_id = f"IB-{bank['next']:04d}"
        bank["next"] += 1
        draft["id"] = idea_id
        bank["items"][idea_id] = draft
        new.append(idea_id)
    return new


def intake_web(bank, sources, settings, now, request=None, brand=None, own_pages=(), skipped=None):
    """Add web results (articles, TikTok and Instagram pages found by search) that are not yet in the bank, leaving out the brand's own
    pages and copies of items already there. `sources` is the Research tab's rows:
    {source_id: {title, url, snippet, published, domain, ...}}."""
    skipped = skipped if skipped is not None else {}
    skipped.setdefault("own", 0)
    skipped.setdefault("duplicate", 0)
    known = {item["source_item_id"] for item in bank["items"].values()}
    seen = {item["fingerprint"] for item in bank["items"].values() if item.get("fingerprint")}
    new = []
    for sid, row in sources.items():
        key = f"web:{sid}"
        if key in known or not row.get("url") or len(new) >= settings.max_new_per_run:
            continue
        item = _new_item("", "web", key, {"title": row.get("title"), "text": row.get("snippet"), "url": row.get("url"),
                                           "posted_at": row.get("published"), "handle": row.get("domain")}, now, request)
        if brand is not None and settings.skip_own_brand and own_reason(brand, own_pages, item):
            skipped["own"] += 1
            continue
        item["fingerprint"] = fingerprint(item)
        if item["fingerprint"] in seen:
            skipped["duplicate"] += 1
            continue
        idea_id = f"IB-{bank['next']:04d}"
        bank["next"] += 1
        item["id"] = idea_id
        bank["items"][idea_id] = item
        seen.add(item["fingerprint"])
        new.append(idea_id)
    return new


# ---------------------------------------------------------------------------------------------------------------------
# files in Drive
# ---------------------------------------------------------------------------------------------------------------------

class AssetSkip(Exception):
    """A file that was not saved, and why (worded for the sheet)."""


_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
        "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}


def fetch_media(get, url, settings):
    """Download one picture or video. `get` is requests.get-like. Returns (bytes, mime, kind) or raises AssetSkip."""
    if not str(url).startswith("https://"):
        raise AssetSkip("no https address for the file")
    try:
        resp = get(url, timeout=60, stream=True, headers={"User-Agent": "Mozilla/5.0"})
    except Exception as exc:                                    # network trouble of any kind is a reason, not a crash
        raise AssetSkip(f"could not be downloaded ({exc.__class__.__name__})") from exc
    if resp.status_code >= 400:
        raise AssetSkip(f"the site answered {resp.status_code} (the link may have expired)")
    mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    kind = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else ""
    if not kind or mime not in _EXT:
        raise AssetSkip(f"not a picture or video ({mime or 'unknown type'})")
    if kind == "video" and not settings.save_videos:
        raise AssetSkip("a video, and saving videos is switched off")
    limit = (settings.video_max_mb if kind == "video" else settings.image_max_mb) * 1024 * 1024
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise AssetSkip(f"a {kind} bigger than {limit // (1024 * 1024)} MB")
    data = b"".join(resp.iter_content(1024 * 256)) if hasattr(resp, "iter_content") else resp.content
    if len(data) > limit:
        raise AssetSkip(f"a {kind} bigger than {limit // (1024 * 1024)} MB")
    if not data:
        raise AssetSkip("an empty file")
    return data, mime, kind


def _parse_time(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def link_expiry(url):
    """When a Meta picture or video link stops working, read from its `oe=` part (hexadecimal seconds), or None if it has none.
    These links last only hours or days, so a file can only be saved soon after the ad was collected."""
    found = re.search(r"[?&]oe=([0-9a-fA-F]{8})(?:&|$)", str(url or ""))
    if not found:
        return None
    try:
        return datetime.fromtimestamp(int(found.group(1), 16), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def needs_assets(item):
    """Only items that were judged relevant get a folder: nothing is saved for an item that is waiting to be scored or has been removed."""
    return state_of(item) == "kept" and bool(item["media_url"]) and not item["assets"] and item["asset_tries"] < ASSET_TRIES


def save_assets(bank, ids, brand_name, drive, get, settings, brand_folder=None, now=None):
    """Save the picture or video of each given item in its own Drive folder, and note the folder on the item.
    A file that is byte for byte the same as one already saved for another kept item is not saved again: that item is a copy and is removed.
    Returns (saved, lines). An item with nothing to save gets no folder."""
    lines, saved, expired = [], 0, 0
    clock = _parse_time(now) or datetime.now(timezone.utc)
    folder_of_brand = brand_folder
    hashes = {a["sha256"]: i for i, it in bank["items"].items() if state_of(it) == "kept" for a in it["assets"] if a.get("sha256")}
    for idea_id in ids:
        item = bank["items"][idea_id]
        if not needs_assets(item):
            continue
        ends = link_expiry(item["media_url"])
        if ends and ends <= clock:               # not tried and not counted as a try: a fresh link from a new collection starts again
            item["asset_note"] = f"the link expired on {ends:%Y-%m-%d}; the file is saved if the ad is collected again"
            expired += 1
            continue
        item["asset_tries"] += 1
        try:
            data, mime, kind = fetch_media(get, item["media_url"], settings)
        except AssetSkip as exc:
            item["asset_note"] = str(exc)
            lines.append(f"{idea_id}: no file saved: {exc}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        twin = hashes.get(digest)
        if twin and twin != idea_id:
            item.update({"state": "removed", "duplicate_of": twin, "removed_reason": f"a copy of {twin} (the very same file)"})
            lines.append(f"{idea_id}: removed, it is a copy of {twin} (the very same file)")
            continue
        try:
            folder_of_brand = folder_of_brand or drive.brand_folder(brand_name)
            if not item["topic"]:
                item["topic"] = safe_name(item["title"] or item["handle"] or item["kind"], 40)
            folder = item["drive_folder_id"] or drive.item_folder(folder_of_brand, idea_id, item["topic"])
            name = f"{idea_id}-{kind}.{_EXT[mime]}"
            existing = drive.list_files(folder)
            file_id = existing.get(name) or drive.upload(folder, name, data, mime)
        except DriveError as exc:
            item["asset_note"] = f"not saved in Drive: {exc}"
            lines.append(f"{idea_id}: not saved in Drive: {exc}")
            raise
        item.update({"drive_folder_id": folder, "drive_folder_url": folder_url(folder), "asset_note": ""})
        item["assets"].append({"name": name, "drive_id": file_id, "kind": kind, "bytes": len(data), "sha256": digest})
        hashes[digest] = idea_id
        saved += 1
    if expired:
        lines.append(f"{expired} item(s): the picture or video link has expired (Meta's links last only hours or days), so no file was saved; "
                     "it is saved if the ad is collected again")
    return saved, lines


# ---------------------------------------------------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------------------------------------------------

SCORE_SYSTEM = (
    "You help a marketing agency decide which sourced content is worth building a creative brief on. For each numbered item "
    "decide how useful it is for THIS brand and, if a request is given, for THAT request. Items come from competitors, creators, "
    "influencers and the web; judge the idea, not who made it. Reply with JSON only: {\"items\": [{\"id\": \"IB-0001\", "
    "\"relevance\": 0-100, \"topic\": \"3 to 6 words naming the idea\", \"summary\": \"under 30 words: what it is and why it works\", "
    "\"how_to_use\": \"under 40 words: how WE could use the idea for this brand, in our own words and with our own products\", "
    "\"risk\": \"under 20 words: a claim, legal, copyright or brand-fit concern, or the word none\"}]}. Score 80 to 100 only for ideas "
    "that clearly fit the brand and the request; 40 to 79 for usable with changes; under 40 for weak or off-brand. Use only the "
    "facts given. Never invent numbers, quotes or results.")


def brand_context(brand, store):
    """What the brand sells, from its catalogue: enough for a relevance judgement, short enough to send every time."""
    latest = store.load_latest(brand.slug) or {}
    products = [p for p in (latest.get("products") or {}).values() if isinstance(p, dict) and not p.get("excluded")]
    cats = {}
    for p in products:
        cat = p.get("category") or "other"
        cats.setdefault(cat, []).append(p)
    parts = [f"Brand: {brand.name} ({brand.domain})."]
    if cats:
        pieces = []
        for cat, plist in sorted(cats.items(), key=lambda kv: -len(kv[1])):
            lows = [price_range(p)[0] for p in plist if price_range(p)[0] is not None]
            highs = [price_range(p)[1] for p in plist if price_range(p)[1] is not None]
            span = f" ${min(lows):g}-${max(highs):g}" if lows else ""
            pieces.append(f"{len(plist)} {cat}{span}")
        parts.append("Catalogue: " + "; ".join(pieces) + ".")
        names = [p.get("title") for p in sorted(products, key=lambda p: -len(p.get("variants") or [])) if p.get("title")][:8]
        if names:
            parts.append("Example products: " + "; ".join(names) + ".")
    return " ".join(parts)


def _item_line(item):
    m = item["metrics"]
    facts = ", ".join(f"{k} {v}" for k, v in m.items())
    return (f'{item["id"]} [{item["kind"]}, {item["platform"]}] by {item["handle"] or "unknown"}: "{item["title"]}" - '
            f'{item["text"][:300]}' + (f" ({facts})" if facts else ""))


def clean_scores(raw, wanted_ids):
    """The valid scores in a model's answer, {id: {relevance, topic, summary, how_to_use, risk}}; anything malformed is left out."""
    out = {}
    items = raw.get("items") if isinstance(raw, dict) else raw
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict) or entry.get("id") not in wanted_ids:
            continue
        rel = entry.get("relevance")
        if isinstance(rel, bool) or not isinstance(rel, (int, float)) or not 0 <= rel <= 100:
            continue
        texts = {k: _clip(entry.get(k), 240) for k in ("topic", "summary", "how_to_use", "risk")}
        if not texts["summary"]:
            continue
        out[entry["id"]] = {"relevance": int(round(rel)), **texts}
    return out


def score_items(bank, ids, claude, brand, store, settings, budget, now, request=None):
    """Score unscored items in batches, most interesting first, within the call limits. Returns (scored count, lines)."""
    todo = [i for i in ids if bank["items"][i]["score"] is None and state_of(bank["items"][i]) != "removed"]
    todo.sort(key=lambda i: _interest(bank["items"][i]["metrics"]), reverse=True)
    context = brand_context(brand, store)
    ask = (f"\nRequest: {request['text']}" + (f" (occasion: {request['occasion']})" if request.get("occasion") else "")) if request else ""
    scored, lines, calls = 0, [], 0
    for start in range(0, len(todo), settings.score_batch):
        if calls >= settings.max_scoring_calls:
            lines.append(f"{len(todo) - start} item(s) left unscored: this run's limit of {settings.max_scoring_calls} scoring calls is reached")
            break
        if not budget.take():
            lines.append(f"{len(todo) - start} item(s) left unscored: the monthly Claude limit ({budget.cap} calls) is reached")
            break
        batch = todo[start:start + settings.score_batch]
        calls += 1
        user = f"{context}{ask}\n\nItems:\n" + "\n".join(_item_line(bank["items"][i]) for i in batch)
        try:
            got = clean_scores(claude.json_task(SCORE_SYSTEM, user, max_tokens=2500), set(batch))
        except ProviderError as exc:
            lines.append(f"{len(batch)} item(s) not scored: {exc}")
            continue
        for idea_id, s in got.items():
            item = bank["items"][idea_id]
            item.update({"relevance": s["relevance"], "score": s["relevance"], "summary": s["summary"], "how_to_use": s["how_to_use"],
                         "risk": s["risk"], "scored_at": now, "model": getattr(claude, "model", "")})
            if s["topic"] and not item["drive_folder_id"]:
                item["topic"] = s["topic"]
            scored += 1
        if len(got) < len(batch):
            lines.append(f"{len(batch) - len(got)} item(s) in a batch came back unusable and stay unscored")
    return scored, lines


# ---------------------------------------------------------------------------------------------------------------------
# keeping the bank clean: relevance, copies, the brand's own content, and the folders of what was removed
# ---------------------------------------------------------------------------------------------------------------------

def _remove(item, reason, duplicate_of=""):
    item.update({"state": "removed", "removed_reason": reason, "duplicate_of": duplicate_of})


def apply_relevance(bank, settings):
    """A scored item is kept if it scored at least the minimum and removed otherwise (so raising the minimum later tidies the bank).
    Returns how many were removed now."""
    removed = 0
    for item in bank["items"].values():
        if item["state"] == "removed" or item["score"] is None:
            continue
        if item["score"] >= settings.min_relevance:
            item["state"] = "kept"
        else:
            _remove(item, f"not relevant enough (scored {item['score']}, the minimum is {settings.min_relevance})")
            removed += 1
    return removed


def revive_relevant(bank, settings):
    """An item removed only for a low score comes back when the minimum is lowered to its score (a copy or the brand's own never does).
    Its Drive folder was trashed, so it gets a new one if its picture link still works. Returns how many came back."""
    back = 0
    for item in bank["items"].values():
        if (item["state"] == "removed" and item["score"] is not None and item["score"] >= settings.min_relevance
                and item["removed_reason"].startswith("not relevant enough")):
            item.update({"state": "kept", "removed_reason": "", "removal_note": ""})
            back += 1
    return back


def relevance_bands(scores, minimum):
    """How far below the minimum the removed items scored, so the minimum can be chosen with the facts in front of you."""
    near = sum(1 for sc in scores if minimum - 10 <= sc < minimum)
    middle = sum(1 for sc in scores if minimum - 20 <= sc < minimum - 10)
    far = len(scores) - near - middle
    return (f"{len(scores)} item(s) were not relevant enough: {near} scored {max(0, minimum - 10)}-{minimum - 1}, "
            f"{middle} scored {max(0, minimum - 20)}-{max(0, minimum - 11)}, {far} scored lower. To keep more, lower ideabank.min_relevance in "
            f"research.yaml and run tidy again: items removed for a low score come back when they reach the new minimum")


def tidy_duplicates(bank):
    """Keep one original of every idea that appears more than once and remove the copies. Returns how many were removed."""
    groups = {}
    for item in bank["items"].values():
        if item["state"] != "removed" and item["fingerprint"]:
            groups.setdefault(item["fingerprint"], []).append(item)
    removed = 0
    for group in groups.values():
        if len(group) > 1:
            original = pick_original(group)
            for item in group:
                if item is not original:
                    _remove(item, f"a copy of {original['id']}", original["id"])
                    removed += 1
    return removed


def tidy_own(bank, brand, own_pages, settings):
    """Remove items that turn out to be the brand's own content (a Meta page found later, say). Returns how many."""
    if not settings.skip_own_brand:
        return 0
    removed = 0
    for item in bank["items"].values():
        if item["state"] != "removed":
            why = own_reason(brand, own_pages, item)
            if why:
                _remove(item, f"the brand's own content: {why}")
                removed += 1
    return removed


def trash_folders(bank, drive):
    """Move the Drive folders of removed items to the trash. A folder that cannot be moved (the service account lacks the Content manager
    role) is noted on its item and tried again next run. Returns (moved, lines)."""
    moved, stuck, reason = 0, [], ""
    for item in bank["items"].values():
        if item["state"] != "removed" or not item["drive_folder_id"]:
            continue
        try:
            drive.trash(item["drive_folder_id"])
        except DriveError as exc:
            item["removal_note"], reason = f"the folder could not be moved to the trash: {exc}", str(exc)
            stuck.append(item_folder_name(item["id"], item["topic"]))
            continue
        item.update({"drive_folder_id": "", "drive_folder_url": "", "assets": [], "removal_note": ""})
        moved += 1
    lines = []
    if stuck:
        lines.append(f"{len(stuck)} folder(s) of removed items could not be moved to the Drive trash ({reason}) Give the service account the Content "
                     f"manager role in the Shared Drive, or delete them by hand: {'; '.join(stuck[:6])}" + (" ..." if len(stuck) > 6 else ""))
    return moved, lines


# ---------------------------------------------------------------------------------------------------------------------
# the sheet
# ---------------------------------------------------------------------------------------------------------------------

IDEABANK_COLUMNS = ["idea_id", "kind", "platform", "title", "handle", "url", "posted_at", "score", "summary", "how_to_use", "risk",
                    "topic", "region", "request_id", "likes", "comments", "shares", "views", "days_running", "signal",
                    "drive_folder", "assets", "asset_note", "added_at", "scored_at"]


def sheet_rows(bank):
    rows = {}
    for idea_id, item in bank["items"].items():
        if state_of(item) != "kept":                    # waiting to be scored, or removed: not shown
            continue
        m = item["metrics"]
        rows[idea_id] = {"idea_id": idea_id, "kind": item["kind"], "platform": item["platform"], "title": item["title"],
                         "handle": item["handle"], "url": item["url"], "posted_at": item["posted_at"],
                         "score": item["score"], "summary": item["summary"],
                         "how_to_use": item["how_to_use"], "risk": item["risk"], "topic": item["topic"], "region": item["region"],
                         "request_id": item["request_id"], "likes": m.get("likes", ""), "comments": m.get("comments", ""),
                         "shares": m.get("shares", ""), "views": m.get("views", ""), "days_running": m.get("days_running", ""),
                         "signal": m.get("signal", ""), "drive_folder": item["drive_folder_url"], "assets": len(item["assets"]),
                         "asset_note": item["asset_note"], "added_at": item["added_at"], "scored_at": item["scored_at"]}
    return rows


def normalise_decision(text):
    """'use', 'maybe', 'skip' or '' from whatever a person typed in the Decision column."""
    word = str(text or "").strip().lower()
    if word in DECISION_USE:
        return "use"
    if word in DECISION_MAYBE:
        return "maybe"
    if word in DECISION_SKIP:
        return "skip"
    return ""


def read_decisions(worksheet):
    """{idea_id: {"decision": use|maybe|skip|"", "notes": text}} from the Idea Bank tab. The tool never writes these columns."""
    values = worksheet.get_all_values()
    if not values:
        return {}
    header = [str(h).strip().lower() for h in values[0]]
    if "idea_id" not in header or "decision" not in header:
        raise IdeaBankError("the Idea Bank tab has no idea_id and Decision columns; run the Sheets update first")
    i_id, i_dec = header.index("idea_id"), header.index("decision")
    i_notes = header.index("notes") if "notes" in header else None
    out = {}
    for row in values[1:]:
        idea_id = str(row[i_id]).strip() if len(row) > i_id else ""
        if re.fullmatch(r"IB-\d{4,}", idea_id):
            out[idea_id] = {"decision": normalise_decision(row[i_dec] if len(row) > i_dec else ""),
                            "notes": str(row[i_notes]).strip() if i_notes is not None and len(row) > i_notes else ""}
    return out


# ---------------------------------------------------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------------------------------------------------

def _counts(bank):
    out = {"new": 0, "kept": 0, "removed": 0}
    for item in bank["items"].values():
        out[state_of(item)] += 1
    return out


def run_ideabank(brand, store, *, settings, now, claude=None, drive=None, get=None, request=None, web_sources=None,
                 dry_run=False, budget=None, since=""):
    """Take new sourced items into the brand's bank, keep only what is useful, save its files and score it. Returns (new ids, summary lines).

    In order: leave out the brand's own content and copies; tidy what is already in the bank the same way; score what is not scored; keep
    items that scored at least `min_relevance` and remove the rest; save pictures and videos only for the kept ones; move the Drive folders
    of removed items to the trash. Missing helpers are not errors: without Drive the files are skipped, without Claude items wait unscored."""
    lines = []
    bank = normalise_bank(store.read_ideabank(brand.slug))
    pages = own_page_ids(store, brand.slug)
    skipped = {}
    new = intake_collected(bank, store.read_collected(brand.slug), settings, now, request, since, brand, pages, skipped)
    if web_sources:
        new += intake_web(bank, web_sources, settings, now, request, brand, pages, skipped)
    left_out = [f"{skipped['own']} of the brand's own" if skipped["own"] else "", f"{skipped['duplicate']} cop(ies)" if skipped["duplicate"] else ""]
    lines.append(f"{brand.slug}: {len(new)} new item(s) taken in" + (f" (left out {' and '.join(x for x in left_out if x)})" if any(left_out) else ""))
    if skipped["refreshed"]:
        lines.append(f"{brand.slug}: {skipped['refreshed']} picture or video link(s) were refreshed from this collection, for items with no file yet")
    if dry_run:
        return new, lines
    before = _counts(bank)
    was_removed = {i for i, it in bank["items"].items() if it["state"] == "removed"}
    own = tidy_own(bank, brand, pages, settings)
    copies = tidy_duplicates(bank)                           # before scoring, so no Claude call is spent on a copy
    if claude is not None and budget is not None:
        unscored = [i for i, it in bank["items"].items() if it["score"] is None and it["state"] != "removed"]
        scored, notes = score_items(bank, unscored, claude, brand, store, settings, budget, now, request)
        lines += notes
        lines.append(f"{brand.slug}: {scored} item(s) scored")
    back = revive_relevant(bank, settings)
    irrelevant = apply_relevance(bank, settings)
    weak = [it["score"] for i, it in bank["items"].items()
            if it["state"] == "removed" and i not in was_removed and it["removed_reason"].startswith("not relevant enough")]
    if back:
        lines.append(f"{brand.slug}: {back} item(s) removed earlier for a low score reach the minimum now and were brought back")
    if weak:
        lines.append(f"{brand.slug}: {relevance_bands(weak, settings.min_relevance)}")
    copies += tidy_duplicates(bank)                          # and again among the items that were kept
    waiting = sum(1 for it in bank["items"].values() if state_of(it) == "new")
    if waiting:
        lines.append(f"{brand.slug}: {waiting} item(s) wait to be scored before they are shown or get a folder"
                     + ("" if claude is not None else " (no Claude key)"))
    retry = [i for i, it in bank["items"].items() if needs_assets(it)]
    try:
        if settings.save_assets and drive is not None and get is not None:
            saved, notes = save_assets(bank, retry, brand.name, drive, get, settings, now=now)
            lines += notes
            lines.append(f"{brand.slug}: files saved for {saved} item(s)")
        elif settings.save_assets and retry:
            lines.append(f"{brand.slug}: files not saved (no Drive folder is set up: add the IDEA_BANK_FOLDER_ID secret)")
    except DriveError as exc:
        lines.append(f"{brand.slug}: Drive stopped the saving of files: {exc}")
    if drive is not None:
        try:
            moved, notes = trash_folders(bank, drive)
            lines += notes
            if moved:
                lines.append(f"{brand.slug}: {moved} folder(s) of removed items moved to the Drive trash")
        except DriveError as exc:
            lines.append(f"{brand.slug}: Drive stopped the tidy-up of folders: {exc}")
    after = _counts(bank)
    removed_now = after["removed"] - before["removed"]
    if removed_now:
        lines.append(f"{brand.slug}: {removed_now} item(s) removed ({own} the brand's own, {copies} cop(ies), {irrelevant} not relevant enough); "
                     f"{after['kept']} kept")
    store.write_ideabank(brand.slug, bank)
    return new, lines
