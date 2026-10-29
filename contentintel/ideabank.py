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
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .collectors import collected_rows
from . import mediacheck
from .drive import DriveError, DriveMismatch, folder_url, item_folder_name, safe_name
from .videofetch import classify_failure, cookie_file, platform_of, video_page
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
    max_scoring_calls: int = 16
    save_assets: bool = True
    save_videos: bool = True
    image_max_mb: int = 15
    video_max_mb: int = 80
    claude_monthly_calls: int = 300
    min_relevance: int = 50
    fetch_videos: bool = True
    max_per_technique: int = 2
    refresh_links: bool = True
    link_refreshes_per_run: int = 10
    vision: bool = True
    vision_calls_per_run: int = 3
    vision_batch: int = 4
    enrich_calls_per_run: int = 4
    enrich_batch: int = 10
    cover_lookups_per_run: int = 20
    video_fetches_per_run: int = 12
    video_timeout_seconds: int = 150
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
                  "image_max_mb", "video_max_mb", "claude_monthly_calls", "min_relevance", "skip_own_brand", "fetch_videos", "video_fetches_per_run", "video_timeout_seconds", "max_per_technique", "refresh_links", "link_refreshes_per_run", "vision", "vision_calls_per_run", "vision_batch",
                    "enrich_calls_per_run", "enrich_batch", "cover_lookups_per_run"}
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
    for key, minimum in (("max_new_per_run", 1), ("score_batch", 1), ("max_scoring_calls", 0), ("video_fetches_per_run", 0), ("video_timeout_seconds", 10), ("max_per_technique", 0), ("link_refreshes_per_run", 0), ("vision_calls_per_run", 0), ("vision_batch", 1),
                           ("enrich_calls_per_run", 0), ("enrich_batch", 1), ("cover_lookups_per_run", 0), ("image_max_mb", 1), ("video_max_mb", 1),
                         ("claude_monthly_calls", 0)):
        if key in bank:
            kw[key] = _whole(bank[key], f"ideabank.{key}", minimum)
    if "min_relevance" in bank:
        kw["min_relevance"] = _whole(bank["min_relevance"], "ideabank.min_relevance", 0)
        if kw["min_relevance"] > 100:
            raise IdeaBankError(f"ideabank.min_relevance must be between 0 and 100, got {bank['min_relevance']!r}")
    for key in ("save_assets", "save_videos", "skip_own_brand", "fetch_videos", "vision", "refresh_links"):
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


def _media_format(details):
    """Meta's own label for an ad (IMAGE, VIDEO, CAROUSEL...) from the details text of a collected ad, or ''."""
    found = re.search(r"format:\s*([A-Za-z_]+)", str(details or ""))
    return found.group(1).upper() if found else ""


def _new_item(idea_id, source, source_item_id, row, now, request):
    return {"id": idea_id, "source_item_id": source_item_id, "kind": KINDS.get(source, "web result"), "platform": source,
            "handle": _clip(row.get("handle"), 80), "title": _clip(row.get("title"), 200), "text": _clip(row.get("text")),
            "url": row.get("url") or "", "posted_at": row.get("posted_at") or "", "metrics": _metrics(row),
            "media_url": row.get("media_url") or "", "page_id": row.get("page_id") or "", "region": (request or {}).get("region", ""),
            "request_id": (request or {}).get("id", ""), "occasion": (request or {}).get("occasion", ""), "added_at": now,
            "topic": "", "relevance": None, "score": None, "summary": "", "how_to_use": "", "risk": "", "scored_at": "", "model": "",
            "drive_folder_id": "", "drive_folder_url": "", "assets": [], "asset_note": "", "asset_tries": 0,
            "state": "new", "removed_reason": "", "duplicate_of": "", "removal_note": "", "fingerprint": "",
            "fit": None, "idea": None, "mechanism": "", "concept": "", "generic": False, "category": "", "scoring_version": 0,
            "format": "", "media_format": _media_format(row.get("details")), "video_tries": 0, "video_note": "", "video_status": "", "technique": "", "hook_line": "", "first_test": "", "audience": "", "channel": "",
            "visual_hook": "", "look": "", "visual_quality": "", "visual_tries": 0, "media_role": "", "rescued": False, "link_refreshes": 0}


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
        for key, default in (("page_id", ""), ("removed_reason", ""), ("duplicate_of", ""), ("removal_note", ""), ("fit", None), ("idea", None),
                             ("mechanism", ""), ("concept", ""), ("generic", False), ("category", ""), ("format", ""), ("media_format", ""), ("video_tries", 0), ("video_note", ""), ("video_status", ""),
                           ("technique", ""), ("hook_line", ""), ("first_test", ""), ("audience", ""), ("channel", ""), ("visual_hook", ""), ("look", ""), ("visual_quality", ""), ("visual_tries", 0), ("media_role", ""), ("rescued", False), ("link_refreshes", 0)):
            item.setdefault(key, default)
        item.setdefault("scoring_version", 1 if item.get("score") is not None else 0)       # scored before the concept-first rubric = version 1
        for asset in item["assets"]:                    # files saved before they were checked: what they are is known, that they are whole is not
            asset.setdefault("kind", "image")
            asset.setdefault("source", "cover" if item["platform"] in COVER_PLATFORMS and asset["kind"] == "image" else "media")
            asset.setdefault("verified", False)
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


def fetch_media_checked(get, url, settings):
    """Download one picture or video and prove it is whole. Returns (bytes, MediaCheck) or raises AssetSkip with the reason.
    The real type comes from the bytes, not from what the server said. A download that is interrupted or arrives short is tried once more,
    because a dropped connection is common and the second attempt usually works."""
    if not str(url).startswith("https://"):
        raise AssetSkip("no https address for the file")
    last = ""
    for attempt in (1, 2):
        try:
            resp = get(url, timeout=60, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        except Exception as exc:                                    # network trouble of any kind is a reason, not a crash
            raise AssetSkip(f"could not be downloaded ({exc.__class__.__name__})") from exc
        if resp.status_code >= 400:
            raise AssetSkip(f"the site answered {resp.status_code} (the link may have expired)")
        mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        hinted = "image" if mime.startswith("image/") else "video" if mime.startswith("video/") else ""
        if not hinted or mime not in _EXT:
            raise AssetSkip(f"not a picture or video ({mime or 'unknown type'})")
        if hinted == "video" and not settings.save_videos:
            raise AssetSkip("a video, and saving videos is switched off")
        limit = (settings.video_max_mb if hinted == "video" else settings.image_max_mb) * 1024 * 1024
        declared = resp.headers.get("Content-Length")
        declared = int(declared) if declared and declared.isdigit() else None
        if declared and declared > limit:
            raise AssetSkip(f"a {hinted} bigger than {limit // (1024 * 1024)} MB")
        try:
            data = b"".join(resp.iter_content(1024 * 256)) if hasattr(resp, "iter_content") else resp.content
        except Exception as exc:                                    # a connection that drops half way through the body
            last = f"the download was interrupted ({exc.__class__.__name__})"
            continue
        if len(data) > limit:
            raise AssetSkip(f"a {hinted} bigger than {limit // (1024 * 1024)} MB")
        check = mediacheck.check_media(data, declared_length=declared)
        short = [p for p in check.problems if "cut short" in p]
        if short and attempt == 1:
            last = short[0]
            continue
        break
    else:
        raise AssetSkip(last)
    if check.problems:
        raise AssetSkip("; ".join(check.problems))
    if check.kind == "video" and not settings.save_videos:
        raise AssetSkip("a video, and saving videos is switched off")
    return data, check


def fetch_media(get, url, settings):
    """As fetch_media_checked, returning (bytes, mime, kind) with the type read from the bytes."""
    data, check = fetch_media_checked(get, url, settings)
    return data, check.mime, check.kind


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


COVER_PLATFORMS = ("tiktok", "instagram")


def _ensure_item_folder(item, brand_name, drive, cache):
    cache["brand"] = cache.get("brand") or drive.brand_folder(brand_name)
    if not item["topic"]:
        item["topic"] = safe_name(item["title"] or item["handle"] or item["kind"], 40)
    return item["drive_folder_id"] or drive.item_folder(cache["brand"], item["id"], item["topic"])


def _upload_verified(drive, folder, name, data, mime):
    """Put a file in Drive and prove Drive holds exactly these bytes. A copy that does not match is moved to the trash and sent once more.
    Returns (file id, md5)."""
    existing = drive.list_files(folder)
    for attempt in (1, 2):
        file_id = existing.get(name) if attempt == 1 else None
        if not file_id:
            file_id = drive.upload(folder, name, data, mime)
        try:
            return file_id, drive.verify(file_id, data)
        except DriveMismatch as exc:
            try:
                drive.trash(file_id)
            except DriveError:
                pass
            if attempt == 2:
                raise DriveError(f"the copy in Drive did not match the download, even on a second try ({exc})") from exc


def _record(item, name, file_id, md5, data, check, source):
    item["assets"].append({"name": name, "drive_id": file_id, "kind": check.kind, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                           "md5": md5, "width": check.width, "height": check.height, "duration": round(check.duration, 1), "source": source,
                           "verified": True})


def save_assets(bank, ids, brand_name, drive, get, settings, brand_folder=None, now=None):
    """Save the picture or video of each given item in its own Drive folder, and note the folder on the item.
    Every file is checked before it is uploaded (it must be whole, readable and not blank) and again after (Drive must hold exactly the
    bytes that were sent). A file that is byte for byte the same as one already saved for another kept item is not saved again: that item is
    a copy and is removed. Returns (saved, lines). An item with nothing to save gets no folder."""
    lines, saved, expired = [], 0, 0
    clock = _parse_time(now) or datetime.now(timezone.utc)
    cache = {"brand": brand_folder}
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
            data, check = fetch_media_checked(get, item["media_url"], settings)
        except AssetSkip as exc:
            item["asset_note"] = str(exc)
            lines.append(f"{idea_id}: no file saved: {exc}")
            continue
        digest = hashlib.sha256(data).hexdigest()
        twin = hashes.get(digest)
        if twin and twin != idea_id and _remove(item, f"a copy of {twin} (the very same file)", twin):          # a rescued item is never removed
            lines.append(f"{idea_id}: removed, it is a copy of {twin} (the very same file)")
            continue
        try:
            folder = _ensure_item_folder(item, brand_name, drive, cache)
            name = f"{idea_id}-{check.kind}.{check.ext}"
            file_id, md5 = _upload_verified(drive, folder, name, data, check.mime)
        except DriveError as exc:
            item["asset_note"] = f"not saved in Drive: {exc}"
            lines.append(f"{idea_id}: not saved in Drive: {exc}")
            raise
        cover = item.get("media_role") == "cover" or (item["platform"] in COVER_PLATFORMS and check.kind == "image")
        item.update({"drive_folder_id": folder, "drive_folder_url": folder_url(folder), "asset_note": ""})
        _record(item, name, file_id, md5, data, check, "cover" if cover else "media")
        hashes[digest] = idea_id
        saved += 1
    if expired:
        lines.append(f"{expired} item(s): the picture or video link has expired (Meta's links last only hours or days), so no file was saved; "
                     "it is saved if the ad is collected again")
    return saved, lines


def needs_link_refresh(item, settings, clock):
    """A kept Meta ad with no saved file whose picture or video link has expired (or was refused): Meta's links last hours or days, but the ad
    itself is often still shown in the Ad Library, and its page has a fresh link. Tried at most twice."""
    if not settings.refresh_links or settings.link_refreshes_per_run <= 0:
        return False
    if (state_of(item) != "kept" or item["platform"] != "meta_ads" or item["assets"] or not item["media_url"]
            or not str(item["url"]).startswith("https://www.facebook.com/ads/library/") or item.get("link_refreshes", 0) >= 2):
        return False
    ends = link_expiry(item["media_url"])
    note = item.get("asset_note", "")
    return bool((ends and ends <= clock) or "expired" in note or "the site answered 4" in note)


def refresh_meta_links(bank, factory, settings, now):
    """Open the Ad Library page of each such ad in the browser and take the fresh picture or video link from its data. Returns (refreshed, lines)."""
    clock = _parse_time(now) or datetime.now(timezone.utc)
    todo = [it for it in bank["items"].values() if needs_link_refresh(it, settings, clock)]
    if not todo:
        return 0, []
    if factory is None:
        return 0, [f"{len(todo)} Meta ad(s) have an expired picture or video link: no browser is available to fetch a fresh one"]
    from .collectors import _page_data, extract_json_items                       # imported here: collectors does not know the Idea Bank
    found = 0
    try:
        with factory("meta_ads") as browser:
            for item in todo[:settings.link_refreshes_per_run]:
                item["link_refreshes"] = item.get("link_refreshes", 0) + 1
                try:
                    browser.open(item["url"])
                    row = extract_json_items("meta_ads", _page_data(browser)).get(item["source_item_id"])
                except Exception as exc:                                         # a page that will not load is a reason, not a crash
                    item["asset_note"] = f"the link expired and the Ad Library page could not be opened ({exc.__class__.__name__}: {str(exc)[:100]})"
                    continue
                if row and row.get("media_url") and row["media_url"] != item["media_url"]:
                    item.update({"media_url": row["media_url"], "asset_note": "", "asset_tries": 0})
                    found += 1
                else:
                    item["asset_note"] = "the link expired and the ad is no longer shown in the Ad Library"
    except Exception as exc:
        return found, [f"Meta ad links not refreshed: the browser could not start ({exc.__class__.__name__}: {str(exc)[:120]})"]
    lines = [f"{found} Meta ad picture or video link(s) refreshed from the Ad Library, so their files can be saved"] if found else []
    left = len(todo) - min(len(todo), settings.link_refreshes_per_run)
    if left:
        lines.append(f"{left} more expired Meta link(s) wait for the next run (limit {settings.link_refreshes_per_run} a run)")
    if todo and not found:
        lines.append(f"{len(todo) - left} expired Meta ad(s) could not be refreshed: they are no longer shown in the Ad Library")
    return found, lines


def post_is_carousel(item):
    return item.get("media_format") == "CAROUSEL" or item.get("format") == "carousel"


def preview_label(item):
    """What a person looking at the sheet will really find in Drive, in plain words, worked out from the files that are there (not from the
    last download attempt):
      a full video, checked  |  a cover only because the video could not be downloaded  |  a post that simply has no video (an image, or the
      first slide of a carousel)  |  an image ad  |  nothing saved because the link expired or is unavailable."""
    assets = item.get("assets") or []
    status = item.get("video_status", "")
    video = next((a for a in assets if a.get("kind") == "video"), None)
    first = video or (assets[0] if assets else None)
    size = f"{first['width']}x{first['height']}" if first and first.get("width") else ""
    if video:
        label = f"video saved and checked ({video.get('duration', 0):.0f}s, {size})" if size else "video saved and checked"
    elif first:
        if status == "none":
            label = "carousel: first slide saved, the other slides were not" if post_is_carousel(item) else (
                f"image post saved ({size}), it has no video" if size else "image post saved, it has no video")
        elif first.get("source") == "cover":
            why = {"blocked": "the platform refused the download", "gone": "the post is no longer available"}.get(status, item.get("video_note") or "")
            label = ("cover image only: the video could not be downloaded" + (f" ({why})" if why else "")) if status in ("blocked", "gone", "failed") \
                else "cover image only: the video has not been fetched yet"
        elif post_is_carousel(item):
            label = "carousel: first slide saved, the other slides were not"
        else:
            label = f"image saved ({size})" if size else "image saved"
    else:
        note = item.get("asset_note", "")
        if "expired" in note:
            return "no saved media: the original link expired"
        if note:
            return "no saved media: the original link is unavailable"
        return "no saved media: original link only"
    if first.get("kind") == "image" and first.get("width") and min(first["width"], first.get("height") or first["width"]) < mediacheck.LOW_RES_SIDE:
        label += " - low resolution"
    if any(not a.get("verified") for a in assets):
        label += " - not yet checked"
    return label


def audit_files(bank, drive):
    """Download every saved file back from Drive and check it again, as a person opening it would find it. A file that is broken, cut short
    or missing is removed from the item (and the item is queued to be saved again if its link still works); a file with the wrong ending
    (a picture saved as .jpg that is really a .png) is renamed; what is learnt about each file is written back. Returns summary lines."""
    counts = {"checked": 0, "good": 0, "renamed": 0, "broken": 0, "missing": 0, "low": 0}
    problems = []
    for item in bank["items"].values():
        if not item["assets"]:
            continue
        keep = []
        for asset in item["assets"]:
            counts["checked"] += 1
            try:
                data = drive.download(asset["drive_id"])
            except DriveError as exc:
                counts["missing"] += 1
                problems.append(f"{item['id']}: {asset['name']} is missing or unreadable in Drive ({exc})")
                continue
            check = mediacheck.check_media(data, declared_length=asset.get("bytes"))
            if check.problems:
                counts["broken"] += 1
                problems.append(f"{item['id']}: {asset['name']} is broken ({'; '.join(check.problems)})")
                try:
                    drive.trash(asset["drive_id"])
                except DriveError:
                    pass
                continue
            if not mediacheck.extension_matches(asset["name"], check):
                new = mediacheck.corrected_name(asset["name"], check)
                try:
                    drive.rename(asset["drive_id"], new)
                    asset["name"] = new
                    counts["renamed"] += 1
                except DriveError as exc:
                    problems.append(f"{item['id']}: {asset['name']} is really a .{check.ext} file but could not be renamed ({exc})")
            asset.update({"kind": check.kind, "bytes": len(data), "width": check.width, "height": check.height, "duration": round(check.duration, 1),
                          "sha256": hashlib.sha256(data).hexdigest(), "md5": hashlib.md5(data).hexdigest(), "verified": True})
            counts["low"] += 1 if check.low_resolution else 0
            counts["good"] += 1
            keep.append(asset)
        lost = len(keep) != len(item["assets"])
        item["assets"] = keep
        if any(a["kind"] == "video" for a in keep):
            item["video_status"] = "saved"                  # what is in Drive decides, not what was last attempted
        elif item.get("video_status") == "saved":
            item["video_status"] = ""
        if lost:
            item["asset_tries"] = 0                         # asked again next time, if the link still works
            item["asset_note"] = "audit: a saved file was broken or missing and was removed; it is saved again if the link still works"
    covers = sum(1 for it in bank["items"].values() if it["assets"] and not any(a["kind"] == "video" for a in it["assets"])
                 and all(a.get("source") == "cover" for a in it["assets"]))
    lines = [f"audit: {counts['checked']} saved file(s) downloaded from Drive and checked: {counts['good']} complete, {counts['renamed']} renamed "
             f"(the ending did not match the real type), {counts['broken']} broken, {counts['missing']} missing; {counts['low']} low resolution; "
             f"{covers} item(s) have only a cover picture, not the video"]
    return lines + [f"audit: {p}" for p in problems[:12]] + ([f"audit: ... and {len(problems) - 12} more"] if len(problems) > 12 else [])


def needs_video(item, settings):
    """A kept item with a TikTok or Instagram page whose real video has not been saved, and not given up on: a post that has no video, or is
    gone, is final; one the platform refused is tried again in a later run (it costs no try, since the cause is the server, not the post)."""
    return (settings.fetch_videos and settings.video_fetches_per_run > 0 and state_of(item) == "kept" and video_page(item)
            and not any(a.get("kind") == "video" for a in item["assets"]) and item.get("video_status") not in ("none", "gone")
            and item.get("video_tries", 0) < 2)


BLOCKED_STREAK = 3          # after this many refusals in a row from one platform in one run, the rest of that platform waits for the next run


def save_videos(bank, ids, brand_name, drive, settings, runner, cookies_path=None, brand_folder=None):
    """Fetch and save the real video for each given item (at most `video_fetches_per_run` a run). Every outcome is recorded on the item as
    `video_status`: saved, none (the post has no video: an image or carousel), gone, blocked (the platform refused this server) or failed.
    Returns (saved, lines)."""
    lines, saved, tried = [], 0, 0
    cache = {"brand": brand_folder}
    streak, skipped = {}, {}
    pending = [i for i in ids if needs_video(bank["items"][i], settings)]
    for idea_id in pending:
        item = bank["items"][idea_id]
        platform = platform_of(item["url"])
        if streak.get(platform, 0) >= BLOCKED_STREAK:
            skipped[platform] = skipped.get(platform, 0) + 1
            continue
        if tried >= settings.video_fetches_per_run:
            lines.append(f"{len(pending) - tried} more video(s) wait for the next run (limit {settings.video_fetches_per_run} a run)")
            break
        tried += 1
        with tempfile.TemporaryDirectory() as tmp:
            found, why = runner(item["url"], tmp, cookies_path, settings.video_max_mb, settings.video_timeout_seconds)
            data = Path(found).read_bytes() if found else b""
        if not found:
            head, _, browser_why = str(why).partition("; the browser route also failed: ")
            status, note = classify_failure(head, item["url"])
            item.update({"video_status": status, "video_note": note})
            if status == "none":                               # an image or carousel post: the picture saved IS the creative
                for a in item["assets"]:
                    a["source"] = "media"
            if status != "blocked":
                item["video_tries"] += 1
            streak[platform] = streak.get(platform, 0) + 1 if status == "blocked" else 0
            lines.append(f"{idea_id}: no video saved: {note}" + (f" (the browser route also failed: {browser_why})" if browser_why else ""))
            continue
        check = mediacheck.check_media(data)
        if check.problems or check.kind != "video":
            item["video_tries"] += 1
            item.update({"video_status": "failed", "video_note": "; ".join(check.problems) or "what came back is not a video"})
            lines.append(f"{idea_id}: the video was not usable: {item['video_note']}")
            continue
        if len(data) > settings.video_max_mb * 1024 * 1024:
            item["video_tries"] += 1
            item.update({"video_status": "failed", "video_note": f"a video bigger than {settings.video_max_mb} MB"})
            lines.append(f"{idea_id}: {item['video_note']}")
            continue
        streak[platform] = 0
        folder = _ensure_item_folder(item, brand_name, drive, cache)
        name = f"{idea_id}-video.{check.ext}"
        file_id, md5 = _upload_verified(drive, folder, name, data, check.mime)
        item.update({"drive_folder_id": folder, "drive_folder_url": folder_url(folder), "video_note": "", "video_status": "saved"})
        _record(item, name, file_id, md5, data, check, "page")
        saved += 1
    for platform, n in skipped.items():
        lines.append(f"{n} more {platform or 'platform'} video(s) skipped this run: it refused {BLOCKED_STREAK} downloads in a row from this server (they are tried again next run)")
    return saved, lines


# ---------------------------------------------------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------------------------------------------------

SCORING_VERSION = 2          # 2 = the concept-first rubric below; an item scored under an older one is scored again, once
CATEGORIES = ("same", "adjacent", "other")
CHANNELS = ("paid social", "organic social", "creator", "email", "site", "mixed")
ITEM_FORMATS = ("ugc", "ai", "influencer", "static", "carousel", "video", "meme", "article", "other")   # what kind of content an item is
FORMAT_BOOST = 8             # added to the score of an item whose format a request asked for (the idea is still judged on its own merit)

SCORE_SYSTEM = (
    "You help a marketing agency find ideas worth turning into this brand's next creative. For each numbered item judge TWO things "
    "separately. 'fit' (0-100): how well the item's product category, audience and occasion match THIS brand and, if a request is given, "
    "THAT request. 'idea' (0-100): how strong and reusable the concept itself is: a specific hook, format, story or structure that could be "
    "re-made for our product. A generic item has no such concept: a discount or promo code, a price, a restock or 'new arrivals' notice, "
    "free shipping, a plain product shot, a list of reviews or perks with nothing distinctive, or a bare claim such as 'so comfy' or 'so tasty'. Mark those 'generic': true "
    "and give them an 'idea' under 40. Evidence counts: an ad that has run 60 or more days, or in several variants, was kept alive by an "
    "advertiser who watches results, and 150 or more days is very strong; raise 'idea' for that only when a real concept is there. Judge the "
    "idea, not who made it; a big brand's generic ad is still generic. 'category': 'same' (our own kind of product), 'adjacent' (a neighbouring category, whose idea carries over to ours) or 'other' (a product category unrelated to ours, whose idea would not carry over); judge the category from the brand context given, never from a fixed list. Reply with JSON only: {\"items\": [{\"id\": \"IB-0001\", \"fit\": 0-100, \"idea\": 0-100, \"generic\": true or false, "
    "\"category\": \"same\"|\"adjacent\"|\"other\", \"format\": \"ugc\"|\"ai\"|\"influencer\"|\"static\"|\"carousel\"|\"video\"|\"meme\"|\"article\"|\"other\", \"mechanism\": \"2 to 4 words naming the technique, such as problem-first hook, social proof, "
    "compliment hook, founder story, checklist, occasion reframe\", \"concept\": \"under 25 words: the reusable idea with the product taken out\", "
    "\"topic\": \"3 to 6 words naming the idea\", \"summary\": \"under 30 words: what it is and why it works\", \"how_to_use\": \"under 40 words: how "
    "WE could use the idea for this brand, in our own words and with our own products\", \"risk\": \"under 20 words: a claim, legal, copyright or "
    "brand-fit concern, or the word none\", \"hook_line\": \"under 20 words: the first line or first visual to open OUR version with\", \"first_test\": \"under 25 words: the cheapest first test (what to make, who sees it, what to measure)\", \"audience\": \"under 12 words: who it is for\", \"channel\": \"paid social\"|\"organic social\"|\"creator\"|\"email\"|\"site\"|\"mixed\"}]}. The 'format' is the main kind of content the item is: 'ugc' made by real customers or everyday creators, 'ai' made or edited with AI, 'influencer' a paid or gifted creator partnership post, 'static' a single-image post or ad, 'carousel' several slides, 'video' a brand-made video, 'meme' humour or a trend format, 'article' a web page or guide. If the request lists formats wanted, say so honestly when an item is in one; never stretch an item into a format it is not. Be strict: most items are ordinary, so most scores should be under 60. Use only the facts given. "
    "Never invent numbers, quotes or results.")


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
    if item.get("media_format"):
        facts = (facts + ", " if facts else "") + f"ad format {item['media_format']}"
    return (f'{item["id"]} [{item["kind"]}, {item["platform"]}] by {item["handle"] or "unknown"}: "{item["title"]}" - '
            f'{item["text"][:300]}' + (f" ({facts})" if facts else ""))


def _percent(value):
    return None if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100 else value


PROVEN_MIN_FIT = 50          # ...but only if it fits the brand: a long-running ad for something else is not a lesson for this brand
PROVEN_DAYS = 150            # an ad that has run this long (in a real category, not generic) is proof enough to keep, whatever its score
GENERIC_CAP = 45             # a generic item and an unrelated category are held under the usual minimum (50) however they are scored
OTHER_CAP = 45


def combined_score(fit, idea, generic, category):
    """The score code decides, not the model: the idea counts more than the fit, a generic item cannot reach the usual minimum, and an
    unrelated product category is held down however well it is made."""
    score = round(0.4 * fit + 0.6 * idea)
    if generic:
        score = min(score, GENERIC_CAP)
    if category == "other":
        score = min(score, OTHER_CAP)
    return score


def clean_scores(raw, wanted_ids):
    """The valid scores in a model's answer, {id: {relevance, fit, idea, generic, category, mechanism, concept, topic, summary, how_to_use,
    risk}}; anything malformed is left out. An answer with 'fit' and 'idea' is combined by combined_score; an older-style answer with only
    'relevance' is still accepted as it stands."""
    out = {}
    items = raw.get("items") if isinstance(raw, dict) else raw
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict) or entry.get("id") not in wanted_ids:
            continue
        texts = {k: _clip(entry.get(k), 240) for k in ("topic", "summary", "how_to_use", "risk")}
        if not texts["summary"]:
            continue
        fit, idea = _percent(entry.get("fit")), _percent(entry.get("idea"))
        kind = str(entry.get("format") or "").strip().lower()
        channel = str(entry.get("channel") or "").strip().lower()
        extra = {"fit": None, "idea": None, "generic": False, "category": "", "mechanism": _clip(entry.get("mechanism"), 60),
                 "hook_line": _clip(entry.get("hook_line"), 160), "first_test": _clip(entry.get("first_test"), 200),
                 "audience": _clip(entry.get("audience"), 100), "channel": channel if channel in CHANNELS else "",
                 "concept": _clip(entry.get("concept"), 240), "format": kind if kind in ITEM_FORMATS else ""}
        if fit is not None and idea is not None:
            generic = entry.get("generic") is True or str(entry.get("generic")).strip().lower() in ("true", "yes")
            category = str(entry.get("category") or "").strip().lower()
            category = category if category in CATEGORIES else "adjacent"
            rel = combined_score(fit, idea, generic, category)
            extra.update({"fit": int(round(fit)), "idea": int(round(idea)), "generic": generic, "category": category})
        else:
            rel = _percent(entry.get("relevance"))
            if rel is None:
                continue
        out[entry["id"]] = {"relevance": int(round(rel)), **extra, **texts}
    return out


def needs_scoring(item):
    """Not scored yet, or scored under an older rubric (so the bank is judged by one standard). Removed items are never scored again."""
    return item["state"] != "removed" and (item["score"] is None or item.get("scoring_version", 1) < SCORING_VERSION)


def score_items(bank, ids, claude, brand, store, settings, budget, now, request=None):
    """Score unscored items in batches, most interesting first, within the call limits. Returns (scored count, lines)."""
    todo = [i for i in ids if needs_scoring(bank["items"][i])]
    todo.sort(key=lambda i: _interest(bank["items"][i]["metrics"]), reverse=True)
    context = brand_context(brand, store)
    ask = (f"\nRequest: {request['text']}" + (f" (occasion: {request['occasion']})" if request.get("occasion") else "")
           + (f"\nFormats wanted: {', '.join(request['formats'])}" if request.get("formats") else "")) if request else ""
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
                         "risk": s["risk"], "scored_at": now, "model": getattr(claude, "model", ""), "fit": s["fit"], "idea": s["idea"],
                         "generic": s["generic"], "category": s["category"], "mechanism": s["mechanism"], "concept": s["concept"],
                         "format": s["format"], "scoring_version": SCORING_VERSION})
            if (request and s["format"] and s["format"] in (request.get("formats") or [])        # an item in a format the request asked for;
                    and not s["generic"] and s["category"] != "other"):                          # a generic or unrelated one never gets the boost
                item["score"] = item["relevance"] = min(100, item["score"] + FORMAT_BOOST)
            for key in ("hook_line", "first_test", "audience", "channel"):
                if s.get(key):
                    item[key] = s[key]
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
    if item.get("rescued"):                              # a person rescued this item: nothing the tool does removes it again
        return False
    item.update({"state": "removed", "removed_reason": reason, "duplicate_of": duplicate_of})
    return True


def is_proven(item):
    """A long-running ad: someone has kept paying for it for months, which is better evidence than any score. Not for a generic ad (a
    400-day discount code is persistent, not clever) nor for an unrelated category, and never for an item that has not been scored."""
    return ((item["metrics"].get("days_running") or 0) >= PROVEN_DAYS and not item.get("generic") and item.get("category") != "other"
            and item.get("score") is not None and (item.get("fit") is None or item["fit"] >= PROVEN_MIN_FIT))


def apply_relevance(bank, settings):
    """A scored item is kept if it scored at least the minimum and removed otherwise (so raising the minimum later tidies the bank).
    Returns how many were removed now."""
    removed = 0
    for item in bank["items"].values():
        if item["state"] == "removed" or item["score"] is None:
            continue
        if item["score"] >= settings.min_relevance or is_proven(item):             # a rescued item is protected inside _remove
            item["state"] = "kept"
        else:
            removed += 1 if _remove(item, f"not relevant enough (scored {item['score']}, the minimum is {settings.min_relevance})") else 0
    return removed


def tidy_skipped_brands(bank, skip_names):
    """Remove items from a brand the owner chose to skip (`benchmark_pages: {Name: skip}`), including ones collected before the choice.
    Names are compared the way benchmark names are, so 'Liquid Death' also matches 'liquid-death'. Returns how many were removed."""
    from .benchmarks import norm_name
    skip = {norm_name(n): n for n in skip_names if norm_name(n)}
    removed = 0
    for item in bank["items"].values():
        key = norm_name(item["handle"])
        if item["state"] != "removed" and key and key in skip:
            removed += 1 if _remove(item, f"from {skip[key]}, a brand you chose to skip") else 0
    return removed


def revive_relevant(bank, settings):
    """An item removed only for a low score comes back when the minimum is lowered to its score (a copy or the brand's own never does).
    Its Drive folder was trashed, so it gets a new one if its picture link still works. Returns how many came back."""
    back = 0
    for item in bank["items"].values():
        if (item["state"] == "removed" and item["score"] is not None and (item["score"] >= settings.min_relevance or is_proven(item))
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
                    removed += 1 if _remove(item, f"a copy of {original['id']}", original["id"]) else 0
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
                removed += 1 if _remove(item, f"the brand's own content: {why}") else 0
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
        item.update({"trashed_folder": item["drive_folder_id"], "trashed_assets": item["assets"],        # remembered, so a rescue can take them back
                     "drive_folder_id": "", "drive_folder_url": "", "assets": [], "removal_note": ""})
        moved += 1
    lines = []
    if stuck:
        lines.append(f"{len(stuck)} folder(s) of removed items could not be moved to the Drive trash ({reason}) Give the service account the Content "
                     f"manager role in the Shared Drive, or delete them by hand: {'; '.join(stuck[:6])}" + (" ..." if len(stuck) > 6 else ""))
    return moved, lines


# ---------------------------------------------------------------------------------------------------------------------
# the sheet
# ---------------------------------------------------------------------------------------------------------------------

# ---------------------------------------------------------------------------------------------------------------------
# new in this round: one technique, at most twice; what a reviewer needs to act; what the picture shows; thumbnails; rescue
# ---------------------------------------------------------------------------------------------------------------------

TECHNIQUE_SYSTEM = (
    "You group advertising and social ideas by the technique they use, for a creative team that wants variety. Two items belong in one group "
    "when re-made they would be the same ad: the same hook structure, format or story shape (for example 'a gift guide sorted by type of "
    "person', 'a reaction clip to opening a gift', 'a before and after', 'a blind taste test'), even if the products differ. Items that merely "
    "share a topic or a product are NOT the same technique. Reply with JSON only: {\"groups\": [{\"technique\": \"2 to 5 words\", "
    "\"ids\": [\"IB-0001\", \"IB-0002\"]}]}. Only groups of two or more; each id in at most one group; leave out items that stand alone.")
ENRICH_SYSTEM = (
    "You help a creative team act on an idea they already like. For each numbered item, give four short, concrete fields for THIS brand. "
    "'hook_line' (under 20 words): the first line or first visual to open our version with. 'first_test' (under 25 words): the cheapest first "
    "test: what to make, who sees it, what to measure. 'audience' (under 12 words): who it is for. 'channel': one of paid social, organic "
    "social, creator, email, site, mixed. Use only the brand context and the item; never invent numbers, quotes or results. Reply with JSON "
    "only: {\"items\": [{\"id\": \"IB-0001\", \"hook_line\": \"\", \"first_test\": \"\", \"audience\": \"\", \"channel\": \"\"}]}.")
LOOKS = ("ugc", "polished", "ai-generated", "screenshot", "graphic", "photo", "unclear")
VISION_SYSTEM = (
    "You are a creative director looking at saved pictures from ads and social posts (a video appears as one frame). For each picture, in the "
    "order given, say what a viewer sees. 'visual_hook' (under 25 words): what is on screen and what text it carries in the first second. "
    "'look': ugc (shot by an everyday person on a phone), polished (studio or agency production), ai-generated (it looks rendered or made by "
    "AI: unnatural skin or hands, warped text, a plastic finish), screenshot, graphic (type or illustration on a plain ground), photo, or "
    "unclear. 'quality': good, ok or poor for judging the creative. Say what you can see, not what you guess. Reply with JSON only: "
    "{\"items\": [{\"id\": \"IB-0001\", \"visual_hook\": \"\", \"look\": \"\", \"quality\": \"\"}]}.")


def _compact(item):
    return f"{item['id']}: technique: {item.get('mechanism') or '-'}; idea: {item.get('concept') or item.get('summary') or '-'}; format: {item.get('format') or '-'}; score {item.get('score')}"


def clean_groups(raw, valid_ids):
    """[(technique, [ids])] from a model's answer: only ids that exist, only groups of two or more, each id once."""
    out, used = [], set()
    groups = raw.get("groups") if isinstance(raw, dict) else raw
    for entry in groups if isinstance(groups, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("ids"), list):
            continue
        ids = []
        for i in entry["ids"]:
            if i in valid_ids and i not in used and i not in ids:
                ids.append(i)
        if len(ids) >= 2:
            used.update(ids)
            out.append((_clip(entry.get("technique"), 60) or "same technique", ids))
    return out


def group_techniques(bank, claude, settings, budget):
    """Keep each technique to its best `max_per_technique` ideas (one Claude call). The rest are removed with the reason and the better ideas
    named, so a person can see them on the Rejected tab and rescue any. A proven long-running ad outranks a higher score. Returns (removed, lines)."""
    if not settings.max_per_technique or claude is None or budget is None:
        return 0, []
    kept = [it for it in bank["items"].values() if state_of(it) == "kept" and (it.get("mechanism") or it.get("concept"))]
    if len(kept) < 3:
        return 0, []
    kept.sort(key=lambda it: (it["score"] or 0), reverse=True)
    kept = kept[:150]
    if not budget.take():
        return 0, [f"techniques not compared: the monthly Claude limit ({budget.cap} calls) is reached"]
    try:
        groups = clean_groups(claude.json_task(TECHNIQUE_SYSTEM, "Items:\n" + "\n".join(_compact(it) for it in kept), max_tokens=2500), {it["id"] for it in kept})
    except ProviderError as exc:
        return 0, [f"techniques not compared: {exc}"]
    removed, lines = 0, []
    for name, ids in groups:
        items = [bank["items"][i] for i in ids]
        items.sort(key=lambda it: (bool(it.get("rescued")), is_proven(it), it["score"] or 0, it["metrics"].get("days_running") or 0), reverse=True)
        best = items[:settings.max_per_technique]
        for it in best:
            it["technique"] = name
        names = ", ".join(it["id"] for it in best)
        for it in items[settings.max_per_technique:]:
            if _remove(it, f"another idea with the same technique ({name}) scored higher: {names}", best[0]["id"]):
                removed += 1
    if removed:
        lines.append(f"{removed} item(s) removed: the same technique as {settings.max_per_technique} better idea(s) (see the Rejected tab to rescue any)")
    return removed, lines


def clean_enrichment(raw, wanted):
    out = {}
    items = raw.get("items") if isinstance(raw, dict) else raw
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict) or entry.get("id") not in wanted:
            continue
        channel = str(entry.get("channel") or "").strip().lower()
        got = {"hook_line": _clip(entry.get("hook_line"), 160), "first_test": _clip(entry.get("first_test"), 200), "audience": _clip(entry.get("audience"), 100),
               "channel": channel if channel in CHANNELS else ""}
        if got["hook_line"] or got["first_test"]:
            out[entry["id"]] = got
    return out


def needs_enrichment(item):
    return state_of(item) == "kept" and not item.get("hook_line") and not item.get("first_test") and bool(item.get("summary") or item.get("concept"))


def enrich_items(bank, claude, brand, store, settings, budget):
    """Give kept items that were scored before these fields existed their hook line, first test, audience and channel, without touching their
    scores. Returns (filled, lines)."""
    todo = [i for i, it in bank["items"].items() if needs_enrichment(it)]
    todo.sort(key=lambda i: bank["items"][i]["score"] or 0, reverse=True)
    if not todo or claude is None or budget is None or not settings.enrich_calls_per_run:
        return 0, []
    context, filled, lines, calls = brand_context(brand, store), 0, [], 0
    for start in range(0, len(todo), settings.enrich_batch):
        if calls >= settings.enrich_calls_per_run:
            lines.append(f"{len(todo) - start} item(s) wait for their hook line and first test: this run's limit of {settings.enrich_calls_per_run} calls is reached")
            break
        if not budget.take():
            lines.append(f"{len(todo) - start} item(s) wait for their hook line and first test: the monthly Claude limit ({budget.cap} calls) is reached")
            break
        calls += 1
        batch = todo[start:start + settings.enrich_batch]
        user = f"{context}\n\nItems:\n" + "\n".join(_item_line(bank["items"][i]) for i in batch)
        try:
            got = clean_enrichment(claude.json_task(ENRICH_SYSTEM, user, max_tokens=2500), set(batch))
        except ProviderError as exc:
            lines.append(f"{len(batch)} item(s) got no hook line: {exc}")
            continue
        for idea_id, fields in got.items():
            bank["items"][idea_id].update({k: v for k, v in fields.items() if v})
            filled += 1
    return filled, lines


def clean_visuals(raw, wanted):
    out = {}
    items = raw.get("items") if isinstance(raw, dict) else raw
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict) or entry.get("id") not in wanted:
            continue
        look = str(entry.get("look") or "").strip().lower().replace(" ", "-")
        look = "ai-generated" if look in ("ai", "ai-rendered", "ai-made") else look
        quality = str(entry.get("quality") or "").strip().lower()
        hook = _clip(entry.get("visual_hook"), 200)
        if hook:
            out[entry["id"]] = {"visual_hook": hook, "look": look if look in LOOKS else "unclear", "visual_quality": quality if quality in ("good", "ok", "poor") else ""}
    return out


def vision_candidates(bank, settings):
    return [i for i, it in bank["items"].items() if state_of(it) == "kept" and it["assets"] and not it.get("visual_hook") and it.get("visual_tries", 0) < 2]


def review_visuals(bank, claude, settings, budget, load_picture):
    """Let Claude look at the saved pictures (a frame of each video) and say what the viewer sees, whether it looks AI-made, and whether it is
    good enough to judge. `load_picture(item)` returns (media_type, bytes) or None. An item the picture shows to be AI-made is labelled `ai`
    (the text alone never could). Returns (reviewed, lines)."""
    todo = vision_candidates(bank, settings)
    if not todo or claude is None or budget is None or not settings.vision or not settings.vision_calls_per_run or not hasattr(claude, "json_task_with_images"):
        return 0, []
    todo.sort(key=lambda i: bank["items"][i]["score"] or 0, reverse=True)
    done, lines, calls = 0, [], 0
    for start in range(0, len(todo), settings.vision_batch):
        if calls >= settings.vision_calls_per_run:
            lines.append(f"{len(todo) - start} item(s) wait for a look at their picture: this run's limit of {settings.vision_calls_per_run} calls is reached")
            break
        batch, pictures = [], []
        for idea_id in todo[start:start + settings.vision_batch]:
            item = bank["items"][idea_id]
            item["visual_tries"] += 1
            try:
                loaded = load_picture(item)
            except Exception:                                                   # a file that cannot be fetched is skipped, not a crash
                loaded = None
            if loaded:
                batch.append(idea_id)
                pictures.append(loaded)
        if not batch:
            continue
        if not budget.take():
            lines.append(f"{len(todo) - start} item(s) wait for a look at their picture: the monthly Claude limit ({budget.cap} calls) is reached")
            break
        calls += 1
        listing = "\n".join(f"Picture {n}: {i} (the ad says: {_clip(bank['items'][i]['title'] or bank['items'][i]['text'], 120) or 'nothing'})" for n, i in enumerate(batch, 1))
        try:
            got = clean_visuals(claude.json_task_with_images(VISION_SYSTEM, f"The pictures, in order:\n{listing}", pictures, max_tokens=1800), set(batch))
        except ProviderError as exc:
            lines.append(f"{len(batch)} picture(s) not reviewed: {exc}")
            continue
        for idea_id, v in got.items():
            item = bank["items"][idea_id]
            item.update(v)
            if v["look"] == "ai-generated" and item.get("format") != "ai":
                item["format"] = "ai"                                         # seen in the picture itself
            done += 1
    return done, lines


def load_picture_from_drive(drive, max_bytes=20 * 1024 * 1024):
    """A function for review_visuals: the first picture of an item from Drive, shrunk to a size Claude can read; for a video, one frame."""
    def load(item):
        asset = next((a for a in item["assets"] if a.get("kind") == "image"), None) or next((a for a in item["assets"] if a.get("kind") == "video"), None)
        if not asset or asset.get("bytes", 0) > max_bytes:
            return None
        data = drive.download(asset["drive_id"])
        if asset["kind"] == "video":
            data = mediacheck.extract_frame(data)
            if not data:
                return None
        return _shrunk(data)
    return load


def _shrunk(data, longest=1200):
    """JPEG bytes no longer than `longest` pixels (Pillow), or the picture as it is when it cannot be shrunk."""
    kind = mediacheck.sniff(data)
    if mediacheck.Image is None or not kind[0]:
        return (kind[1] or "image/jpeg", data)
    import io
    im = mediacheck.Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((longest, longest))
    out = io.BytesIO()
    im.save(out, "JPEG", quality=85)
    return "image/jpeg", out.getvalue()


def tiktok_cover(url, get):
    """The thumbnail address TikTok itself gives for a video page (its public oEmbed), or ''."""
    try:
        resp = get("https://www.tiktok.com/oembed", params={"url": url}, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            return ""
        thumb = (resp.json() or {}).get("thumbnail_url") or ""
    except Exception:
        return ""
    return thumb if str(thumb).startswith("https://") else ""


def resolve_covers(bank, get, settings):
    """A kept TikTok video page with no picture (a web search result has none from the collectors); ask TikTok for its thumbnail so there is something to save.
    Returns (found, lines)."""
    found, asked = 0, 0
    for item in bank["items"].values():
        if (state_of(item) != "kept" or item["media_url"] or item["assets"] or "tiktok.com" not in item["url"]
                or not video_page(item) or item.get("asset_tries", 0) >= ASSET_TRIES):
            continue
        if asked >= settings.cover_lookups_per_run:
            break
        asked += 1
        thumb = tiktok_cover(item["url"], get)
        if thumb:
            item.update({"media_url": thumb, "media_role": "cover"})
            found += 1
    return found, ([f"{found} TikTok thumbnail(s) found for web results that had no picture"] if found else [])


REJECTED_COLUMNS = ["idea_id", "platform", "title", "handle", "url", "score", "fit", "idea", "format", "mechanism", "concept", "removed_reason", "duplicate_of",
                    "days_running", "likes", "views", "preview"]


def rejected_rows(bank, limit=500):
    """The removed items, newest first, with the reason each was removed, so a person can check the tool's judgement and rescue any."""
    rows = {}
    for idea_id, item in sorted(bank["items"].items(), reverse=True):
        if item["state"] != "removed" or len(rows) >= limit:
            continue
        m = item["metrics"]
        rows[idea_id] = {"idea_id": idea_id, "platform": item["platform"], "title": item["title"], "handle": item["handle"], "url": item["url"],
                         "score": "" if item["score"] is None else item["score"], "fit": "" if item.get("fit") is None else item["fit"],
                         "idea": "" if item.get("idea") is None else item["idea"], "format": item.get("format", ""), "mechanism": item.get("mechanism", ""),
                         "concept": item.get("concept", ""), "removed_reason": item["removed_reason"], "duplicate_of": item["duplicate_of"],
                         "days_running": m.get("days_running", ""), "likes": m.get("likes", ""), "views": m.get("views", ""), "preview": preview_label(item)}
    return rows


def read_rescues(worksheet):
    """The ids a person marked Use or Maybe in the Rescue column of the Rejected tab."""
    values = worksheet.get_all_values()
    if not values:
        return []
    header = [str(h).strip().lower() for h in values[0]]
    if "idea_id" not in header or "rescue" not in header:
        return []
    i_id, i_res = header.index("idea_id"), header.index("rescue")
    out = []
    for row in values[1:]:
        idea_id = str(row[i_id]).strip() if len(row) > i_id else ""
        if re.fullmatch(r"IB-\d{4,}", idea_id) and normalise_decision(row[i_res] if len(row) > i_res else "") in ("use", "maybe"):
            out.append(idea_id)
    return out


def rescue_items(bank, ids, drive=None):
    """Bring back removed items a person rescued. They stay kept whatever any later tidy-up decides. Their Drive folder is taken out of the trash
    if it still can be. Returns (rescued, lines)."""
    rescued, lines = 0, []
    for idea_id in ids:
        item = bank["items"].get(idea_id)
        if not item or item["state"] != "removed":
            continue
        item.update({"state": "kept", "rescued": True, "removed_reason": "", "duplicate_of": "", "removal_note": ""})
        if item["score"] is None:
            item["score"] = 0
        folder = item.get("drive_folder_id") or item.get("trashed_folder") or ""
        assets = item["assets"] or item.get("trashed_assets") or []
        gone = {"drive_folder_id": "", "drive_folder_url": "", "assets": [], "asset_tries": 0}
        if folder and drive is not None:
            try:
                drive.restore(folder)
                item.update({"drive_folder_id": folder, "drive_folder_url": folder_url(folder), "assets": assets})
            except DriveError:                                   # past the 30 days in the trash, or removed by hand: the files are saved afresh
                item.update(gone)
        elif folder:
            item.update(gone)
        item.pop("trashed_folder", None)
        item.pop("trashed_assets", None)
        rescued += 1
    if rescued:
        lines.append(f"{rescued} item(s) rescued from the Rejected tab and kept")
    return rescued, lines


IDEABANK_COLUMNS = ["idea_id", "kind", "platform", "title", "handle", "url", "posted_at", "score", "fit", "idea", "format", "mechanism", "technique", "concept",
                    "summary", "how_to_use", "hook_line", "first_test", "audience", "channel", "risk", "topic", "region", "request_id", "likes", "comments", "shares", "views", "days_running",
                    "signal", "preview", "visual_hook", "look", "drive_folder", "assets", "asset_note", "added_at", "scored_at"]


def sheet_rows(bank):
    rows = {}
    for idea_id, item in bank["items"].items():
        if state_of(item) != "kept":                    # waiting to be scored, or removed: not shown
            continue
        m = item["metrics"]
        rows[idea_id] = {"idea_id": idea_id, "kind": item["kind"], "platform": item["platform"], "title": item["title"],
                         "handle": item["handle"], "url": item["url"], "posted_at": item["posted_at"],
                         "score": item["score"], "fit": "" if item.get("fit") is None else item["fit"],
                         "idea": "" if item.get("idea") is None else item["idea"], "format": item.get("format", ""), "mechanism": item.get("mechanism", ""),
                         "technique": item.get("technique", ""), "concept": item.get("concept", ""), "summary": item["summary"],
                         "how_to_use": item["how_to_use"], "hook_line": item.get("hook_line", ""), "first_test": item.get("first_test", ""),
                         "audience": item.get("audience", ""), "channel": item.get("channel", ""), "risk": item["risk"], "topic": item["topic"], "region": item["region"],
                         "request_id": item["request_id"], "likes": m.get("likes", ""), "comments": m.get("comments", ""),
                         "shares": m.get("shares", ""), "views": m.get("views", ""), "days_running": m.get("days_running", ""),
                         "signal": m.get("signal", ""), "preview": preview_label(item),
                         "visual_hook": item.get("visual_hook", ""), "look": item.get("look", ""),
                         "drive_folder": item["drive_folder_url"], "assets": len(item["assets"]),
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
                 dry_run=False, budget=None, since="", skip_brands=(), audit=False, video_runner=None, cookies=None, rescues=(), browser_factory=None, protected=()):
    """Take new sourced items into the brand's bank, keep only what is useful, save its files and score it. Returns (new ids, summary lines).

    In order: leave out the brand's own content and copies; tidy what is already in the bank the same way; score what is not scored; keep
    items that scored at least `min_relevance` and remove the rest; save pictures and videos only for the kept ones; move the Drive folders
    of removed items to the trash. Missing helpers are not errors: without Drive the files are skipped, without Claude items wait unscored."""
    lines = []
    bank = normalise_bank(store.read_ideabank(brand.slug))
    if rescues and not dry_run:
        _, notes = rescue_items(bank, rescues, drive)
        lines += [f"{brand.slug}: {n}" for n in notes]
    for idea_id in protected:                          # approved by a person (Use or Maybe): no tidy-up of the tool's removes it
        if idea_id in bank["items"] and state_of(bank["items"][idea_id]) == "kept":
            bank["items"][idea_id]["rescued"] = True
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
    skipped_brands = tidy_skipped_brands(bank, skip_brands)
    copies = tidy_duplicates(bank)                           # before scoring, so no Claude call is spent on a copy
    revive_relevant(bank, settings)                          # before scoring too: an item that now reaches a lowered minimum is judged again
    if claude is not None and budget is not None:
        unscored = [i for i, it in bank["items"].items() if needs_scoring(it)]
        scored, notes = score_items(bank, unscored, claude, brand, store, settings, budget, now, request)
        lines += notes
        lines.append(f"{brand.slug}: {scored} item(s) scored")
    irrelevant = apply_relevance(bank, settings)
    weak = [it["score"] for i, it in bank["items"].items()
            if it["state"] == "removed" and i not in was_removed and it["removed_reason"].startswith("not relevant enough")]
    back = sum(1 for i in was_removed if bank["items"][i]["state"] == "kept")        # brought back AND still kept after being judged again
    if back:
        lines.append(f"{brand.slug}: {back} item(s) removed earlier for a low score reach the minimum now and were brought back")
    if weak:
        lines.append(f"{brand.slug}: {relevance_bands(weak, settings.min_relevance)}")
    copies += tidy_duplicates(bank)                          # and again among the items that were kept
    if claude is not None and budget is not None:
        same_technique, notes = group_techniques(bank, claude, settings, budget)
        lines += [f"{brand.slug}: {n}" for n in notes]
        enriched, notes = enrich_items(bank, claude, brand, store, settings, budget)
        lines += [f"{brand.slug}: {n}" for n in notes]
        if enriched:
            lines.append(f"{brand.slug}: {enriched} item(s) got a hook line, first test, audience and channel")
    else:
        same_technique = 0
    if get is not None:
        _, notes = resolve_covers(bank, get, settings)
        lines += [f"{brand.slug}: {n}" for n in notes]
    waiting = sum(1 for it in bank["items"].values() if state_of(it) == "new")
    if waiting:
        lines.append(f"{brand.slug}: {waiting} item(s) wait to be scored before they are shown or get a folder"
                     + ("" if claude is not None else " (no Claude key)"))
    if audit and drive is not None:
        try:
            lines += [f"{brand.slug}: {line}" for line in audit_files(bank, drive)]
        except DriveError as exc:
            lines.append(f"{brand.slug}: the audit of saved files stopped: {exc}")
    if settings.save_assets and drive is not None and get is not None:
        _, notes = refresh_meta_links(bank, browser_factory, settings, now)
        lines += [f"{brand.slug}: {n}" for n in notes]
    retry = [i for i, it in bank["items"].items() if needs_assets(it)]
    try:
        if settings.save_assets and drive is not None and get is not None:
            saved, notes = save_assets(bank, retry, brand.name, drive, get, settings, now=now)
            lines += notes
            lines.append(f"{brand.slug}: files saved for {saved} item(s)")
            wanting = [i for i, it in bank["items"].items() if needs_video(it, settings)]
            if wanting and video_runner is None:
                lines.append(f"{brand.slug}: {len(wanting)} TikTok/Instagram video(s) not saved: yt-dlp is not available here, so only cover pictures are kept")
            elif wanting:
                with tempfile.TemporaryDirectory() as tmp:
                    jar = cookie_file([(cookies or {}).get("instagram"), (cookies or {}).get("tiktok")], Path(tmp) / "cookies.txt")
                    got, notes = save_videos(bank, wanting, brand.name, drive, settings, video_runner, jar)
                lines += notes
                lines.append(f"{brand.slug}: real videos saved for {got} item(s)")
        elif settings.save_assets and retry:
            lines.append(f"{brand.slug}: files not saved (no Drive folder is set up: add the IDEA_BANK_FOLDER_ID secret)")
    except DriveError as exc:
        lines.append(f"{brand.slug}: Drive stopped the saving of files: {exc}")
    if drive is not None and claude is not None and budget is not None:
        try:
            looked, notes = review_visuals(bank, claude, settings, budget, load_picture_from_drive(drive))
            lines += [f"{brand.slug}: {n}" for n in notes]
            if looked:
                lines.append(f"{brand.slug}: {looked} saved picture(s) looked at (what the viewer sees, and whether it looks AI-made)")
        except DriveError as exc:
            lines.append(f"{brand.slug}: Drive stopped the look at the saved pictures: {exc}")
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
        lines.append(f"{brand.slug}: {removed_now} item(s) removed ({own} the brand's own, {skipped_brands} from brands you skip, {copies} cop(ies), "
                     f"{irrelevant} not relevant enough" + (f", {same_technique} the same technique as better ideas" if same_technique else "") + f"); {after['kept']} kept")
    store.write_ideabank(brand.slug, bank)
    return new, lines
