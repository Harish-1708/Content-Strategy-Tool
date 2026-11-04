"""A Shopify store's public catalogue: products, prices, stock and every collection each product sits in, with its position.

Shopify stores publish /meta.json, /products.json and /collections.json. They are meant to be read, but they are a courtesy and a store
may switch them off; when it does, that is reported, not worked around. Scrapling's ShopifySpider walks the same files but keeps only the
first collection of each product; here every membership and its position is kept, because merchandising (what a store puts first) is
exactly what the observatory is for.
"""
import hashlib
import re
from urllib.parse import quote, urlsplit

from .. import contract as K
from ..safety import clean_text, visible_text
from . import polite

_PAGE_HINT = re.compile(r"<html|<!doctype", re.I)


def base_url(domain):
    host = (domain or "").strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
    return f"https://{host}"


def _money(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _hash(text):
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def normalize_product(raw, base, keep_text=300):
    """One product in the compact shape the snapshots and the diff engine use."""
    variants = []
    for v in (raw.get("variants") or [])[:100]:
        price, was = _money(v.get("price")), _money(v.get("compare_at_price"))
        variants.append({"id": v.get("id"), "title": clean_text(str(v.get("title") or "")).strip()[:80], "sku": clean_text(str(v.get("sku") or "")).strip()[:60],
                         "price": price, "compare_at": was if was and price is not None and was > price else None, "available": bool(v.get("available", True))})
    prices = [v["price"] for v in variants if v["price"] is not None]
    compare = [v["compare_at"] for v in variants if v["compare_at"] is not None]
    images = raw.get("images") or []
    body = " ".join(visible_text(raw.get("body_html") or "").split())
    handle = clean_text(str(raw.get("handle") or "")).strip()
    return {"id": raw.get("id"), "handle": handle, "url": f"{base}/products/{quote(handle)}" if handle else "", "title": clean_text(str(raw.get("title") or "")).strip()[:200],
            "vendor": clean_text(str(raw.get("vendor") or "")).strip()[:80], "type": clean_text(str(raw.get("product_type") or "")).strip()[:80],
            "tags": sorted({clean_text(str(t)).strip() for t in (raw.get("tags") if isinstance(raw.get("tags"), list) else str(raw.get("tags") or "").split(","))
                            if str(t).strip()})[:40],
            "created_at": str(raw.get("created_at") or ""), "updated_at": str(raw.get("updated_at") or ""), "published_at": str(raw.get("published_at") or ""),
            "image": str((images[0] or {}).get("src", "")) if images and isinstance(images[0], dict) else "", "images": len(images),
            "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None, "compare_at_max": max(compare) if compare else None,
            "on_sale": bool(compare), "available": any(v["available"] for v in variants) if variants else False, "variants": variants,
            "description_hash": _hash(body), "description": body[:keep_text]}


def _json(client, url):
    """The JSON a store answers with. A page instead of JSON (a password page, a login, a shop that switched the feed off) is UPSTREAM_BROKEN."""
    got = client.get(url, accept="application/json,*/*;q=0.5")
    text = got.body[:2000].decode("utf-8", errors="ignore")
    if _PAGE_HINT.search(text) or "json" not in got.headers.get("content-type", "json").lower() and not text.lstrip().startswith(("{", "[")):
        raise K.Failure(K.UPSTREAM_BROKEN, f"{urlsplit(url).netloc} answered with a web page instead of the product data (not a Shopify store, "
                        "password-protected, or the feed is switched off)")
    try:
        data = got.json()
    except ValueError:
        raise K.Failure(K.UPSTREAM_BROKEN, f"{urlsplit(url).netloc} answered with something that is not JSON") from None
    if not isinstance(data, dict):
        raise K.Failure(K.UPSTREAM_BROKEN, f"{urlsplit(url).netloc} answered with an unexpected shape")
    return data


def probe_meta(client, base):
    """The store's own description (name, currency, published product count) or None when it does not publish /meta.json."""
    try:
        data = _json(client, base + "/meta.json")
    except K.Failure as exc:
        if polite.is_fatal(exc):
            raise
        return None
    count = data.get("published_products_count")
    return {"name": clean_text(str(data.get("name") or "")).strip()[:120], "currency": str(data.get("currency") or "")[:8], "country": str(data.get("country") or "")[:8],
            "domain": str(data.get("domain") or "")[:120], "myshopify_domain": str(data.get("myshopify_domain") or "")[:120],
            "published_products_count": int(count) if isinstance(count, int) else None,
            "description": " ".join(clean_text(str(data.get("description") or "")).split())[:300]}


def is_store(client, base):
    """(True|False, note): does this site publish Shopify product data? Costs one or two polite requests."""
    meta = probe_meta(client, base)
    if meta and (meta.get("myshopify_domain") or meta.get("published_products_count") is not None):
        return True, "meta.json says it is a Shopify store"
    try:
        data = _json(client, base + "/products.json?limit=1")
    except K.Failure as exc:
        if polite.is_fatal(exc):
            raise
        return False, "no public product data"
    return ("products" in data), ("products.json is public" if "products" in data else "products.json has no products list")


def _best_selling(client, base, per_page):
    """Does ?sort_by=best-selling change the order of the 'all' collection? (the blueprint's open question: tested for real, per store)"""
    plain = _json(client, f"{base}/collections/all/products.json?limit={per_page}&page=1")
    sorted_ = _json(client, f"{base}/collections/all/products.json?limit={per_page}&page=1&sort_by=best-selling")
    a = [p.get("id") for p in plain.get("products") or []][:60]
    b = [p.get("id") for p in sorted_.get("products") or []][:60]
    if not a or not b:
        return {"honored": None, "top": [], "note": "the 'all' collection returned no products"}
    if a == b:
        return {"honored": False, "top": [], "note": "the sort parameter did not change the order, so no best-seller proxy is available for this store"}
    return {"honored": True, "top": b, "note": "the sort parameter changed the order; the first places are the store's best sellers"}


def walk_store(client, domain, settings, now):
    """The whole public catalogue as one snapshot. Final failures (blocked, rate-limited) are raised; everything softer is a note."""
    cfg = settings["shopify"]
    base = base_url(domain)
    notes, products, order = [], {}, []
    per_page = cfg["per_page"]
    meta = probe_meta(client, base)
    master, complete = "products.json", True

    def add(raw):
        item = normalize_product(raw, base)
        if item["id"] is not None and item["id"] not in products:
            products[item["id"]] = item
            order.append(item["id"])
        return item["id"]

    page = 1
    try:
        while True:
            data = _json(client, f"{base}/products.json?limit={per_page}&page={page}")
            batch = [r for r in (data.get("products") or []) if isinstance(r, dict)]
            for raw in batch:
                if len(products) < cfg["max_products"]:
                    add(raw)
            if len(batch) < per_page:
                break
            if len(products) >= cfg["max_products"]:
                complete = False
                notes.append(f"stopped at the product ceiling ({cfg['max_products']})")
                break
            page += 1
    except K.Failure as exc:
        if polite.is_fatal(exc):
            raise
        if getattr(exc, "budget", False) or getattr(exc, "robots_path", False):
            complete = False
            notes.append("the request limit for this site was reached while reading the product list" if getattr(exc, "budget", False)
                         else "robots.txt does not allow the product list, so it was not read")
        else:
            master, complete = "collections", False
            notes.append(f"products.json could not be read ({exc.code}); the catalogue is built from the collections instead")

    collections, collection_errors = [], 0
    stopped_for_budget = False
    try:
        listed, n = [], 1
        while n <= 4:
            data = _json(client, f"{base}/collections.json?limit=250&page={n}")
            batch = [c for c in (data.get("collections") or []) if isinstance(c, dict)]
            listed.extend(batch)
            if len(batch) < 250:
                break
            n += 1
    except K.Failure as exc:
        if polite.is_fatal(exc):
            raise
        listed = []
        complete = complete and master == "products.json"
        notes.append(f"collections.json could not be read ({exc.code}); positions and collection changes are unavailable")
    if len(listed) > cfg["max_collections"]:
        notes.append(f"{len(listed)} collections exist; the first {cfg['max_collections']} were read")
        listed = listed[:cfg["max_collections"]]
        complete = False
    for coll in listed:
        handle = clean_text(str(coll.get("handle") or "")).strip()
        if not handle or coll.get("products_count") == 0:
            continue
        members, p = [], 1
        try:
            while p <= cfg["max_pages_per_collection"]:
                data = _json(client, f"{base}/collections/{quote(handle)}/products.json?limit={per_page}&page={p}")
                batch = [r for r in (data.get("products") or []) if isinstance(r, dict)]
                for raw in batch:
                    pid = add(raw) if master == "collections" or raw.get("id") not in products else raw.get("id")
                    if pid is not None:
                        members.append(pid)
                if len(batch) < per_page:
                    break
                p += 1
            else:
                complete = False
                notes.append(f"collection {handle} has more than {cfg['max_pages_per_collection'] * per_page} products; the rest were not read")
        except K.Failure as exc:
            if polite.is_fatal(exc):
                raise
            if getattr(exc, "budget", False):
                stopped_for_budget = True
                break
            collection_errors += 1
            complete = False
            continue
        collections.append({"id": coll.get("id"), "handle": handle, "title": clean_text(str(coll.get("title") or "")).strip()[:120],
                            "products_count": coll.get("products_count") if isinstance(coll.get("products_count"), int) else len(members),
                            "updated_at": str(coll.get("updated_at") or ""), "members": members})
    if stopped_for_budget:
        complete = False
        notes.append(f"the request limit was reached after {len(collections)} of {len(listed)} collections; the rest waits for the next run")
    if collection_errors:
        notes.append(f"{collection_errors} collection(s) could not be read")

    best = {"honored": None, "top": [], "note": "not tested"}
    if cfg["probe_best_selling"] and master == "products.json" and not stopped_for_budget:
        try:
            best = _best_selling(client, base, per_page)
        except K.Failure as exc:
            if polite.is_fatal(exc):
                raise
            best = {"honored": None, "top": [], "note": f"the test could not be made ({exc.code})"}

    expected = (meta or {}).get("published_products_count")
    if expected is not None and products and len(products) < expected * 0.98:
        complete = False
        notes.append(f"the store says it has {expected} published products but {len(products)} were read")
    if not products:
        raise K.Failure(K.UPSTREAM_BROKEN, f"{urlsplit(base).netloc} published no products that could be read")
    return {"kind": "shopify_catalogue", "schema": 1, "domain": urlsplit(base).netloc, "captured_at": now, "complete": complete, "master_source": master,
            "meta": meta or {}, "notes": notes, "products": [products[i] for i in sorted(products, key=lambda x: (isinstance(x, str), x))], "collections": collections,
            "best_selling": best, "stats": {"products": len(products), "collections": len(collections), "expected_products": expected}}


def positions(snapshot):
    """{product_id: [(collection_handle, position starting at 1, collection size)]}, every membership kept."""
    out = {}
    for coll in snapshot.get("collections", []):
        size = len(coll["members"])
        for pos, pid in enumerate(coll["members"], start=1):
            out.setdefault(pid, []).append((coll["handle"], pos, size))
    return out


def summary(snapshot):
    """A few honest numbers about a catalogue snapshot, for reports and the sheet."""
    prods = snapshot.get("products", [])
    prices = [p["price_min"] for p in prods if p.get("price_min") is not None]
    return {"products": len(prods), "collections": len(snapshot.get("collections", [])), "available": sum(1 for p in prods if p.get("available")),
            "on_sale": sum(1 for p in prods if p.get("on_sale")), "price_min": min(prices) if prices else None, "price_max": max(prices) if prices else None,
            "currency": (snapshot.get("meta") or {}).get("currency", ""), "complete": snapshot.get("complete", False)}
