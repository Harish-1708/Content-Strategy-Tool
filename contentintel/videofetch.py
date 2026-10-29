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
