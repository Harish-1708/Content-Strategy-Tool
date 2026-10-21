"""Collectors for competitor ads, social posts and Deep Research reports.

They drive a real headless browser, the way a person would:
  - Meta Ad Library: the public pages, no login.
  - Instagram and TikTok: logged in as a dummy account.
  - Gemini Deep Research: the Gemini web app, logged in as a dummy Google account, waiting
    the 10+ minutes a report takes.
Posts and ads are read from the data each site sends to its own page (found by shape, so a
renamed wrapper does not break it), with the page's links as a fallback. Every run is capped
and paced. Nothing here knows about Playwright: that lives in browser.py.
"""
import hashlib
import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus

import yaml

from .browser import BrowserError, Challenge, LoginWall
from .providers import AuthError, ProviderError, QuotaError

BROWSER_SOURCES = ("meta_ads", "tiktok", "instagram")
SOURCES = BROWSER_SOURCES + ("deep_research",)
SITE_OF = {"meta_ads": "meta_ads", "tiktok": "tiktok", "instagram": "instagram", "deep_research": "gemini"}
AD_LIBRARY = "https://www.facebook.com/ads/library"
_TS = "%Y-%m-%dT%H:%M:%SZ"
TEXT_LIMIT = 1000


class CollectError(Exception):
    """A problem a person can fix in a research file."""


# ---------------------------------------------------------------------------------------
# settings (the collectors section of research.yaml)
# ---------------------------------------------------------------------------------------

DEFAULT_APIFY_ACTORS = {"meta_ads": "apify/facebook-ads-scraper", "tiktok": "clockworks/tiktok-scraper",
                        "instagram": "apify/instagram-hashtag-scraper"}
DEFAULT_COLLECTORS = {
    "browser": {"monthly_items": 150, "items_per_run": 30, "cache_days": 3},
    "gemini": {"monthly_tasks": 2, "tasks_per_run": 1, "cache_days": 14},
    "apify": {"mode": "off", "monthly_items": 100, "items_per_run": 30, "max_cost_per_run_usd": 1.0,
              "actors": DEFAULT_APIFY_ACTORS},
    "claude": {"model": "claude-sonnet-5-5", "monthly_calls": 60, "max_rounds": 3, "max_tokens": 700,
               "prepare_prompt": True},
}
APIFY_MODES = ("off", "fallback", "always")
BACKENDS = ("auto", "browser", "apify", "fallback")


@dataclass(frozen=True)
class CollectorSettings:
    monthly_items: int
    items_per_run: int
    browser_cache_days: int
    monthly_tasks: int
    tasks_per_run: int
    gemini_cache_days: int
    apify_mode: str
    apify_monthly_items: int
    apify_items_per_run: int
    apify_max_cost_per_run_usd: float
    actors: dict
    claude_model: str
    claude_monthly_calls: int
    claude_max_rounds: int
    claude_max_tokens: int
    claude_prepare_prompt: bool


def _count(value, where, minimum=0, whole=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        raise CollectError(f"{where} must be a number of at least {minimum}, got {value!r}")
    if whole and value != int(value):
        raise CollectError(f"{where} must be a whole number, got {value!r}")
    return int(value) if whole else float(value)


def _keys(data, allowed, where):
    if not isinstance(data, dict):
        raise CollectError(f"{where} must be a set of name: value pairs")
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise CollectError(f"{where}: unknown {', '.join(map(repr, extra))} "
                           f"(allowed: {', '.join(sorted(allowed))})")


def load_collector_settings(path="research.yaml"):
    data = {}
    path = Path(path)
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    given = data.get("collectors") or {}
    _keys(given, DEFAULT_COLLECTORS, "research.yaml collectors")
    part = {k: {**DEFAULT_COLLECTORS[k], **(given.get(k) or {})} for k in DEFAULT_COLLECTORS}
    for name, section in part.items():
        _keys(section, DEFAULT_COLLECTORS[name], f"collectors.{name}")
    browser, gemini, apify, claude = part["browser"], part["gemini"], part["apify"], part["claude"]
    actors = {**DEFAULT_APIFY_ACTORS, **((given.get("apify") or {}).get("actors") or {})}
    _keys(actors, DEFAULT_APIFY_ACTORS, "collectors.apify.actors")
    for name, actor in actors.items():
        if not isinstance(actor, str) or not re.fullmatch(r"[\w.-]+[/~][\w.-]+", actor):
            raise CollectError(f"collectors.apify.actors.{name} must look like username/actor-name, got {actor!r}")
    if apify["mode"] is False:                    # YAML reads an unquoted `off` as false
        apify["mode"] = "off"
    if apify["mode"] is True:                     # ... and `on` as true, which does not say which mode
        raise CollectError("collectors.apify.mode: 'on' is not a mode; write fallback or always")
    if apify["mode"] not in APIFY_MODES:
        raise CollectError(f"collectors.apify.mode must be one of {', '.join(APIFY_MODES)}, got {apify['mode']!r}")
    if not isinstance(claude["model"], str) or not claude["model"].strip():
        raise CollectError("collectors.claude.model must be a model name")
    if not isinstance(claude["prepare_prompt"], bool):
        raise CollectError("collectors.claude.prepare_prompt must be true or false")
    return CollectorSettings(
        monthly_items=_count(browser["monthly_items"], "collectors.browser.monthly_items"),
        items_per_run=_count(browser["items_per_run"], "collectors.browser.items_per_run", 1),
        browser_cache_days=_count(browser["cache_days"], "collectors.browser.cache_days"),
        monthly_tasks=_count(gemini["monthly_tasks"], "collectors.gemini.monthly_tasks"),
        tasks_per_run=_count(gemini["tasks_per_run"], "collectors.gemini.tasks_per_run"),
        gemini_cache_days=_count(gemini["cache_days"], "collectors.gemini.cache_days"),
        apify_mode=apify["mode"],
        apify_monthly_items=_count(apify["monthly_items"], "collectors.apify.monthly_items"),
        apify_items_per_run=_count(apify["items_per_run"], "collectors.apify.items_per_run", 1),
        apify_max_cost_per_run_usd=_count(apify["max_cost_per_run_usd"], "collectors.apify.max_cost_per_run_usd", 0.01, whole=False),
        actors={k: v.replace("~", "/") for k, v in actors.items()},
        claude_model=claude["model"].strip(),
        claude_monthly_calls=_count(claude["monthly_calls"], "collectors.claude.monthly_calls"),
        claude_max_rounds=_count(claude["max_rounds"], "collectors.claude.max_rounds"),
        claude_max_tokens=_count(claude["max_tokens"], "collectors.claude.max_tokens", 50),
        claude_prepare_prompt=claude["prepare_prompt"])


def effective_backend(requested, settings):
    """browser, apify or fallback: what --backend asks for, or what research.yaml says when it is auto."""
    if requested == "auto":
        return {"off": "browser", "fallback": "fallback", "always": "apify"}[settings.apify_mode]
    return requested


# ---------------------------------------------------------------------------------------
# a brand's seeds: ads, social and deep_research sections of brands/<brand>/research.yaml
# ---------------------------------------------------------------------------------------

def _strings(value, where):
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise CollectError(f"{where} must be a list of text values")
    strip = where.endswith(("hashtags", "profiles"))
    return [v.strip().lstrip("#@") if strip else v.strip() for v in value]


def parse_collect_config(data, where):
    """Validate the ads / social / deep_research sections; returns a normalised dict."""
    ads = data.get("ads") or {}
    _keys(ads, ("country", "status", "pages", "keywords"), f"{where} ads")
    country = ads.get("country", "us")
    if not isinstance(country, str) or len(country) != 2 or not country.isalpha():
        raise CollectError(f"{where} ads.country must be a two-letter code, got {country!r}")
    status = ads.get("status", "active")
    if status not in ("all", "active", "inactive"):
        raise CollectError(f"{where} ads.status must be all, active or inactive, got {status!r}")
    pages = []
    for n, item in enumerate(ads.get("pages") or [], start=1):
        if isinstance(item, dict):
            _keys(item, ("name", "url"), f"{where} ads.pages item {n}")
            item = item.get("url")
        if not isinstance(item, str) or not item.startswith("https://www.facebook.com/"):
            raise CollectError(f"{where} ads.pages item {n} must be a https://www.facebook.com/... address")
        pages.append(item.strip())
    social = data.get("social") or {}
    _keys(social, ("tiktok", "instagram"), f"{where} social")
    tiktok = social.get("tiktok") or {}
    _keys(tiktok, ("hashtags", "profiles", "searches"), f"{where} social.tiktok")
    instagram = social.get("instagram") or {}
    _keys(instagram, ("hashtags",), f"{where} social.instagram")
    deep = data.get("deep_research") or {}
    _keys(deep, ("questions", "context"), f"{where} deep_research")
    context = deep.get("context") or ""
    if not isinstance(context, str):
        raise CollectError(f"{where} deep_research.context must be text")
    return {
        "ads": {"country": country.upper(), "status": status, "pages": pages,
                "keywords": _strings(ads.get("keywords"), f"{where} ads.keywords")},
        "tiktok": {"hashtags": _strings(tiktok.get("hashtags"), f"{where} social.tiktok.hashtags"),
                   "profiles": _strings(tiktok.get("profiles"), f"{where} social.tiktok.profiles"),
                   "searches": _strings(tiktok.get("searches"), f"{where} social.tiktok.searches")},
        "instagram": {"hashtags": _strings(instagram.get("hashtags"), f"{where} social.instagram.hashtags")},
        "deep_research": {"questions": _strings(deep.get("questions"), f"{where} deep_research.questions"),
                          "context": " ".join(context.split())[:2000]}}


def ad_library_url(keyword, country, status):
    return (AD_LIBRARY + "/?active_status=" + status + "&ad_type=all&country=" + country
            + "&q=" + quote_plus(keyword) + "&search_type=keyword_unordered&media_type=all")


@dataclass
class Task:
    source: str
    seeds: str                 # human-readable, shown in reports
    payload: dict
    key: str                   # same seeds -> same key, for reuse
    seed_count: int = 1


def _key(source, seeds):
    return hashlib.sha1((source + "|" + json.dumps(seeds, sort_keys=True)).encode()).hexdigest()[:16]


def ad_start_urls(ads):
    """Ad Library pages to read. An Ad Library link is used as written; a plain Facebook page
    address becomes a search for the page's name."""
    urls = []
    for page in ads["pages"]:
        if page.startswith(AD_LIBRARY):
            urls.append(page)
        else:
            slug = page.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            urls.append(ad_library_url(slug, ads["country"].lower(), ads["status"]))
    urls += [ad_library_url(k, ads["country"].lower(), ads["status"]) for k in ads["keywords"]]
    return urls


def plan_collections(cfg):
    """The tasks a brand's seeds produce. Nothing configured means nothing planned."""
    tasks = []
    urls = ad_start_urls(cfg["ads"])
    if urls:
        tasks.append(Task("meta_ads", f"{len(urls)} advertiser pages/keywords", {"urls": urls},
                          _key("meta_ads", urls), len(urls)))
    tt = cfg["tiktok"]
    seeds = {k: v for k, v in tt.items() if v}
    if seeds:
        count = sum(len(v) for v in seeds.values())
        tasks.append(Task("tiktok", f"{count} hashtags/profiles/searches", seeds, _key("tiktok", seeds), count))
    ig = cfg["instagram"]["hashtags"]
    if ig:
        tasks.append(Task("instagram", f"{len(ig)} hashtags", {"hashtags": ig}, _key("instagram", ig), len(ig)))
    for question in cfg["deep_research"]["questions"]:
        tasks.append(Task("deep_research", question[:80],
                          {"question": question, "context": cfg["deep_research"]["context"]},
                          _key("deep_research", question)))
    return tasks


def seed_urls(task, site_cfg):
    """The pages to open for a task."""
    p = task.payload
    if task.source == "meta_ads":
        return list(p["urls"])
    if task.source == "instagram":
        return [site_cfg["hashtag_url"].format(tag=quote(t)) for t in p["hashtags"]]
    urls = [site_cfg["hashtag_url"].format(tag=quote(t)) for t in p.get("hashtags", [])]
    urls += [site_cfg["profile_url"].format(name=quote(n)) for n in p.get("profiles", [])]
    urls += [site_cfg["search_url"].format(q=quote_plus(s)) for s in p.get("searches", [])]
    return urls


# ---------------------------------------------------------------------------------------
# reading what the sites send: found by shape, so a renamed wrapper does not break it
# ---------------------------------------------------------------------------------------

def _get(d, *names):
    for name in names:
        value = d.get(name)
        if value not in (None, ""):
            return value
    return None


def _int(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value):
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc).strftime(_TS)
    return str(value) if value else ""


def _clip(text):
    return " ".join(str(text or "").split())[:TEXT_LIMIT]


def _row(source, ident, **fields):
    row = {"item_id": f"{source}:{ident}", "source": source, "handle": "", "title": "", "text": "",
           "url": "", "posted_at": "", "likes": None, "comments": None, "shares": None, "views": None,
           "media_url": ""}
    row.update(fields)
    row["text"] = _clip(row["text"])
    row["title"] = _clip(row["title"])[:200]
    return row


def _first_number(*values):
    for v in values:
        n = _int(v)
        if n is not None:
            return n
    return None


def normalize_meta_ad(d):
    ident = _get(d, "adArchiveID", "adArchiveId", "ad_archive_id")
    if not ident:
        return None
    snap = d.get("snapshot") or {}
    body = snap.get("body")
    text = body.get("text") if isinstance(body, dict) else (body or d.get("body"))
    cards = snap.get("cards") or []
    first = cards[0] if cards and isinstance(cards[0], dict) else {}
    media = ""
    for pool in (snap.get("videos") or [], snap.get("images") or [], cards):
        if pool and isinstance(pool[0], dict):
            media = _get(pool[0], "videoHdUrl", "video_hd_url", "videoSdUrl", "video_sd_url", "originalImageUrl",
                         "original_image_url", "resizedImageUrl", "resized_image_url") or ""
            if media:
                break
    return _row("meta_ads", ident, handle=_get(d, "pageName", "page_name") or _get(snap, "pageName", "page_name") or "",
                title=snap.get("title") or first.get("title") or "", text=text or first.get("body") or "",
                url=f"https://www.facebook.com/ads/library/?id={ident}",
                posted_at=_iso(_get(d, "startDate", "start_date")), media_url=media)


def normalize_tiktok(d):
    ident = _get(d, "id")
    author = d.get("author")
    meta = d.get("authorMeta") if isinstance(d.get("authorMeta"), dict) else {}
    handle = (_get(author, "uniqueId", "unique_id", "nickname") if isinstance(author, dict) else author) \
        or _get(meta, "name", "nickName") or ""
    url = _get(d, "webVideoUrl") or (f"https://www.tiktok.com/@{handle}/video/{ident}" if handle and ident else None)
    if not ident or not url:
        return None
    stats = d.get("stats") if isinstance(d.get("stats"), dict) else {}
    video = d.get("video") if isinstance(d.get("video"), dict) else {}
    return _row("tiktok", ident, handle=handle, text=_get(d, "desc", "text") or "", url=url,
                posted_at=_iso(_get(d, "createTime", "createTimeISO")),
                likes=_first_number(stats.get("diggCount"), d.get("diggCount")),
                comments=_first_number(stats.get("commentCount"), d.get("commentCount")),
                shares=_first_number(stats.get("shareCount"), d.get("shareCount")),
                views=_first_number(stats.get("playCount"), d.get("playCount")),
                media_url=_get(video, "cover", "originCover", "dynamicCover")
                or _get(d.get("videoMeta") if isinstance(d.get("videoMeta"), dict) else {}, "coverUrl") or "")


def normalize_instagram(d):
    code = _get(d, "code", "shortcode", "shortCode")
    if not code:
        return None
    ident = code            # the post code is what links carry too, so a post is never counted twice
    owner = d.get("user") or d.get("owner") or {}
    caption = d.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    if not caption:
        edges = (d.get("edge_media_to_caption") or {}).get("edges") or []
        if edges and isinstance(edges[0], dict):
            caption = (edges[0].get("node") or {}).get("text")
    kind = "reel" if (d.get("product_type") == "clips" or d.get("media_type") == 2) else "p"
    candidates = (d.get("image_versions2") or {}).get("candidates") or []
    media = (candidates[0].get("url") if candidates and isinstance(candidates[0], dict) else None) \
        or _get(d, "display_url", "thumbnail_src", "displayUrl") or ""
    given = d.get("url") if isinstance(d.get("url"), str) and d["url"].startswith("https://www.instagram.com/") else None
    return _row("instagram", ident, handle=owner.get("username") or d.get("ownerUsername") or "", text=caption or "",
                url=given.split("?")[0] if given else f"https://www.instagram.com/{kind}/{code}/",
                posted_at=_iso(_get(d, "taken_at", "taken_at_timestamp", "timestamp")),
                likes=_first_number(d.get("like_count"), d.get("likesCount"), (d.get("edge_media_preview_like") or {}).get("count")),
                comments=_first_number(d.get("comment_count"), d.get("commentsCount"), (d.get("edge_media_to_comment") or {}).get("count")),
                views=_first_number(d.get("play_count"), d.get("view_count"), d.get("video_view_count"), d.get("videoPlayCount")),
                media_url=media)


NORMALIZERS = {"meta_ads": normalize_meta_ad, "tiktok": normalize_tiktok, "instagram": normalize_instagram}
PREDICATES = {
    "meta_ads": lambda d: bool(_get(d, "adArchiveID", "adArchiveId", "ad_archive_id")) and isinstance(d.get("snapshot"), dict),
    "tiktok": lambda d: isinstance(d.get("stats"), dict) and "desc" in d and bool(d.get("id")),
    "instagram": lambda d: bool(_get(d, "code", "shortcode")) and any(
        k in d for k in ("like_count", "caption", "taken_at", "taken_at_timestamp", "edge_media_to_caption")),
}


def normalize_items(source, items):
    """(rows, unreadable): what could be used from a list of ready-made items, and how many could not."""
    rows, bad = [], 0
    for item in items:
        row = NORMALIZERS[source](item) if isinstance(item, dict) else None
        if row:
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def apify_input(task, n):
    """What each Apify scraper is asked for."""
    per_seed = max(1, n // max(1, task.seed_count))
    p = task.payload
    if task.source == "meta_ads":
        return {"startUrls": [{"url": u} for u in p["urls"]], "resultsLimit": n}
    if task.source == "instagram":
        return {"hashtags": p["hashtags"], "resultsType": "posts", "resultsLimit": per_seed}
    out = {k: p[k] for k in ("hashtags", "profiles") if p.get(k)}
    if p.get("searches"):
        out["searchQueries"] = p["searches"]
    out["resultsPerPage"] = per_seed
    return out


def _walk(obj, depth=0):
    """Every dict inside a nested structure, in reading order."""
    if depth > 40:
        return
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value, depth + 1)


def _richness(row):
    return sum(1 for v in row.values() if v not in (None, ""))


def _merge(into, row):
    old = into.get(row["item_id"])
    if old is None or _richness(row) > _richness(old):
        into[row["item_id"]] = row


def extract_json_items(source, responses):
    """Ads/posts found anywhere in the data a site sent to its own page."""
    found = {}
    for response in responses:
        for d in _walk(response):
            if PREDICATES[source](d):
                row = NORMALIZERS[source](d)
                if row:
                    _merge(found, row)
    return found


_LINK_PATTERNS = {"instagram": re.compile(r"/(?:p|reel|tv)/([\w-]+)"),
                  "tiktok": re.compile(r"/@([\w.]+)/video/(\d+)")}


def extract_link_items(source, links):
    """A fallback when the site's data cannot be read: the posts the page links to."""
    found = {}
    for link in links:
        href = link.get("href", "")
        match = _LINK_PATTERNS.get(source) and _LINK_PATTERNS[source].search(href)
        if not match:
            continue
        clean = href.split("?")[0]
        if source == "instagram":
            row = _row("instagram", match.group(1), url=clean, text=link.get("text", ""))
        else:
            row = _row("tiktok", match.group(2), handle=match.group(1), url=clean, text=link.get("text", ""))
        found.setdefault(row["item_id"], row)
    return found


def extract_items(source, responses, links):
    """(rows, how many came from the site's data, how many only from the page's links)."""
    rows = extract_json_items(source, responses)
    from_json = len(rows)
    extra = {k: v for k, v in extract_link_items(source, links).items() if k not in rows}
    rows.update(extra)
    return list(rows.values()), from_json, len(extra)


# ---------------------------------------------------------------------------------------
# driving a site
# ---------------------------------------------------------------------------------------

def session_state(browser, site_cfg):
    """ok, login_wall, challenge or unknown, judged from what the page shows."""
    if browser.exists(site_cfg.get("challenge_any", [])):
        return "challenge"
    if browser.exists(site_cfg["logged_in_any"]):
        return "ok"
    if browser.exists(site_cfg["login_wall_any"]):
        return "login_wall"
    return "unknown"


def _secret_names(site_cfg):
    return site_cfg.get("secrets", {})


def run_steps(browser, steps, creds):
    for step in steps:
        if "goto" in step:
            browser.open(step["goto"])
        elif "fill" in step:
            if not browser.type_text(step["fill"], creds.get(step["with"]) or ""):
                raise BrowserError(f"could not find the {step['with']} box on the login page "
                                   "(the page may have changed: update login_steps in browser.yaml)")
        elif "click" in step:
            if not browser.click(step["click"]):
                raise BrowserError("could not find a button on the login page "
                                   "(the page may have changed: update login_steps in browser.yaml)")
        elif "click_optional" in step:
            browser.click(step["click_optional"])
        elif "wait" in step:
            browser.wait(step["wait"])


def ensure_session(browser, site, site_cfg, creds):
    """Open the site and make sure the dummy account is logged in. Returns the state seen."""
    cookies_name = _secret_names(site_cfg).get("cookies", "the cookies secret")
    browser.open(site_cfg["home"])
    browser.wait(3)
    state = session_state(browser, site_cfg)
    if state == "challenge":
        browser.screenshot(f"challenge-{site}")
        raise Challenge(f"{site}: the site asked for a security check (a code, a puzzle or 'confirm it's you'). "
                        f"Log in once as the dummy account in a normal browser, then export its cookies into {cookies_name}")
    if state != "ok" and creds.get("username") and creds.get("password") and site_cfg.get("login_steps"):
        run_steps(browser, site_cfg["login_steps"], creds)
        state = session_state(browser, site_cfg)
        if state == "challenge":
            browser.screenshot(f"challenge-{site}")
            raise Challenge(f"{site}: logging in with the username and password triggered a security check. "
                            f"Log in once in a normal browser and export the cookies into {cookies_name}")
    if state == "login_wall":
        browser.screenshot(f"login-wall-{site}")
        raise LoginWall(f"{site}: not logged in. The cookies in {cookies_name} may have expired; "
                        "export fresh ones from the dummy account")
    if state == "unknown":
        browser.screenshot(f"unknown-state-{site}")
    return state


def _gather(browser, source, site_cfg, scrolls, pause, max_items):
    """Scroll a results page until there are enough items. Returns (rows, ids found in the site's data)."""
    rows = {}
    for round_no in range(scrolls + 1):
        for row in extract_json_items(source, browser.json_responses()).values():
            _merge(rows, row)
        if len(rows) >= max_items or round_no == scrolls:
            break
        browser.scroll(1, pause)
    links = browser.links(site_cfg.get("link_contains", [])) if source in _LINK_PATTERNS else []
    from_data = extract_json_items(source, browser.json_responses())
    for row in from_data.values():
        _merge(rows, row)
    for key, row in extract_link_items(source, links).items():
        if key not in rows:
            rows[key] = row
    return list(rows.values())[:max_items], set(from_data)


def run_browser_task(browser, task, n, site_cfg, browser_cfg, creds):
    """Collect up to n ads or posts for one task. Returns (rows, stats).

    Each page gets an equal share of what is still needed, so a page that gives less than its
    share leaves more for the pages after it.
    """
    source, state = task.source, "ok"
    limits, pacing = browser_cfg["limits"], browser_cfg["pacing"]
    if source != "meta_ads":
        state = ensure_session(browser, source, site_cfg, creds)
    urls = seed_urls(task, site_cfg)
    found, from_data = {}, set()
    for number, url in enumerate(urls):
        browser.open(url)
        browser.wait(site_cfg.get("settle_seconds", 3))
        if source == "meta_ads":
            browser.click(site_cfg.get("consent_any", []))
        else:
            after = session_state(browser, site_cfg)
            if after == "challenge":
                browser.screenshot(f"challenge-{source}")
                raise Challenge(f"{source}: a security check appeared while collecting; clear it once in a normal "
                                "browser as the dummy account and export fresh cookies")
            if after == "login_wall":
                browser.screenshot(f"login-wall-{source}")
                raise LoginWall(f"{source}: the page asked for a login; the cookies may have expired")
        share = max(1, (n - len(found)) // (len(urls) - number))
        rows, data_ids = _gather(browser, source, site_cfg, limits["max_scrolls_per_page"],
                                 pacing["scroll_pause_seconds"], share)
        from_data |= data_ids
        for row in rows:
            _merge(found, row)
        if len(found) >= n:
            break
    kept = list(found.values())[:n]
    if not kept:
        browser.screenshot(f"empty-{source}")
    in_data = sum(1 for r in kept if r["item_id"] in from_data)
    return kept, {"json": in_data, "links": len(kept) - in_data, "state": state}


def _snip(text):
    return " ".join(str(text or "").split())[:300]


def collect_deep_research(browser, question, site_cfg, *, claude=None, context="", claude_budget=0,
                          max_rounds=3, prepare_prompt=True):
    """Ask Gemini Deep Research in its web app and wait for the report.

    With Claude available it first turns the question into a complete prompt, and answers any
    clarifying question Gemini asks before it starts. Returns the report text, its sources, how long
    it waited, the prompt sent, the questions answered and how many Claude calls were used.
    """
    wait = site_cfg["wait"]
    calls, notes, exchanges, answered = 0, [], [], set()
    prompt = question
    if claude is not None and prepare_prompt and claude_budget >= 1:
        try:
            prompt = claude.prepare_prompt(question, context)
            calls += 1
        except (AuthError, QuotaError, ProviderError) as exc:
            notes.append(f"Claude could not prepare the prompt ({exc}); the question was sent as written")
    browser.open(site_cfg["app_url"])
    browser.wait(4)
    state = session_state(browser, site_cfg)
    if state == "login_wall":
        browser.screenshot("login-wall-gemini")
        raise LoginWall("gemini: not logged in. The cookies in GEMINI_COOKIES may have expired; "
                        "export fresh ones from the dummy Google account")
    for number, selectors in enumerate(site_cfg["deep_research_clicks"], start=1):
        if not browser.click(selectors):
            browser.screenshot("gemini-no-deep-research-button")
            raise BrowserError(f"gemini: could not find the button to switch to Deep Research (step {number}); "
                               "the page may have changed, or the account may not have Deep Research. "
                               "See the screenshot and update deep_research_clicks in browser.yaml")
        browser.wait(1)
    if not browser.type_text(site_cfg["prompt_box"], prompt):
        browser.screenshot("gemini-no-prompt-box")
        raise BrowserError("gemini: could not find the prompt box (update prompt_box in browser.yaml)")
    if not browser.click(site_cfg["send"]):
        browser.screenshot("gemini-no-send-button")
        raise BrowserError("gemini: could not find the send button (update send in browser.yaml)")
    poll = wait["poll_seconds"]
    waited = plan_waited = still = 0
    seen = ""
    while not browser.exists(site_cfg["start_research"]) and plan_waited < wait["plan_wait_seconds"]:
        browser.wait(poll)
        waited += poll
        plan_waited += poll
        text = browser.text(site_cfg["response"])
        still = still + poll if text and text == seen else 0
        seen = text
        if (claude is None or still < wait["question_settle_seconds"] or text in answered
                or browser.exists(site_cfg.get("still_working_any", [])) or browser.exists(site_cfg["start_research"])):
            continue
        # Gemini has stopped talking and is not offering to start: it is probably asking something
        if len(exchanges) >= max_rounds or claude_budget - calls < 1:
            browser.screenshot("gemini-question-unanswered")
            raise BrowserError("gemini: asked a question after Claude's answer limit was reached. "
                               f"Gemini said: {_snip(text)}")
        try:
            reply = claude.answer(prompt, context, text)
            calls += 1
        except (AuthError, QuotaError, ProviderError) as exc:
            browser.screenshot("gemini-question-unanswered")
            raise BrowserError(f"gemini: asked a question and Claude could not answer it ({exc}). "
                               f"Gemini said: {_snip(text)}") from exc
        answered.add(text)
        still = 0
        if not reply:
            continue                                   # it was a plan or a status, not a question
        if not browser.type_text(site_cfg["prompt_box"], reply) or not browser.click(site_cfg["send"]):
            browser.screenshot("gemini-answer-not-sent")
            raise BrowserError("gemini: could not type the answer to Gemini's question "
                               "(update prompt_box and send in browser.yaml)")
        exchanges.append({"gemini": text[:1500], "reply": reply})
        plan_waited, seen = 0, ""
    browser.click(site_cfg["start_research"])         # absent when Gemini started on its own
    limit = wait["max_minutes"] * 60
    last_text, stable = "", 0
    while True:
        browser.wait(poll)
        waited += poll
        text = browser.text(site_cfg["response"])
        stable = stable + poll if text and text == last_text else 0
        last_text = text
        working = browser.exists(site_cfg.get("still_working_any", []))
        if len(text) >= wait["min_chars"] and not working and (
                browser.exists(site_cfg["done_any"]) or stable >= wait["settle_seconds"]):
            break
        if waited >= limit:
            browser.screenshot("gemini-timeout")
            raise BrowserError(f"gemini: the report was not finished after {wait['max_minutes']} minutes "
                               "(see the screenshot; raise wait.max_minutes in browser.yaml if it was still working)"
                               + (f". Last thing Gemini showed: {_snip(last_text)}" if last_text else ""))
    links = browser.response_links(site_cfg["response"])
    found = []
    for url in links + re.findall(r"https?://[^\s)\]>\"'<]+", text):
        url = url.rstrip(".,;")
        if url not in found:
            found.append(url)
    return {"text": text, "sources": found[:30], "waited_seconds": waited, "prompt": prompt,
            "exchanges": exchanges, "claude_calls": calls, "notes": notes}


# ---------------------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------------------

@dataclass
class TaskReport:
    source: str
    seeds: str
    status: str                 # ran | reused | skipped | failed | would_run
    detail: str = ""
    items: int = 0


@dataclass
class CollectResult:
    run_id: str
    now: str
    dry_run: bool
    brands: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    problems_for: list = field(default_factory=list)

    @property
    def problems(self):
        return bool(self.problems_for or any(t.status == "failed" for r in self.brands.values() for t in r))


def _parse(ts):
    return datetime.strptime(ts, _TS)


def _fresh(records, source, key, now, days):
    if days <= 0:
        return None
    newest = None
    for rec in records:
        if rec.get("source") == source and rec.get("key") == key:
            newest = rec
    if newest and (_parse(now) - _parse(newest["at"])).total_seconds() < days * 86400:
        return newest
    return None


def has_access(site_cfg, creds):
    """Can we get into this site: no login needed, or cookies, or a username and password."""
    if not site_cfg["needs_login"]:
        return True
    return bool(creds.get("cookies") or (creds.get("username") and creds.get("password")))


def missing_access_message(site_cfg):
    names = site_cfg.get("secrets", {})
    options = names.get("cookies", "cookies")
    if "username" in names:
        options += f" (or {names['username']} and {names['password']})"
    return f"no login: set {options}"


def _routes(deep, backend, apify, apify_state, site_cfg, creds, down):
    """The ways to collect one task, best first, or the reason there is none."""
    routes, why = [], ""
    if deep or backend != "apify":
        if not has_access(site_cfg, creds):
            why = missing_access_message(site_cfg)
        elif down:
            why = down
        else:
            routes.append("browser")
    if not deep and backend in ("apify", "fallback"):
        if apify is None:
            why = why or "no key: set APIFY_TOKEN"
        elif apify_state["disabled"]:
            why = why or apify_state["disabled"]
        elif apify_state["exhausted"]:
            why = why or "Apify is out of credit this run"
        else:
            routes.append("apify")
    return routes, why


def _context(brand, task):
    return f"Brand: {brand.name}. Website: {brand.domain}. {task.payload.get('context', '')}".strip()


def run_collect(brands, store, *, browser_factory, creds, settings, browser_cfg, ledger_cls, sources, now,
                run_id, configs=None, dry_run=False, root="brands", apify=None, claude=None, backend="browser"):
    """Plan and run collection tasks for each brand, within the caps in `settings`.

    backend: browser (the default), apify (Apify only) or fallback (the browser first, Apify when
    the browser fails). Deep Research always uses the browser.
    """
    from .research import load_brand_research         # local import: research imports this module
    ledger = ledger_cls(store.load_usage(), now[:7])
    result = CollectResult(run_id=run_id, now=now, dry_run=dry_run)
    disabled, opened, deep_runs = {}, {}, 0
    apify_state = {"disabled": None, "exhausted": False}

    def items_left(route):
        if route == "apify":
            return min(settings.apify_items_per_run, settings.apify_monthly_items - ledger.month_used("apify_items"))
        return min(settings.items_per_run, settings.monthly_items - ledger.month_used("collected_items"))

    def save(brand, task, rows, stats, via, extra=None):
        record = {"at": now, "run": run_id, "source": task.source, "key": task.key, "seeds": task.seeds, "via": via,
                  "from_site_data": stats.get("json", 0), "from_links": stats.get("links", 0), "items": rows}
        record.update(extra or {})
        store.append_collected(brand.slug, record)
        store.save_usage(ledger.usage)

    with ExitStack() as stack:
        for brand in brands:
            reports = result.brands.setdefault(brand.slug, [])
            cfg = (configs or {}).get(brand.slug) or load_brand_research(brand, root)["collect"]
            records = store.read_collected(brand.slug)
            for task in plan_collections(cfg):
                if task.source not in sources:
                    continue
                site = SITE_OF[task.source]
                site_cfg = browser_cfg["sites"][site]
                site_creds = creds.get(site, {})
                deep = task.source == "deep_research"
                report = TaskReport(task.source, task.seeds, "skipped")
                reports.append(report)
                routes, why = _routes(deep, backend, apify, apify_state, site_cfg, site_creds, disabled.get(site))
                if not routes:
                    report.detail = why
                    continue
                days = settings.gemini_cache_days if deep else settings.browser_cache_days
                if _fresh(records, task.source, task.key, now, days):
                    report.status, report.detail = "reused", f"already collected in the last {days} days"
                    continue
                if deep:
                    left = settings.monthly_tasks - ledger.month_used("deep_research_tasks")
                    if deep_runs >= settings.tasks_per_run or left < 1:
                        report.detail = ("per-run limit reached" if deep_runs >= settings.tasks_per_run
                                         else "out of Deep Research tasks under your monthly cap")
                        continue
                    budget = max(0, settings.claude_monthly_calls - ledger.month_used("claude_calls")) if claude else 0
                    if dry_run:
                        deep_runs += 1
                        ledger.charge("deep_research_tasks", 1)
                        report.status = "would_run"
                        report.detail = (f"1 Deep Research task in the Gemini web app (waits up to "
                                         f"{site_cfg['wait']['max_minutes']} minutes)"
                                         + ("; Claude prepares the prompt and answers Gemini's questions" if budget else
                                            "; no Claude, so the question is sent as written and Gemini's questions go unanswered"))
                        continue
                    try:
                        if site not in opened:
                            opened[site] = stack.enter_context(browser_factory(site))
                        deep_runs += 1
                        out = collect_deep_research(opened[site], task.payload["question"], site_cfg, claude=claude,
                                                    context=_context(brand, task), claude_budget=budget,
                                                    max_rounds=settings.claude_max_rounds,
                                                    prepare_prompt=settings.claude_prepare_prompt)
                        ledger.charge("deep_research_tasks", 1)
                        if out["claude_calls"]:
                            ledger.charge("claude_calls", out["claude_calls"])
                        name = f"{run_id}-{task.key}.md"
                        store.write_deep_report(brand.slug, name, out["text"])
                        item = _row("deep_research", f"{task.key}-{now[:10]}", handle="Gemini Deep Research",
                                    title=task.payload["question"], text=out["text"],
                                    url=f"data/{brand.slug}/research/deep/{name}")
                        item["extra_sources"] = out["sources"]
                    except (LoginWall, Challenge) as exc:
                        disabled[site] = str(exc)
                        result.problems_for.append(site)
                        result.notes.append(str(exc))
                        report.status, report.detail = "failed", str(exc)
                        continue
                    except BrowserError as exc:
                        report.status, report.detail = "failed", str(exc)
                        if site not in opened:
                            disabled[site] = str(exc)
                            result.problems_for.append(site)
                        continue
                    save(brand, task, [item], {}, "browser", {"deep": {
                        "prompt": out["prompt"], "exchanges": out["exchanges"], "claude_calls": out["claude_calls"],
                        "notes": out["notes"]}})
                    report.status, report.items = "ran", 1
                    report.detail = (f"report received after {out['waited_seconds'] // 60} min"
                                     + (f"; {len(out['exchanges'])} question(s) from Gemini answered by Claude" if out["exchanges"] else "")
                                     + "".join(f"; {n}" for n in out["notes"]))
                    continue
                viable = [(r, items_left(r)) for r in routes if items_left(r) >= 1]
                if not viable:
                    report.detail = "out of items under your monthly cap"
                    continue
                if dry_run:
                    route, n = viable[0]
                    ledger.charge("apify_items" if route == "apify" else "collected_items", n)
                    report.status = "would_run"
                    report.detail = (f"up to {n} items via Apify ({settings.actors[task.source]})" if route == "apify"
                                     else f"up to {n} items from {site}" + (" (Apify if the browser fails)" if "apify" in routes else ""))
                    continue
                errors, rows, stats, via, hard = [], None, None, None, False
                for route, n in viable:
                    try:
                        if route == "apify":
                            raw = apify.run(settings.actors[task.source], apify_input(task, n), max_items=n,
                                            max_cost_usd=settings.apify_max_cost_per_run_usd)
                            ledger.charge("apify_items", len(raw))
                            store.save_usage(ledger.usage)               # spent is spent, even if nothing was readable
                            rows, bad = normalize_items(task.source, raw)
                            stats = {"json": len(rows), "links": 0, "unreadable": bad}
                        else:
                            if site not in opened:
                                opened[site] = stack.enter_context(browser_factory(site))
                            rows, stats = run_browser_task(opened[site], task, n, site_cfg, browser_cfg, site_creds)
                            ledger.charge("collected_items", len(rows))
                    except (LoginWall, Challenge) as exc:
                        disabled[site] = str(exc)
                        result.problems_for.append(site)
                        result.notes.append(str(exc))
                        errors.append(str(exc))
                        hard = True
                        continue
                    except BrowserError as exc:
                        errors.append(str(exc))
                        hard = True
                        if site not in opened:                       # the browser itself would not start
                            disabled[site] = str(exc)
                            result.problems_for.append(site)
                        continue
                    except AuthError as exc:
                        apify_state["disabled"] = str(exc)
                        result.problems_for.append("apify")
                        result.notes.append(f"apify switched off for this run: {exc}")
                        errors.append(str(exc))
                        hard = True
                        continue
                    except QuotaError as exc:
                        apify_state["exhausted"] = True
                        result.notes.append(f"apify is out of credit: {exc}")
                        errors.append(str(exc))
                        continue
                    except ProviderError as exc:
                        errors.append(str(exc))
                        hard = True
                        continue
                    if rows:
                        via = route
                        break
                    hard = True
                    errors.append("the page opened but no ads or posts could be read "
                                  "(a screenshot was saved; run Recon to see what the site sends)"
                                  if route == "browser" else "Apify returned no readable items")
                if via is None:
                    report.status = "failed" if hard else "skipped"
                    report.detail = " | ".join(errors)[:400]
                    continue
                save(brand, task, rows, stats, via)
                report.status, report.items = "ran", len(rows)
                report.detail = (f"{len(rows)} items via Apify" + (f" (the browser failed: {errors[0][:100]})" if errors else "")
                                 if via == "apify" else
                                 f"{len(rows)} items ({stats['json']} from the site's data, {stats['links']} only from page links)")
    return result


def format_collect_report(result, settings, ledger=None):
    lines = [f"Collection run {result.now}" + ("  [dry run: nothing was collected or saved]" if result.dry_run else "")]
    for slug, reports in result.brands.items():
        lines.append("")
        lines.append(f"=== {slug} ===")
        if not reports:
            lines.append("  nothing configured (add ads, social or deep_research seeds in research.yaml)")
        for r in reports:
            lines.append(f"  {r.source:<13} {r.status:<9} {r.seeds}" + (f" -> {r.detail}" if r.detail else ""))
    if ledger is not None:
        lines.append("")
        lines.append(f"Used this month ({ledger.month}), against your caps: items {ledger.month_used('collected_items'):g}/"
                     f"{settings.monthly_items}, Apify items {ledger.month_used('apify_items'):g}/{settings.apify_monthly_items}, "
                     f"Deep Research tasks {ledger.month_used('deep_research_tasks'):g}/{settings.monthly_tasks}, "
                     f"Claude calls {ledger.month_used('claude_calls'):g}/{settings.claude_monthly_calls}")
    lines.extend(f"note: {n}" for n in result.notes)
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# check, test and recon
# ---------------------------------------------------------------------------------------

CHECK_SEEDS = {
    "meta_ads": lambda: Task("meta_ads", "check", {"urls": [ad_library_url("overalls", "us", "active")]}, "check"),
    "instagram": lambda: Task("instagram", "check", {"hashtags": ["overalls"]}, "check"),
    "tiktok": lambda: Task("tiktok", "check", {"hashtags": ["overalls"]}, "check"),
}
TEST_QUESTION = ("In a short report, how are comfortable loungewear overalls marketed to young adults on TikTok "
                 "and Instagram? Keep the report brief.")


def check_collectors(browser_factory, creds, settings, browser_cfg, store, ledger_cls, now, sources, items=3,
                     apify=None, claude=None, backend="browser"):
    """Prove each collector works: open the site, confirm the login, and collect a handful of items.

    Deep Research is only opened (login and prompt box); no research is started. With backend apify the
    ads and social sources are checked through Apify instead. Claude gets a tiny real call.
    """
    ledger = ledger_cls(store.load_usage(), now[:7])
    rows = []
    for source in sources:
        site = SITE_OF[source]
        site_cfg = browser_cfg["sites"][site]
        row = {"source": source, "ok": False, "detail": ""}
        rows.append(row)
        if source in BROWSER_SOURCES and backend == "apify":
            if apify is None:
                row["detail"] = "no key: set APIFY_TOKEN"
                continue
            if settings.apify_monthly_items - ledger.month_used("apify_items") < items:
                row["detail"] = "skipped: no Apify items left under your monthly cap"
                continue
            try:
                raw = apify.run(settings.actors[source], apify_input(CHECK_SEEDS[source](), items), max_items=items,
                                max_cost_usd=min(0.25, settings.apify_max_cost_per_run_usd))
            except (AuthError, QuotaError, ProviderError) as exc:
                row["detail"] = str(exc)
                continue
            ledger.charge("apify_items", len(raw))
            store.save_usage(ledger.usage)
            got, bad = normalize_items(source, raw)
            row["ok"] = bool(got)
            row["detail"] = (f"via Apify: {len(raw)} items, {len(got)} readable" + (f", {bad} unreadable" if bad else "")
                             if raw else "via Apify: the scraper ran but returned no items (check the actor name in research.yaml)")
            continue
        site_creds = creds.get(site, {})
        if not has_access(site_cfg, site_creds):
            row["detail"] = missing_access_message(site_cfg)
            continue
        if source != "deep_research" and settings.monthly_items - ledger.month_used("collected_items") < items:
            row["detail"] = "skipped: no items left under your monthly cap"
            continue
        try:
            with browser_factory(site) as browser:
                if source == "deep_research":
                    browser.open(site_cfg["app_url"])
                    browser.wait(4)
                    state = session_state(browser, site_cfg)
                    browser.screenshot("check-gemini")
                    if state == "login_wall":
                        raise LoginWall("gemini: not logged in; export fresh cookies into GEMINI_COOKIES")
                    has_box = browser.exists(site_cfg["prompt_box"])
                    row["ok"] = has_box
                    row["detail"] = (f"Gemini opened (login looks {state}) and the prompt box is there; "
                                     "no research was started" if has_box else
                                     "Gemini opened but the prompt box was not found (update prompt_box in browser.yaml)")
                    continue
                found, stats = run_browser_task(browser, CHECK_SEEDS[source](), items, site_cfg, browser_cfg, site_creds)
                browser.screenshot(f"check-{source}")
        except (LoginWall, Challenge, BrowserError) as exc:
            row["detail"] = str(exc)
            continue
        ledger.charge("collected_items", len(found))
        store.save_usage(ledger.usage)
        row["ok"] = bool(found)
        row["detail"] = (f"login {stats['state']}; {len(found)} items ({stats['json']} from the site's data, "
                         f"{stats['links']} only from page links)"
                         + (f": {found[0]['handle']} {found[0]['text'][:50]}" if found else
                            "; nothing could be read (run Recon to see what the site sends)"))
    if "deep_research" in sources:
        row = {"source": "claude", "ok": False, "detail": ""}
        rows.append(row)
        if claude is None:
            row["detail"] = ("no key: set ANTHROPIC_API_KEY (without it the Deep Research prompt is not prepared "
                             "and Gemini's questions go unanswered)")
        else:
            try:
                claude.check()
                row["ok"], row["detail"] = True, f"key accepted; {claude.model} answered"
            except (AuthError, QuotaError, ProviderError) as exc:
                row["detail"] = str(exc)
    return rows


def format_check_collectors(rows):
    lines = ["Collector check: open each site, confirm the login, collect a handful"]
    for r in rows:
        lines.append(f"  {r['source']:<13} {'OK' if r['ok'] else 'FAILED'}: {r['detail']}")
    return "\n".join(lines)


def run_deep_research_test(browser_factory, creds, browser_cfg, settings, store, ledger_cls, now, claude, debug_dir):
    """Run ONE real, short Deep Research report to prove the whole path (Claude's prompt, Gemini's
    questions, the wait, the report). Nothing is saved as collected data; the report goes in the debug folder."""
    site_cfg = browser_cfg["sites"]["gemini"]
    if not has_access(site_cfg, creds.get("gemini", {})):
        return {"ok": False, "detail": missing_access_message(site_cfg)}
    ledger = ledger_cls(store.load_usage(), now[:7])
    budget = max(0, settings.claude_monthly_calls - ledger.month_used("claude_calls")) if claude else 0
    try:
        with browser_factory("gemini") as browser:
            out = collect_deep_research(browser, TEST_QUESTION, site_cfg, claude=claude,
                                        context="A test run of the Kelson Agency content strategy tool.",
                                        claude_budget=budget, max_rounds=settings.claude_max_rounds,
                                        prepare_prompt=settings.claude_prepare_prompt)
    except (LoginWall, Challenge, BrowserError) as exc:
        return {"ok": False, "detail": str(exc)}
    if out["claude_calls"]:
        ledger.charge("claude_calls", out["claude_calls"])
        store.save_usage(ledger.usage)
    path = Path(debug_dir) / "test-deep-research.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Deep Research test", "", "## Prompt sent to Gemini", out["prompt"], "", "## Questions from Gemini and Claude's answers"]
    lines += [f"- Gemini: {e['gemini']}\n  Claude: {e['reply']}" for e in out["exchanges"]] or ["(Gemini asked nothing)"]
    lines += ["", "## Report", out["text"]]
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"ok": True, "detail": (f"a report of {len(out['text'])} characters arrived after {out['waited_seconds'] // 60} minutes; "
                                    f"Claude made {out['claude_calls']} call(s); {len(out['exchanges'])} Gemini question(s) answered; "
                                    f"saved as {path}" + "".join(f"; {n}" for n in out["notes"]))}


RECON_PAGES = {
    "meta_ads": lambda cfg: ad_library_url("overalls", "us", "active"),
    "instagram": lambda cfg: cfg["hashtag_url"].format(tag="overalls"),
    "tiktok": lambda cfg: cfg["hashtag_url"].format(tag="overalls"),
    "deep_research": lambda cfg: cfg["app_url"],
}


def recon(browser_factory, creds, browser_cfg, sources, debug_dir):
    """Save what each site looks like and sends (pictures, page HTML, data shapes) so that
    browser.yaml and the readers can be tuned from real samples. Returns report lines."""
    out = Path(debug_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = ["Recon: pictures, page HTML and data samples are saved in the debug folder"]
    for source in sources:
        site = SITE_OF[source]
        site_cfg = browser_cfg["sites"][site]
        if not has_access(site_cfg, creds.get(site, {})):
            lines.append(f"  {source}: {missing_access_message(site_cfg)}")
            continue
        try:
            with browser_factory(site) as browser:
                if site_cfg["needs_login"] and source != "deep_research":
                    try:
                        state = ensure_session(browser, site, site_cfg, creds.get(site, {}))
                    except (LoginWall, Challenge, BrowserError) as exc:
                        lines.append(f"  {source}: {exc}")
                        continue
                    lines.append(f"  {source}: login looks {state}")
                browser.open(RECON_PAGES[source](site_cfg))
                browser.wait(site_cfg.get("settle_seconds", 5))
                browser.scroll(2, browser_cfg["pacing"]["scroll_pause_seconds"])
                browser.screenshot(f"recon-{site}")
                responses = browser.json_responses()
                samples = []
                for obj in responses[:60]:
                    text = json.dumps(obj)[:1500]
                    samples.append({"type": type(obj).__name__,
                                    "keys": sorted(obj)[:25] if isinstance(obj, dict) else len(obj),
                                    "bytes": len(json.dumps(obj)), "sample": text})
                (out / f"recon-{site}-network.json").write_text(json.dumps(samples, indent=1), encoding="utf-8")
                matches = (len(extract_json_items(source, responses)) if source in PREDICATES else 0)
                links = (browser.links(site_cfg.get("link_contains", [])) if source in _LINK_PATTERNS else [])
                lines.append(f"  {source}: {len(responses)} data responses saved; readable items found in them: {matches}"
                             + (f"; {len(links)} post links on the page" if links else ""))
        except BrowserError as exc:
            lines.append(f"  {source}: {exc}")
    return lines


# ---------------------------------------------------------------------------------------
# rows for the sheet
# ---------------------------------------------------------------------------------------

def collected_rows(records):
    """One row per distinct ad, post or report, oldest collection first."""
    rows = {}
    for rec in records:
        for item in rec.get("items", []):
            row = rows.setdefault(item["item_id"], {
                **{k: item.get(k) for k in ("item_id", "source", "handle", "title", "text", "url",
                                            "posted_at", "likes", "comments", "shares", "views", "media_url")},
                "first_seen": rec["at"], "last_seen": rec["at"], "times_seen": 0, "queries": []})
            row["last_seen"] = max(row["last_seen"], rec["at"])
            row["first_seen"] = min(row["first_seen"], rec["at"])
            row["times_seen"] += 1
            for k in ("handle", "title", "text", "likes", "comments", "shares", "views", "media_url"):
                if item.get(k) not in (None, ""):
                    row[k] = item[k]
            if rec["seeds"] not in row["queries"]:
                row["queries"].append(rec["seeds"])
    return {k: {**v, "queries": " | ".join(v["queries"][:3])} for k, v in rows.items()}
