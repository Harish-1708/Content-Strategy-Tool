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


def make_checks(domain, settings, contact, now, *, client=None, reader_http=None, other_domains=()):
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
        """Proves the feed reader on the first site that has a feed: the brand's own, then the other brands'. A site with no feed is not a failure of the route; if none has one the check is skipped."""
        from ..acceptance import Skipped
        c = get_client()
        tried = []
        for n, dom in enumerate([domain, *other_domains]):
            b = S.base_url(dom)
            tried.append(dom)
            try:
                snap_page = monitor._snapshot_page(c, b + "/", cfg)
            except K.Failure:
                if n == 0:
                    raise
                continue                                                                  # another brand's site that cannot be read is simply not the one to prove it on
            for url in [a["href"] for a in snap_page["alternates"]] + F.guess_feed_urls(b)[:2]:
                try:
                    snap = monitor._snapshot_feed(c, url, cfg)
                    return len(snap["items"]), f"read {len(snap['items'])} item(s) of a {snap['format']} feed of {dom}"
                except K.Failure as exc:
                    if n == 0 and polite.is_fatal(exc):
                        raise
        raise Skipped(f"no feed to read: {', '.join(tried)} {'advertises' if len(tried) == 1 else 'advertise'} none and {'has' if len(tried) == 1 else 'have'} none at the usual addresses, so this route could not be proved on {'it' if len(tried) == 1 else 'them'}")

    def reader():
        r = readers.JinaReader(cfg, get_client(), **({"http": reader_http} if reader_http else {}))
        got = r.read(base + "/")
        return 1, f"read the home page of {domain} through the reader ({len(got['text'])} characters)"
    return {("competitor_stores", "public_json"): store, ("competitor_stores", "sitemap"): sitemap, ("competitor_pages", "static_fetch"): page,
            ("competitor_pages", "feed"): feed, ("competitor_pages", "reader"): reader}


# The Agent-Reach research channels the acceptance run proves, each with the one small thing it tries. The first seven need OpenCLI and a browser logged in to the site.
OPENCLI_CHANNELS = {"instagram": ("search", "Instagram"), "reddit": ("search", "Reddit"), "twitter": ("search", "X"), "facebook": ("search", "Facebook"), "linkedin": ("jobs", "LinkedIn"),
                    "xiaohongshu": ("search", "Xiaohongshu"), "boss": ("search", "Boss")}
AGENT_REACH_CHANNELS = tuple(OPENCLI_CHANNELS) + ("github", "web")


def agent_reach_checks(settings, contact, now, *, environ=None, cli=None, github=None, web_client=None):
    """{('agent_reach_research', channel): callable} for the channels that can be proved here: GitHub (its public API) and web (the polite reader on a public page, which needs the contact address),
    and the OpenCLI channels only where OpenCLI is installed and the owner's guards allow it. `cli` may be a function from a channel to an OpenCLI client (for tests). The rest show as skipped with the reason."""
    import os

    from .. import agent_reach_catalog as CAT
    from .. import agent_reach_instagram as AR
    from .. import services as SV
    environ = environ if environ is not None else os.environ
    out = {}
    for channel, (action, _site) in OPENCLI_CHANNELS.items():
        client = cli(channel) if cli else AR.OpenCLI(environ.get("OPENCLI_BIN", "opencli"), site=channel, environ=environ)
        if not SV.agent_reach_allowed(settings, channel, CAT.purpose_for(channel))[0] or AR.hosted_runner(environ) or not client.available():
            continue

        def run(ch=channel, act=action, c=client):
            argv, _clean = CAT.prepare(ch, act, {"query": environ.get("AGENT_REACH_CHECK_QUERY", "marketing")}, limit=2)
            rows = AR.rows_from(c.call(argv))
            if not rows:
                raise K.Failure(K.UPSTREAM_BROKEN, f"the {ch} {act} through Agent-Reach returned nothing")
            return len(rows), f"read {len(rows)} {ch} result(s) through Agent-Reach (OpenCLI)"
        out[("agent_reach_research", channel)] = run
    if SV.agent_reach_allowed(settings, "github", "general_research")[0]:
        def github_check():
            client = github or SV.agent_reach_client(settings, "github", "general_research", environ=environ)
            tried = []
            for query in (environ.get("GITHUB_CHECK_QUERY", "agent reach"), "python"):               # a broad query: it must match something, or the route is not working
                neutral = ["search", query, "--limit", 2]
                rows = AR.rows_from(client.call_action("github", "search", {"query": query, "limit": "2"}, None, neutral) if hasattr(client, "call_action") else client.call(neutral))
                if rows:
                    return len(rows), f"read {len(rows)} GitHub repositor(ies) through the public API for {query!r}"
                tried.append(query)
            raise K.Failure(K.UPSTREAM_BROKEN, f"GitHub answered but found no repository for {' or '.join(repr(q) for q in tried)}, which always match thousands: most likely GitHub is limiting this machine (the workflow passes GITHUB_TOKEN to lift that) or the Agent-Reach GitHub search is not working")
        out[("agent_reach_research", "github")] = github_check
    if contact and SV.agent_reach_allowed(settings, "web", "general_research")[0]:
        def web_check():
            from . import polite
            c = web_client or polite.PoliteClient(settings, contact=contact, now_fn=lambda: now)
            got = c.get(environ.get("WEB_CHECK_URL", "https://example.com/"))
            if not getattr(got, "body", b""):
                raise K.Failure(K.UPSTREAM_BROKEN, "the page came back empty")
            return 1, f"read a public page through the shared page reader ({len(got.body)} bytes)"
        out[("agent_reach_research", "web")] = web_check
    return out


def agent_reach_missing(settings, contact, environ=None, cli=None):
    """{('agent_reach_research', channel): the true reason it cannot be proved here}, for the channels agent_reach_checks leaves out."""
    import os

    from .. import agent_reach_catalog as CAT
    from .. import agent_reach_instagram as AR
    from .. import services as SV
    environ = environ if environ is not None else os.environ
    out = {}
    for channel in AGENT_REACH_CHANNELS:
        allowed, why = SV.agent_reach_allowed(settings, channel, CAT.purpose_for(channel) if channel != "web" else "general_research")
        key = ("agent_reach_research", channel)
        if not allowed:
            out[key] = f"switched off: {why}"
        elif channel == "web" and not contact:
            out[key] = "no contact address: set OBSERVATORY_CONTACT (the polite reader puts it in the request)"
        elif channel in OPENCLI_CHANNELS:
            site = OPENCLI_CHANNELS[channel][1]
            if AR.hosted_runner(environ):
                out[key] = f"cannot run on a GitHub-hosted runner: it needs OpenCLI and a browser logged in to {site}, which must stay on a machine you control"
            else:
                client = cli(channel) if cli else AR.OpenCLI(environ.get("OPENCLI_BIN", "opencli"), site=channel, environ=environ)
                if not client.available():
                    out[key] = f"cannot run: OpenCLI is not installed on this machine (it needs a browser logged in to {site}, on a machine you control)"
    return out


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
