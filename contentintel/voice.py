"""Audience voice: what people actually say about a topic, brand or competitor, with the engagement that proves it.

The engine is last30days-skill (https://github.com/mvanhorn/last30days-skill, MIT licence, copyright 2026 Matt Van Horn), connected as it is: its own program, run from its own repository checkout
(connection `last30days`, contentintel/connections.py), reading Reddit (real upvote counts), Hacker News, YouTube, GitHub, Polymarket, Digg, Bluesky, dev.to and more without a key. The platform adds only
what a program run by a workflow needs: a query plan written here (so the engine needs no language model of its own), the owner's switches, a health score, and the answer in the platform's shape.

Three modes: research (one topic), competitors (several named peers side by side) and discover (what is accelerating in a field). The platform's own backup is a small Hacker News and Reddit reader
(below) that runs when the repository cannot, as the owner's route chooses. Everything comes back as `voice items`: {source, title, url, summary, engagement, published, cluster}; they are kept per brand in
data/<brand>/extras/voice.json and used four ways: as audience evidence when ideas are scored, as quotes in briefs, as seeds for further research, and in the competitor view.
Nothing is made up: an empty answer is an empty answer, and a source that failed is reported as failed, never as "nothing said".
"""
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote_plus

from . import connections as CONN
from . import contract as K
from .safety import scrub

SCRIPT = Path("skills") / "last30days" / "scripts" / "last30days.py"
MODES = ("research", "competitors", "discover")
KEEP = 2000                      # items kept per brand
SUMMARY = 400
FAILED = {"rate-limited", "auth-failed", "unreachable", "timeout", "schema-drift", "error", "payment-required"}
# What the engine may read without any key; the owner's other keys (a GitHub token, a search key) are handed over only if the connection's secrets switch is on.
DEFAULT_SOURCES = ("reddit", "hackernews", "youtube", "github", "polymarket", "digg", "bluesky", "devto")
# Three lanes the engine runs through the ScrapeCreators service (100 free credits once, then paid from about 10 dollars a month). They are added to every query ONLY when the owner has set SCRAPECREATORS_API_KEY and the connection's secrets switch is on.
KEYED_LANES = ("tiktok", "instagram", "meta_ads")
SIGNALS = ("complaint", "praise", "question", "comparison")


def build_plan(topic, sources=None, intent="product"):
    """The JSON query plan the engine accepts with --plan: the topic itself, what people complain about, and what people compare it with. Written here so no language model is needed."""
    topic = " ".join(str(topic).split())[:120]
    srcs = list(sources or DEFAULT_SOURCES)
    return {"intent": intent, "freshness_mode": "balanced_recent", "cluster_mode": "topic", "subqueries": [
        {"label": "primary", "search_query": topic, "ranking_query": f"What are people saying about {topic} right now?", "sources": srcs, "weight": 1.0},
        {"label": "problems", "search_query": f"{topic} problem complaint regret returns", "ranking_query": f"What do people complain about or regret with {topic}?", "sources": [s for s in srcs if s in ("reddit", "youtube", "hackernews", "bluesky")] or srcs, "weight": 0.8},
        {"label": "choosing", "search_query": f"{topic} vs alternative worth it review", "ranking_query": f"What do people compare {topic} with, and what makes them choose?", "sources": [s for s in srcs if s in ("reddit", "youtube", "hackernews")] or srcs, "weight": 0.7}]}


def _engagement_total(eng):
    """One headline number for the engagement counters of a result (the biggest counter-like value; nothing is invented)."""
    if not isinstance(eng, dict):
        return 0
    nums = [v for k, v in eng.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and "ratio" not in k and v >= 0]
    return int(max(nums)) if nums else 0


def _item(source, title, url, summary, eng, published, cluster, topic, mode, entity=""):
    return {"source": str(source or "")[:30], "title": " ".join(str(title or "").split())[:200], "url": str(url or "")[:400], "summary": " ".join(str(summary or "").split())[:SUMMARY],
            "engagement": {k: v for k, v in (eng or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}, "weight": _engagement_total(eng), "published": str(published or "")[:30],
            "cluster": str(cluster or "")[:120], "topic": topic, "mode": mode, "entity": entity}


def _from_report(report, topic, mode, entity=""):
    clusters = report.get("clusters") or []
    out = []
    for r in report.get("results") or []:
        if not isinstance(r, dict):
            continue
        idx = r.get("cluster")
        cl = clusters[idx].get("title", "") if isinstance(idx, int) and 0 <= idx < len(clusters) and isinstance(clusters[idx], dict) else ""
        out.append(_item(r.get("source"), r.get("title"), r.get("url") or r.get("candidate_id"), r.get("summary"), r.get("engagement"), r.get("published_at"), cl, topic, mode, entity))
    return out


def normalise(report, topic, mode="research"):
    """voice items from any of the engine's three JSON shapes: a research report, a comparison envelope (one report per entity) or a discovery export (topics that are accelerating)."""
    if not isinstance(report, dict):
        raise K.Failure(K.UPSTREAM_BROKEN, "last30days answered with something that is not a report")
    status = report.get("source_status") if isinstance(report.get("source_status"), dict) else {}
    if report.get("comparison") and isinstance(report.get("reports"), list):
        items, status = [], {}
        for entry in report["reports"]:
            if isinstance(entry, dict) and isinstance(entry.get("report"), dict):
                items += _from_report(entry["report"], topic, "competitors", str(entry.get("entity") or ""))
                for k, v in (entry["report"].get("source_status") or {}).items():
                    status[k] = v if status.get(k) in (None, "ok", "no-results") else status[k]
        return items, status
    if report.get("kind") == "discovery":
        items = []
        for r in report.get("results") or []:
            if isinstance(r, dict):
                title = r.get("topic") or r.get("title") or r.get("name") or r.get("headline") or ""
                eng = {k: v for k, v in r.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in ("rank",)}
                items.append(_item(",".join(map(str, r.get("sources") or [])) or r.get("source") or "discovery", title, r.get("url") or "", r.get("summary") or r.get("angle") or r.get("why") or "", eng, r.get("published_at") or "", "", topic or report.get("domain", ""), "discover"))
        return items, status
    return _from_report(report, topic, mode), status


def failed_sources(status):
    return sorted(k for k, v in (status or {}).items() if v in FAILED)


class ExternalLast30Days:
    """last30days run as itself: `python skills/last30days/scripts/last30days.py <topic> --emit json --plan <file>` inside its own checkout, in an environment that holds only what the connection declares."""

    def __init__(self, settings, *, root=None, environ=None, runner=subprocess.run, python=None, timeout=420):
        self.settings, self.runner, self.timeout = settings, runner, timeout
        self.root = (Path(root) if root else CONN.external_dir() / "last30days").resolve()          # absolute: the engine is started INSIDE this folder, so a relative script path would be looked for there a second time
        self.environ = environ if environ is not None else {}
        self.python = python or sys.executable
        self.engine_notes = ""                     # what the engine said about sources that failed, even when other sources answered
        self.last_error = ""                       # why the repository's own route failed on the last call, in words (the platform's backup answers; this is what the run reports)

    def command(self, topic, mode, plan_path, peers=(), depth="quick"):
        script = str(self.root / SCRIPT)
        if mode == "discover":
            cmd = [self.python, script, "--discover", topic or "", "--discover-shallow", "--emit", "json"]
        else:
            query = f"{topic} vs " + " vs ".join(peers) if mode == "competitors" and peers else topic
            cmd = [self.python, script, query, "--emit", "json", "--plan", str(plan_path), "--no-browser-cookies"]
            if mode == "competitors" and peers:
                cmd += ["--competitors-list", ",".join(peers)]
        return cmd + [f"--{depth}"] if depth in ("quick", "deep") and mode != "discover" else cmd

    def run(self, topic, mode="research", peers=(), depth="quick"):
        self.last_error = self.engine_notes = ""
        try:
            return self._run(topic, mode, peers, depth)
        except K.Failure as exc:
            self.last_error = f"{exc.code}: {exc.message}"
            raise

    def _run(self, topic, mode, peers, depth):
        if not (self.root / SCRIPT).is_file():
            raise K.Failure(K.NEED_SETUP, "last30days is not installed here (run the connections install action)")
        env = CONN.external_env(self.settings, "last30days", self.environ)
        with tempfile.TemporaryDirectory(prefix="l30-") as tmp:
            env["HOME"], env["LAST30DAYS_MEMORY_DIR"], env["XDG_CONFIG_HOME"] = tmp, "", tmp         # nothing is saved outside this folder; the engine's own config is never read
            plan = Path(tmp) / "plan.json"
            lanes = DEFAULT_SOURCES + (KEYED_LANES if env.get("SCRAPECREATORS_API_KEY") else ())          # env holds the key only if the owner allows it
            plan.write_text(json.dumps(build_plan(topic, lanes)), encoding="utf-8")
            cmd = self.command(topic, mode, plan, peers, depth)
            try:
                done = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout, env=env, cwd=str(self.root))
            except subprocess.TimeoutExpired:
                raise K.Failure(K.TIMEOUT, f"last30days did not finish within {self.timeout} seconds") from None
            except OSError as exc:
                raise K.Failure(K.NEED_SETUP, f"last30days could not start ({exc.__class__.__name__})") from None
        if done.returncode != 0:
            raise K.Failure(K.UPSTREAM_BROKEN, f"last30days stopped ({done.returncode}): {scrub((done.stderr or '')[-300:], 300)}")
        engine_notes = " | ".join(ln.strip() for ln in (done.stderr or "").splitlines() if ln.strip() and any(w in ln.lower() for w in ("failed", "missing", "blocked", "error", "refused", "denied", "forbidden", "sign in", "not a bot", "401", "403", "429")))[-400:]
        self.engine_notes = scrub(engine_notes, 400)
        text = (done.stdout or "").strip()
        start = text.find("{")
        try:
            report = json.loads(text[start:]) if start >= 0 else None
        except ValueError:
            report = None
        if report is None:
            raise K.Failure(K.UPSTREAM_BROKEN, "last30days printed no JSON report")
        items, status = normalise(report, topic, mode)
        bad = failed_sources(status)
        if not items and bad and len(bad) == len(status):
            raise K.Failure(K.UPSTREAM_BROKEN, "every source last30days tried failed: " + ", ".join(f"{k} {status[k]}" for k in bad)
                            + (f". The engine said: {scrub(engine_notes, 400)}" if engine_notes else ""))
        return items, status


class BuiltinVoice:
    """The platform's own backup: Hacker News (its public search) and Reddit (its public search), each a plain read with the engagement the site shows. `get(url)` returns (status, text)."""
    HN = "https://hn.algolia.com/api/v1/search?query={q}&tags=story&hitsPerPage=15&numericFilters=created_at_i>{since}"
    REDDIT = "https://www.reddit.com/search.json?q={q}&sort=top&t=month&limit=20"

    def __init__(self, get, now_epoch):
        self.get, self.now = get, now_epoch

    def _json(self, url):
        status, text = self.get(url)
        if status == 429:
            raise K.Failure(K.RATE_LIMITED, "the site asked us to slow down")
        if status in (401, 403):
            raise K.Failure(K.PLATFORM_BLOCKED, f"the site refused this address (HTTP {status})")
        if status != 200:
            raise K.Failure(K.UPSTREAM_BROKEN, f"HTTP {status}")
        try:
            return json.loads(text)
        except ValueError:
            raise K.Failure(K.UPSTREAM_BROKEN, "the answer was not JSON") from None

    def run(self, topic, mode="research", peers=(), depth="quick"):
        if mode == "discover":
            raise K.Failure(K.BAD_INPUT, "the backup reader cannot discover trending topics (that needs last30days)")
        items, status = [], {}
        for entity in ((topic,) + tuple(peers) if mode == "competitors" else (topic,)):
            q = quote_plus(entity)
            try:
                for hit in (self._json(self.HN.format(q=q, since=int(self.now) - 30 * 86400)).get("hits") or []):
                    items.append(_item("hackernews", hit.get("title"), hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}", "",
                                       {"points": hit.get("points") or 0, "num_comments": hit.get("num_comments") or 0}, str(hit.get("created_at") or "")[:10], "", topic, mode, entity if mode == "competitors" else ""))
                status["hackernews"] = "ok"
            except K.Failure as exc:
                status["hackernews"] = "rate-limited" if exc.code == K.RATE_LIMITED else "unreachable"
            try:
                for child in ((self._json(self.REDDIT.format(q=q)).get("data") or {}).get("children") or []):
                    d = child.get("data") or {}
                    items.append(_item("reddit", d.get("title"), "https://www.reddit.com" + str(d.get("permalink") or ""), d.get("selftext"),
                                       {"score": d.get("score") or 0, "num_comments": d.get("num_comments") or 0}, "", d.get("subreddit_name_prefixed") or "", topic, mode, entity if mode == "competitors" else ""))
                status["reddit"] = "ok"
            except K.Failure as exc:
                status["reddit"] = "rate-limited" if exc.code == K.RATE_LIMITED else "auth-failed" if exc.code == K.PLATFORM_BLOCKED else "unreachable"
        if not items and all(v != "ok" for v in status.values()):
            raise K.Failure(K.UPSTREAM_BROKEN, "the backup reader could not reach Hacker News or Reddit")
        return items, status


def gather(settings, topic, *, mode="research", peers=(), depth="quick", now="", external=None, builtin=None, ledger=None):
    """(items, source status, which route answered). The owner's route and switch decide the order (contentintel/connections.py); a failed route is recorded and the other is tried."""
    if mode not in MODES:
        raise K.Failure(K.BAD_INPUT, f"mode must be one of {', '.join(MODES)}")
    (items, status), which = CONN.run_connection(settings, "last30days", (lambda: external.run(topic, mode, peers, depth)) if external else None,
                                                 (lambda: builtin.run(topic, mode, peers, depth)) if builtin else None, now=now, ledger=ledger)
    return items, status, which


def merge(existing, items, now):
    """Add voice items to a brand's kept voice (data/<brand>/extras/voice.json): the same address is kept once with the larger engagement; the most engaged KEEP items stay."""
    def key_of(i):
        return f"{i.get('entity', '')}|" + (i["url"] or f"{i['source']}:{i['title']}")      # the same thread under two competitors is kept for each of them

    by_url = {key_of(i): i for i in (existing or {}).get("items", [])}
    fresh = 0
    for it in items:
        key = key_of(it)
        old = by_url.get(key)
        if old is None:
            fresh += 1
            by_url[key] = dict(it, first_seen=now, last_seen=now)
        else:
            old.update(last_seen=now)
            if it["weight"] > old.get("weight", 0):
                old.update(engagement=it["engagement"], weight=it["weight"])
    kept = sorted(by_url.values(), key=lambda i: (-i.get("weight", 0), i.get("title", "")))[:KEEP]
    return {"updated": now, "items": kept}, fresh


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]{4,}", str(text).lower())}


def evidence_for(voice, text, limit=2):
    """The kept voice items that share the most words with `text` (an idea, a topic): [] when nothing really overlaps (two shared words at least)."""
    want = _words(text)
    scored = []
    for it in (voice or {}).get("items", []):
        common = want & _words(f"{it.get('title', '')} {it.get('summary', '')}")
        if len(common) >= 2:
            scored.append((len(common), it.get("weight", 0), it))
    return [it for _, _, it in sorted(scored, key=lambda t: (-t[0], -t[1]))[:limit]]


def prompt_lines(voice, limit=5):
    """The most engaged things people said, as short lines for a scoring or brief prompt. '' when nothing is kept."""
    top = sorted((voice or {}).get("items", []), key=lambda i: -i.get("weight", 0))[:limit]
    return "\n".join(f"- ({i['source']}, {i['weight']} engagement) {i['title']}" + (f": {i['summary'][:160]}" if i.get("summary") else "") for i in top if i.get("title"))


GENERIC_LABELS = {"core", "merch", "kids", "other", "accessories", "gift", "gifts", "sale", "new", "bundle", "bundles", "all", "default", "uncategorised", "uncategorized"}


def topics_for(brand, store, extra=(), limit=5, seeds=()):
    """Topics to ask about for a brand, from its own catalogue (its biggest product categories) and its name: nothing is hardcoded to a brand."""
    latest = store.load_latest(brand.slug) or {}
    cats = {}
    for p in (latest.get("products") or {}).values():
        if isinstance(p, dict) and not p.get("excluded") and p.get("category"):
            cats[p["category"]] = cats.get(p["category"], 0) + 1
    labels = [c for c, _ in sorted(cats.items(), key=lambda kv: -kv[1]) if c and len(c) >= 4 and c.lower() not in GENERIC_LABELS]
    room = max(1, int(limit) - len(extra) - 1)
    # what people search for beats the shop's own category labels ('core', 'merch', 'kids' say nothing to an audience): the brand's seed keywords first, then real category names
    out = list(dict.fromkeys([*(str(s).strip() for s in seeds if str(s).strip()), *labels]))[:room]
    return list(dict.fromkeys([*extra, *out, brand.name]))


def seed_topics(voice, limit=5):
    """Topics worth asking about next, taken from what people were already discussing: the most engaged cluster headlines (a suggestion for the owner; nothing is added to research.yaml by itself)."""
    best = {}
    for it in (voice or {}).get("items", []):
        head = it.get("cluster") or it.get("title") or ""
        if head and it.get("weight", 0) > best.get(head, 0):
            best[head] = it["weight"]
    return [h for h, _ in sorted(best.items(), key=lambda kv: -kv[1])[:limit]]


def by_entity(voice):
    """{entity: [number of items, total engagement]} for the competitors-mode items, so brands can be compared by how much people talk about each."""
    out = {}
    for it in (voice or {}).get("items", []):
        if it.get("entity"):
            n = out.setdefault(it["entity"], [0, 0])
            n[0] += 1
            n[1] += it.get("weight", 0)
    return out


def comment_items(bank_items, limit_per_video=8):
    """What viewers wrote under the videos the platform has already read (the top comments yt-dlp returned for each saved or collected video): the audience's own words, with the likes each comment got, from the very
    platforms the brand is about (TikTok, Instagram, YouTube...). Needs no key and no extra reading: the comments are already in the Idea Bank items' video facts. Returns voice items."""
    out = []
    for item in (bank_items or {}).values():
        meta = item.get("video_meta") or {}
        for c in (meta.get("top_comments") or [])[:limit_per_video]:
            text = " ".join(str(c.get("text") or "").split())
            if len(text) < 8:
                continue
            likes = int(c.get("likes") or 0)
            tag = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]                       # one address per comment, so two comments under one video are two items
            out.append(_item(f"{item.get('platform', 'video')} comment", text, f"{item.get('url', '')}#c-{tag}", item.get("title", ""), {"likes": likes}, meta.get("posted", ""),
                             str(item.get("title") or "")[:80], item.get("topic") or "", "comments"))
    return out


def _row_item(row, source, topic, mode="apify"):
    """A collected-style row (a TikTok or Instagram post, a Reddit post) as a voice item: the caption or title and the engagement it got."""
    text = " ".join(str(row.get("text") or row.get("title") or "").split())
    if len(text) < 8:
        return None
    eng = {k: row[k] for k in ("likes", "comments", "shares", "views") if isinstance(row.get(k), (int, float)) and not isinstance(row.get(k), bool)}
    return _item(source, row.get("title") or text[:120], row.get("url", ""), text, eng, row.get("posted_at", ""), row.get("details", ""), topic, mode)


class ApifyVoice:
    """An extra lane: Apify's ready-made TikTok and Instagram scrapers, asked about one topic. It is OFF unless the owner asks for it in a run (voice --apify / the workflow's apify box), needs APIFY_TOKEN, and spends only
    within its OWN monthly allowance (voice_apify_items, default 40) and never beyond the monthly Apify allowance the Collect step already uses (collectors.apify.monthly_items): both counters are charged for what was read,
    and each Apify call carries a cost ceiling (default 0.25 dollars). `apify` is the platform's Apify provider; `ledger` is research.Ledger; `shared_cap` is the monthly items Collect may use."""
    ACTORS = {"tiktok": "clockworks/tiktok-scraper", "instagram": "apify/instagram-hashtag-scraper"}

    def __init__(self, apify, ledger, *, per_run=15, monthly=40, shared_cap=100, max_cost_usd=0.25, normalize=None):
        self.apify, self.ledger, self.per_run, self.monthly, self.shared_cap, self.max_cost = apify, ledger, per_run, monthly, shared_cap, max_cost_usd
        self.normalize = normalize
        self.spent_this_run = 0

    def room(self):
        return max(0, min(self.per_run - self.spent_this_run, self.monthly - self.ledger.month_used("voice_apify_items"), self.shared_cap - self.ledger.month_used("apify_items")))

    def run(self, topic):
        """(items, notes). Each source asks for at most half the room left, so TikTok and Instagram share it. Raises nothing: a refusal or a missing allowance is a note."""
        items, notes = [], []
        tag = re.sub(r"[^a-z0-9]", "", topic.lower())
        for source in ("tiktok", "instagram"):
            n = self.room() // (2 if source == "tiktok" else 1)
            if n < 1:
                notes.append(f"apify {source}: no allowance left (this run {self.spent_this_run}/{self.per_run}, voice this month {self.ledger.month_used('voice_apify_items'):g}/{self.monthly}, Apify this month {self.ledger.month_used('apify_items'):g}/{self.shared_cap})")
                continue
            payload = {"searchQueries": [topic], "resultsPerPage": n} if source == "tiktok" else {"hashtags": [tag], "resultsType": "posts", "resultsLimit": n}
            if source == "instagram" and not tag:
                continue
            try:
                raw = self.apify.run(self.ACTORS[source], payload, max_items=n, max_cost_usd=self.max_cost)
            except Exception as exc:                                              # noqa: BLE001 - a provider's refusal, quota or outage is reported, never raised
                notes.append(f"apify {source}: {exc.__class__.__name__}: {scrub(str(exc), 200)}")
                continue
            self.ledger.charge("apify_items", len(raw))
            self.ledger.charge("voice_apify_items", len(raw))
            self.spent_this_run += len(raw)
            rows, bad = self.normalize(source, raw) if self.normalize else ([], len(raw))
            got = [i for i in (_row_item(r, f"apify {source}", topic) for r in rows) if i]
            items += got
            notes.append(f"apify {source}: {len(raw)} item(s) read, {len(got)} usable" + (f", {bad} unreadable" if bad else ""))
        return items, notes


def reddit_items(rows, limit=300):
    """Reddit posts the platform already collected (Actions > Observatory > reddit, by the official API once approved or by the stored dummy-account session through Agent-Reach), as voice items with their score and comments."""
    out = []
    for r in (rows.values() if isinstance(rows, dict) else rows or []):
        if isinstance(r, dict) and r.get("source") == "reddit" and (r.get("title") or r.get("text")):
            item = _row_item(dict(r, text=" ".join(x for x in (str(r.get("title") or ""), str(r.get("text") or "")) if x)), "reddit", r.get("details", ""), "collected")
            if item:
                item["engagement"] = {"score": r.get("likes") or 0, "num_comments": r.get("comments") or 0}
                item["via"] = r.get("found_via") or next((q for q in (str(r.get("queries") or "").split(" | ")) if q in ("api", "agent_reach")), "")          # collected rows keep the route among their 'queries'
                item["weight"] = _engagement_total(item["engagement"])
                out.append(item)
    return sorted(out, key=lambda i: -i["weight"])[:limit]
