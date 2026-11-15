"""What yt-dlp knows about a video without downloading it: views, likes, comments, length, date, the sound used, hashtags, who posted, the posting account's followers, captions and the top comments.

The platform already uses yt-dlp to save a video file. This module uses the same program for the facts around the file, for every site it supports (TikTok, Instagram, Facebook, YouTube and
more), so that every collected video can be judged by more than its likes:

  outlier   views divided by the followers of the account that posted it: 8x means eight times what that account's own audience would normally bring. A small account with a large reach
            is the clearest sign that the idea, not the account, did the work.
  sound     the track and artist (the same sound across many videos is a trend to brief on).
  captions  which languages exist (the words spoken in the first seconds are read from them where they do).
  comments  what viewers said, in their own words (audience language for briefs).

It reads public data only, never works around a bot check (a refusal is reported and left alone), and every number comes from the program's own answer: nothing is estimated.
yt-dlp is the connection `yt_dlp` (contentintel/connections.py); the owner can switch it off and the platform then simply has no video facts.
"""
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from . import contract as K
from .safety import scrub

_BOT = re.compile(r"sign in to confirm|not a bot|captcha|unusual traffic|verify to continue|login required|log in to|rate-limit reached|requires login|empty media response", re.I)
_GONE = re.compile(r"video unavailable|private video|this video is not available|has been removed|no longer available|members-only|does not exist|unsupported url|not found|404", re.I)
_LIMIT = re.compile(r"http error 429|too many requests|rate.?limit", re.I)
_HASHTAG = re.compile(r"#([A-Za-z0-9_]{2,40})")
TOP_COMMENTS = 8


def classify(stderr, returncode):
    text = scrub(str(stderr), 300)
    if _LIMIT.search(text):
        return K.Failure(K.RATE_LIMITED, "the site asked us to slow down", retry_after=3600)
    if _BOT.search(text):
        return K.Failure(K.PLATFORM_BLOCKED, "the site showed a bot check or wants a login for this video; it was not worked around")
    if _GONE.search(text):
        return K.Failure(K.NOT_FOUND, "the site says this video is not available")
    return K.Failure(K.UPSTREAM_BROKEN, f"yt-dlp failed ({returncode}): {text[:200]}")


def _date(info):
    raw = str(info.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    stamp = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")
    return ""


def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def outlier(views, followers):
    """views / followers, to one decimal, or None when either is missing or followers is zero (nothing is estimated)."""
    v, f = _int(views), _int(followers)
    return round(v / f, 1) if v is not None and f else None


def normalise(info):
    """The facts of one yt-dlp answer (the dict --dump-single-json prints), in the platform's own shape."""
    if not isinstance(info, dict):
        raise K.Failure(K.UPSTREAM_BROKEN, "yt-dlp answered with something that is not a video record")
    tags = [str(t).lstrip("#") for t in (info.get("tags") or []) if str(t).strip()]
    tags += [h for h in _HASHTAG.findall(str(info.get("description") or info.get("title") or ""))]
    artist = info.get("artist") or (", ".join(map(str, info["artists"])) if isinstance(info.get("artists"), list) else "")
    music = " - ".join(x for x in (str(artist or ""), str(info.get("track") or "")) if x)
    comments = []
    for c in sorted((info.get("comments") or []), key=lambda c: -(_int(c.get("like_count")) or 0))[:TOP_COMMENTS]:
        if isinstance(c, dict) and str(c.get("text") or "").strip():
            comments.append({"text": " ".join(str(c["text"]).split())[:400], "likes": _int(c.get("like_count")) or 0, "author": str(c.get("author") or "")[:60]})
    subs = sorted(set(list((info.get("subtitles") or {}).keys())) | {f"{k} (automatic)" for k in (info.get("automatic_captions") or {}).keys() if str(k).startswith("en")})
    followers = _int(info.get("channel_follower_count"))
    views = _int(info.get("view_count"))
    out = {"url": str(info.get("webpage_url") or ""), "site": str(info.get("extractor_key") or info.get("extractor") or ""), "title": " ".join(str(info.get("title") or "").split())[:200],
           "uploader": str(info.get("uploader") or info.get("channel") or "")[:80], "uploader_id": str(info.get("uploader_id") or "")[:80], "posted": _date(info),
           "views": views, "likes": _int(info.get("like_count")), "comments": _int(info.get("comment_count")), "reposts": _int(info.get("repost_count")),
           "duration_s": info.get("duration") if isinstance(info.get("duration"), (int, float)) and not isinstance(info.get("duration"), bool) else None,
           "music": music[:120], "hashtags": list(dict.fromkeys(t.lower() for t in tags))[:20], "followers": followers, "outlier": outlier(views, followers),
           "captions": subs[:8], "top_comments": comments}
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def fetch(url, *, run=subprocess.run, python=None, timeout=90, cookies=None, comments=False, environ=None):
    """The facts for one video address. Raises K.Failure (never anything else) when it cannot be read."""
    args = [python or sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist", "--skip-download", "--dump-single-json", "--socket-timeout", "20", "--retries", "1"]
    if comments:
        args += ["--write-comments", "--extractor-args", f"youtube:max_comments={TOP_COMMENTS * 4},all,all,0;tiktok:comment_count={TOP_COMMENTS * 4}"]
    if cookies:
        args += ["--cookies", str(cookies)]
    try:
        done = run(args + [url], capture_output=True, text=True, timeout=timeout, env=environ)
    except subprocess.TimeoutExpired:
        raise K.Failure(K.TIMEOUT, f"yt-dlp did not finish in {timeout} seconds") from None
    except OSError as exc:
        raise K.Failure(K.NEED_SETUP, f"yt-dlp could not start ({exc.__class__.__name__})") from None
    if done.returncode != 0:
        raise classify(done.stderr or done.stdout, done.returncode)
    lines = [ln for ln in (done.stdout or "").splitlines() if ln.strip().startswith("{")]
    if not lines:
        raise K.Failure(K.UPSTREAM_BROKEN, "yt-dlp returned nothing for this video")
    try:
        return normalise(json.loads(lines[-1]))
    except ValueError:
        raise K.Failure(K.UPSTREAM_BROKEN, "yt-dlp's answer could not be read") from None


VIDEO_HOSTS = ("tiktok.com", "instagram.com", "youtube.com", "youtu.be", "facebook.com", "fb.watch")


def wants_facts(item):
    """True for a video-like item with a public address that has no facts yet and has not used up its tries."""
    url = str(item.get("url") or "").lower()
    if item.get("video_meta") is not None or int(item.get("meta_tries") or 0) >= 2:
        return False
    if item.get("platform") == "meta_ads":
        return False                                    # an Ad Library card is not a public video page
    return any(h in url for h in VIDEO_HOSTS) and any(p in url for p in ("/video/", "/reel", "/p/", "/watch", "youtu.be", "/shorts/", "/tv/", "/videos/"))


def followers_index(data_dir="data"):
    """{(platform, handle): followers} from the latest public profile reading of every followed account (the Social Profiles the monitor keeps)."""
    out = {}
    try:
        from .observatory.snapshots import Snapshots
        snaps = Snapshots(data_dir)
        for site in snaps.sites():
            for platform in ("instagram", "tiktok", "youtube", "facebook", "x", "linkedin"):
                try:
                    _, snap = snaps.latest(site, f"profile-{platform}", complete_only=True)
                except Exception:                                              # noqa: BLE001 - an unreadable snapshot is simply not used
                    continue
                m = (snap or {}).get("metrics") or {}
                n = _int(m.get("followers"))
                if n and snap.get("handle"):
                    out[(platform, str(snap["handle"]).lstrip("@").lower())] = n
    except Exception:                                                          # noqa: BLE001
        return out
    return out


def enrich_items(bank_items, ids, *, run=subprocess.run, python=None, followers=None, cookies=None, comments=True, limit=40, now="", timeout=90):
    """Add the facts to the given Idea Bank items (a dict id -> item). Returns (number enriched, notes). A refusal stops the loop for that site (never worked around); nothing is estimated."""
    done, notes, blocked = 0, [], set()
    followers = followers or {}
    for idea_id in ids:
        item = bank_items[idea_id]
        site = item.get("platform", "")
        if not wants_facts(item) or site in blocked:
            continue
        if done >= limit:
            notes.append(f"{limit} video(s) were read this run; the rest wait for the next run")
            break
        try:
            facts = fetch(item["url"], run=run, python=python, cookies=cookies, comments=comments, timeout=timeout)
        except K.Failure as exc:
            item["meta_tries"] = int(item.get("meta_tries") or 0) + (0 if exc.code in (K.PLATFORM_BLOCKED, K.RATE_LIMITED, K.NEED_SETUP, K.TIMEOUT) else 1)
            if exc.code in (K.PLATFORM_BLOCKED, K.RATE_LIMITED):
                blocked.add(site)
                notes.append(f"{site}: {exc.message or exc.code} (no more {site} videos are asked about in this run)")
            elif exc.code == K.NEED_SETUP:
                notes.append("video facts skipped: yt-dlp is not available here")
                break
            continue
        handle = str(item.get("handle") or facts.get("uploader_id") or "").lstrip("@").lower()
        if facts.get("followers") is None and (site, handle) in followers:
            facts["followers"] = followers[(site, handle)]
            facts["followers_from"] = "the account's public profile"
            ratio = outlier(facts.get("views"), facts["followers"])
            if ratio is not None:
                facts["outlier"] = ratio
        item["video_meta"] = facts
        item["video_meta_at"] = now
        m = item.setdefault("metrics", {})
        for src, dst in (("views", "views"), ("likes", "likes"), ("comments", "comments")):
            if m.get(dst) in (None, "") and facts.get(src) is not None:
                m[dst] = facts[src]
        if facts.get("outlier") is not None:
            m["outlier"] = facts["outlier"]
        done += 1
    return done, notes


def line(facts):
    """A short, factual line for a prompt or a sheet cell."""
    if not facts:
        return ""
    bits = []
    if facts.get("outlier") is not None:
        bits.append(f"{facts['outlier']:g}x the poster's followers in views")
    if facts.get("music"):
        bits.append(f"sound: {facts['music']}")
    if facts.get("duration_s"):
        bits.append(f"{facts['duration_s']:g} s long")
    if facts.get("captions"):
        bits.append("has captions")
    return "; ".join(bits)


def tmpdir():
    return tempfile.TemporaryDirectory(prefix="vmeta-")
