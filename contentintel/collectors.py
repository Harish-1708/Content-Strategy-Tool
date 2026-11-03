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
import copy
import collections
import hashlib
import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlsplit

import yaml

from . import benchmarks as bench
from . import contract
from . import deepcheck
from . import evidence as evidence_mod
from .browser import AccountWarning, BrowserError, Challenge, LoginWall, describe_cookies, describe_form_facts, site_pacing, site_scrolls
from .providers import AuthError, ProviderConfigError, ProviderError, QuotaError
from .safety import clean_text

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
    "browser": {"monthly_items": 0, "items_per_run": 30, "cache_days": 3},          # 0 = no monthly limit
    "gemini": {"monthly_tasks": 2, "tasks_per_run": 1, "cache_days": 14},
    "apify": {"mode": "off", "monthly_items": 100, "items_per_run": 30, "max_cost_per_run_usd": 1.0, "check_max_cost_usd": 0.25,
              "actors": DEFAULT_APIFY_ACTORS},
    "claude": {"model": "claude-sonnet-5-5", "monthly_calls": 60, "max_rounds": 3, "max_tokens": 700, "prompt_max_tokens": 2000, "review_rounds": 1,
               "prepare_prompt": True},
    "auto_seeds": {"enabled": True, "refresh_days": 30, "max_keywords": 8, "max_hashtags": 5, "max_questions": 2,
                   "max_benchmarks": 8, "scopes": ["direct", "adjacent", "cross_industry"]},
    "benchmarks": {"enabled": True, "max_pages": 6, "discovery_per_run": 3, "items_per_name": 10,
                   "items_per_page": 8, "retry_days": 30},
    # days an account rests after a warning (0: only the rest of that run). Other settings keep the defaults.
    "safety": {"cooldown_days": {"instagram": 0, "tiktok": 3}},
}
COOLDOWN_SITES = ("instagram", "tiktok", "gemini")
AUTO_SCOPES = ("direct", "adjacent", "cross_industry")
APIFY_MODES = ("off", "fallback", "always", "extra")
BACKENDS = ("auto", "browser", "apify", "fallback", "extra")


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
    apify_check_max_cost_usd: float
    actors: dict
    claude_model: str
    claude_monthly_calls: int
    claude_max_rounds: int
    claude_max_tokens: int
    claude_prepare_prompt: bool
    auto_enabled: bool
    auto_refresh_days: int
    auto_max_keywords: int
    auto_max_hashtags: int
    auto_max_questions: int
    auto_scopes: tuple
    auto_max_benchmarks: int = 8
    benchmarks_enabled: bool = True
    benchmark_max_pages: int = 6
    benchmark_discovery_per_run: int = 3
    benchmark_items_per_name: int = 10
    benchmark_items_per_page: int = 8
    benchmark_retry_days: int = 30
    claude_prompt_max_tokens: int = 2000
    claude_review_rounds: int = 1
    cooldown_days: dict = field(default_factory=dict)


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
    cooldown_given = (given.get("safety") or {}).get("cooldown_days") or {}
    if not isinstance(cooldown_given, dict):
        raise CollectError("collectors.safety.cooldown_days must be a list of site: days pairs")
    _keys(cooldown_given, COOLDOWN_SITES, "collectors.safety.cooldown_days")
    cooldown_days = {site: _count(days, f"collectors.safety.cooldown_days.{site}")
                     for site, days in {**DEFAULT_COLLECTORS["safety"]["cooldown_days"], **cooldown_given}.items()}
    browser, gemini, apify, claude = part["browser"], part["gemini"], part["apify"], part["claude"]
    auto, bm = part["auto_seeds"], part["benchmarks"]
    if not isinstance(bm["enabled"], bool):
        raise CollectError("collectors.benchmarks.enabled must be true or false")
    if not isinstance(auto["enabled"], bool):
        raise CollectError("collectors.auto_seeds.enabled must be true or false")
    scopes = auto["scopes"]
    if not isinstance(scopes, list) or not scopes or any(s not in AUTO_SCOPES for s in scopes):
        raise CollectError(f"collectors.auto_seeds.scopes must be a list of: {', '.join(AUTO_SCOPES)}")
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
        apify_check_max_cost_usd=_count(apify["check_max_cost_usd"], "collectors.apify.check_max_cost_usd", 0.01, whole=False),
        actors={k: v.replace("~", "/") for k, v in actors.items()},
        claude_model=claude["model"].strip(),
        claude_monthly_calls=_count(claude["monthly_calls"], "collectors.claude.monthly_calls"),
        claude_max_rounds=_count(claude["max_rounds"], "collectors.claude.max_rounds"),
        claude_max_tokens=_count(claude["max_tokens"], "collectors.claude.max_tokens", 50),
        claude_prompt_max_tokens=_count(claude["prompt_max_tokens"], "collectors.claude.prompt_max_tokens", 200),
        claude_review_rounds=_count(claude["review_rounds"], "collectors.claude.review_rounds", 0),
        claude_prepare_prompt=claude["prepare_prompt"], auto_enabled=auto["enabled"],
        auto_refresh_days=_count(auto["refresh_days"], "collectors.auto_seeds.refresh_days", 1),
        auto_max_keywords=_count(auto["max_keywords"], "collectors.auto_seeds.max_keywords", 1),
        auto_max_hashtags=_count(auto["max_hashtags"], "collectors.auto_seeds.max_hashtags", 1),
        auto_max_questions=_count(auto["max_questions"], "collectors.auto_seeds.max_questions"),
        auto_scopes=tuple(scopes),
        auto_max_benchmarks=_count(auto["max_benchmarks"], "collectors.auto_seeds.max_benchmarks"),
        benchmarks_enabled=bm["enabled"],
        benchmark_max_pages=_count(bm["max_pages"], "collectors.benchmarks.max_pages"),
        benchmark_discovery_per_run=_count(bm["discovery_per_run"], "collectors.benchmarks.discovery_per_run"),
        benchmark_items_per_name=_count(bm["items_per_name"], "collectors.benchmarks.items_per_name", 1),
        benchmark_items_per_page=_count(bm["items_per_page"], "collectors.benchmarks.items_per_page", 1),
        benchmark_retry_days=_count(bm["retry_days"], "collectors.benchmarks.retry_days", 1),
        cooldown_days=cooldown_days)


def effective_backend(requested, settings):
    """browser, apify or fallback: what --backend asks for, or what research.yaml says when it is auto."""
    if requested == "auto":
        return {"off": "browser", "fallback": "fallback", "always": "apify", "extra": "extra"}[settings.apify_mode]
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
    _keys(ads, ("country", "status", "pages", "keywords", "benchmark_pages"), f"{where} ads")
    country = ads_country(ads.get("country", "worldwide"), f"{where} ads.country")
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
    auto_seeds = data.get("auto_seeds", True)
    if not isinstance(auto_seeds, bool):
        raise CollectError(f"{where} auto_seeds must be true or false")
    track_own = data.get("track_own_brand", True)
    if not isinstance(track_own, bool):
        raise CollectError(f"{where} track_own_brand must be true or false")
    try:
        benchmark_pages = bench.parse_manual(ads.get("benchmark_pages"), f"{where} ads.benchmark_pages")
    except bench.BenchmarkError as exc:
        raise CollectError(str(exc)) from exc
    deep = data.get("deep_research") or {}
    _keys(deep, ("questions", "context"), f"{where} deep_research")
    context = deep.get("context") or ""
    if not isinstance(context, str):
        raise CollectError(f"{where} deep_research.context must be text")
    return {
        "auto_seeds": auto_seeds, "track_own_brand": track_own,
        "ads": {"country": country, "status": status, "pages": pages,
                "keywords": _strings(ads.get("keywords"), f"{where} ads.keywords"),
                "benchmark_pages": benchmark_pages},
        "tiktok": {"hashtags": _strings(tiktok.get("hashtags"), f"{where} social.tiktok.hashtags"),
                   "profiles": _strings(tiktok.get("profiles"), f"{where} social.tiktok.profiles"),
                   "searches": _strings(tiktok.get("searches"), f"{where} social.tiktok.searches")},
        "instagram": {"hashtags": _strings(instagram.get("hashtags"), f"{where} social.instagram.hashtags")},
        "deep_research": {"questions": _strings(deep.get("questions"), f"{where} deep_research.questions"),
                          "context": " ".join(context.split())[:2000]}}


def ads_country(value, where):
    """The Ad Library's country: a two-letter code (upper case) or ALL for worldwide."""
    if isinstance(value, str) and value.strip().lower() in ("worldwide", "all", "any", "global"):
        return "ALL"
    if not isinstance(value, str) or len(value.strip()) != 2 or not value.strip().isalpha():
        raise CollectError(f"{where} must be a two-letter code such as us, or worldwide, got {value!r}")
    return value.strip().upper()


def ad_library_url(keyword, country, status):
    return (AD_LIBRARY + "/?active_status=" + status + "&ad_type=all&country=" + country.upper()
            + "&q=" + quote_plus(keyword) + "&search_type=keyword_unordered&media_type=all")


def ad_library_page_url(page_id, country, status):
    """Every ad one advertiser page runs: the Ad Library's own link for a page."""
    return (AD_LIBRARY + "/?active_status=" + status + "&ad_type=all&country=" + country.upper()
            + "&view_all_page_id=" + str(page_id) + "&media_type=all")


@dataclass
class Task:
    source: str
    seeds: str                 # human-readable, shown in reports
    payload: dict
    key: str                   # same seeds -> same key, for reuse
    seed_count: int = 1
    item_cap: int = 0          # most items this task may collect (0: only the run and month caps apply)


def _key(source, seeds):
    return hashlib.sha1((source + "|" + json.dumps(seeds, sort_keys=True)).encode()).hexdigest()[:16]


def ad_page_urls(ads):
    """Ad Library pages written in research.yaml. An Ad Library link is used as written; a plain Facebook
    page address becomes a search for the page's name."""
    urls = []
    for page in ads["pages"]:
        if page.startswith(AD_LIBRARY):
            urls.append(page)
        else:
            slug = page.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
            urls.append(ad_library_url(slug, ads["country"].lower(), ads["status"]))
    return urls


def ad_keyword_urls(ads):
    return [ad_library_url(k, ads["country"].lower(), ads["status"]) for k in ads["keywords"]]


def ad_start_urls(ads):
    return ad_page_urls(ads) + ad_keyword_urls(ads)


def _extend(manual, extra, limit=None):
    """The brand's own entries first, then new ones from `extra`, without repeats (ignoring case)."""
    out, seen = list(manual), {m.lower() for m in manual}
    for item in extra[:limit] if limit else extra:
        if item.lower() not in seen:
            seen.add(item.lower())
            out.append(item)
    return out


def merge_auto_seeds(cfg, auto, settings):
    """A brand's own seeds first, then what its catalogue and Claude suggest. Nothing is written twice."""
    if not auto or not cfg.get("auto_seeds", True):
        return cfg
    out = copy.deepcopy(cfg)
    keywords = [k for scope in settings.auto_scopes for k in auto.get("keywords", {}).get(scope, [])]
    out["ads"]["keywords"] = _extend(out["ads"]["keywords"], keywords)
    out["tiktok"]["hashtags"] = _extend(out["tiktok"]["hashtags"], auto.get("tiktok_hashtags", []))
    out["instagram"]["hashtags"] = _extend(out["instagram"]["hashtags"], auto.get("instagram_hashtags", []))
    out["deep_research"]["questions"] = _extend(out["deep_research"]["questions"], auto.get("questions", []),
                                                settings.auto_max_questions)
    out["deep_research"]["context"] = out["deep_research"]["context"] or auto.get("context", "")
    return out


def with_own_brand(cfg, brand):
    """The brand's own name is always searched (Meta ads, TikTok and Instagram hashtags), first, whatever
    Claude suggests; `track_own_brand: false` in the brand's research.yaml switches it off."""
    if not cfg.get("track_own_brand", True):
        return cfg
    out = copy.deepcopy(cfg)
    tag = re.sub(r"[^a-z0-9]", "", brand.name.lower())
    out["ads"]["keywords"] = _extend([brand.name], out["ads"]["keywords"])
    if tag:
        out["tiktok"]["hashtags"] = _extend([tag], out["tiktok"]["hashtags"])
        out["instagram"]["hashtags"] = _extend([tag], out["instagram"]["hashtags"])
    return out


def _meta_tasks(cfg, follow, settings):
    """Meta Ad Library tasks. Without a follow plan: one task of pages and keywords (the original shape). With one:
    followed advertiser pages, keyword searches, and the look-ups that find a benchmark brand's page, each
    with its own share of items so that none starves the others."""
    ads, tasks = cfg["ads"], []
    country, status = ads["country"].lower(), ads["status"]
    if follow is None:
        urls = ad_start_urls(ads)
        return [Task("meta_ads", f"{len(urls)} advertiser pages/keywords", {"urls": urls}, _key("meta_ads", urls),
                     len(urls))] if urls else []
    labels, pages = {}, []
    urls = ad_page_urls(ads)
    for page in follow.pages:
        url = ad_library_page_url(page["page_id"], country, status)
        if url not in urls:
            urls.append(url)
            pages.append(page)
            labels[url] = f"{page['name']} (page)"
    per_page = settings.benchmark_items_per_page if settings else 8
    if urls:
        tasks.append(Task("meta_ads", f"{len(urls)} followed advertiser pages",
                          {"urls": urls, "labels": labels, "pages": pages}, _key("meta_ads", ["pages"] + urls),
                          len(urls), item_cap=per_page * len(urls)))
    keywords = ad_keyword_urls(ads)
    if keywords:
        tasks.append(Task("meta_ads", f"{len(keywords)} keywords", {"urls": keywords}, _key("meta_ads", keywords),
                          len(keywords)))
    if follow.discover:
        per_name = settings.benchmark_items_per_name if settings else 10
        names = [d["name"] for d in follow.discover]
        look = [ad_library_url(n, country, status) for n in names]
        tasks.append(Task("meta_ads", f"look up the advertiser page of {len(names)} brand(s): {', '.join(names)}",
                          {"urls": look, "discover": follow.discover}, _key("meta_ads", ["discover"] + names),
                          len(names), item_cap=per_name * len(names)))
    return tasks


def plan_collections(cfg, auto=None, settings=None, follow=None):
    """The tasks a brand's seeds produce (its own, plus auto seeds when given). Nothing configured means nothing planned."""
    cfg = merge_auto_seeds(cfg, auto, settings) if auto else cfg
    tasks = _meta_tasks(cfg, follow, settings)
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
    return " ".join(clean_text(text or "").split())[:TEXT_LIMIT]          # invisible and control characters never enter collected text


def _row(source, ident, **fields):
    row = {"item_id": f"{source}:{ident}", "source": source, "handle": "", "title": "", "text": "",
           "url": "", "posted_at": "", "likes": None, "comments": None, "shares": None, "views": None,
           "media_url": "", "details": "", "saves": None, "reach": None, "days_running": None,
           "variants": None, "found_via": "", "page_id": "", "page_likes": None}
    row.update(fields)
    row["details"] = _clip(row["details"])[:400]
    row["text"] = _clip(row["text"])
    row["title"] = _clip(row["title"])[:200]
    return row


def _first_number(*values):
    for v in values:
        n = _int(v)
        if n is not None:
            return n
    return None


_TEMPLATE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_SHARED_HOSTS = {"amzn", "amazon", "markable", "walmart", "shopify", "myshopify", "etsy", "ebay", "facebook", "instagram",
                 "linktr", "tiktok", "google", "youtube", "bitly", "shop", "store"}


def _site_label(url):
    """The name part of a web address: rhoback.com -> rhoback, shop.chicme.co.uk -> chicme."""
    parts = [p for p in (urlsplit(url or "").hostname or "").lower().split(".") if p and p != "www"]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net"):
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else (parts[0] if parts else "")


def verified_brand(page_name, landing):
    """The advertiser's name, but only when the ad's own landing site agrees that it is the brand
    (page "Rhoback" selling on rhoback.com). A catalogue ad's {{product.brand}} is the PRODUCT's brand, which is
    the advertiser's name only for a brand selling its own products, so an unverified name is never filled in."""
    name, label = bench.norm_name(page_name), _site_label(landing)
    if len(label) < 4 or label in _SHARED_HOSTS or len(name) < 3:
        return ""
    return page_name.strip() if (label in name or name in label) else ""


def clean_template_text(text, page_name="", landing="", title=""):
    """Catalogue ads carry template tokens such as {{product.brand}}. Fill a token only with something verified
    (the brand when the landing site confirms it, the card's real product name); drop the rest. Text without
    tokens is returned exactly as it came."""
    if not text or "{{" not in text:
        return text
    brand = verified_brand(page_name, landing)

    def fill(match):
        token = match.group(1).lower()
        if token == "product.brand":
            return brand
        if token == "product.name":
            return title if title and "{{" not in title else ""
        return ""
    out = re.sub(r"\{\{[^}]*\}?\}?", "", _TEMPLATE.sub(fill, text))
    return " ".join(out.split()).strip(" -|,:;")


def _meta_link(snap, first):
    link = _get(snap, "link_url", "linkUrl") or ""
    if "{{" in link:                                   # dynamic product ads carry a template here
        link = _get(first, "link_url", "linkUrl") or ""
    return link


def _meta_page(d, snap):
    """The advertiser page behind an ad: (page id as text, follower count or None)."""
    page_id = _get(d, "page_id", "pageID", "pageId") or _get(snap, "page_id", "pageID", "pageId")
    page_id = str(page_id) if page_id not in (None, "") and re.fullmatch(r"\d{5,20}", str(page_id)) else ""
    return page_id, _int(_get(snap, "page_like_count", "pageLikeCount") or _get(d, "page_like_count", "pageLikeCount"))


def _meta_details(d, snap, first):
    """Platforms, call to action, format, status, end date, advertiser page and landing page, as one readable line."""
    platforms = _get(d, "publisher_platform", "publisherPlatform") or []
    platforms = [platforms] if isinstance(platforms, str) else platforms
    link = _meta_link(snap, first)
    page_id = _meta_page(d, snap)[0]
    active = d.get("is_active", d.get("isActive"))
    end = _iso(_get(d, "end_date", "endDate"))
    parts = ["/".join(str(p).title() for p in platforms) if platforms else "",
             f"CTA: {_get(snap, 'cta_text', 'ctaText') or _get(first, 'cta_text', 'ctaText')}"
             if (_get(snap, "cta_text", "ctaText") or _get(first, "cta_text", "ctaText")) else "",
             f"format: {_get(snap, 'display_format', 'displayFormat')}" if _get(snap, "display_format", "displayFormat") else "",
             "active" if active is True else ("inactive" if active is False else ""),
             f"ends {end[:10]}" if end else "", f"page: {page_id}" if page_id else "",
             f"landing: {link}" if link else ""]
    return " | ".join(p for p in parts if p)


def _meta_signals(d):
    """What Meta publishes about how an ad is doing: how long it has run, how many copies, and reach where shown."""
    start, end = _get(d, "startDate", "start_date"), _get(d, "endDate", "end_date")
    active = _int(_get(d, "totalActiveTime", "total_active_time"))
    days = None
    if active and active > 0:
        days = max(1, round(active / 86400))
    elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)) and end >= start > 0:
        days = int((end - start) // 86400) + 1
    count = _int(_get(d, "collationCount", "collation_count"))
    impressions = d.get("impressions_with_index")
    reach = _get(d, "reachEstimate", "reach_estimate") or (
        impressions.get("impressions_text") if isinstance(impressions, dict) else None)
    return {"days_running": days, "variants": count if count and count > 1 else None, "reach": reach}


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
    title = snap.get("title") or ""
    if not title or "{{" in title:                     # dynamic product ads have a placeholder, the card has the real name
        title = first.get("title") or ""
    handle = _get(d, "pageName", "page_name") or _get(snap, "pageName", "page_name") or ""
    page_id, page_likes = _meta_page(d, snap)
    text = clean_template_text(text or first.get("body") or "", handle, _meta_link(snap, first), title)
    return _row("meta_ads", ident, handle=handle, page_id=page_id, page_likes=page_likes,
                title=title, text=text,
                url=f"https://www.facebook.com/ads/library/?id={ident}",
                posted_at=_iso(_get(d, "startDate", "start_date")), media_url=media,
                details=_meta_details(d, snap, first), **_meta_signals(d))


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
                saves=_first_number(stats.get("collectCount"), d.get("collectCount")),
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
    if browser.exists(site_cfg.get("signed_out_any", [])):      # a signed-out page can look just like a signed-in one
        return "login_wall"                                     # (Gemini shows its prompt box either way), so this wins
    if browser.exists(site_cfg["logged_in_any"]):
        return "ok"
    if browser.exists(site_cfg["login_wall_any"]):
        return "login_wall"
    return "unknown"


def warning_seen(browser, site_cfg):
    """What a site's own warning says, or '' when none is showing. Phrases are matched on the page itself, and an
    address such as /accounts/suspended counts too; both are set per site in browser.yaml."""
    selectors = site_cfg.get("warning_any", [])
    if selectors and browser.exists(selectors):
        return " ".join((browser.text(selectors) or "").split())[:160] or "a warning message appeared"
    url = getattr(browser, "url", "") or ""
    for part in site_cfg.get("warning_url_any", []):
        if part in url:
            return f"the page address contains {part}"
    return ""


def check_warning(browser, site, site_cfg):
    """Stop at once, with a picture, when the site is warning the account."""
    reason = warning_seen(browser, site_cfg)
    if reason:
        browser.screenshot(f"warning-{site}")
        raise AccountWarning(site, reason)


def active_cooldowns(data, now):
    """site -> entry for the accounts still resting at `now`."""
    out = {}
    for site, entry in (data if isinstance(data, dict) else {}).items():
        try:
            if isinstance(entry, dict) and _parse(entry["until"]) > _parse(now):
                out[site] = entry
        except (KeyError, ValueError, TypeError):
            continue
    return out


def rest_until(now, days):
    return (_parse(now) + timedelta(days=days)).strftime(_TS)


def rest_by_hand(store, sites, now, days, settings):
    """Start a rest straight away, without opening the account: for when a warning was seen on the site itself.
    `days` of 0 means each site's own setting. Returns lines for the log."""
    names = [n.strip().lower() for n in str(sites).split(",") if n.strip()]
    unknown = [n for n in names if n not in COOLDOWN_SITES]
    if not names or unknown:
        raise CollectError(f"--rest needs one or more of {', '.join(COOLDOWN_SITES)}"
                           + (f"; {', '.join(unknown)} is not one" if unknown else ""))
    if days < 0:
        raise CollectError("--rest-days must be 0 (each site's own setting) or more")
    data, lines = store.read_cooldowns(), []
    for site in names:
        length = days or settings.cooldown_days.get(site) or 3
        entry = {"until": rest_until(now, length), "at": now, "reason": "rested by hand"}
        data[site] = entry
        lines.append(f"{site}: resting until {entry['until'][:10]} ({length} days), as you asked; nothing was opened")
    store.write_cooldowns(data)
    return lines


def clear_cooldowns(store, now):
    """Let every resting account be used again (a person has dealt with the warning). Returns how many were resting."""
    data = store.read_cooldowns()
    resting = active_cooldowns(data, now)
    if resting:
        store.write_cooldowns({k: v for k, v in data.items() if k not in resting})
    return len(resting)


def cookie_facts(site, creds):
    """A sentence about the cookies that were loaded (names and counts, never values), or nothing without cookies."""
    text = (creds or {}).get("cookies")
    return f" Cookie facts (no values shown): {describe_cookies(text, site)}." if text else ""


def clear_challenge(browser, site_cfg):
    """Press a security check's own Close button once, if the site's settings name one. A puzzle is never solved.
    True when the check is gone afterwards."""
    selectors = site_cfg.get("challenge_dismiss", [])
    if not selectors or not browser.click(selectors):
        return False
    browser.wait(3)
    return not browser.exists(site_cfg.get("challenge_any", []))


def _closed_note(site_cfg):
    return " (closing it with its X button did not clear it)" if site_cfg.get("challenge_dismiss") else ""


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
    check_warning(browser, site, site_cfg)
    state = session_state(browser, site_cfg)
    if state == "challenge" and clear_challenge(browser, site_cfg):
        state = session_state(browser, site_cfg)
    if state == "challenge":
        browser.screenshot(f"challenge-{site}")
        raise Challenge(f"{site}: the site asked for a security check (a code, a puzzle or 'confirm it's you')"
                        f"{_closed_note(site_cfg)}. "
                        f"Log in once as the dummy account in a normal browser, then export its cookies into {cookies_name}")
    # A password login is the backup for "no cookies". When cookies WERE given and the site did not accept them, a password
    # login from a data-centre address is the likeliest way to get the account warned, so it is not tried: the cookies are
    # reported instead (below), with what they contained.
    if state != "ok" and not creds.get("cookies") and creds.get("username") and creds.get("password") and site_cfg.get("login_steps"):
        try:
            run_steps(browser, site_cfg["login_steps"], creds)
        except BrowserError as exc:
            browser.screenshot(f"login-form-{site}")
            facts = getattr(browser, "form_facts", None)
            raise BrowserError(f"{exc}. Before logging in the page looked '{state}'; after trying, " + describe_form_facts(facts() if callable(facts) else None)) from None
        check_warning(browser, site, site_cfg)
        state = session_state(browser, site_cfg)
        if state == "challenge":
            browser.screenshot(f"challenge-{site}")
            raise Challenge(f"{site}: logging in with the username and password triggered a security check. "
                            f"Log in once in a normal browser and export the cookies into {cookies_name}")
    if state == "login_wall":
        browser.screenshot(f"login-wall-{site}")
        raise LoginWall(f"{site}: not logged in. The cookies in {cookies_name} may have expired; "
                        f"export fresh ones from the dummy account.{cookie_facts(site, creds)}")
    if state == "unknown":
        browser.screenshot(f"unknown-state-{site}")
    return state


def _page_data(browser):
    """Everything readable the page gave us: separate data requests, and the data inside the page itself."""
    embedded = browser.embedded_json() if hasattr(browser, "embedded_json") else []
    return browser.json_responses() + embedded


def rate_limited(responses):
    """True when the site answered a data request with 'rate limit exceeded' (Meta does this when scrolling)."""
    for response in responses:
        if isinstance(response, dict):
            for error in response.get("errors") or []:
                if isinstance(error, dict) and ("rate limit" in str(error.get("message", "")).lower()
                                                or error.get("code") == 1675004):
                    return True
    return False


def _gather(browser, source, site_cfg, scrolls, pause, max_items):
    """Read a results page, scrolling only while more is needed and the site allows.
    Returns (rows, ids found in the site's data, whether the site rate-limited us)."""
    rows, limited = {}, False
    for round_no in range(scrolls + 1):
        for row in extract_json_items(source, _page_data(browser)).values():
            _merge(rows, row)
        limited = rate_limited(browser.json_responses())
        if len(rows) >= max_items or round_no == scrolls or limited:
            break
        browser.scroll(1, pause)
    links = browser.links(site_cfg.get("link_contains", [])) if source in _LINK_PATTERNS else []
    from_data = extract_json_items(source, _page_data(browser))
    for row in from_data.values():
        _merge(rows, row)
    for key, row in extract_link_items(source, links).items():
        if key not in rows:
            rows[key] = row
    return list(rows.values())[:max_items], set(from_data), limited


def seed_label(url):
    """A short name for the seed behind a page address: the search words, #hashtag, @profile."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if query.get("view_all_page_id"):
        return "page:" + query["view_all_page_id"][0]
    if query.get("q"):
        return query["q"][0]
    match = re.search(r"/tags?/([^/?]+)", parts.path)
    if match:
        return "#" + unquote(match.group(1))
    match = re.search(r"/(@[^/?]+)", parts.path)
    return unquote(match.group(1)) if match else url


def add_variant_counts(rows):
    """Several ads with the same advertiser, title and text are copies of one creative: say how many.

    Every copy shows the same number: the larger of how many we found and what the site itself reports.
    """
    groups = collections.defaultdict(list)
    for row in rows:
        if row["text"] or row["title"]:
            groups[(row["handle"], row["title"], row["text"])].append(row)
    for members in groups.values():
        count = max(len(members), *(r.get("variants") or 1 for r in members))
        if count > 1:
            for row in members:
                row["variants"] = count
    return rows


def search_for_tag(browser, site_cfg, tag):
    """Search the site for #tag the way a person does: click the search box, type the tag, press Enter. If the box
    cannot be found or typed into, the search address is opened instead. Returns how it was done."""
    box = site_cfg.get("search_box", [])
    if box and browser.type_text(box, "#" + tag, submit=True):
        return "typed"
    browser.open(site_cfg["search_url"].format(q=quote_plus("#" + tag)))
    return "address"


def take_pages(urls, budget, cursor):
    """The pages to open in careful mode: at most `budget` of them, starting where the last run stopped, so over
    several runs every page gets its turn. Returns (urls to open, how many there were in all)."""
    if budget is None or len(urls) <= budget:
        return list(urls), len(urls)
    start = cursor % len(urls)
    return (list(urls[start:]) + list(urls[:start]))[:max(budget, 0)], len(urls)


def run_browser_task(browser, task, n, site_cfg, browser_cfg, creds, page_budget=None, cursor=0):
    """Collect up to n ads or posts for one task. Returns (rows, stats).

    Each page gets an equal share of what is still needed, so a page that gives less than its
    share leaves more for the pages after it.
    """
    source, state = task.source, "ok"
    scrolls, pause = site_scrolls(browser_cfg, source), site_pacing(browser_cfg, source)["scroll_pause_seconds"]
    if source != "meta_ads":
        state = ensure_session(browser, source, site_cfg, creds)
    urls, pages_total = take_pages(seed_urls(task, site_cfg), page_budget, cursor)
    found, from_data, limited, via = {}, set(), False, {}
    fallbacks, page_errors, how = [], 0, []
    errors, challenges, settle = site_cfg.get("error_any", []), site_cfg.get("challenge_any", []), site_cfg.get("settle_seconds", 3)

    def search_instead(tag):
        fallbacks.append("#" + tag)
        how.append(search_for_tag(browser, site_cfg, tag))
        browser.wait(settle)

    for number, url in enumerate(urls):
        browser.open(url)
        browser.wait(settle)
        if source != "meta_ads":
            check_warning(browser, source, site_cfg)                  # a warning on any page stops the run at once
        # In the automated browser a tag page can fail (an error, or a puzzle) while the same search works by hand.
        tag = unquote(url.split("/tag/", 1)[1].split("?")[0]) if site_cfg.get("hashtag_via_search_on_error") and "/tag/" in url else None
        searched = pressed_x = False
        if tag is not None and (browser.exists(errors) or browser.exists(challenges)):
            if browser.exists(challenges):
                clear_challenge(browser, site_cfg)
                pressed_x = True                                      # once per page: never retried on this one
            if browser.exists(errors) or browser.exists(challenges):
                search_instead(tag)
                searched, pressed_x = True, False                     # the search page is a new page, with its own puzzle
        if source == "meta_ads":
            browser.click(site_cfg.get("consent_any", []))
        else:
            after = session_state(browser, site_cfg)
            if after == "challenge" and not pressed_x and clear_challenge(browser, site_cfg):
                after = session_state(browser, site_cfg)
            if after == "challenge":
                browser.screenshot(f"challenge-{source}")
                raise Challenge(f"{source}: a security check appeared while collecting{_closed_note(site_cfg)}; clear it "
                                "once in a normal browser as the dummy account and export fresh cookies")
            if after == "login_wall":
                browser.screenshot(f"login-wall-{source}")
                raise LoginWall(f"{source}: the page asked for a login; the cookies may have expired")
        share = max(1, (n - len(found)) // (len(urls) - number))
        rows, data_ids, hit_limit = _gather(browser, source, site_cfg, scrolls, pause, share)
        if tag is not None and not searched and not rows and browser.exists(errors):        # the error came after the first wait
            search_instead(tag)
            rows, data_ids, hit_limit = _gather(browser, source, site_cfg, scrolls, pause, share)
        if errors and browser.exists(errors):
            page_errors += 1
        if source != "meta_ads":
            check_warning(browser, source, site_cfg)                  # ... and so does one that appeared while reading
        from_data |= data_ids
        limited = limited or hit_limit
        label = (task.payload.get("labels") or {}).get(url) or seed_label(url)
        for row in rows:
            labels = via.setdefault(row["item_id"], [])
            if label not in labels:
                labels.append(label)
            _merge(found, row)
        if len(found) >= n:
            break
    kept = list(found.values())[:n]
    for row in kept:
        row["found_via"] = " | ".join(via.get(row["item_id"], []))
    if source == "meta_ads":
        add_variant_counts(kept)
    if not kept:
        browser.screenshot(f"empty-{source}")
    in_data = sum(1 for r in kept if r["item_id"] in from_data)
    return kept, {"json": in_data, "links": len(kept) - in_data, "state": state, "rate_limited": limited and len(kept) < n,
                  "fallbacks": fallbacks, "fallback_how": how, "page_errors": page_errors,
                  "pages": len(urls) + len(how), "pages_planned": len(urls), "pages_total": pages_total}


def _snip(text):
    return " ".join(str(text or "").split())[:300]


def switch_to_deep_research(browser, site_cfg):
    """Press the buttons that switch the Gemini chat to Deep Research (the menu next to the prompt box, then
    More tools, then Deep research). Raises BrowserError naming the step that could not be found. Returns a
    note when the page did not confirm the switch (the prompt box should then say what to research), else ''."""
    for number, selectors in enumerate(site_cfg["deep_research_clicks"], start=1):
        if not browser.click(selectors):
            browser.screenshot("gemini-no-deep-research-button")
            raise BrowserError(f"gemini: could not find the button to switch to Deep Research (step {number}); "
                               "the page may have changed, or the account may not have Deep Research. "
                               "See the screenshot and update deep_research_clicks in browser.yaml")
        browser.wait(1)
    browser.wait(1)
    active = site_cfg.get("deep_research_active_any", [])
    if active and not browser.exists(active):
        browser.screenshot("gemini-deep-research-unconfirmed")
        return ("the buttons were pressed but the page did not show Deep Research as switched on "
                "(see gemini-deep-research-unconfirmed; check deep_research_active_any in browser.yaml)")
    return ""


_RESTART = re.compile(r"(?<!#)# (?=[A-Z])")          # "# Title" starting a second document, even glued to the end of the first


def _distinct_words(text):
    return {w for w in re.findall(r"[a-z]{5,}", text.lower())}


def single_copy(text):
    """Gemini's page can hand back the report twice: a first copy that stops in the middle, then a complete one
    starting with its own "# Title". Returns (text, doubled). The second part is only taken for a repeat of the first
    when it restarts with a top-level heading AND shares most of its distinctive words (about 70% in a real doubled
    report, about 40% between two sections of one genuine report, so the bar is 60%); the longer copy is kept."""
    n = len(text)
    for m in _RESTART.finditer(text):
        first, second = text[:m.start()], text[m.start():]
        if len(first) < 0.25 * n or len(second) < 0.5 * len(first):
            continue
        a, b = _distinct_words(first), _distinct_words(second)
        if a and b and len(a & b) / len(a) >= 0.6 and len(a & b) / len(a | b) >= 0.3:
            return (second if len(second) >= 0.9 * len(first) else first).strip(), True
    return text, False


def collect_deep_research(browser, question, site_cfg, *, claude=None, context="", claude_budget=0,
                          max_rounds=3, prepare_prompt=True, review_rounds=0):
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
    mode_note = switch_to_deep_research(browser, site_cfg)
    if mode_note:
        notes.append(mode_note)
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
    start_clicked = browser.click(site_cfg["start_research"])         # absent when Gemini started on its own
    limit = wait["max_minutes"] * 60
    last_text, stable = "", 0
    while True:
        browser.wait(poll)
        waited += poll
        text = browser.text(site_cfg["response"])
        stable = stable + poll if text and text == last_text else 0
        last_text = text
        working = browser.exists(site_cfg.get("still_working_any", []))
        done_seen = browser.exists(site_cfg["done_any"])
        if len(text) >= wait["min_chars"] and not working and (done_seen or stable >= wait["settle_seconds"]):
            break
        if waited >= limit:
            browser.screenshot("gemini-timeout")
            raise BrowserError(f"gemini: the report was not finished after {wait['max_minutes']} minutes "
                               "(see the screenshot; raise wait.max_minutes in browser.yaml if it was still working)"
                               + (f". Last thing Gemini showed: {_snip(last_text)}" if last_text else ""))
    browser.screenshot("gemini-report")                 # the finished page, so the report's own element can be pinned down
    links = browser.response_links(site_cfg["response"])
    found = []
    for url in links + re.findall(r"https?://[^\s)\]>\"'<]+", text):
        url = url.rstrip(".,;")
        if url not in found:
            found.append(url)
    raw_text = text
    text, doubled = single_copy(text)
    if doubled:
        notes.append("the page returned the report twice; the complete copy was kept (the raw text is saved beside it)")
    if not start_clicked and not done_seen:
        notes.append("no Start research button was pressed and no finished-report button appeared: this may be an ordinary "
                     "chat answer and not a Deep Research report (see the saved gemini-report picture)")
    elif not start_clicked:
        notes.append("Gemini started the research on its own (no Start research button was shown)")
    reviews, followups, shown = [], [], last_text
    if review_rounds > 0 and claude is not None:
        for round_no in range(1, review_rounds + 1):
            if claude_budget - calls < 1:
                notes.append("the check of the report was skipped: Claude's call limit is reached")
                break
            try:
                verdict = deepcheck.review_report(claude, question, prompt, deepcheck.join_report(text, followups), found, round_no)
                calls += 1
            except (AuthError, QuotaError, ProviderError) as exc:
                notes.append(f"Claude could not check the report ({exc}); it stands as it is")
                break
            reviews.append({"round": round_no, **verdict})
            if verdict["verdict"] == "ok":
                break
            if not deepcheck.send_followup(browser, site_cfg, verdict["follow_up"]):
                browser.screenshot("gemini-followup-not-sent")
                notes.append("Claude asked for a follow-up but it could not be sent (prompt box or send button not found); "
                             "the report stands as it is")
                break
            answer, seconds, finished = deepcheck.wait_for_followup(browser, site_cfg, shown, max(0, limit - waited))
            waited += seconds
            if not finished:
                browser.screenshot("gemini-followup-timeout")
                notes.append(f"the answer to follow-up {round_no} was not finished in time"
                             + ("; what had arrived is included" if answer else "; nothing arrived"))
            if answer:
                shown = answer
                answer = single_copy(answer)[0].strip()
                for url in browser.response_links(site_cfg["response"]) + re.findall(r"https?://[^\s)\]>\"'<]+", answer):
                    url = url.rstrip(".,;")
                    if url not in found:
                        found.append(url)
            followups.append({"message": verdict["follow_up"], "answer": answer, "finished": finished})
            if not answer:
                break
        if reviews:
            notes.append("Claude checked the report for completeness and checkable examples; it cannot confirm that the facts are true")
    text = deepcheck.join_report(text, followups)
    if not found:
        notes.append("no source links were found in the report")
    return {"text": text, "raw_text": raw_text, "doubled": doubled, "sources": found[:30], "waited_seconds": waited,
            "prompt": prompt, "exchanges": exchanges, "claude_calls": calls, "notes": notes, "reviews": reviews, "followups": followups,
            "diagnostics": {"mode_confirmed": not mode_note, "start_clicked": bool(start_clicked), "done_button_seen": bool(done_seen),
                            "plan_seconds": plan_waited, "waited_seconds": waited, "review_rounds_used": len(reviews)}}


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
    follow: dict = field(default_factory=dict)        # brand slug -> bench.FollowPlan, as it stands at the end of the run
    resting: dict = field(default_factory=dict)       # site -> {until, at, reason}: accounts resting after a warning

    @property
    def needs_review(self):
        return sum(1 for plan in self.follow.values() for r in plan.review if r["status"] == "ambiguous")

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
    if newest and newest.get("partial"):
        return None                                  # a run that covered only some pages is not a finished collection
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
    if not deep and backend in ("apify", "fallback", "extra"):
        if apify is None:
            why = why or "no key: set APIFY_TOKEN"
        elif apify_state["disabled"]:
            why = why or apify_state["disabled"]
        elif apify_state["exhausted"]:
            why = why or "Apify is out of credit this run"
        else:
            routes.append("apify")
    return routes, why


def _fill(base, other):
    """`base` with every blank field filled from `other`, and where each one was found from both."""
    out = dict(base)
    for key, value in other.items():
        if out.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            out[key] = value
    seen = [v for v in f"{base.get('found_via', '')} | {other.get('found_via', '')}".split(" | ") if v]
    out["found_via"] = " | ".join(dict.fromkeys(seen))
    return out


def merge_routes(parts):
    """Combine what the browser and Apify each found for one task. An ad or post both found is kept once, with
    the richer copy filling the other's blanks. Returns (rows, stats) where stats counts each route."""
    found = {}
    for _, rows, _ in parts:
        for row in rows:
            old = found.get(row["item_id"])
            if old is None:
                found[row["item_id"]] = dict(row)
            else:
                base, other = (row, old) if _richness(row) > _richness(old) else (old, row)
                found[row["item_id"]] = _fill(base, other)
    merged = list(found.values())
    first = parts[0][2]
    stats = {"json": sum(p[2].get("json", 0) for p in parts), "links": sum(p[2].get("links", 0) for p in parts),
             "routes": {route: len(rows) for route, rows, _ in parts},
             "common": sum(len(rows) for _, rows, _ in parts) - len(merged),
             "rate_limited": any(p[2].get("rate_limited") for p in parts)}
    for key in ("fallbacks", "fallback_how", "page_errors", "pages", "pages_planned", "pages_total"):
        if key in first:
            stats[key] = first[key]
    return merged, stats


def _context(brand, task):
    return f"Brand: {brand.name}. Website: {brand.domain}. {task.payload.get('context', '')}".strip()


def _page_notes(stats):
    """What happened to tag pages that errored, for run and check reports."""
    text = ""
    if stats.get("fallbacks"):
        way = " and ".join(sorted(set(stats.get("fallback_how") or []))).replace("typed", "typed into the search box, as a person would") \
            .replace("address", "the search address")
        text += (f" - the page for {', '.join(stats['fallbacks'])} would not load (an error or a security check), "
                 f"so TikTok's search results were read instead ({way or 'searched'})")
    if stats.get("pages_planned", 0) < stats.get("pages_total", 0):
        text += (f" - careful mode: opened {stats['pages_planned']} of {stats['pages_total']} pages this run (the page caps in browser.yaml); "
                 "the rest are taken on the next runs")
    if stats.get("page_errors"):
        text += f" - {stats['page_errors']} page(s) still showed 'Something went wrong' (a block on the automated browser, or try again later)"
    return text


def apply_discovery(state, task, rows, stats, now):
    """File a decision for each brand a look-up task searched for. Returns (new state, lines for the report).

    A brand is left undecided (and looked up again next run) when the site rate-limited the search and nothing
    for it came back: a failure to read is not the same as 'this brand has no page'."""
    lines = []
    for item in task.payload["discover"]:
        candidates = bench.candidates_from_items(rows, item["name"])
        if not candidates and stats.get("rate_limited"):
            lines.append(f"{item['name']}: the site rate-limited the search; it will be looked up again")
            continue
        resolution = bench.resolve(item["name"], candidates)
        state = bench.record(state, item["name"], item["kind"], resolution, now)
        lines.append(f"{item['name']}: {resolution['status']} - {resolution['reason']}"
                     + (f" -> {resolution['page_name']} (id {resolution['page_id']})" if resolution["status"] == "resolved" else ""))
    return state, lines


def check_followed(task, rows):
    """Followed pages that gave back no ads from that page: a wrong id, or a page with nothing running."""
    got = collections.Counter(str(r.get("page_id") or "") for r in rows)
    return [f"{p['name']} (page {p['page_id']}) returned no ads from that page; check the id, or it may have nothing running"
            for p in task.payload.get("pages", []) if not got.get(str(p["page_id"]))]


def _follow_plan(brand, cfg, auto, state, settings, now):
    names = (auto or {}).get("benchmarks", []) if cfg.get("auto_seeds", True) else []
    own = brand.name if cfg.get("track_own_brand", True) else ""        # not tracking the brand means no page look-up either
    return bench.plan_follow(own, names, cfg["ads"]["benchmark_pages"], state, now,
                             max_pages=settings.benchmark_max_pages, discovery_per_run=settings.benchmark_discovery_per_run,
                             retry_days=settings.benchmark_retry_days)


def format_benchmark_status(brand, store, auto, settings, now, root="brands"):
    """The benchmark review list as it stands, without collecting anything."""
    from .research import load_brand_research         # local import: research imports this module
    cfg = load_brand_research(brand, root)["collect"]
    state = bench.clean_state(store.read_benchmarks(brand.slug))
    return "\n" + bench.format_follow_plan(_follow_plan(brand, cfg, auto, state, settings, now), brand.slug, brand.name)


def run_collect(brands, store, *, browser_factory, creds, settings, browser_cfg, ledger_cls, sources, now,
                run_id, configs=None, dry_run=False, root="brands", apify=None, claude=None, backend="browser", region=None,
                health=None, registry=None):
    """Plan and run collection tasks for each brand, within the caps in `settings`.

    backend: browser (the default), apify (Apify only) or fallback (the browser first, Apify when
    the browser fails). Deep Research always uses the browser.

    health (a HealthLedger) records how each route does and keeps a route that is resting after a failure from being
    asked again; registry (the declared sources) lets each saved batch carry its provenance. Both are optional.
    """
    from .research import load_brand_research         # local import: research imports this module
    ledger = ledger_cls(store.load_usage(), now[:7])
    result = CollectResult(run_id=run_id, now=now, dry_run=dry_run)
    disabled, opened, deep_runs = {}, {}, 0
    apify_state = {"disabled": None, "exhausted": False}
    cooldowns = store.read_cooldowns()
    result.resting = dict(active_cooldowns(cooldowns, now))
    for site, entry in result.resting.items():            # an account that was warned rests: skipped, not failed
        disabled[site] = f"resting until {entry['until'][:10]} after an account warning ({str(entry.get('reason', ''))[:100]})"

    page_log = store.read_page_log()
    today, run_pages = now[:10], {}

    def page_budget(site, site_cfg):
        """Pages a careful site may still open: under its per-run cap and its per-day cap. None means no caps."""
        left = []
        if site_cfg.get("max_pages_per_run"):
            left.append(site_cfg["max_pages_per_run"] - run_pages.get(site, 0))
        if site_cfg.get("max_pages_per_day"):
            left.append(site_cfg["max_pages_per_day"] - page_log["days"].get(today, {}).get(site, 0))
        return max(0, min(left)) if left else None

    def cap_message(site, site_cfg):
        per_run = site_cfg.get("max_pages_per_run")
        if per_run and run_pages.get(site, 0) >= per_run:
            return f"careful mode: {site} has opened its {per_run} pages for this run; the next run continues"
        return (f"careful mode: {site} has opened its {site_cfg.get('max_pages_per_day')} pages for today ({today}); "
                "it continues tomorrow")

    def note_pages(site, stats):
        opened = stats.get("pages", 0)
        run_pages[site] = run_pages.get(site, 0) + opened
        day = page_log["days"].setdefault(today, {})
        day[site] = day.get(site, 0) + opened
        page_log["cursor"][site] = page_log["cursor"].get(site, 0) + stats.get("pages_planned", opened)
        store.write_page_log(page_log)

    def rest_after(exc, site):
        """After an account warning: stop using the account for its rest period, and say so in the report."""
        if not isinstance(exc, AccountWarning):
            return ""
        days = settings.cooldown_days.get(site, 0)
        if days <= 0 or dry_run:
            return " It will not be used again in this run."
        entry = {"until": rest_until(now, days), "at": now, "reason": exc.reason[:200]}
        cooldowns[site] = entry
        store.write_cooldowns(cooldowns)
        result.resting[site] = entry
        return (f" The account will rest until {entry['until'][:10]} ({days} days); change this under "
                "collectors.safety.cooldown_days in research.yaml.")

    def items_left(route):
        if route == "apify":
            return min(settings.apify_items_per_run, settings.apify_monthly_items - ledger.month_used("apify_items"))
        if not settings.monthly_items:                    # 0 means no monthly limit: only the per-run limit applies
            return settings.items_per_run
        return min(settings.items_per_run, settings.monthly_items - ledger.month_used("collected_items"))

    def seen(source, route, *, exc=None, items=0, message=""):
        """Tell the health ledger how a route just did. Nothing happens without a ledger, or in a dry run."""
        if health is None or dry_run:
            return
        if exc is not None:
            err = contract.classify_exception(exc)
            health.observe(source, route, ok=False, code=err["code"], message=err["message"], now=now)
        elif items:
            health.observe(source, route, ok=True, items=items, now=now)
        else:
            health.observe(source, route, ok=False, code=contract.UPSTREAM_BROKEN,
                           message=message or "the route answered but nothing usable came back", now=now)

    def save(brand, task, rows, stats, via, extra=None):
        record = {"at": now, "run": run_id, "source": task.source, "key": task.key, "seeds": task.seeds, "via": via,
                  "from_site_data": stats.get("json", 0), "from_links": stats.get("links", 0), "items": rows}
        if registry is not None:
            first = via.split("+")[0]
            record["provenance"] = evidence_mod.provenance(registry, task.source, via, now, run_id,
                                                           health.status(task.source, first, now) if health is not None else "")
        record.update(extra or {})
        store.append_collected(brand.slug, record)
        store.save_usage(ledger.usage)

    with ExitStack() as stack:
        for brand in brands:
            reports = result.brands.setdefault(brand.slug, [])
            cfg = (configs or {}).get(brand.slug) or load_brand_research(brand, root)["collect"]
            if region:                                          # --region: this run only, over the brand's own country
                cfg = copy.deepcopy(cfg)
                cfg["ads"]["country"] = ads_country(region, "--region")
            records = store.read_collected(brand.slug)
            auto = None
            if settings.auto_enabled and cfg.get("auto_seeds", True):
                from .autoseeds import ensure_auto_seeds        # local import: autoseeds does not depend on this module
                seeded = ensure_auto_seeds(brand, store, claude, settings, ledger, now, dry_run)
                auto = seeded.seeds
                if seeded.note:
                    result.notes.append(f"{brand.slug}: auto seeds {seeded.note}")
            cfg = with_own_brand(cfg, brand)
            state = bench.clean_state(store.read_benchmarks(brand.slug))
            follow = _follow_plan(brand, cfg, auto, state, settings, now) if settings.benchmarks_enabled else None
            tasks = plan_collections(cfg, auto, settings, follow)
            planned_pages = {str(p["page_id"]) for t in tasks for p in t.payload.get("pages", [])}
            for task in tasks:              # the list can grow: a brand found by a look-up is followed in the same run
                if task.source not in sources:
                    continue
                site = SITE_OF[task.source]
                site_cfg = browser_cfg["sites"][site]
                site_creds = creds.get(site, {})
                deep = task.source == "deep_research"
                report = TaskReport(task.source, task.seeds, "skipped")
                reports.append(report)
                routes, why = _routes(deep, backend, apify, apify_state, site_cfg, site_creds, disabled.get(site))
                if health is not None and routes:
                    resting = {r: health.is_paused(task.source, r, now) for r in routes}
                    if any(resting.values()):
                        why = why or next(p["message"] for p in resting.values() if p)
                        routes = [r for r in routes if not resting[r]]
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
                                                    prepare_prompt=settings.claude_prepare_prompt,
                                                    review_rounds=settings.claude_review_rounds)
                        ledger.charge("deep_research_tasks", 1)
                        if out["claude_calls"]:
                            ledger.charge("claude_calls", out["claude_calls"])
                        name = f"{run_id}-{task.key}.md"
                        store.write_deep_report(brand.slug, name, out["text"])
                        if out["doubled"]:
                            store.write_deep_report(brand.slug, name[:-3] + "-raw.md", out["raw_text"])
                        d = out["diagnostics"]
                        item = _row("deep_research", f"{task.key}-{now[:10]}", handle="Gemini Deep Research",
                                    title=task.payload["question"], text=out["text"],
                                    url=f"data/{brand.slug}/research/deep/{name}",
                                    details=(f"Deep Research confirmed on: {'yes' if d['mode_confirmed'] else 'NOT CONFIRMED'} | "
                                             f"Start research pressed: {'yes' if d['start_clicked'] else 'no'} | "
                                             f"finished-report button seen: {'yes' if d['done_button_seen'] else 'no'} | "
                                             f"waited {d['waited_seconds']} s"))
                        item["extra_sources"] = out["sources"]
                        seen(task.source, "browser", items=1)
                    except (LoginWall, Challenge) as exc:
                        seen(task.source, "browser", exc=exc)
                        message = str(exc) + (cookie_facts(site, site_creds)
                                              if isinstance(exc, LoginWall) and "Cookie facts" not in str(exc) else "") + rest_after(exc, site)
                        disabled[site] = message
                        result.problems_for.append(site)
                        result.notes.append(message)
                        report.status, report.detail = "failed", message
                        continue
                    except BrowserError as exc:
                        seen(task.source, "browser", exc=exc)
                        report.status, report.detail = "failed", str(exc)
                        if site not in opened:
                            disabled[site] = str(exc)
                            result.problems_for.append(site)
                        continue
                    save(brand, task, [item], {}, "browser", {"deep": {
                        "prompt": out["prompt"], "exchanges": out["exchanges"], "claude_calls": out["claude_calls"],
                        "notes": out["notes"], "diagnostics": out["diagnostics"], "reviews": out["reviews"],
                        "followups": [{"message": f["message"], "finished": f["finished"]} for f in out["followups"]]}})
                    report.status, report.items = "ran", 1
                    report.detail = (f"report received after {out['waited_seconds'] // 60} min"
                                     + (f"; {len(out['exchanges'])} question(s) from Gemini answered by Claude" if out["exchanges"] else "")
                                     + "".join(f"; {n}" for n in out["notes"]))
                    continue
                def allowed(route, task=task):
                    left = items_left(route)
                    return min(left, task.item_cap) if task.item_cap else left
                viable = [(r, allowed(r)) for r in routes if allowed(r) >= 1]
                if not viable:
                    report.detail = "out of items under your monthly cap"
                    continue
                if dry_run:
                    picks = viable if backend == "extra" else viable[:1]
                    for pick_route, pick_n in picks:
                        ledger.charge("apify_items" if pick_route == "apify" else "collected_items", pick_n)
                    route, n = picks[0]
                    careful = page_budget(site, site_cfg) if any(r == "browser" for r, _ in picks) else None
                    report.status = "would_run"
                    if len(picks) > 1:
                        report.detail = (f"up to {picks[0][1]} items from {site} and up to {picks[1][1]} via Apify "
                                         f"({settings.actors[task.source]}), merged")
                    else:
                        report.detail = (f"up to {n} items via Apify ({settings.actors[task.source]})" if route == "apify"
                                         else f"up to {n} items from {site}" + (" (Apify if the browser fails)" if "apify" in routes and backend != "extra" else
                                                                                " (Apify is an extra, but has no key or credit)" if backend == "extra" else ""))
                    if careful is not None:
                        report.detail += f" (careful mode: opens at most {careful} pages)"
                    continue
                errors, rows, stats, via, hard, parts = [], None, None, None, False, []
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
                            budget = page_budget(site, site_cfg)
                            if budget is not None and budget <= 0:
                                errors.append(cap_message(site, site_cfg))
                                continue
                            if site not in opened:
                                opened[site] = stack.enter_context(browser_factory(site))
                            rows, stats = run_browser_task(opened[site], task, n, site_cfg, browser_cfg, site_creds,
                                                           page_budget=budget, cursor=page_log["cursor"].get(site, 0))
                            ledger.charge("collected_items", len(rows))
                            note_pages(site, stats)
                    except (LoginWall, Challenge) as exc:
                        seen(task.source, route, exc=exc)
                        message = str(exc) + rest_after(exc, site)
                        disabled[site] = message
                        result.problems_for.append(site)
                        result.notes.append(message)
                        errors.append(message)
                        hard = True
                        continue
                    except BrowserError as exc:
                        seen(task.source, route, exc=exc)
                        errors.append(str(exc))
                        hard = True
                        if site not in opened:                       # the browser itself would not start
                            disabled[site] = str(exc)
                            result.problems_for.append(site)
                        continue
                    except AuthError as exc:
                        seen(task.source, route, exc=exc)
                        apify_state["disabled"] = str(exc)
                        result.problems_for.append("apify")
                        result.notes.append(f"apify switched off for this run: {exc}")
                        errors.append(str(exc))
                        hard = True
                        continue
                    except QuotaError as exc:
                        seen(task.source, route, exc=exc)
                        apify_state["exhausted"] = True
                        result.notes.append(f"apify is out of credit: {exc}")
                        errors.append(str(exc))
                        continue
                    except ProviderError as exc:
                        seen(task.source, route, exc=exc)
                        errors.append(str(exc))
                        hard = True
                        continue
                    if rows:
                        seen(task.source, route, items=len(rows))
                        parts.append((route, rows, stats))
                        if backend != "extra":
                            break
                        continue                                     # as an extra, the other route runs too
                    hard = True
                    errors.append("the page opened but no ads or posts could be read "
                                  "(a screenshot was saved; run Recon to see what the site sends)"
                                  if route == "browser" else "Apify returned no readable items")
                    seen(task.source, route, message=errors[-1])
                if parts:
                    route, rows, stats = parts[0]
                    via = route
                    if len(parts) > 1:
                        rows, stats = merge_routes(parts)
                        via = "+".join(r for r, _, _ in parts)
                if via is None:
                    report.status = "failed" if hard else "skipped"
                    report.detail = " | ".join(errors)[:400]
                    continue
                partial = stats.get("pages_planned", 0) < stats.get("pages_total", 0)
                save(brand, task, rows, stats, via, {"partial": True, "pages_done": stats["pages_planned"],
                                                     "pages_total": stats["pages_total"]} if partial else None)
                extra = []
                if task.payload.get("discover"):
                    state, lines = apply_discovery(state, task, rows, stats, now)
                    store.write_benchmarks(brand.slug, state)
                    extra += lines
                    fresh = [p for p in _follow_plan(brand, cfg, auto, state, settings, now).pages
                             if str(p["page_id"]) not in planned_pages]
                    if fresh:
                        planned_pages |= {str(p["page_id"]) for p in fresh}
                        tasks.extend(_meta_tasks({"ads": {**cfg["ads"], "pages": [], "keywords": []}},
                                                 bench.FollowPlan(pages=fresh), settings))
                extra += check_followed(task, rows)
                result.notes.extend(f"{brand.slug}: {line}" for line in extra)
                report.status, report.items = "ran", len(rows)
                report.detail = (f"{len(rows)} items: {stats['routes'].get('browser', 0)} from the browser, "
                                 f"{stats['routes'].get('apify', 0)} via Apify, {stats['common']} found by both"
                                 + (f" ({errors[0][:100]})" if errors else "")
                                 if "+" in via else
                                 f"{len(rows)} items via Apify" + (f" (the browser failed: {errors[0][:100]})" if errors else "")
                                 if via == "apify" else
                                 f"{len(rows)} items ({stats['json']} from the site's data, {stats['links']} only from page links)"
                                 + (" - the site rate-limited further scrolling, so this is the first page of results only"
                                    if stats.get("rate_limited") else "")
                                 + _page_notes(stats))
            if follow is not None and "meta_ads" in sources:
                result.follow[brand.slug] = _follow_plan(brand, cfg, auto, state, settings, now)
    return result


def format_collect_report(result, settings, ledger=None):
    lines = [f"Collection run {result.now}" + ("  [dry run: nothing was collected or saved]" if result.dry_run else "")]
    for site, entry in sorted(result.resting.items()):
        lines.append(f"Resting: {site} until {entry['until'][:10]} after an account warning ({str(entry.get('reason', ''))[:100]}); "
                     "its browser tasks are skipped, and Apify still runs if it is switched on")
    for slug, reports in result.brands.items():
        lines.append("")
        lines.append(f"=== {slug} ===")
        if not reports:
            lines.append("  nothing to collect: no seeds written in research.yaml, and no catalogue yet to build them from (run Sync first)")
        for r in reports:
            lines.append(f"  {r.source:<13} {r.status:<9} {r.seeds}" + (f" -> {r.detail}" if r.detail else ""))
    for slug, plan in result.follow.items():
        lines += ["", f"--- {slug}: benchmark brands ---", bench.format_follow_plan(plan, slug, slug)]
    if result.needs_review:
        lines += ["", f"ACTION: {result.needs_review} benchmark brand(s) are ambiguous and are not being followed until you "
                      "choose a page (see NEEDS YOU above)."]
    if ledger is not None:
        lines.append("")
        items_cap = f"/{settings.monthly_items}" if settings.monthly_items else " (no monthly cap)"
        lines.append(f"Used this month ({ledger.month}), against your caps: items {ledger.month_used('collected_items'):g}"
                     f"{items_cap}, Apify items {ledger.month_used('apify_items'):g}/{settings.apify_monthly_items}, "
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
                                max_cost_usd=min(settings.apify_check_max_cost_usd, settings.apify_max_cost_per_run_usd))
            except ProviderConfigError as exc:
                row["detail"] = "skipped: " + str(exc)
                row["code"] = contract.code_for(exc)
                continue
            except (AuthError, QuotaError, ProviderError) as exc:
                row["detail"] = str(exc)
                row["code"] = contract.code_for(exc)
                continue
            ledger.charge("apify_items", len(raw))
            store.save_usage(ledger.usage)
            got, bad = normalize_items(source, raw)
            row["ok"] = bool(got)
            row["items"] = len(got)
            row["detail"] = (f"via Apify: {len(raw)} items, {len(got)} readable" + (f", {bad} unreadable" if bad else "")
                             if raw else "via Apify: the scraper ran but returned no items (check the actor name in research.yaml)")
            continue
        site_creds = creds.get(site, {})
        if not has_access(site_cfg, site_creds):
            row["detail"] = missing_access_message(site_cfg)
            continue
        if source != "deep_research" and settings.monthly_items and settings.monthly_items - ledger.month_used("collected_items") < items:
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
                        raise LoginWall("gemini: not logged in (Gemini shows its Sign in button, so the cookies were not "
                                        f"accepted); export fresh cookies into GEMINI_COOKIES.{cookie_facts('gemini', site_creds)}")
                    has_box = browser.exists(site_cfg["prompt_box"])
                    if not has_box:
                        row["detail"] = "Gemini opened but the prompt box was not found (update prompt_box in browser.yaml)"
                        continue
                    unconfirmed = switch_to_deep_research(browser, site_cfg)
                    browser.screenshot("check-gemini-deep-research")
                    row["ok"] = not unconfirmed
                    row["items"] = 0 if unconfirmed else 1
                    row["detail"] = (f"Gemini opened (login looks {state}), the prompt box is there and the Deep Research "
                                     "button was found and pressed; no research was started"
                                     if not unconfirmed else f"Gemini opened (login looks {state}) but {unconfirmed}")
                    continue
                found, stats = run_browser_task(browser, CHECK_SEEDS[source](), items, site_cfg, browser_cfg, site_creds)
                browser.screenshot(f"check-{source}")
        except (LoginWall, Challenge, BrowserError) as exc:
            row["detail"] = str(exc)
            row["code"] = contract.code_for(exc)
            continue
        ledger.charge("collected_items", len(found))
        store.save_usage(ledger.usage)
        row["ok"] = bool(found)
        row["items"] = len(found)
        row["detail"] = (f"login {stats['state']}; {len(found)} items ({stats['json']} from the site's data, "
                         f"{stats['links']} only from page links)"
                         + (" - rate-limited, first page only" if stats.get("rate_limited") else "")
                         + _page_notes(stats)
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


def format_test_file(out):
    """Everything about one report in one readable file: what was asked, what happened, what came back."""
    d = out["diagnostics"]
    lines = ["# Deep Research test", "", "## Prompt sent to Gemini", out["prompt"], "",
             "## Questions from Gemini and Claude's answers"]
    lines += [f"- Gemini: {e['gemini']}\n  Claude: {e['reply']}" for e in out["exchanges"]] or ["(Gemini asked nothing)"]
    lines += ["", "## What happened",
              f"- Deep Research switched on (the prompt box asked what to research): {'yes' if d['mode_confirmed'] else 'NOT CONFIRMED'}",
              f"- Start research button pressed: {'yes' if d['start_clicked'] else 'no'}",
              f"- finished-report button seen: {'yes' if d['done_button_seen'] else 'no'}",
              f"- waited {d['waited_seconds']} seconds in all, {d['plan_seconds']} of them for the plan",
              f"- report length: {len(out['text'])} characters; claude calls: {out['claude_calls']}"]
    lines += [f"- NOTE: {n}" for n in out["notes"]]
    lines += ["", "## Report", out["text"], "", "## Sources found"]
    lines += [f"- {u}" for u in out["sources"]] or ["(none found)"]
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
        return {"ok": False, "detail": str(exc) + (cookie_facts("gemini", creds.get("gemini", {}))
                                                   if isinstance(exc, LoginWall) and "Cookie facts" not in str(exc) else "")}
    if out["claude_calls"]:
        ledger.charge("claude_calls", out["claude_calls"])
        store.save_usage(ledger.usage)
    path = Path(debug_dir) / "test-deep-research.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(format_test_file(out), encoding="utf-8")
    if out["doubled"]:
        (Path(debug_dir) / "test-deep-research-raw.txt").write_text(out["raw_text"], encoding="utf-8")
    return {"ok": True, "detail": (f"a report of {len(out['text'])} characters arrived after {out['waited_seconds'] // 60} minutes; "
                                    f"Claude made {out['claude_calls']} call(s); {len(out['exchanges'])} Gemini question(s) answered; "
                                    f"saved as {path}" + "".join(f"; {n}" for n in out["notes"]))}


# Meta's own page search, saved by Recon so a later update can look advertiser pages up officially instead of
# reading them from ad results. Nothing reads these yet; they exist to show what the site sends.
RECON_EXTRA = {
    "meta_ads": [("page-search", lambda browser, cfg: browser.open(AD_LIBRARY + "/?active_status=active&ad_type=all&country=US"
                                                                   "&q=Gymshark&search_type=page&media_type=all"))],
    # the way a person searches: type #overalls into the box and press Enter (what a real run does when a tag page fails)
    "tiktok": [("search", lambda browser, cfg: search_for_tag(browser, cfg, "overalls"))],
}
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
                responses, embedded = browser.json_responses(), (browser.embedded_json() if hasattr(browser, "embedded_json") else [])
                samples = []
                for obj in responses[:60]:
                    text = json.dumps(obj)[:1500]
                    samples.append({"type": type(obj).__name__,
                                    "keys": sorted(obj)[:25] if isinstance(obj, dict) else len(obj),
                                    "bytes": len(json.dumps(obj)), "sample": text})
                (out / f"recon-{site}-network.json").write_text(json.dumps(samples, indent=1), encoding="utf-8")
                blocks = [{"type": type(obj).__name__, "keys": sorted(obj)[:25] if isinstance(obj, dict) else len(obj),
                           "bytes": len(json.dumps(obj)),
                           "readable_items": len(extract_json_items(source, [obj])) if source in PREDICATES else 0,
                           "sample": json.dumps(obj)[:800]} for obj in embedded[:60]]
                (out / f"recon-{site}-page-data.json").write_text(json.dumps(blocks, indent=1), encoding="utf-8")
                in_net = len(extract_json_items(source, responses)) if source in PREDICATES else 0
                in_page = len(extract_json_items(source, embedded)) if source in PREDICATES else 0
                total = len(extract_json_items(source, responses + embedded)) if source in PREDICATES else 0
                links = (browser.links(site_cfg.get("link_contains", [])) if source in _LINK_PATTERNS else [])
                line = (f"  {source}: {len(responses)} data responses and {len(embedded)} data blocks inside the page saved")
                if source in PREDICATES:
                    line += f"; readable items: {total} ({in_net} in the responses, {in_page} in the page)"
                    if rate_limited(responses):
                        line += "; the site rate-limited a data request"
                    if total == 0 and not links:
                        line += "  <-- nothing readable: open the saved screenshot and HTML"
                if links:
                    line += f"; {len(links)} post links on the page"
                lines.append(line)
                for name, action in RECON_EXTRA.get(source, []):
                    action(browser, site_cfg)
                    browser.wait(site_cfg.get("settle_seconds", 5))
                    browser.screenshot(f"recon-{site}-{name}")
                    extra_net, extra_page = browser.json_responses(), (browser.embedded_json() if hasattr(browser, "embedded_json") else [])
                    for suffix, objs in (("network", extra_net), ("page-data", extra_page)):
                        (out / f"recon-{site}-{name}-{suffix}.json").write_text(json.dumps(
                            [{"type": type(o).__name__, "keys": sorted(o)[:25] if isinstance(o, dict) else len(o),
                              "bytes": len(json.dumps(o)), "sample": json.dumps(o)[:1500]} for o in objs[:60]], indent=1),
                            encoding="utf-8")
                    lines.append(f"  {source} ({name}): {len(extra_net)} data responses and {len(extra_page)} data blocks saved")
        except BrowserError as exc:
            lines.append(f"  {source}: {exc}")
    return lines


# ---------------------------------------------------------------------------------------
# rows for the sheet
# ---------------------------------------------------------------------------------------

def _engagement(row):
    """Likes, comments, shares and saves as a share of views, for posts that show views."""
    views = row.get("views")
    known = [row[k] for k in ("likes", "comments", "shares", "saves") if isinstance(row.get(k), (int, float))]
    if not isinstance(views, (int, float)) or views <= 0 or not known:
        return None
    return round(100 * sum(known) / views, 2)


LONG_RUNNING_DAYS = 60       # an ad that ran this long, or one with MANY_COPIES variants, is worth a look
MANY_COPIES = 3


def ad_signal(row):
    """A cautious pointer, never a verdict: how long an ad ran and how many copies exist say it is worth studying,
    not that it earns money. Blank when there is nothing to say; a missing number is never treated as zero."""
    if row.get("source") != "meta_ads":
        return ""
    days, copies = row.get("days_running"), row.get("variants")
    parts = []
    if isinstance(days, int) and not isinstance(days, bool) and days >= LONG_RUNNING_DAYS:
        parts.append(f"ran {days} days")
    if isinstance(copies, int) and not isinstance(copies, bool) and copies >= MANY_COPIES:
        parts.append(f"{copies} copies")
    return "worth studying: " + ", ".join(parts) if parts else ""


def _landing(details):
    found = re.search(r"landing: (\S+)", details or "")
    return found.group(1) if found else ""


_ROW_FIELDS = ("item_id", "source", "handle", "title", "text", "url", "posted_at", "likes", "comments", "shares",
               "views", "saves", "reach", "days_running", "variants", "media_url", "details")


def collected_rows(records):
    """One row per distinct ad, post or report, oldest collection first, with the latest numbers."""
    rows = {}
    for rec in records:
        for item in rec.get("items", []):
            row = rows.setdefault(item["item_id"], {
                **{k: item.get(k) for k in _ROW_FIELDS},
                "first_seen": rec["at"], "last_seen": rec["at"], "times_seen": 0, "queries": []})
            row["last_seen"] = max(row["last_seen"], rec["at"])
            row["first_seen"] = min(row["first_seen"], rec["at"])
            row["times_seen"] += 1
            for k in _ROW_FIELDS[2:5] + _ROW_FIELDS[7:15] + ("media_url", "details"):
                if item.get(k) not in (None, ""):
                    row[k] = item[k]
            for label in str(item.get("found_via") or rec["seeds"]).split(" | "):
                if label.strip() and label.strip() not in row["queries"]:
                    row["queries"].append(label.strip())
    for row in rows.values():            # rows saved before template tokens were handled are repaired as they are written
        landing = _landing(row.get("details"))
        row["text"] = clean_template_text(row.get("text"), row.get("handle") or "", landing, row.get("title") or "")
        row["title"] = clean_template_text(row.get("title"), row.get("handle") or "", landing)
    return {k: {**v, "engagement_pct": _engagement(v), "signal": ad_signal(v), "queries": " | ".join(v["queries"][:5])}
            for k, v in rows.items()}
