"""Agent-Reach's YouTube channel, as a shared service client: video details, search and subtitles through yt-dlp.

This follows what Agent-Reach itself does for YouTube (https://github.com/Panniantong/Agent-Reach, MIT): it runs yt-dlp, not a logged-in browser. So, unlike the Instagram and Reddit channels, it needs
no login, no OpenCLI and no machine the owner controls; it can run anywhere yt-dlp can. The one genuine limit is YouTube's own: it may show a bot check to the addresses of data centres (a hosted
runner is one), and that is reported as a refusal and left alone, never worked around. Transcription (Agent-Reach's Whisper step) needs a speech-to-text key and is not offered here.

The client answers the same `call([action, target, ...])` interface as the OpenCLI client, so every module that asks Agent-Reach for a channel gets rows in one shape: title, url, channel, channel_id,
channel_url, uploader_id, description, published, views, duration.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

from . import contract as K
from .safety import scrub

_BOT = re.compile(r"sign in to confirm|not a bot|confirm you.re not a bot|captcha|unusual traffic", re.I)
_GONE = re.compile(r"video unavailable|private video|this video is not available|has been removed|no longer available|members-only|age-restricted|requested format|does not exist|404", re.I)
_LIMIT = re.compile(r"http error 429|too many requests|rate.?limit", re.I)


def classify(stderr, returncode):
    """The Failure for a yt-dlp failure, from its message. A bot check and a rate limit are refusals: they are reported and the caller does not try another way round them."""
    text = scrub(str(stderr), 400)
    if _BOT.search(text):
        return K.Failure(K.PLATFORM_BLOCKED, "YouTube showed a bot check to this address (common for data-centre addresses such as a hosted runner); it was not worked around")
    if _LIMIT.search(text):
        return K.Failure(K.RATE_LIMITED, "YouTube asked us to slow down", retry_after=3600)
    if _GONE.search(text):
        return K.Failure(K.NOT_FOUND, "YouTube says this video is not available")
    return K.Failure(K.UPSTREAM_BROKEN, f"yt-dlp failed ({returncode}): {text[:200]}")


def _row(info):
    handle = str(info.get("uploader_id") or "")
    return {"title": str(info.get("title") or ""), "url": str(info.get("webpage_url") or info.get("url") or (f"https://www.youtube.com/watch?v={info['id']}" if info.get("id") else "")),
            "channel": str(info.get("channel") or info.get("uploader") or ""), "channel_id": str(info.get("channel_id") or ""), "channel_url": str(info.get("channel_url") or info.get("uploader_url") or ""),
            "uploader_id": handle if handle.startswith("@") else "", "description": str(info.get("description") or ""), "published": str(info.get("upload_date") or ""),
            "views": info.get("view_count"), "duration": info.get("duration")}


class YtDlpYouTube:
    """yt-dlp for YouTube. `needs_browser` is False: no login and no hosted-runner refusal. `run` is replaced in tests."""
    site = "youtube"
    needs_browser = False

    def __init__(self, *, run=subprocess.run, python=None, timeout=90, environ=None, installed=None):
        self.run, self.python, self.timeout, self.environ = run, python or sys.executable, timeout, environ
        self._installed = installed

    def available(self):
        if self._installed is not None:
            return self._installed
        return importlib.util.find_spec("yt_dlp") is not None

    def _ytdlp(self, args, cwd=None):
        if not self.available():
            raise K.Failure(K.NEED_SETUP, "yt-dlp is not installed here (pip install yt-dlp)")
        try:
            done = self.run([self.python, "-m", "yt_dlp", "--no-warnings", *args], capture_output=True, text=True, timeout=self.timeout, cwd=cwd, env=self.environ)
        except subprocess.TimeoutExpired:
            raise K.Failure(K.TIMEOUT, f"yt-dlp did not finish in {self.timeout} seconds") from None
        except OSError as exc:
            raise K.Failure(K.NEED_SETUP, f"yt-dlp could not start ({exc.__class__.__name__})") from None
        if done.returncode != 0:
            raise classify(done.stderr or done.stdout, done.returncode)
        return done.stdout or ""

    def search(self, query, limit=5):
        out = self._ytdlp(["--flat-playlist", "--dump-json", f"ytsearch{max(1, int(limit))}:{query}"])
        rows = []
        for line in out.splitlines():
            if line.strip():
                try:
                    rows.append(_row(json.loads(line)))
                except ValueError:
                    continue
        return rows

    def read(self, target):
        url = target if target.startswith("http") else f"https://www.youtube.com/watch?v={target}"
        out = self._ytdlp(["--dump-json", "--skip-download", url])
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if not lines:
            raise K.Failure(K.UPSTREAM_BROKEN, "yt-dlp returned nothing for this video")
        return [_row(json.loads(lines[-1]))]

    def subtitle(self, target, langs="en,en-orig"):
        url = target if target.startswith("http") else f"https://www.youtube.com/watch?v={target}"
        wanted = [x.strip() for x in langs.split(",") if x.strip()]
        with tempfile.TemporaryDirectory(prefix="subs-") as tmp:
            out = self._ytdlp(["--skip-download", "--no-simulate", "--write-subs", "--write-auto-subs", "--sub-langs", ",".join(wanted), "--sub-format", "vtt", "--print", "%(title)s", "-o", "%(id)s.%(ext)s", url], cwd=tmp)
            files = sorted(f for f in os.listdir(tmp) if f.endswith(".vtt"))
            if not files:
                raise K.Failure(K.NOT_FOUND, f"no subtitles in {', '.join(wanted)} for this video")
            with open(os.path.join(tmp, files[0]), encoding="utf-8", errors="replace") as fh:
                text = vtt_to_text(fh.read())
        title = next((ln for ln in out.splitlines() if ln.strip()), "")
        return [{"title": title, "url": url, "text": text, "description": text}]

    def call(self, args):
        """The OpenCLI-style interface: ['search', query, '--limit', n] | ['read', target] | ['subtitle', target]. Returns rows."""
        args = [str(a) for a in args]
        action, rest = args[0], args[1:]
        if action == "search" and rest:
            limit = int(rest[rest.index("--limit") + 1]) if "--limit" in rest and rest.index("--limit") + 1 < len(rest) else 5
            return self.search(rest[0], limit)
        if action == "read" and rest:
            return self.read(rest[0])
        if action == "subtitle" and rest:
            return self.subtitle(rest[0])
        raise K.Failure(K.BAD_INPUT, f"the YouTube channel offers search, read and subtitle (not {action!r})")


def vtt_to_text(vtt):
    """The words of a WebVTT caption file, without timings, tags or the repeated lines automatic captions produce."""
    out, last = [], ""
    for line in vtt.splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if line and line != last:
            out.append(line)
            last = line
    return " ".join(out)
