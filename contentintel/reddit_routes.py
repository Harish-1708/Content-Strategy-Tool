"""Reddit collection through two routes that complement each other: Reddit's official Data API, and Agent-Reach (OpenCLI in a browser a person has logged in to).

Both produce the same record (item id reddit:<post id>), so a post found by both is stored once: the API's values stand, Agent-Reach fills gaps and disagreements are noted
(the same policy as the Instagram routes, in agent_reach_instagram.merge_with_existing). Each route keeps its own health record, so a failing one rests while the other carries on.

TERMS, plainly. Reddit's Data API is free only for approved NON-COMMERCIAL use (about 100 requests a minute per client); commercial use needs a contract with Reddit (reported at $0.24
per 1,000 calls with a $12,000-a-year minimum), and new access requests were reported to have closed on 31 October 2026. These are secondary reports; Reddit's own pages could not be
opened when this was checked. An agency's use is commercial, so the API route REFUSES TO RUN until the owner has recorded that Reddit approved it
(reddit.api_approval in observatory.yaml: the basis, who approved, when, and a reference). Without that record no request is made and nothing can be charged.
The Agent-Reach route reads through a logged-in browser: that may also be against Reddit's terms for automated use, which is the owner's call, and it runs only on a machine you control.
"""
import re
import time

from . import contract as K
from .agent_reach_instagram import _count, _pick, rows_from
from .collectors import _iso, _row
from .safety import clean_text

SOURCE = "reddit"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API_BASE = "https://oauth.reddit.com"
POST_ID = re.compile(r"/comments/([a-z0-9]{3,10})", re.I)
APPROVAL_BASES = ("non_commercial_approved", "commercial_contract")


def approval_problem(approval):
    """'' when a usable approval record is present, else the plain-words reason the official API route will not run."""
    from . import guards
    if not guards.on("reddit_approval_required"):                  # the owner has not asked for the approval gate: the route runs whenever credentials exist
        return ""
    if not approval or not all(str(approval.get(k, "")).strip() for k in ("basis", "approved_by", "date")):
        return ("the Reddit API route needs a recorded approval from Reddit before it will run: set reddit.api_approval in observatory.yaml (basis, approved_by, date, reference). "
                "Reddit's free API is for approved non-commercial use only, and commercial use needs a paid contract. No request was made")
    if approval["basis"] not in APPROVAL_BASES:
        return f"reddit.api_approval.basis must be one of {', '.join(APPROVAL_BASES)}"
    return ""


def default_http(method, url, *, headers=None, params=None, data=None, auth=None, timeout=30):
    import requests
    try:
        resp = requests.request(method, url, headers=headers, params=params, data=data, auth=auth, timeout=timeout, allow_redirects=False)
    except requests.exceptions.Timeout:
        raise TimeoutError("timeout") from None
    except requests.exceptions.RequestException as exc:
        raise OSError(exc.__class__.__name__) from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body, {k.lower(): v for k, v in resp.headers.items()}


class RedditAPI:
    def __init__(self, client_id, client_secret, contact, approval, *, http=default_http, sleep=time.sleep, timeout=60):
        self.id, self.secret, self.contact, self.approval = client_id, client_secret, contact, approval
        self.http, self.sleep, self.timeout, self.token = http, sleep, timeout, ""

    @property
    def agent(self):
        return f"KelsonResearch/1.0 (contact: {self.contact})"            # Reddit requires a descriptive agent; it names us and how to reach us

    def _ready(self):
        problem = approval_problem(self.approval)
        if problem:
            raise K.Failure(K.NEED_SETUP, problem)
        if not (self.id and self.secret):
            raise K.Failure(K.NEED_SETUP, "the Reddit API route needs the secrets REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET (an app made at reddit.com/prefs/apps once Reddit has approved your access)")
        if not self.contact:
            raise K.Failure(K.NEED_SETUP, "Reddit requires a user agent that says who to contact: set OBSERVATORY_CONTACT")

    def _call(self, method, url, **kw):
        try:
            return self.http(method, url, timeout=self.timeout, **kw)
        except TimeoutError:
            raise K.Failure(K.TIMEOUT, "Reddit did not answer in time") from None
        except OSError as exc:
            raise K.Failure(K.UPSTREAM_BROKEN, f"network error talking to Reddit ({exc})") from None

    def _authorise(self):
        status, body, _ = self._call("POST", TOKEN_URL, headers={"User-Agent": self.agent}, data={"grant_type": "client_credentials"}, auth=(self.id, self.secret))
        if status in (401, 403) or (isinstance(body, dict) and body.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client")):
            raise K.Failure(K.NEED_SETUP, "Reddit rejected the app credentials (check the id and secret, and that Reddit has approved the app)")
        if status == 429:
            raise K.Failure(K.RATE_LIMITED, "Reddit asked us to slow down while signing in")
        token = (body or {}).get("access_token") if isinstance(body, dict) else None
        if status != 200 or not token:
            raise K.Failure(K.UPSTREAM_BROKEN, f"Reddit sign-in answered {status} without a token")
        self.token = token

    def listing(self, path, params):
        """The posts of one Reddit listing, as raw dicts. One sign-in per run; the rate limit Reddit reports is obeyed, never worked around."""
        self._ready()
        if not self.token:
            self._authorise()
        status, body, headers = self._call("GET", API_BASE + path, headers={"Authorization": "bearer " + self.token, "User-Agent": self.agent}, params={**params, "raw_json": 1})
        remaining, reset = headers.get("x-ratelimit-remaining"), headers.get("x-ratelimit-reset")
        if status == 429:
            wait = int(_num(reset) or _num(headers.get("retry-after")) or 60)
            raise K.Failure(K.RATE_LIMITED, "Reddit's rate limit was reached; this route rests", retry_after=wait)
        if status == 401:
            self.token = ""
            raise K.Failure(K.NEED_SETUP, "Reddit rejected the access token; it will sign in again next run")
        if status == 403:
            raise K.Failure(K.PLATFORM_BLOCKED, "Reddit refused access to this community (private, quarantined or banned); that is left alone")
        if status == 404:
            raise K.Failure(K.NOT_FOUND, "Reddit has no such community")
        if status >= 500 or status != 200:
            raise K.Failure(K.UPSTREAM_BROKEN, f"Reddit answered {status}")
        if remaining is not None and _num(remaining) is not None and _num(remaining) < 2 and _num(reset) and _num(reset) <= 60:
            self.sleep(_num(reset))                                               # near the limit: wait it out instead of crossing it
        kids = (((body or {}).get("data") or {}).get("children")) if isinstance(body, dict) else None
        return [k.get("data") or {} for k in (kids or []) if isinstance(k, dict) and k.get("kind") == "t3"]

    def subreddit(self, name, limit):
        return self.listing(f"/r/{name.strip().removeprefix('r/')}/new", {"limit": min(limit, 100)})

    def search(self, query, limit, subreddit=None):
        path = f"/r/{subreddit}/search" if subreddit else "/search"
        return self.listing(path, {"q": query, "sort": "new", "limit": min(limit, 100), "type": "link", **({"restrict_sr": 1} if subreddit else {})})


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_api_post(d):
    pid = d.get("id")
    if not pid or d.get("removed_by_category") in ("moderator", "deleted", "reddit"):
        return None
    thumb = d.get("thumbnail") if str(d.get("thumbnail", "")).startswith("http") else ""
    flair = f" [{d['link_flair_text']}]" if d.get("link_flair_text") else ""
    return _row(SOURCE, pid, handle=str(d.get("author") or ""), title=clean_text(str(d.get("title", ""))), text=clean_text(str(d.get("selftext", ""))),
                url="https://www.reddit.com" + str(d.get("permalink") or f"/comments/{pid}/"), posted_at=_iso(d.get("created_utc")), likes=_count(d.get("score")), comments=_count(d.get("num_comments")),
                media_url=thumb, details=f"r/{d.get('subreddit', '')}{flair}", found_via="api")


def normalize_agent_row(raw, hint=""):
    """One OpenCLI Reddit row as the same record, or None without a post link or id. Field names are read through the generic names Agent-Reach itself maps, and were not verified live."""
    url = str(_pick(raw, "url", "link", "permalink") or "")
    found = POST_ID.search(url) or POST_ID.search(str(raw.get("id") or ""))
    pid = (found.group(1).lower() if found else None) or (str(raw["id"]).removeprefix("t3_") if re.fullmatch(r"(?:t3_)?[a-z0-9]{3,10}", str(raw.get("id") or ""), re.I) else None)
    if not pid:
        return None
    if url.startswith("/"):
        url = "https://www.reddit.com" + url
    sub = str(_pick(raw, "subreddit", "community") or hint or "").removeprefix("r/")
    return _row(SOURCE, pid, handle=str(_pick(raw, "author", "user", "username") or "").removeprefix("u/"), title=clean_text(str(_pick(raw, "title", "name") or "")),
                text=clean_text(str(_pick(raw, "selftext", "text", "content", "body", "desc") or "")), url=url.split("?")[0] if url.startswith("https://") else f"https://www.reddit.com/comments/{pid}/",
                posted_at=_iso(_pick(raw, "created_utc", "created_at", "published_at", "date", "time")), likes=_count(raw.get("score"), raw.get("upvotes"), raw.get("ups")),
                comments=_count(raw.get("num_comments"), raw.get("comments"), raw.get("comment_count")), details=f"r/{sub}" if sub else "", found_via="agent_reach")


def collect(route, client, targets, limit):
    """([rows], [problems]) for targets [('r', subreddit) | ('q', query)] through one route. A missing community is noted and skipped; a refusal, rate limit, sign-in or setup problem stops the route."""
    rows, problems = [], []
    for kind, value in targets:
        try:
            if route == "api":
                posts = client.subreddit(value, limit) if kind == "r" else client.search(value, limit)
                made = [normalize_api_post(p) for p in posts]
            else:
                args = ["subreddit", value.removeprefix("r/"), "--limit", limit] if kind == "r" else ["search", value, "--limit", limit]
                made = [normalize_agent_row(r, value if kind == "r" else "") for r in rows_from(client.call(args))[:limit]]
            rows.extend(r for r in made if r)
        except K.Failure as exc:
            problems.append({"target": f"r/{value}" if kind == "r" else value, "code": exc.code, "message": str(exc)})
            if exc.code != K.NOT_FOUND:
                break
    seen, unique = set(), []
    for r in rows:                                              # one record per post even when two targets returned it
        if r["item_id"] not in seen:
            seen.add(r["item_id"])
            unique.append(r)
    return unique, problems
