"""Stage 11: the search fallback. When the tool's own search (Serper) fails or has no key, a self-hosted SearXNG-compatible metasearch (RustXNG) can answer instead. It is OFF until its address is set
(RUSTXNG_URL, the owner's own host: nothing public is built in), it only ever reads public search results, and it returns the same {url, title} shape, so every caller (the story-bank refresh, the claim check, the
foundation) can use it without change. Its answer is a fallback, never a replacement: the primary is always asked first."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


def searx_search(base_url, query, *, opener=None, limit=10):
    """The results of one query from a SearXNG-compatible endpoint (JSON format), as [{url, title}]. Raises on any problem so the caller can say why."""
    base = base_url.rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError("the search fallback address must start with https:// or http://")
    url = f"{base}/search?{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "KelsonResearchBot/1.0"})
    with (opener or urllib.request.urlopen)(req, timeout=20) as resp:                         # noqa: S310 - the owner's own configured host
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return [{"url": r["url"], "title": r.get("title", "")} for r in (data.get("results") or []) if isinstance(r, dict) and r.get("url")][:limit]


def with_fallback(primary, base_url, *, notes=None, searcher=searx_search):
    """A search function that asks `primary` first and, if it is missing, fails or finds nothing, asks the fallback at `base_url`. With no base_url the primary is returned as it is."""
    if not base_url:
        return primary

    def search(query):
        if primary is not None:
            try:
                got = primary(query)
                if got:
                    return got
            except Exception as exc:                                                        # noqa: BLE001 - the primary failed: say so, then the fallback
                if notes is not None:
                    notes.append(f"search: the primary search failed ({type(exc).__name__}); the fallback was asked")
        try:
            return searcher(base_url, query)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            if notes is not None:
                notes.append(f"search: the fallback failed too ({type(exc).__name__})")
            return []
    return search


def configured(environ=None):
    return (environ if environ is not None else os.environ).get("RUSTXNG_URL", "").strip()
