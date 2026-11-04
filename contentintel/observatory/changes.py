"""Change detection: what is different between two readings of the same competitor site, as events a person can act on.

Rules that keep it honest and quiet:
  * A reading that is not complete never produces a *removal* (missing might only mean 'not read'), and an incomplete baseline never produces a
    *new product* (it might only have been missing from the baseline).
  * A product must be missing for several runs in a row before it is called removed (settings: removal_confirm_runs).
  * A collection's order is compared among the products that are in both readings, so one new product at the top is not reported as every
    other product moving down.
  * Each event is a plain sentence with before and after values, a severity, and where it was seen, and says nothing about *why* it happened.
"""
import difflib
import hashlib
import re

from .shopify import positions

SEVERITIES = ("major", "notable", "info")
_HERO = re.compile(r"best|new|sale|featured|hero|launch|bundle|favou?rite|trending|popular|gift|limited|all", re.I)


def severity_rank(sev):
    return SEVERITIES.index(sev) if sev in SEVERITIES else len(SEVERITIES)


def event(site, scope, etype, summary, *, at, severity="info", subject=None, before=None, after=None, details=None, tier="public_polite", access="feed"):
    subject = subject or {}
    key = subject.get("id") if subject.get("id") is not None else subject.get("url", subject.get("key", ""))
    digest = hashlib.sha1(f"{site}|{scope}|{etype}|{key}|{at}|{summary}".encode("utf-8", errors="replace")).hexdigest()[:16]
    return {"event_id": f"ob-{digest}", "at": at, "site": site, "scope": scope, "type": etype, "severity": severity, "summary": summary[:300], "subject": subject,
            "before": before, "after": after, "details": details or {}, "tier": tier, "access_type": access}


def _money(value):
    return f"${value:,.2f}" if isinstance(value, (int, float)) else "?"


def _pct(old, new):
    return round((new - old) / old * 100, 1) if old else None


def _depth(product):
    best = 0.0
    for v in product.get("variants", []):
        if v.get("compare_at") and v.get("price") is not None and v["compare_at"] > 0:
            best = max(best, (v["compare_at"] - v["price"]) / v["compare_at"] * 100)
    return round(best, 1)


def _subject(p):
    return {"kind": "product", "id": p["id"], "handle": p.get("handle", ""), "title": p.get("title", ""), "url": p.get("url", "")}


def _cap(events, limit, site, at):
    if len(events) <= limit:
        return events
    ordered = sorted(events, key=lambda e: (severity_rank(e["severity"]), e["type"]))
    kept, dropped = ordered[:limit - 1], len(ordered) - (limit - 1)
    kept.append(event(site, "run", "events_truncated", f"{dropped} further minor change(s) were not listed (limit {limit} per run)", at=at, details={"dropped": dropped}))
    return kept


# ---- Shopify catalogue ------------------------------------------------------------------------------------------------------------------------

def diff_catalogue(site, old, new, cfg, state, at):
    """(events, new_state) between two catalogue snapshots. `old` should be the latest complete reading."""
    c = cfg["changes"]
    state = {k: dict(v) if isinstance(v, dict) else v for k, v in (state or {}).items()}
    pending, removed_memo = state.setdefault("missing", {}), state.setdefault("removed", {})
    om, nm = {p["id"]: p for p in old.get("products", [])}, {p["id"]: p for p in new.get("products", [])}
    old_ok, new_ok = old.get("complete", True), new.get("complete", True)
    events = []
    scope = "catalogue"

    def add(*a, **k):
        events.append(event(site, scope, *a, at=at, access="feed", **k))

    # products that came back after a short disappearance are not new: remember them before clearing their missing-record
    present = {str(pid) for pid in nm}                  # state is saved as JSON, where every key is text
    returning = {key for key in pending if key in present}
    for key in returning:
        del pending[key]
    fresh = [p for pid, p in nm.items() if pid not in om and str(pid) not in returning]
    new_products = []
    for p in fresh:
        if str(p["id"]) in removed_memo:
            del removed_memo[str(p["id"])]
            add("product_relisted", f"{p['title']} is listed again", severity="notable", subject=_subject(p), after={"price": p.get("price_min")})
        elif old_ok:
            new_products.append(p)
    pos_new = positions(new)
    for p in new_products:
        recent = _recent(p.get("published_at") or p.get("created_at"), at, 14)
        homes = [h for h, _, _ in pos_new.get(p["id"], [])]
        add("product_new", f"New product: {p['title']} at {_money(p.get('price_min'))}" + (f" (in {', '.join(homes[:3])})" if homes else ""),
            severity="major" if recent else "notable", subject=_subject(p), after={"price_min": p.get("price_min"), "price_max": p.get("price_max"), "on_sale": p.get("on_sale"),
                                                                              "collections": homes[:6], "published_at": p.get("published_at", "")}, details={"recent": recent})
    if len(new_products) >= 3:
        titles = [p["title"] for p in new_products[:8]]
        add("new_drop", f"{len(new_products)} new products appeared together: {', '.join(titles[:4])}" + ("..." if len(new_products) > 4 else ""), severity="major",
            subject={"kind": "drop", "key": f"drop-{at[:10]}"}, details={"count": len(new_products), "titles": titles})

    # removals: only from a complete reading, and only once they have been missing long enough
    if new_ok:
        for pid, p in om.items():
            if pid not in nm and str(pid) not in pending:
                pending[str(pid)] = {"id": pid, "count": 0, "title": p["title"], "handle": p.get("handle", ""), "url": p.get("url", ""), "first_missing_at": at,
                                     "price": p.get("price_min")}
        for key in list(pending):
            if key in present:
                continue
            pending[key]["count"] += 1
            if pending[key]["count"] >= c["removal_confirm_runs"]:
                info = pending.pop(key)
                removed_memo[key] = {"title": info["title"], "at": at}
                add("product_removed", f"Removed: {info['title']} (missing since {info['first_missing_at'][:10]})", severity="notable",
                    subject={"kind": "product", "id": info["id"], "handle": info["handle"], "title": info["title"], "url": info["url"]}, before={"price": info.get("price")})
    if len(removed_memo) > 500:
        for key in sorted(removed_memo, key=lambda k: removed_memo[k]["at"])[:len(removed_memo) - 500]:
            del removed_memo[key]

    best_old = set((old.get("best_selling") or {}).get("top", [])[:10]) if (old.get("best_selling") or {}).get("honored") else None
    best_new = set((new.get("best_selling") or {}).get("top", [])[:10]) if (new.get("best_selling") or {}).get("honored") else None
    for pid, n in nm.items():
        o = om.get(pid)
        if o is None:
            continue
        sub = _subject(n)
        if n["price_min"] is not None and o["price_min"] is not None and n["price_min"] != o["price_min"]:
            pct = _pct(o["price_min"], n["price_min"])
            if pct is not None and abs(pct) >= c["price_change_min_pct"]:
                add("price_change", f"{n['title']}: {_money(o['price_min'])} to {_money(n['price_min'])} ({pct:+.1f}%)", severity="notable" if abs(pct) >= 10 else "info",
                    subject=sub, before={"price_min": o["price_min"], "price_max": o["price_max"]}, after={"price_min": n["price_min"], "price_max": n["price_max"]}, details={"pct": pct})
        elif n["price_max"] != o["price_max"] and n["price_max"] is not None and o["price_max"] is not None:
            pct = _pct(o["price_max"], n["price_max"])
            if pct is not None and abs(pct) >= c["price_change_min_pct"]:
                add("price_change", f"{n['title']}: top price {_money(o['price_max'])} to {_money(n['price_max'])} ({pct:+.1f}%)", severity="info", subject=sub,
                    before={"price_max": o["price_max"]}, after={"price_max": n["price_max"]}, details={"pct": pct})
        d_old, d_new = _depth(o), _depth(n)
        if not o["on_sale"] and n["on_sale"]:
            add("sale_started", f"{n['title']} went on sale: {_money(n.get('price_min'))} (was {_money(n.get('compare_at_max'))}, {d_new:.0f}% off)", severity="major" if d_new >= 30 else "notable",
                subject=sub, after={"price_min": n["price_min"], "compare_at_max": n["compare_at_max"], "depth_pct": d_new})
        elif o["on_sale"] and not n["on_sale"]:
            add("sale_ended", f"{n['title']} is no longer on sale ({_money(n.get('price_min'))})", severity="info", subject=sub, before={"depth_pct": d_old})
        elif o["on_sale"] and n["on_sale"] and abs(d_new - d_old) >= 10:
            add("discount_depth_change", f"{n['title']}: discount {d_old:.0f}% to {d_new:.0f}%", severity="notable", subject=sub, before={"depth_pct": d_old}, after={"depth_pct": d_new})
        was_top = best_old is not None and pid in best_old
        if o["available"] and not n["available"]:
            add("stock_out", f"{n['title']} is sold out" + (" (it was among the best sellers)" if was_top else ""), severity="major" if was_top else "notable", subject=sub,
                details={"best_seller": was_top})
        elif not o["available"] and n["available"]:
            add("back_in_stock", f"{n['title']} is back in stock", severity="notable" if o["available"] is False else "info", subject=sub)
        ov, nv = {v["id"]: v for v in o["variants"]}, {v["id"]: v for v in n["variants"]}
        if set(ov) != set(nv):
            add("variants_changed", f"{n['title']}: {len(set(nv) - set(ov))} option(s) added, {len(set(ov) - set(nv))} removed", severity="info", subject=sub,
                details={"added": [nv[i]["title"] for i in set(nv) - set(ov)][:6], "removed": [ov[i]["title"] for i in set(ov) - set(nv)][:6]})
        if n["title"] != o["title"]:
            add("title_change", f"Renamed: {o['title']} to {n['title']}", severity="info", subject=sub, before={"title": o["title"]}, after={"title": n["title"]})
        if _img(n.get("image")) != _img(o.get("image")) and n.get("image") and o.get("image"):
            add("image_change", f"{n['title']}: main picture changed", severity="info", subject=sub)
        if n["description_hash"] != o["description_hash"]:
            add("description_change", f"{n['title']}: description rewritten", severity="info", subject=sub, before={"text": o.get("description", "")[:200]}, after={"text": n.get("description", "")[:200]})
        if set(n["tags"]) != set(o["tags"]):
            added, gone = sorted(set(n["tags"]) - set(o["tags"])), sorted(set(o["tags"]) - set(n["tags"]))
            add("tags_change", f"{n['title']}: tags +{', '.join(added[:4]) or 'none'} -{', '.join(gone[:4]) or 'none'}", severity="info", subject=sub, details={"added": added[:10], "removed": gone[:10]})
        if best_new is not None and best_old is not None and pid in best_new and pid not in best_old:
            add("best_seller_entry", f"{n['title']} entered the best sellers", severity="notable", subject=sub)

    events.extend(_diff_collections(site, old, new, nm, om, c, at, new_ok))
    state["updated_at"] = at
    return _cap(events, c["max_events_per_competitor"], site, at), state


def _img(src):
    return str(src or "").split("?")[0]


def _recent(ts, at, days):
    from .. import health
    age = health.age_days(ts, at) if ts else None
    if age is None and ts and len(ts) >= 10:
        age = health.age_days(ts[:10] + "T00:00:00Z", at)
    return age is not None and age <= days


def _diff_collections(site, old, new, nm, om, c, at, new_ok):
    events = []
    oc, nc = {x["handle"]: x for x in old.get("collections", [])}, {x["handle"]: x for x in new.get("collections", [])}
    fresh_ids = {pid for pid in nm if pid not in om}

    def add(etype, summary, **kw):
        events.append(event(site, "collections", etype, summary, at=at, access="feed", **kw))

    for handle, coll in nc.items():
        if handle not in oc:
            titles = [nm[i]["title"] for i in coll["members"][:5] if i in nm]
            add("collection_new", f"New collection: {coll['title'] or handle} ({len(coll['members'])} products)", severity="notable" if _HERO.search(handle) else "info",
                subject={"kind": "collection", "id": handle, "handle": handle, "title": coll["title"]}, after={"products": len(coll["members"]), "first": titles})
    if new_ok:
        for handle, coll in oc.items():
            if handle not in nc:
                add("collection_removed", f"Collection removed: {coll['title'] or handle}", severity="notable", subject={"kind": "collection", "id": handle, "handle": handle, "title": coll["title"]},
                    before={"products": len(coll["members"])})
    for handle, nco in nc.items():
        oco = oc.get(handle)
        if not oco:
            continue
        sub = {"kind": "collection", "id": handle, "handle": handle, "title": nco["title"]}
        old_set, new_set = set(oco["members"]), set(nco["members"])
        hero = bool(_HERO.search(handle))
        for pid in [i for i in nco["members"] if i not in old_set and i not in fresh_ids][:8]:
            if pid in nm:
                add("collection_add", f"{nm[pid]['title']} added to {nco['title'] or handle} (position {nco['members'].index(pid) + 1})", severity="notable" if hero else "info",
                    subject=_subject(nm[pid]), after={"collection": handle, "position": nco["members"].index(pid) + 1})
        if new_ok:
            for pid in [i for i in oco["members"] if i not in new_set and i in nm][:8]:
                add("collection_remove", f"{nm[pid]['title']} removed from {nco['title'] or handle}", severity="notable" if hero else "info", subject=_subject(nm[pid]), before={"collection": handle})
        common = [i for i in oco["members"] if i in new_set]
        if len(common) < 2:
            continue
        old_rank = {pid: r for r, pid in enumerate(common, start=1)}
        new_rank = {pid: r for r, pid in enumerate([i for i in nco["members"] if i in old_set], start=1)}
        moves = sorted(((old_rank[p] - new_rank[p], p) for p in common if old_rank[p] != new_rank[p]), key=lambda t: -abs(t[0]))
        old_full, new_full = {p: r for r, p in enumerate(oco["members"], start=1)}, {p: r for r, p in enumerate(nco["members"], start=1)}
        top = c["collection_top_n"]
        entered = [p for p in common if new_full[p] <= top < old_full[p] and new_rank[p] < old_rank[p]]
        left = [p for p in common if old_full[p] <= top < new_full[p] and new_rank[p] > old_rank[p]]
        big = [(d, p) for d, p in moves if abs(d) >= c["collection_move_min_positions"]]
        if big or entered or left:
            title = lambda p: nm[p]["title"] if p in nm else str(p)
            parts = []
            if entered:
                parts.append("now in the first " + str(top) + ": " + ", ".join(title(p) for p in entered[:3]))
            if left:
                parts.append("dropped out of the first " + str(top) + ": " + ", ".join(title(p) for p in left[:3]))
            if big and not parts:
                parts.append("biggest move: " + f"{title(big[0][1])} {'up' if big[0][0] > 0 else 'down'} {abs(big[0][0])}")
            add("collection_reorder", f"{nco['title'] or handle} was reordered; " + "; ".join(parts), severity="notable" if (entered and hero) else "info", subject=sub,
                details={"entered_top": [title(p) for p in entered[:6]], "left_top": [title(p) for p in left[:6]], "moved": [{"title": title(p), "by": d} for d, p in big[:5]],
                         "moved_count": len(moves)})
    return events


# ---- sitemap ----------------------------------------------------------------------------------------------------------------------------------

def diff_sitemap(site, old, new, cfg, at):
    c = cfg["changes"]
    ou, nu = old.get("urls", {}), new.get("urls", {})
    events = []

    def add(etype, summary, **kw):
        events.append(event(site, "sitemap", etype, summary, at=at, access="feed", **kw))

    added = [u for u in nu if u not in ou]
    by_cat = {}
    for u in added:
        by_cat.setdefault(nu[u]["cat"], []).append(u)
    if not old.get("truncated"):
        if by_cat.get("product"):
            add("sitemap_new_products", f"{len(by_cat['product'])} new product page(s) in the sitemap", severity="notable", subject={"kind": "sitemap", "key": "products"},
                details={"urls": by_cat["product"][:10], "count": len(by_cat["product"])})
        for u in by_cat.get("article", [])[:10]:
            add("blog_post_new", f"New blog or news page: {u}", severity="notable", subject={"kind": "url", "url": u, "cat": "article"}, after={"lastmod": nu[u]["lastmod"]})
        for cat in ("collection", "page", "other"):
            for u in by_cat.get(cat, [])[:15]:
                add("new_landing_page", f"New {cat} page: {u}", severity="notable", subject={"kind": "url", "url": u, "cat": cat}, after={"lastmod": nu[u]["lastmod"]})
    if not new.get("truncated") and not old.get("truncated"):
        gone = [u for u in ou if u not in nu]
        gone_by = {}
        for u in gone:
            gone_by.setdefault(ou[u]["cat"], []).append(u)
        if gone_by.get("product"):
            add("sitemap_removed_products", f"{len(gone_by['product'])} product page(s) left the sitemap", severity="info", subject={"kind": "sitemap", "key": "products-gone"},
                details={"urls": gone_by["product"][:10], "count": len(gone_by["product"])})
        for cat in ("collection", "page", "article", "other"):
            for u in gone_by.get(cat, [])[:15]:
                add("landing_page_removed", f"{cat.title()} page left the sitemap: {u}", severity="notable" if cat in ("collection", "page") else "info", subject={"kind": "url", "url": u, "cat": cat})
    changed = [u for u in nu if u in ou and nu[u]["lastmod"] and ou[u]["lastmod"] and nu[u]["lastmod"] != ou[u]["lastmod"]]
    product_changes = [u for u in changed if nu[u]["cat"] == "product"]
    if product_changes:
        add("sitemap_products_updated", f"{len(product_changes)} product page(s) were updated according to the sitemap", severity="info", subject={"kind": "sitemap", "key": "products-updated"},
            details={"count": len(product_changes), "urls": product_changes[:10]})
    for u in [u for u in changed if nu[u]["cat"] != "product"][:20]:
        add("page_updated", f"{nu[u]['cat'].title()} page updated: {u}", severity="info", subject={"kind": "url", "url": u, "cat": nu[u]["cat"]}, before={"lastmod": ou[u]["lastmod"]}, after={"lastmod": nu[u]["lastmod"]})
    return _cap(events, c["max_events_per_competitor"], site, at)


# ---- feeds ------------------------------------------------------------------------------------------------------------------------------------

def diff_feed(site, old, new, cfg, at):
    c = cfg["changes"]
    oi, ni = {i["id"]: i for i in old.get("items", [])}, {i["id"]: i for i in new.get("items", [])}
    product_feed = new.get("format") in ("merchant", "csv", "tsv")
    events = []

    def add(etype, summary, **kw):
        events.append(event(site, "feed", etype, summary, at=at, access="feed", **kw))

    for iid, item in ni.items():
        sub = {"kind": "feed_item", "id": iid, "title": item["title"], "url": item["link"]}
        if iid not in oi:
            if product_feed:
                add("feed_product_new", f"New product in the feed: {item['title']}" + (f" at {_money(item['price'])}" if item.get("price") is not None else ""), severity="notable", subject=sub,
                    after={"price": item.get("price"), "sale_price": item.get("sale_price")})
            else:
                add("blog_post_new", f"New post: {item['title']}", severity="notable", subject=sub, after={"published": item.get("published", ""), "summary": item.get("summary", "")[:200]})
            continue
        if not product_feed:
            continue
        o = oi[iid]
        eff_o = o.get("sale_price") if o.get("sale_price") is not None else o.get("price")
        eff_n = item.get("sale_price") if item.get("sale_price") is not None else item.get("price")
        if eff_o is not None and eff_n is not None and eff_o != eff_n:
            pct = _pct(eff_o, eff_n)
            if pct is not None and abs(pct) >= c["price_change_min_pct"]:
                add("feed_price_change", f"{item['title']}: {_money(eff_o)} to {_money(eff_n)} ({pct:+.1f}%)", severity="notable" if abs(pct) >= 10 else "info", subject=sub,
                    before={"price": eff_o}, after={"price": eff_n}, details={"pct": pct})
        if (o.get("availability") or "") != (item.get("availability") or "") and item.get("availability"):
            add("feed_availability_change", f"{item['title']}: {o.get('availability') or '?'} to {item['availability']}", severity="notable", subject=sub)
    return _cap(events, c["max_events_per_competitor"], site, at)


# ---- single pages -----------------------------------------------------------------------------------------------------------------------------

def _lines(text):
    return [l for l in (text or "").splitlines() if l.strip()]


def diff_page(site, old, new, cfg, at):
    c = cfg["changes"]
    url = new.get("url", "")
    sub = {"kind": "page", "id": url, "url": url, "title": new.get("title", "")}
    events = []

    def add(etype, summary, **kw):
        events.append(event(site, "page", etype, summary, at=at, access="page_text", subject=sub, **kw))

    if old.get("h1") != new.get("h1") and (old.get("h1") or new.get("h1")):
        add("headline_change", f"Headline changed on {url}: \"{(old.get('h1') or ['(none)'])[0][:90]}\" to \"{(new.get('h1') or ['(none)'])[0][:90]}\"", severity="notable",
            before={"h1": old.get("h1")}, after={"h1": new.get("h1")})
    oa, na = set(old.get("announcements", [])), set(new.get("announcements", []))
    if oa != na:
        add("announcement_change", f"Announcement bar changed on {url}: " + (f"now \"{sorted(na - oa)[0][:100]}\"" if na - oa else "message removed"), severity="notable",
            before={"announcements": sorted(oa)}, after={"announcements": sorted(na)})
    oo, no = set(old.get("offers", [])), set(new.get("offers", []))
    if oo != no:
        added, gone = sorted(no - oo), sorted(oo - no)
        big = any(re.match(r"(\d+)% off", o) and int(re.match(r"(\d+)% off", o).group(1)) >= 25 for o in added) or any(o in ("black friday", "cyber monday", "flash sale") for o in added)
        add("offer_change", f"Offers on {url}: " + (f"added {', '.join(added[:4])}" if added else "") + ("; " if added and gone else "") + (f"ended {', '.join(gone[:4])}" if gone else ""),
            severity="major" if big else "notable", before={"offers": sorted(oo)}, after={"offers": sorted(no)}, details={"added": added, "ended": gone})
    if old.get("title") != new.get("title") and new.get("title"):
        add("page_title_change", f"Page title changed on {url}", severity="info", before={"title": old.get("title")}, after={"title": new.get("title")})
    if old.get("text_hash") != new.get("text_hash"):
        ol, nl = _lines(old.get("text")), _lines(new.get("text"))
        delta = sum(len(x) for x in set(nl) - set(ol)) + sum(len(x) for x in set(ol) - set(nl))
        if delta >= c["page_change_min_chars"]:
            sm = difflib.SequenceMatcher(a=ol, b=nl, autojunk=False)
            added, gone = [], []
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag in ("replace", "insert"):
                    added.extend(nl[j1:j2])
                if tag in ("replace", "delete"):
                    gone.extend(ol[i1:i2])
            add("page_changed", f"{url} changed ({len(added)} line(s) added, {len(gone)} removed)", severity="info", details={"added": [x[:160] for x in added[:3]], "removed": [x[:160] for x in gone[:3]],
                                                                                                                                  "chars": delta})
    return events
