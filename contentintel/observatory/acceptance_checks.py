"""One small live check per observatory route, run against the first brand's own website (so nothing third-party is touched by the weekly proof).

Each check is a function returning (items, what was read) or raising the failure with its real code. They share one polite client, so the checks obey the same
robots.txt, pause and identity rules as real monitoring.
"""
import copy

from .. import contract as K
from . import feeds as F
from . import monitor, polite, readers
from . import shopify as S
from . import sitemaps as M

NEEDS = {("competitor_stores", "public_json"): "catalogue_walk", ("competitor_stores", "sitemap"): "read_sitemap", ("competitor_pages", "static_fetch"): "read_page",
         ("competitor_pages", "feed"): "read_feed", ("competitor_pages", "reader"): "read_via_reader"}


def make_checks(domain, settings, contact, now, *, client=None, reader_http=None):
    """{(source, route): callable}. Empty when there is nothing to check against."""
    if not domain or not contact:
        return {}
    cfg = copy.deepcopy(settings)
    cfg["politeness"]["max_requests_per_domain"] = 14
    cfg["shopify"].update(max_collections=2, max_pages_per_collection=1, probe_best_selling=False)
    cfg["sitemaps"]["max_child_sitemaps"] = 2
    state = {}

    def get_client():
        if "c" not in state:
            state["c"] = client or polite.PoliteClient(cfg, contact=contact, now_fn=lambda: now)
        return state["c"]
    base = S.base_url(domain)

    def store():
        snap = S.walk_store(get_client(), domain, cfg, now)
        return len(snap["products"]), f"read {len(snap['products'])} products and {len(snap['collections'])} collection(s) of {domain}"

    def sitemap():
        c = get_client()
        got = M.walk_sitemaps(c, M.default_candidates(base, c.sitemaps_from_robots(base + "/")), cfg)
        return len(got["urls"]), f"read {len(got['urls'])} address(es) from {len(got['files'])} sitemap file(s) of {domain}"

    def page():
        snap = monitor._snapshot_page(get_client(), base + "/", cfg)
        return 1, f"read the home page of {domain} ({snap['title'][:40]!r}, {snap['chars']} characters)"

    def feed():
        c = get_client()
        snap_page = monitor._snapshot_page(c, base + "/", cfg)
        for url in [a["href"] for a in snap_page["alternates"]] + F.guess_feed_urls(base)[:2]:
            try:
                snap = monitor._snapshot_feed(c, url, cfg)
                return len(snap["items"]), f"read {len(snap['items'])} item(s) of a {snap['format']} feed of {domain}"
            except K.Failure as exc:
                if polite.is_fatal(exc):
                    raise
        raise K.Failure(K.NOT_FOUND, f"{domain} advertises no feed and has none at the usual addresses, so this route has nothing to read there")

    def reader():
        r = readers.JinaReader(cfg, get_client(), **({"http": reader_http} if reader_http else {}))
        got = r.read(base + "/")
        return 1, f"read the home page of {domain} through the reader ({len(got['text'])} characters)"
    return {("competitor_stores", "public_json"): store, ("competitor_stores", "sitemap"): sitemap, ("competitor_pages", "static_fetch"): page,
            ("competitor_pages", "feed"): feed, ("competitor_pages", "reader"): reader}
