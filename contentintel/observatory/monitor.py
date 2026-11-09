"""One monitoring run: for each competitor site, read what is permitted, store it, compare it with last time, record what changed.

Order per site: pages (they also reveal the site's feeds), feeds, sitemap, then the Shopify catalogue. Each part is a collector action
(contentintel/contract.py) so its success or failure is recorded in Collector Health under competitor_pages or competitor_stores.
A part that fails keeps the last good reading in place, labelled stale in the report; a site that blocks or rate-limits stops for the run.
"""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .. import contract as K
from .. import health
from ..safety import is_public_url
from . import changes as CH
from . import config as OC
from . import feeds as F
from . import htmlx as H
from . import polite, shopify as S
from . import sitemaps as M

STORES, PAGES = "competitor_stores", "competitor_pages"
PARTS = ("pages", "feeds", "sitemap", "shopify")
RECHECK_STORE_DAYS = 30


def _short(text):
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:8]


def page_kind(url):
    return "page-" + _short(url)


def feed_kind(url):
    return "feed-" + _short(url)


# ---- who is watched ---------------------------------------------------------------------------------------------------------------------------

def plan_sites(brands, settings, root="brands", data_dir="data", discovered=None):
    """{site_id: {'comp': merged competitor, 'brands': [slugs]}}. A site two brands both watch is read once."""
    sites = {}
    for brand in brands:
        file = OC.load_brand_file(brand.slug, root)
        listed = list(file["competitors"])
        known = {c["id"] for c in listed}
        listed += [c for c in OC.research_seeds(brand, root) if c["id"] not in known]
        known = {c["id"] for c in listed}
        listed += [c for c in (discovered or {}).get(brand.slug, []) if c["id"] not in known]
        excluded = set(file["exclude_domains"])
        for comp in listed:
            if not comp["domain"] or comp["domain"].removeprefix("www.") in excluded:
                continue
            slot = sites.setdefault(comp["id"], {"comp": {**comp, "pages": [], "feeds": [], "monitor": dict(comp["monitor"])}, "brands": []})
            merged = slot["comp"]
            merged["pages"] = sorted(set(merged["pages"]) | set(comp["pages"]))
            merged["feeds"] = sorted(set(merged["feeds"]) | set(comp["feeds"]))
            merged["blog"] = merged.get("blog") or comp.get("blog", "")
            merged["advanced"] = merged.get("advanced") or comp.get("advanced", {})
            merged["selectors"] = merged.get("selectors") or comp.get("selectors", {})
            for key, value in comp["monitor"].items():
                merged["monitor"][key] = (merged["monitor"].get(key) or value) if key != "shopify" else (value if merged["monitor"].get(key) == "auto" else merged["monitor"].get(key))
            if brand.slug not in slot["brands"]:
                slot["brands"].append(brand.slug)
    return sites


# ---- one part ---------------------------------------------------------------------------------------------------------------------------------

def _snapshot_page(client, url, settings):
    got = client.get(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", max_bytes=settings["politeness"]["max_bytes"])
    ctype = got.headers.get("content-type", "text/html").lower()
    if "html" not in ctype and "text" not in ctype and "xml" not in ctype:
        raise K.Failure(K.UPSTREAM_BROKEN, f"{url} is not a web page ({ctype.split(';')[0]})")
    parsed = H.parse_page(got.text(), got.final_url, settings["pages"]["max_text_chars"])
    if polite.looks_like_login_wall(got.body, parsed["title"]):
        raise K.Failure(K.UPSTREAM_BROKEN, f"{url} shows only a login page")
    keep = settings["pages"]["keep_text_chars"]
    text = parsed["text"][:keep]
    offers = H.extract_offers(parsed["text"], " ".join(parsed["announcements"]))
    return {"kind": "page", "schema": 1, "url": url, "final_url": got.final_url, "captured_at": "", "complete": True, "status": got.status, "title": parsed["title"],
            "description": parsed["description"], "canonical": parsed["canonical"], "h1": parsed["h1"], "headings": parsed["headings"][:25], "announcements": parsed["announcements"],
            "offers": offers, "products": H.jsonld_products(parsed["jsonld"]), "alternates": parsed["alternates"][:8], "social": H.social_links(parsed["links"], parsed["jsonld"]), "text": text,
            "text_hash": hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16], "text_truncated": parsed["chars"] > keep, "chars": parsed["chars"]}


def _snapshot_feed(client, url, settings):
    got = client.get(url, accept="application/rss+xml,application/atom+xml,application/xml,text/xml,application/json,text/csv,*/*;q=0.5")
    body = polite.decompress(got.body, got.headers, url, settings["sitemaps"]["max_decompressed_bytes"])
    parsed = F.parse_feed(body, got.final_url, settings["feeds"]["max_items"])
    if not parsed["items"]:
        raise K.Failure(K.UPSTREAM_BROKEN, f"{url} is a feed with no items")
    return {"kind": "feed", "schema": 1, "url": url, "captured_at": "", "complete": True, **parsed}


def _snapshot_sitemap(client, base, settings):
    starts = M.default_candidates(base, client.sitemaps_from_robots(base + "/"))
    got = M.walk_sitemaps(client, starts, settings)
    return {"kind": "sitemap", "schema": 1, "domain": urlsplit(base).netloc, "captured_at": "", "complete": not got["truncated"], "truncated": got["truncated"],
            "urls": got["urls"], "files": got["files"], "errors": got["errors"][:5]}


class _Run:
    """The state of one site's run: what was read, which parts failed, and whether the site has said stop."""

    def __init__(self, comp, client, snaps, settings, hl, now):
        self.comp, self.client, self.snaps, self.settings, self.health, self.now = comp, client, snaps, settings, hl, now
        self.site = comp["id"]
        self.parts, self.events, self.stopped = {}, [], ""
        self.state = snaps.load_state(self.site)
        self.base = S.base_url(comp["domain"])

    def action(self, part, source, route, action, fn):
        """Run one collector action; returns the first item or None. Records health, notes the outcome, and notices a stop."""
        holder = {}

        def runner():
            try:
                return [fn()]
            except Exception as exc:
                holder["exc"] = exc
                raise
        env = K.run_action(source, action, [(route, runner)], health=self.health, now=self.now)
        if env["ok"]:
            return env["items"][0]
        exc = holder.get("exc")
        err = env["error"]
        if isinstance(exc, K.Failure) and polite.is_fatal(exc):
            self.stopped = f"{err['code']}: {err['message']}"
        self.parts.setdefault(part, []).append({"ok": False, "code": err["code"], "message": err["message"], "route": route})
        return None

    def keep(self, part, kind, snapshot, differ, label=""):
        """Compare with the last complete reading, store the new one, collect the events. Returns the stored entry."""
        snapshot["captured_at"] = self.now
        _, old = self.snaps.latest(self.site, kind, complete_only=True)
        if old is None:
            _, old = self.snaps.latest(self.site, kind)
        events = []
        baseline = old is None
        if baseline:
            events.append(CH.event(self.site, part, "baseline_recorded", f"First reading of {label or kind} for {self.comp['domain']}: {_count(snapshot)} item(s)", at=self.now,
                                   severity="info", subject={"kind": kind, "key": kind}, details={"items": _count(snapshot), "complete": snapshot.get("complete", True)}))
        else:
            events.extend(differ(old, snapshot))
        entry, written = self.snaps.save(self.site, kind, snapshot, self.now)
        self.events.extend(events)
        self.parts.setdefault(part, []).append({"ok": True, "kind": kind, "written": written, "complete": snapshot.get("complete", True), "items": _count(snapshot),
                                               "events": len(events), "baseline": baseline, "notes": snapshot.get("notes", [])[:4]})
        return entry


def _count(snapshot):
    for key in ("products", "urls", "items"):
        if isinstance(snapshot.get(key), (list, dict)):
            return len(snapshot[key])
    return 1


def _record_social(run, found, snaps, now):
    """Remember the accounts the home page links to, but only those the file and discovery did not already give (what a person wrote always wins), and raise one event when an
    account appears that was not known. An account the site later changes to is updated quietly."""
    own = {k: run.comp.get(k, "") for k in ("instagram", "tiktok", "youtube")}
    wanted = {k: v for k, v in found.items() if v and not own.get(k)}
    if not wanted:
        return
    stored = snaps.read_social(run.site)
    new = {k: v for k, v in wanted.items() if not stored.get(k)}
    changed = {k: v for k, v in wanted.items() if stored.get(k) and stored[k].lstrip("@").lower() != v.lstrip("@").lower()}
    if new or changed:
        snaps.write_social(run.site, {**stored, **new, **changed}, now)
    if new:
        text = ", ".join(f"{k} {v if (k == 'youtube' and v.startswith('UC')) else '@' + v.lstrip('@')}" for k, v in new.items())
        run.events.append(CH.event(run.site, "page", "social_found", f"{run.comp['name']} links to its own {text}", at=now, severity="info", subject={"kind": "site", "id": run.site},
                                   after=new, tier="public_polite", access="page_text"))


def monitor_site(comp, *, client, snaps, settings, hl, now, only=None, page_reader=None):
    """Run every enabled part for one competitor. Returns {'site', 'domain', 'parts', 'events', 'stopped'}."""
    run = _Run(comp, client, snaps, settings, hl, now)
    want = set(only or PARTS)
    mon = comp["monitor"]
    discovered_feeds = []

    # pages (the home page is always read first; it also advertises feeds and reveals whether the site is a Shopify store)
    if "pages" in want and mon.get("pages", True) and settings["pages"]["max_per_competitor"] > 0:
        urls = [run.base + "/"] + [u for u in comp["pages"] if is_public_url(u)]
        chosen = list(dict.fromkeys(urls))[:settings["pages"]["max_per_competitor"]]
        reader = page_reader(comp, chosen) if page_reader else None            # the isolated Scrapling worker, when engine.pages says so
        for url in chosen:
            if run.stopped:
                break
            read = (lambda u=url: reader.get(u)) if reader else (lambda u=url: _snapshot_page(client, u, settings))
            snap = run.action("pages", PAGES, "static_fetch", "read_page", read)
            if snap is None:
                continue
            discovered_feeds.extend(a["href"] for a in snap["alternates"])
            if url == run.base + "/":
                _record_social(run, snap.get("social") or {}, snaps, now)
            run.keep("pages", page_kind(url), snap, lambda old, new: CH.diff_page(run.site, old, new, settings, now), label=url)

    if "feeds" in want and mon.get("feeds", True) and not run.stopped:
        urls = list(comp["feeds"]) + [u for u in discovered_feeds if is_public_url(u)]
        guessed = False
        if not urls:
            urls, guessed = F.guess_feed_urls(run.base, comp.get("blog", ""))[:3], True
        read = 0
        for url in list(dict.fromkeys(urls))[:6]:
            if run.stopped:
                break
            snap = run.action("feeds", PAGES, "feed", "read_feed", lambda u=url: _snapshot_feed(client, u, settings))
            if snap is None:
                if guessed:                                     # a guess that does not exist is not a failure of the site
                    run.parts["feeds"].pop()
                continue
            read += 1
            run.keep("feeds", feed_kind(url), snap, lambda old, new: CH.diff_feed(run.site, old, new, settings, now), label=url)
        if guessed and not read:
            run.parts.setdefault("feeds", []).append({"ok": True, "kind": "feed", "items": 0, "events": 0, "baseline": False, "written": False, "complete": True,
                                                      "notes": ["no blog feed was advertised or found at the usual addresses"]})

    if "sitemap" in want and mon.get("sitemap", True) and not run.stopped:
        snap = run.action("sitemap", STORES, "sitemap", "read_sitemap", lambda: _snapshot_sitemap(client, run.base, settings))
        if snap is not None:
            run.keep("sitemap", "sitemap", snap, lambda old, new: CH.diff_sitemap(run.site, old, new, settings, now))

    if "shopify" in want and mon.get("shopify", "auto") is not False and not run.stopped:
        is_shop = _is_shopify(run, mon.get("shopify", "auto"))
        if is_shop:
            snap = run.action("shopify", STORES, "public_json", "read_catalogue", lambda: S.walk_store(client, comp["domain"], settings, now))
            if snap is not None:
                _, baseline = snaps.latest(run.site, "catalogue", complete_only=True)
                state = run.state.setdefault("catalogue", {})
                snap["captured_at"] = now
                if baseline is not None:
                    events, run.state["catalogue"] = CH.diff_catalogue(run.site, baseline, snap, settings, state, now)
                    entry, written = snaps.save(run.site, "catalogue", snap, now)
                    run.events.extend(events)
                else:
                    events = [CH.event(run.site, "catalogue", "baseline_recorded", f"First reading of the catalogue of {comp['domain']}: {len(snap['products'])} products, "
                                       f"{len(snap['collections'])} collections", at=now, severity="info", subject={"kind": "catalogue", "key": "catalogue"},
                                       details={"products": len(snap["products"]), "collections": len(snap["collections"]), "complete": snap["complete"]})]
                    entry, written = snaps.save(run.site, "catalogue", snap, now)
                    run.events.extend(events)
                run.parts.setdefault("shopify", []).append({"ok": True, "kind": "catalogue", "written": written, "complete": snap["complete"], "items": len(snap["products"]),
                                                            "events": len(events), "baseline": baseline is None, "notes": snap["notes"][:4],
                                                            "best_selling": snap["best_selling"]["honored"]})
    if run.events:
        snaps.append_events(run.site, run.events)
    snaps.save_state(run.site, run.state)
    return {"site": run.site, "domain": comp["domain"], "parts": run.parts, "events": run.events, "stopped": run.stopped, "politeness": client.report()}


def _is_shopify(run, setting):
    if setting is True:
        return True
    memo = run.state.get("shopify") or {}
    if memo and (health.age_days(memo.get("checked_at", ""), run.now) or 999) < RECHECK_STORE_DAYS:
        return bool(memo.get("is_store"))
    got = run.action("shopify", STORES, "public_json", "probe_store", lambda: S.is_store(run.client, run.base))       # a (yes/no, note) pair is always truthy, so a "no" is not mistaken for an empty answer
    if got is None:
        return False
    run.state["shopify"] = {"is_store": bool(got[0]), "note": got[1], "checked_at": run.now}
    if not got[0]:
        run.parts.setdefault("shopify", []).append({"ok": True, "kind": "catalogue", "written": False, "complete": True, "items": 0, "events": 0, "baseline": False,
                                                    "notes": [f"not a Shopify store with public product data ({got[1]})"]})
    return bool(got[0])


# ---- many sites -------------------------------------------------------------------------------------------------------------------------------

def run_sites(sites, *, make_client, snaps, settings, hl, now, only=None, workers=3, ids=None, page_reader=None):
    """Monitor every planned site (or just `ids`), a few at a time; each site has its own client and pace. Returns results in site order."""
    chosen = [sid for sid in sorted(sites) if not ids or sid in ids]

    def one(sid):
        comp = sites[sid]["comp"]
        try:
            return monitor_site(comp, client=make_client(), snaps=snaps, settings=settings, hl=hl, now=now, only=only, page_reader=page_reader)
        except Exception as exc:                 # one site's unexpected problem must never end the others
            err = K.classify_exception(exc)
            return {"site": sid, "domain": comp["domain"], "parts": {}, "events": [], "stopped": f"{err['code']}: {err['message']}", "politeness": {}}
    if workers <= 1 or len(chosen) <= 1:
        return [one(s) for s in chosen]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, chosen))


def stale_report(snaps, site, now, settings, stale_after_days=14):
    """For each kind kept for a site: when it was captured and last confirmed, and whether it is stale."""
    out = []
    for kind in snaps.kinds(site):
        entries = snaps.entries(site, kind)
        if not entries:
            continue
        last = entries[-1]
        age = health.age_days(last["confirmed_at"], now)
        out.append({"kind": kind, "captured_at": last["captured_at"], "confirmed_at": last["confirmed_at"], "age_days": round(age, 1) if age is not None else None,
                    "stale": age is None or age > stale_after_days, "complete": last.get("complete", True)})
    return out
