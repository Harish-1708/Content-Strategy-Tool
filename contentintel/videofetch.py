"""The real video of a TikTok or Instagram post, not just its cover picture.

The collectors only ever see a post's cover image. For a creative team a still cannot show the hook, the pacing or the voice, so for items
that were judged worth keeping the post's own page is handed to yt-dlp, which knows how to fetch the video. This is best effort: TikTok and
Instagram may refuse a data-centre address or an expired login, and the run then says why and keeps the cover. Nothing here is ever
needed for the rest of the tool to work.
"""
import os
import re
import subprocess
from pathlib import Path

import json
import html as html_lib
from .browser import BrowserError, parse_cookies

_TIKTOK = re.compile(r"^https?://(?:www\.|m\.)?tiktok\.com/@[^/]+/video/\d+|^https?://(?:vm|vt)\.tiktok\.com/[A-Za-z0-9]+")
_INSTAGRAM = re.compile(r"^https?://(?:www\.)?instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/[A-Za-z0-9_-]+")


def video_page(item):
    """Does this item have a page that holds a video yt-dlp can fetch (a TikTok video or an Instagram reel or post)?"""
    url = str(item.get("url") or "")
    return bool(_TIKTOK.match(url) or _INSTAGRAM.match(url))


def cookie_file(cookie_texts, path):
    """Write the cookie secrets (browser-export JSON) as the Netscape cookie file yt-dlp reads. Returns the path, or None if there are none."""
    lines = ["# Netscape HTTP Cookie File"]
    for text in cookie_texts:
        try:
            cookies = parse_cookies(text or "")
        except BrowserError:
            continue
        for c in cookies:
            domain = c["domain"]
            lines.append("\t".join([domain, "TRUE" if domain.startswith(".") else "FALSE", c.get("path", "/"), "TRUE" if c.get("secure") else "FALSE",
                                    str(int(c.get("expires", 0))), c["name"], c["value"]]))
    if len(lines) == 1:
        return None
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _last_error(stderr):
    for line in reversed(str(stderr or "").strip().splitlines()):
        line = re.sub(r"^\s*(ERROR|WARNING):\s*(\[[^\]]+\]\s*)?([A-Za-z0-9_-]{6,}:\s+)?", "", line).strip()      # the label and the video id come first
        if line:
            return line[:160]
    return "yt-dlp could not download it"


def ytdlp_download(url, dest_dir, cookies_path, max_mb, timeout, runner=subprocess.run):
    """Download one video into dest_dir. Returns (path, "") or (None, the reason)."""
    command = ["yt-dlp", "--no-playlist", "--quiet", "--no-warnings", "--no-progress", "-f", "b[ext=mp4]/b", "--max-filesize", f"{max_mb}M",
               "--socket-timeout", "20", "--retries", "2", "-o", os.path.join(dest_dir, "video.%(ext)s")]
    if cookies_path:
        command += ["--cookies", cookies_path]
    try:
        done = runner(command + [url], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "yt-dlp is not installed"
    except subprocess.TimeoutExpired:
        return None, f"yt-dlp gave up after {timeout} seconds"
    files = sorted(p for p in Path(dest_dir).glob("video.*") if p.suffix not in (".part", ".ytdl"))
    if done.returncode != 0 or not files:
        return None, _last_error(done.stderr)
    return files[0], ""


# ---- what a failure means ---------------------------------------------------------------------------------------------------------------------

def platform_of(url):
    url = str(url or "").lower()
    return "tiktok" if "tiktok.com" in url else "instagram" if "instagram.com" in url else ""


def classify_failure(message, url=""):
    """(status, plain words) for a failed video download, so a post that simply has no video is never shown as a failure.
    'none': the post has no video (an image or a carousel). 'gone': the post is no longer there. 'blocked': the platform refused this server
    or wants a login. 'failed': anything else."""
    text = str(message or "")
    low = text.lower()
    name = {"tiktok": "TikTok", "instagram": "Instagram"}.get(platform_of(url), "the platform")
    if "no video in this post" in low or "no video formats found" in low or "there is no video" in low:
        return "none", "this post has no video"
    if "yt-dlp is not installed" in low:
        return "failed", "yt-dlp is not installed"
    if any(w in low for w in ("unexpected response from webpage", "failed to parse json", "unable to download webpage", "rate-limit", "rate limit", "login required",
                              "requested content is not available", "http error 403", "http error 429", "http error 401", "sign in", "log in")):
        return "blocked", f"{name} refused the download from this server"
    if any(w in low for w in ("video unavailable", "video not available", "this video is unavailable", "has been removed", "is private", "was deleted", "not found", "http error 404",
                              "content isn't available", "no longer available")):
        return "gone", "the post is no longer available"
    if "gave up after" in low:
        return "failed", text[:80]
    short = re.split(r";\s*please report", text, maxsplit=1)[0].strip()
    return "failed", (short[:120] or "the download failed")


# ---- the browser route -------------------------------------------------------------------------------------------------------------------------

_TIKTOK_ADDR = re.compile(r'"(?:downloadAddr|playAddr)"\s*:\s*"(https:[^"]+)"')
_INSTAGRAM_ADDR = re.compile(r'"(?:video_versions"\s*:\s*\[\s*\{[^{}]*?"url"|video_url|playable_url)"?\s*:?\s*"(https:[^"]+)"')
_VIDEO_TAG = re.compile(r'<video[^>]+src="(https://[^"]+)"', re.I)
_OG_IMAGE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"|<meta[^>]+content="([^"]+)"[^>]+property="og:image"', re.I)


def _unescape(url):
    try:
        url = json.loads('"' + url + '"')
    except ValueError:
        pass
    return html_lib.unescape(url)


def find_video_url(page_html, platform):
    """The address of the video in a post page's own data (a TikTok page's playAddr, an Instagram page's video_versions), or ''."""
    for pattern in ((_TIKTOK_ADDR,) if platform == "tiktok" else (_INSTAGRAM_ADDR,)) + (_VIDEO_TAG,):
        found = pattern.search(page_html or "")
        if found:
            return _unescape(found.group(1))                              # the patterns only match https: addresses, never blob: or http:
    return ""


def find_cover_url(page_html):
    found = _OG_IMAGE.search(page_html or "")
    return _unescape(found.group(1) or found.group(2)) if found else ""


def browser_download(url, dest_dir, factory, max_mb):
    """The video through the logged-in browser: open the post, read the video's address from the page, fetch it with the browser's own
    cookies. Returns (path, "") or (None, the reason)."""
    platform = platform_of(url)
    if not platform:
        return None, "not a TikTok or Instagram page"
    try:
        with factory(platform) as browser:
            browser.open(url)
            src = find_video_url(browser.html(), platform)
            if not src:
                return None, "the page shows no video address"
            status, body = browser.fetch_bytes(src, url, max_mb * 1024 * 1024)
    except Exception as exc:                                              # a page that will not load is a reason, not a crash
        return None, f"the browser could not load the page ({exc.__class__.__name__}: {str(exc)[:100]})"
    if status >= 400 or not body:
        return None, f"the video server answered {status}" if status >= 400 else "the video server sent nothing"
    path = Path(dest_dir) / "video.mp4"
    path.write_bytes(body)
    return path, ""


def layered_runner(factory=None, ytdlp=ytdlp_download):
    """yt-dlp first; if the platform refuses it (not if the post has no video or is gone), the logged-in browser as a second route."""
    def run(url, dest_dir, cookies_path, max_mb, timeout):
        path, why = ytdlp(url, dest_dir, cookies_path, max_mb, timeout) if ytdlp else (None, "yt-dlp is not installed")
        if path or factory is None or classify_failure(why, url)[0] in ("none", "gone"):
            return path, why
        second, why2 = browser_download(url, dest_dir, factory, max_mb)
        return (second, "") if second else (None, f"{why}; the browser route also failed: {why2}")
    return run
