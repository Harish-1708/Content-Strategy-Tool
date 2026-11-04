"""Shared helpers for the observatory tests: scripted web responses, a fake clock, and settings that do not wait for real."""
import copy
import gzip
import json

from contentintel.observatory import config as OC
from contentintel.observatory import polite as P
from contentintel.observatory.pauses import PauseLedger

NOW = "2026-11-03T12:00:00Z"
CONTACT = "research@kelson.example"
PUBLIC = lambda host, port=None: [(2, 1, 6, "", ("93.184.216.34", 0))]          # a resolver that says every name is a public address
PRIVATE = lambda host, port=None: [(2, 1, 6, "", ("10.0.0.5", 0))]


def settings(**overrides):
    cfg = OC.load_settings("does-not-exist.yaml")
    cfg = copy.deepcopy(cfg)
    for path, value in overrides.items():
        section, key = path.split("__")
        cfg[section][key] = value
    return cfg


class Clock:
    """Time that only moves when something sleeps."""

    def __init__(self, start=1000.0):
        self.t, self.sleeps = start, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def reply(status=200, body="", headers=None):
    if isinstance(body, str):
        body = body.encode()
    return status, {k.lower(): v for k, v in (headers or {}).items()}, body


def json_reply(obj, status=200, headers=None):
    return reply(status, json.dumps(obj), {"content-type": "application/json", **(headers or {})})


def gz(text):
    return gzip.compress(text.encode() if isinstance(text, str) else text)


class Web:
    """A scripted internet: routes by exact url (or url prefix ending in *), each a reply or a list played in order (the last repeats)."""

    def __init__(self, routes=None, robots=None):
        self.routes, self.log = {}, []
        for url, value in (routes or {}).items():
            self.on(url, value)
        if robots is not None:
            self.on_robots("https://shop.example", robots)

    def on(self, url, value):
        self.routes[url] = list(value) if isinstance(value, list) else [value]
        return self

    def on_robots(self, origin, text, status=200):
        return self.on(origin + "/robots.txt", reply(status, text, {"content-type": "text/plain"}))

    def __call__(self, method, url, headers, timeout, max_bytes):
        self.log.append(url)
        script = self.routes.get(url)
        if script is None:
            for key, value in self.routes.items():
                if key.endswith("*") and url.startswith(key[:-1]):
                    script = value
                    break
        if script is None:
            return reply(404, "not found")
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        status, hdrs, body = item
        return status, hdrs, body[:max_bytes + 1]

    def count(self, url):
        return self.log.count(url)


def client(web, cfg=None, *, ledger=None, clock=None, resolver=PUBLIC, contact=CONTACT, **kw):
    clock = clock or Clock()
    c = P.PoliteClient(cfg or settings(), contact=contact, http=web, clock=clock, sleep=clock.sleep, now_fn=lambda: NOW, ledger=ledger, resolver=resolver, **kw)
    c.test_clock = clock
    return c


def ledger(tmp_path):
    return PauseLedger(tmp_path)


# ---- a scripted Shopify store ------------------------------------------------------------------------------------------------------------------

def product(pid, title, price="20.00", compare=None, available=True, tags=None, handle=None, vendor="Acme", created="2026-10-01T00:00:00Z", extra_variants=None,
            body="<p>A product.</p>", images=1, ptype="Bars"):
    variants = [{"id": pid * 10, "title": "Default Title", "sku": f"SKU{pid}", "price": price, "compare_at_price": compare, "available": available}]
    for n, v in enumerate(extra_variants or [], start=1):
        variants.append({"id": pid * 10 + n, "title": v.get("title", f"V{n}"), "sku": "", "price": v.get("price", price), "compare_at_price": v.get("compare"),
                         "available": v.get("available", True)})
    return {"id": pid, "title": title, "handle": handle or title.lower().replace(" ", "-"), "vendor": vendor, "product_type": ptype, "tags": tags or [],
            "created_at": created, "updated_at": created, "published_at": created, "body_html": body, "variants": variants,
            "images": [{"src": f"https://cdn.example/{pid}-{i}.jpg"} for i in range(images)]}


def collection(cid, handle, title=None, count=None):
    return {"id": cid, "handle": handle, "title": title or handle.title(), "products_count": count, "updated_at": "2026-10-01T00:00:00Z"}


def register_store(web, domain, products, collections=None, *, per_page=250, meta=True, robots="User-agent: *\nDisallow: /cart\n", best_selling=None,
                   products_json=True, collections_json=True):
    """Script a Shopify store: robots, meta, products.json, collections.json and each collection's products (membership: {handle: [product ids in order]})."""
    base = f"https://{domain}"
    by_id = {p["id"]: p for p in products}
    web.on_robots(base, robots)
    if meta is True:
        meta = {"name": domain, "currency": "USD", "country": "US", "domain": domain, "myshopify_domain": domain.split(".")[0] + ".myshopify.com",
                "published_products_count": len(products)}
    if meta:
        web.on(base + "/meta.json", json_reply(meta))
    if products_json:
        for start in range(0, max(len(products), 1), per_page):
            web.on(f"{base}/products.json?limit={per_page}&page={start // per_page + 1}", json_reply({"products": products[start:start + per_page]}))
        if len(products) % per_page == 0 and products:
            web.on(f"{base}/products.json?limit={per_page}&page={len(products) // per_page + 1}", json_reply({"products": []}))
        web.on(f"{base}/products.json?limit=1", json_reply({"products": products[:1]}))
    if collections_json:
        colls = collections or {}
        listing = [collection(100 + n, h, count=len(ids)) for n, (h, ids) in enumerate(colls.items())]
        web.on(f"{base}/collections.json?limit=250&page=1", json_reply({"collections": listing}))
        for h, ids in colls.items():
            for start in range(0, max(len(ids), 1), per_page):
                web.on(f"{base}/collections/{h}/products.json?limit={per_page}&page={start // per_page + 1}",
                       json_reply({"products": [by_id[i] for i in ids[start:start + per_page] if i in by_id]}))
            if len(ids) % per_page == 0 and ids:
                web.on(f"{base}/collections/{h}/products.json?limit={per_page}&page={len(ids) // per_page + 1}", json_reply({"products": []}))
    all_ids = [p["id"] for p in products]
    web.on(f"{base}/collections/all/products.json?limit={per_page}&page=1", json_reply({"products": [by_id[i] for i in all_ids]}))
    order = best_selling if best_selling is not None else all_ids
    web.on(f"{base}/collections/all/products.json?limit={per_page}&page=1&sort_by=best-selling", json_reply({"products": [by_id[i] for i in order]}))
    return web
