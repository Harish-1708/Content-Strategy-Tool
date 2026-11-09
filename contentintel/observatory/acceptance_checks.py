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


def reddit_checks(settings, contact, *, environ=None, http=None, cli=None):
    """{('reddit', route): callable} for the two Reddit routes, each reading two posts of a public community. Only routes that can run here are included, so the others show as skipped."""
    import os

    from .. import agent_reach_instagram as AR
    from .. import reddit_routes as RR
    environ = environ if environ is not None else os.environ
    out = {}
    sub = environ.get("REDDIT_CHECK_SUBREDDIT", "announcements")
    cfg = settings["reddit"]

    def run(route, client):
        rows, problems = RR.collect(route, client, [("r", sub)], 2)
        if rows:
            return len(rows), f"read {len(rows)} post(s) of r/{sub} through the {route} route"
        first = problems[0] if problems else {"code": K.UPSTREAM_BROKEN, "message": "nothing came back"}
        raise K.Failure(first["code"], first["message"])
    if contact and not RR.approval_problem(cfg["api_approval"]) and environ.get("REDDIT_CLIENT_ID") and environ.get("REDDIT_CLIENT_SECRET"):
        api = RR.RedditAPI(environ["REDDIT_CLIENT_ID"].strip(), environ["REDDIT_CLIENT_SECRET"].strip(), contact, cfg["api_approval"], http=http or RR.default_http, timeout=cfg["timeout_seconds"])
        out[("reddit", "api")] = lambda: run("api", api)
    from .. import services as SV
    opencli = cli or AR.OpenCLI(environ.get("OPENCLI_BIN", "opencli"), site="reddit", environ=environ)
    if not AR.hosted_runner(environ) and opencli.available() and SV.agent_reach_allowed(settings, "reddit", "content_research")[0]:
        out[("reddit", "agent_reach")] = lambda: run("agent_reach", opencli)
    from .. import ytdlp_youtube as YT
    yt = YT.YtDlpYouTube() if cli is None else cli                          # Agent-Reach's YouTube channel is yt-dlp: no login, so a hosted runner is fine
    if (not getattr(yt, "needs_browser", True) or not AR.hosted_runner(environ)) and yt.available() and SV.agent_reach_allowed(settings, "youtube", "youtube_research")[0]:
        def youtube_check():
            rows = AR.rows_from(yt.call(["search", environ.get("YOUTUBE_CHECK_QUERY", "marketing"), "--limit", 2]))
            if not rows:
                raise K.Failure(K.UPSTREAM_BROKEN, "the YouTube search through Agent-Reach returned nothing")
            return len(rows), f"read {len(rows)} YouTube result(s) through Agent-Reach"
        out[("agent_reach_research", "youtube")] = youtube_check
    return out


def reddit_missing(settings, contact, environ=None):
    """{('reddit', route): the plain reason it cannot be checked here}."""
    import os

    from .. import agent_reach_instagram as AR
    from .. import reddit_routes as RR
    environ = environ if environ is not None else os.environ
    out = {}
    problem = RR.approval_problem(settings["reddit"]["api_approval"])
    if problem:
        out[("reddit", "api")] = "cannot run: no recorded Reddit approval (reddit.api_approval); the route would not make any request"
    elif not (environ.get("REDDIT_CLIENT_ID") and environ.get("REDDIT_CLIENT_SECRET")):
        out[("reddit", "api")] = "no key: set REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET"
    elif not contact:
        out[("reddit", "api")] = "no contact address: set OBSERVATORY_CONTACT (Reddit requires a user agent that says who to contact)"
    from .. import services as SV
    for source, route, channel, purpose, name in (("reddit", "agent_reach", "reddit", "content_research", "Reddit"), ("agent_reach_research", "youtube", "youtube", "youtube_research", "YouTube")):
        allowed, why = SV.agent_reach_allowed(settings, channel, purpose)
        if not allowed:
            out[(source, route)] = f"switched off: {why}"
        elif channel == "youtube":
            from .. import ytdlp_youtube as YT
            if not YT.YtDlpYouTube().available():
                out[(source, route)] = "cannot run: yt-dlp is not installed on this machine (pip install yt-dlp)"
        elif AR.hosted_runner(environ):
            out[(source, route)] = f"cannot run on a GitHub-hosted runner: it needs a browser logged in to {name}, which must stay on a machine you control"
        elif not AR.OpenCLI(environ.get("OPENCLI_BIN", "opencli"), site=channel, environ=environ).available():
            out[(source, route)] = "cannot run: OpenCLI is not installed on this machine"
    return out
