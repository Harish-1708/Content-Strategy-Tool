"""The public research corpus: YouTube videos through the official Data API, ranked by how far each beat its own channel, plus one search across
everything the tool has collected (ads, posts, searches, videos, competitor pages and blog posts).

The outlier method and the 21 YouTube hook formulas come from the YouTube agent skill (https://github.com/Jakeschincariol/youtube-agent-skill, MIT licence,
copyright 2026 Jake Schincariol; the formula data file keeps its licence beside it). Raw view counts rank channel size, not ideas, so every video is scored
as a multiple of its own channel's median, which needs at least four videos on the channel. A formula is a judgement about the *words of the title*, not a
claim about why a video worked.

Honest limits: the Data API gives public titles, descriptions and counts. It does not give the captions of other people's videos, so the 'spoken hook'
is not available and the title stands in for it, and every record says so. YouTube's downloader route is switched off because YouTube refuses GitHub's servers.
"""
import json
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import contract as K
from .. import evidence as E
from ..safety import clean_text

HOOKS_PATH = Path(__file__).parent / "data" / "youtube_hooks.json"
API = "https://www.googleapis.com/youtube/v3"
SOURCE, ROUTE = "youtube_public", "data_api"
_KEY_REASONS = ("keyinvalid", "accessnotconfigured", "ipreferrerblocked", "apikeyinvalid", "forbidden")
COST = {"channels": 1, "playlistItems": 1, "videos": 1, "search": 100}
SPOKEN_HOOK_NOTE = "captions of other people's videos are not available through the YouTube Data API, so the title is used as the hook"


# ---- formulas and outliers --------------------------------------------------------------------------------------------------------------------

def load_formulas(path=HOOKS_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out, seen = [], set()
    for f in data["hooks"]:
        if f["id"] in seen:
            raise ValueError(f"formula {f['id']} is listed twice")
        seen.add(f["id"])
        out.append({**f, "_rx": [re.compile(p, re.I) for p in f["match"]]})
    return out


def classify(title, formulas):
    """(id, name) of the formula whose patterns match the title most, else ('', 'Unclassified'). Earlier formulas win ties."""
    best, best_n = None, 0
    for f in formulas:
        n = sum(1 for rx in f["_rx"] if rx.search(title or ""))
        if n > best_n:
            best, best_n = f, n
    return (best["id"], best["name"]) if best else ("", "Unclassified")


def outliers(videos, formulas, *, min_videos=4, min_multiple=2.0):
    """Rank videos by views over their own channel's median. Channels with fewer than min_videos readable counts are listed, not guessed at."""
    by = {}
    for v in videos:
        by.setdefault(v.get("channel_id") or v.get("channel_title") or "?", []).append(v)
    rows, thin, channels = [], [], {}
    for cid, vids in by.items():
        views = [v["views"] for v in vids if isinstance(v.get("views"), (int, float)) and not isinstance(v.get("views"), bool)]
        title = vids[0].get("channel_title") or cid
        if len(views) < min_videos:
            thin.append({"channel": title, "videos": len(views)})
            continue
        med = statistics.median(views)
        channels[cid] = {"title": title, "videos": len(views), "median": med}
        if not med:
            continue
        for v in vids:
            if not isinstance(v.get("views"), (int, float)):
                continue
            mult = v["views"] / med
            if mult >= min_multiple:
                fid, fname = classify(v.get("title", ""), formulas)
                rows.append({"video_id": v["id"], "channel": title, "title": v.get("title", ""), "views": v["views"], "median": int(med), "multiple": round(mult, 2),
                             "formula_id": fid, "formula": fname, "url": v.get("url", ""), "shorts_class": v.get("shorts_class", ""), "channel_videos": len(views)})
    rows.sort(key=lambda r: (-r["multiple"], r["video_id"]))
    counts = {}
    for r in rows:
        counts[r["formula"]] = counts.get(r["formula"], 0) + 1
    return {"outliers": rows, "thin_channels": thin, "channels": channels, "formulas_among_outliers": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}


def _when(text):
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def velocity(videos, now, *, window_days=7, min_hours=12, min_multiple=2.0):
    """How fast each video is gaining views, against how fast the same channel's videos usually gain them: the trend a single count cannot show.

    A video's speed is the views it gained per day between its first and latest stored reading inside the window (needs at least `min_hours` between them). A video read only once
    has no history, so a recent one (published inside the window) is estimated as views divided by its age and says so; an older one with a single reading is left out, since its
    lifetime average says nothing about now. The baseline is the channel's median speed over three or more videos; the multiple is speed over that baseline.
    Views that went down between readings (YouTube corrects counts) give no speed."""
    end = _when(now)
    rows, by_channel = [], {}
    for v in videos:
        pts = sorted((p for p in v.get("history", []) if isinstance(p.get("views"), int) and _when(p.get("at"))), key=lambda p: p["at"])
        recent = [p for p in pts if (end - _when(p["at"])).total_seconds() <= window_days * 86400]
        speed, basis = None, ""
        if len(recent) >= 2:
            hours = (_when(recent[-1]["at"]) - _when(recent[0]["at"])).total_seconds() / 3600
            gain = recent[-1]["views"] - recent[0]["views"]
            if hours >= min_hours and gain >= 0:
                speed, basis = gain / (hours / 24), f"{len(recent)} readings over {hours / 24:.1f} days"
        if speed is None and len(recent) < 2 and isinstance(v.get("views"), int):
            published = _when(v.get("published_at"))
            if published:
                age = (end - published).total_seconds() / 86400
                if 0 < age <= window_days:
                    speed, basis = v["views"] / max(age, 0.5), f"estimated from age ({age:.1f} days, one reading)"
        if speed is not None:
            rec = {"video_id": v["id"], "channel_id": v.get("channel_id") or v.get("channel_title") or "?", "channel": v.get("channel_title", ""), "title": v.get("title", ""),
                   "views": v.get("views"), "per_day": round(speed, 1), "basis": basis, "url": v.get("url", ""), "shorts_class": v.get("shorts_class", "")}
            rows.append(rec)
            by_channel.setdefault(rec["channel_id"], []).append(rec)
    out, thin = [], []
    for cid, recs in by_channel.items():
        if len(recs) < 3:
            thin.append({"channel": recs[0]["channel"] or cid, "videos": len(recs)})
            continue
        baseline = statistics.median(r["per_day"] for r in recs)
        for r in recs:
            r["baseline_per_day"] = round(baseline, 1)
            r["multiple"] = round(r["per_day"] / baseline, 2) if baseline > 0 else None
            if r["multiple"] is not None and r["multiple"] >= min_multiple:
                out.append(r)
    out.sort(key=lambda r: (-r["multiple"], r["video_id"]))
    return {"trending": out, "thin_channels": thin, "evaluated": len(rows)}


def trending_report_text(result, brand_name, now):
    lines = [f"YouTube videos gaining views fastest for {brand_name} (prepared {now[:10]})", "",
             "Speed is views gained per day, set against the usual speed of the same channel's videos. A video read only once is an estimate from its age and is marked as one.", ""]
    if not result["trending"]:
        lines.append("Nothing is gaining faster than its channel's usual pace. Speed needs the same videos read on more than one day (collect again later), "
                     "or new videos on channels with at least three of them.")
    for r in result["trending"][:25]:
        lines.append(f"{r['multiple']:5.2f}x  {r['per_day']:>9,.0f}/day vs {r['baseline_per_day']:>8,.0f} usual  {r['channel'][:22]:<22} {r['title'][:55]}  ({r['basis']})")
    if result["thin_channels"]:
        lines += ["", "Left out (fewer than three videos with a speed, so there is no usual pace): " + ", ".join(f"{t['channel']} ({t['videos']})" for t in result["thin_channels"][:8])]
    return "\n".join(lines) + "\n"


def duration_seconds(iso):
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", str(iso or ""))
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def shorts_class(seconds):
    """'short' up to a minute, 'possible_short' up to three (YouTube allows Shorts that long since 2024), else 'long'. The API does not say which videos are Shorts."""
    if seconds is None:
        return ""
    return "short" if seconds <= 60 else "possible_short" if seconds <= 180 else "long"


# ---- the API, with its own daily budget -------------------------------------------------------------------------------------------------------

def pacific_day(now):
    """The YouTube quota resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        t = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:
        t = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - timedelta(hours=8)
    return t.strftime("%Y-%m-%d")


class Quota:
    def __init__(self, data_dir, cap, now):
        self.path = Path(data_dir) / "_observatory" / "youtube_quota.json"
        self.cap, self.day = cap, pacific_day(now)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        self.used = data.get("used", 0) if data.get("day") == self.day else 0

    def spend(self, units):
        if self.used + units > self.cap:
            failure = K.Failure(K.BAD_INPUT, f"the tool's own daily limit for YouTube ({self.cap} units, kept well under the free 10,000) would be crossed; this waits for tomorrow")
            failure.budget = True                   # our own ceiling, not YouTube's doing, so it must not rest the route
            raise failure
        self.used += units

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": self.day, "used": self.used}), encoding="utf-8")
        tmp.replace(self.path)


def default_get(url, params, timeout):
    import requests
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except requests.exceptions.Timeout:
        raise TimeoutError("timeout") from None
    except requests.exceptions.RequestException as exc:
        raise OSError(exc.__class__.__name__) from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body


class YouTubeAPI:
    def __init__(self, key, quota, get=default_get):
        self.key, self.quota, self.get = key, quota, get

    def _call(self, kind, endpoint, **params):
        self.quota.spend(COST[kind])
        try:
            status, body = self.get(f"{API}/{endpoint}", {**params, "key": self.key}, 30)
        except TimeoutError:
            raise K.Failure(K.TIMEOUT, "YouTube's Data API did not answer in time") from None
        except OSError as exc:
            raise K.Failure(K.UPSTREAM_BROKEN, f"network error ({exc})") from None
        error = (body or {}).get("error") or {} if isinstance(body, dict) else {}
        reason = ((error.get("errors") or [{}])[0].get("reason") or "").lower()
        if status == 200 and isinstance(body, dict):
            return body
        if status == 403 and reason in ("quotaexceeded", "ratelimitexceeded", "dailylimitexceeded", "userratelimitexceeded"):
            raise K.Failure(K.RATE_LIMITED, "YouTube's free daily quota is used up; it renews at midnight Pacific time")
        if status in (400, 403) and (reason in _KEY_REASONS or "api key" in str(error.get("message", "")).lower()):
            raise K.Failure(K.NEED_SETUP, "YouTube rejected the API key, or the YouTube Data API is not switched on for it")
        if status == 404:
            raise K.Failure(K.NOT_FOUND, "YouTube has no such channel or video")
        if status >= 500:
            raise K.Failure(K.UPSTREAM_BROKEN, f"YouTube answered {status}")
        raise K.Failure(K.UPSTREAM_BROKEN, f"YouTube answered {status}: {' '.join(str(error.get('message', '')).split())[:120]}")

    def channel(self, ref):
        ref = ref.strip()
        params = {"part": "snippet,contentDetails,statistics"}
        if ref.startswith("UC") and len(ref) >= 20:
            params["id"] = ref
        else:
            params["forHandle"] = ref if ref.startswith("@") else "@" + ref
        body = self._call("channels", "channels", **params)
        if not body.get("items"):
            raise K.Failure(K.NOT_FOUND, f"no YouTube channel was found for {ref}")
        item = body["items"][0]
        stats = item.get("statistics") or {}
        return {"id": item["id"], "title": clean_text(str((item.get("snippet") or {}).get("title", ""))).strip()[:120], "handle": (item.get("snippet") or {}).get("customUrl", ""),
                "subscribers": int(stats["subscriberCount"]) if str(stats.get("subscriberCount", "")).isdigit() and not stats.get("hiddenSubscriberCount") else None,
                "video_count": int(stats["videoCount"]) if str(stats.get("videoCount", "")).isdigit() else None,
                "uploads": ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", "")}

    def upload_ids(self, playlist, limit):
        ids, token = [], None
        while len(ids) < limit:
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": min(50, limit - len(ids))}
            if token:
                params["pageToken"] = token
            body = self._call("playlistItems", "playlistItems", **params)
            ids += [i["contentDetails"]["videoId"] for i in body.get("items", []) if (i.get("contentDetails") or {}).get("videoId")]
            token = body.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    def videos(self, ids):
        out = []
        for start in range(0, len(ids), 50):
            body = self._call("videos", "videos", part="snippet,contentDetails,statistics", id=",".join(ids[start:start + 50]))
            out.extend(body.get("items", []))
        return out

    def search(self, query, kind="video", limit=10):
        body = self._call("search", "search", part="snippet", q=query, type=kind, maxResults=min(limit, 25), order="viewCount" if kind == "video" else "relevance")
        return body.get("items", [])


def _count(stats, key):
    v = (stats or {}).get(key)
    return int(v) if str(v).isdigit() else None


def video_record(item, now):
    sn, st, cd = item.get("snippet") or {}, item.get("statistics") or {}, item.get("contentDetails") or {}
    secs = duration_seconds(cd.get("duration"))
    desc = clean_text(str(sn.get("description", ""))).strip()
    thumbs = sn.get("thumbnails") or {}
    thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    vid = item["id"]
    return {"id": vid, "channel_id": sn.get("channelId", ""), "channel_title": clean_text(str(sn.get("channelTitle", ""))).strip()[:120], "title": clean_text(str(sn.get("title", ""))).strip()[:200],
            "published_at": sn.get("publishedAt", ""), "duration_s": secs, "shorts_class": shorts_class(secs), "views": _count(st, "viewCount"), "likes": _count(st, "likeCount"),
            "comments": _count(st, "commentCount"), "tags": [clean_text(str(t)).strip() for t in (sn.get("tags") or [])[:10]], "has_captions": str(cd.get("caption", "")).lower() == "true",
            "description_line": " ".join(desc.splitlines()[0].split())[:200] if desc else "", "thumbnail": thumb, "url": f"https://www.youtube.com/watch?v={vid}",
            "spoken_hook": None, "spoken_hook_note": SPOKEN_HOOK_NOTE, "captured_at": now}


# ---- the stored corpus ------------------------------------------------------------------------------------------------------------------------

def corpus_path(store, slug):
    return store.dir(slug) / "corpus" / "youtube.json"


def load_corpus(store, slug):
    path = corpus_path(store, slug)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        data = {}
    return {"version": 1, "channels": data.get("channels", {}), "videos": data.get("videos", {})}


def save_corpus(store, slug, corpus):
    path = corpus_path(store, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def merge_video(corpus, rec, now, history_cap=12):
    """Add or refresh a video. Its view counts over time are kept (a point when it changed or a day passed), which is what trend velocity later needs."""
    old = corpus["videos"].get(rec["id"])
    point = {"at": now, "views": rec["views"], "likes": rec["likes"], "comments": rec["comments"]}
    if old is None:
        corpus["videos"][rec["id"]] = {**rec, "first_seen": now, "last_seen": now, "history": [point]}
        return "new"
    hist = old.get("history", [])
    last = hist[-1] if hist else None
    if last is None or last["views"] != point["views"] or (datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(last["at"], "%Y-%m-%dT%H:%M:%SZ")).days >= 1:
        hist.append(point)
    corpus["videos"][rec["id"]] = {**old, **rec, "first_seen": old.get("first_seen", now), "last_seen": now, "history": hist[-history_cap:]}
    return "updated"


def collect_channels(api, corpus, refs, cfg, now, *, log=None):
    """Read each channel's recent uploads. Returns {'channels': n, 'videos': n, 'errors': [...]}; a channel that fails does not stop the others (unless the key or quota does)."""
    got = {"channels": 0, "videos": 0, "new": 0, "errors": []}
    for ref in refs:
        try:
            ch = api.channel(ref)
            corpus["channels"][ch["id"]] = {**corpus["channels"].get(ch["id"], {"added_at": now}), **ch, "last_checked": now}
            ids = api.upload_ids(ch["uploads"], cfg["videos_per_channel"]) if ch["uploads"] else []
            for item in api.videos(ids):
                if merge_video(corpus, video_record(item, now), now) == "new":
                    got["new"] += 1
                got["videos"] += 1
            got["channels"] += 1
        except K.Failure as exc:
            if exc.code in (K.NEED_SETUP, K.RATE_LIMITED) or getattr(exc, "budget", False):
                got["errors"].append({"channel": ref, "code": exc.code, "message": str(exc)})
                raise _Stopped(got, exc) from None
            got["errors"].append({"channel": ref, "code": exc.code, "message": str(exc)})
    return got


class _Stopped(Exception):
    def __init__(self, partial, failure):
        super().__init__(str(failure))
        self.partial, self.failure = partial, failure


def discover_channels(api, queries, limit):
    """Channel ids found by searching videos (a search costs 100 units, so the number is capped). Returns [(channel_id, channel_title, query)]."""
    found, seen = [], set()
    for q in queries[:limit]:
        for item in api.search(q, "video", 10):
            sn = item.get("snippet") or {}
            cid = sn.get("channelId")
            if cid and cid not in seen:
                seen.add(cid)
                found.append((cid, clean_text(str(sn.get("channelTitle", ""))).strip(), q))
    return found


def channels_from_rows(rows, queries_by_row=None):
    """[(channel reference, channel title, query)] from search rows that name a channel (id or @handle), once each. The reference is what the Data API accepts: a UC... id, or an @handle."""
    found, seen = [], set()
    for query, row in rows:
        cid = str(row.get("channel_id") or "")
        handle = str(row.get("uploader_id") or "")
        if not cid.startswith("UC"):
            m = re.search(r"/channel/(UC[A-Za-z0-9_\-]{22})|/(@[A-Za-z0-9_.\-]{2,60})", str(row.get("channel_url") or ""))
            cid = (m.group(1) or "") if m else ""
            handle = handle or ((m.group(2) or "") if m else "")
        ref = cid if cid.startswith("UC") else handle
        if ref and ref.lower() not in seen:
            seen.add(ref.lower())
            found.append((ref, clean_text(str(row.get("channel", ""))).strip(), query))
    return found


# ---- evidence ---------------------------------------------------------------------------------------------------------------------------------

def evidence_for_video(rec, registry, now, multiple=None, formulas=None, trend=None):
    route = registry.route(SOURCE, ROUTE)
    metrics = []
    for field, name in (("views", "views"), ("likes", "likes"), ("comments", "comments")):
        if isinstance(rec.get(field), int):
            metrics.append({"name": name, "value": rec[field], "unit": "count", "denominator": None, "window": "cumulative at capture", "captured_at": rec["captured_at"],
                            "platform": "youtube", "source": SOURCE, "kind": "measured"})
    if multiple:
        metrics.append({"name": "outlier_multiple", "value": multiple["multiple"], "unit": "ratio", "denominator": multiple["median"], "window": "views against the channel's own median",
                        "captured_at": rec["captured_at"], "platform": "youtube", "source": SOURCE, "kind": "proxy"})
    if trend:
        metrics.append({"name": "views_per_day", "value": trend["per_day"], "unit": "count_per_day", "denominator": None, "window": trend["basis"], "captured_at": rec["captured_at"],
                        "platform": "youtube", "source": SOURCE, "kind": "proxy"})
        if trend.get("multiple") is not None:
            metrics.append({"name": "trend_multiple", "value": trend["multiple"], "unit": "ratio", "denominator": trend["baseline_per_day"], "window": "speed against the channel's usual speed",
                            "captured_at": rec["captured_at"], "platform": "youtube", "source": SOURCE, "kind": "proxy"})
    missing = ["transcript"] + [f for f in ("views", "likes", "comments") if rec.get(f) is None]
    observed = [f for f in ("title", "published_at", "duration_s", "views", "likes", "comments", "thumbnail") if rec.get(f) not in (None, "")]
    hist = rec.get("history") or [{"at": rec["captured_at"]}]
    out = E._assemble(source=SOURCE, platform="youtube", item_id=rec["id"], key_url=rec["url"], url=rec["url"], title=rec["title"], text=rec.get("description_line", ""),
                      creator=rec.get("channel_title", ""), handle_context=rec.get("channel_id", ""), route=ROUTE, tier=route.tier, access_type=route.access_type,
                      captured=rec["captured_at"], first_seen=rec.get("first_seen", rec["captured_at"]), last_seen=rec.get("last_seen", rec["captured_at"]), times_seen=len(hist),
                      metrics=metrics, media_url=rec.get("thumbnail", ""), observed=observed, missing=missing, completeness="partial",                      # the transcript is never available, so a video is never fully observed
                      limitations=[SPOKEN_HOOK_NOTE], health_status="", found_via="channel uploads", registry=registry, now=now)
    out["content"]["format"] = rec.get("shorts_class", "")
    if formulas is not None:
        fid, _ = classify(rec["title"], formulas)
        out["analysis"].update(formula=fid, confidence="low" if fid else None)          # pattern match on the title only
    return out


# ---- one search across everything collected ---------------------------------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


def _terms(text):
    return [w for w in _WORD.findall(str(text or "").lower()) if len(w) > 1]


def build_documents(store, snaps, slug, registry, now, site_ids=()):
    """Every readable thing in the corpus as {id, source, kind, title, text, url, when}: collected posts and ads, searches, videos, competitor pages and posts."""
    docs = []
    for rec in E.build_all(store, slug, registry, now):
        docs.append({"id": rec["evidence_id"], "source": rec["origin"]["source"], "kind": rec["content"].get("format") or rec["origin"]["access_type"], "title": rec["content"]["title"],
                     "text": rec["content"]["text"], "url": rec["origin"]["url"], "when": rec["origin"]["captured_at"]})
    for vid in load_corpus(store, slug)["videos"].values():
        docs.append({"id": "yt-" + vid["id"], "source": SOURCE, "kind": vid.get("shorts_class", ""), "title": vid["title"], "text": f"{vid.get('channel_title', '')} {vid.get('description_line', '')}",
                     "url": vid["url"], "when": vid["captured_at"]})
    reads = store.dir(slug, create=False) / "corpus" / "reader.jsonl"
    if reads.is_file():                                                    # public pages read through the reader on request
        for line in reads.read_text(encoding="utf-8").splitlines():
            try:
                page = json.loads(line)
            except ValueError:
                continue
            docs.append({"id": "read:" + page.get("url", ""), "source": "competitor_pages", "kind": "page", "title": page.get("title", ""), "text": page.get("text", ""),
                         "url": page.get("url", ""), "when": page.get("captured_at", "")})
    about = store.dir(slug, create=False) / "corpus" / "channel_pages.jsonl"
    if about.is_file():                                                    # YouTube channel About pages and the accounts they link to
        for line in about.read_text(encoding="utf-8").splitlines():
            try:
                page = json.loads(line)
            except ValueError:
                continue
            linked = " ".join(f"{k} {v}" for k, v in sorted((page.get("social") or {}).items()))
            docs.append({"id": "channel:" + page.get("url", ""), "source": SOURCE, "kind": "channel_page", "title": page.get("title", ""), "text": (page.get("text", "") + " " + linked).strip(),
                         "url": page.get("url", ""), "when": page.get("captured_at", "")})
    for sid in site_ids:
        for kind in snaps.kinds(sid):
            _, snap = snaps.latest(sid, kind)
            if not snap:
                continue
            if snap.get("kind") == "page":
                docs.append({"id": f"{sid}:{kind}", "source": "competitor_pages", "kind": "page", "title": snap.get("title", ""), "text": snap.get("text", ""), "url": snap.get("url", ""),
                             "when": snap.get("captured_at", "")})
            elif snap.get("kind") == "feed":
                for item in snap.get("items", []):
                    docs.append({"id": f"{sid}:{kind}:{item['id']}", "source": "competitor_pages", "kind": "post", "title": item["title"], "text": item.get("summary", ""), "url": item.get("link", ""),
                                 "when": snap.get("captured_at", "")})
    return docs


def search(docs, query, limit=10, k1=1.5, b=0.75, snippet_chars=160):
    """BM25 over title (counted three times) and text. Returns [{...doc, score, snippet}] best first; no match, no result."""
    q = _terms(query)
    if not q or not docs:
        return []
    toks = [_terms(d["title"]) * 3 + _terms(d["text"]) for d in docs]
    avg = sum(len(t) for t in toks) / len(toks) or 1
    df = {w: sum(1 for t in toks if w in set(t)) for w in set(q)}
    out = []
    for d, t in zip(docs, toks):
        score = 0.0
        for w in set(q):
            tf = t.count(w)
            if tf:
                idf = math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5))
                score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(t) / avg))
        if score > 0:
            body = " ".join(str(d["text"] or d["title"]).split())
            pos = min((body.lower().find(w) for w in q if w in body.lower()), default=0)
            out.append({**d, "score": round(score, 3), "snippet": body[max(0, pos - 40):pos + snippet_chars]})
    out.sort(key=lambda r: (-r["score"], r["id"]))
    return out[:limit]


def report_text(result, brand_name, now):
    lines = [f"YouTube outliers for {brand_name} (prepared {now[:10]})", "",
             "Each video is scored as a multiple of its own channel's median views. The formula named is a match on the title's wording; it does not say why the video worked.", ""]
    if not result["outliers"]:
        lines.append("Nothing cleared the threshold. Collect more videos per channel, or lower the threshold.")
    for r in result["outliers"][:25]:
        lines.append(f"{r['multiple']:5.2f}x  {r['views']:>10,} views vs {r['median']:>9,} median  {r['channel'][:24]:<24} {r['title'][:60]}  [{r['formula']}]")
    if result["formulas_among_outliers"]:
        lines += ["", "Formulas among the outliers: " + ", ".join(f"{n} x {f}" for f, n in result["formulas_among_outliers"].items())]
    if result["thin_channels"]:
        lines += ["", "Left out (fewer than four readable videos, so a median would mean nothing): " + ", ".join(f"{t['channel']} ({t['videos']})" for t in result["thin_channels"][:8])]
    lines += ["", "Spoken hooks are not available: " + SPOKEN_HOOK_NOTE + "."]
    return "\n".join(lines) + "\n"
