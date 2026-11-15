"""Licence-safe pictures and sounds for briefs, mood boards and drafts: Openverse's public search (images and audio, each with its licence, creator and a ready attribution line).

Openverse (https://github.com/WordPress/openverse, MIT licence, copyright WordPress contributors) is used through its hosted public API, not self-hosted (the repository is a whole data platform).
It works without any account at the lower anonymous limits; with OPENVERSE_CLIENT_ID and OPENVERSE_CLIENT_SECRET (a free registration) the platform asks for a token and gets higher limits. The platform's
own backup is a search of Wikimedia Commons (also public, also with licence and author). The owner can switch Openverse off (services.openverse in observatory.yaml / Controls); nothing is downloaded here:
the result is a list of addresses with the licence and the line to credit, which a person (or a later step) chooses from.
"""
import json
import re
from urllib.parse import quote_plus

from . import contract as K

API = "https://api.openverse.org/v1"
TOKEN_URL = API + "/auth_tokens/token/"
COMMONS = "https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrnamespace=6&gsrlimit={n}&gsrsearch={q}&prop=imageinfo&iiprop=url|extmetadata|size&iiurlwidth=640&format=json"
COMMONS_FILETYPE = {"images": "bitmap", "audio": "audio"}
KINDS = ("images", "audio")


def _asset(kind, r):
    return {"kind": kind[:-1] if kind.endswith("s") else kind, "title": " ".join(str(r.get("title") or "").split())[:140], "url": str(r.get("url") or ""), "thumbnail": str(r.get("thumbnail") or ""),
            "page": str(r.get("foreign_landing_url") or ""), "creator": str(r.get("creator") or ""), "license": str(r.get("license") or "").lower(), "license_version": str(r.get("license_version") or ""),
            "license_url": str(r.get("license_url") or ""), "attribution": " ".join(str(r.get("attribution") or "").split())[:300], "source": str(r.get("source") or r.get("provider") or ""),
            "width": r.get("width"), "height": r.get("height"), "duration_ms": r.get("duration"), "via": "openverse"}


def token(post, client_id, client_secret):
    """A bearer token for higher limits, or '' (anonymous). `post(url, data)` returns (status, text)."""
    if not (client_id and client_secret):
        return ""
    status, text = post(TOKEN_URL, {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
    if status != 200:
        return ""                        # a refused registration falls back to anonymous use rather than stopping the work
    try:
        return str(json.loads(text).get("access_token") or "")
    except ValueError:
        return ""


def search(get, query, kind="images", *, limit=10, licence_ok_for_ads=True, bearer=""):
    """Assets for a query. `get(url, headers)` returns (status, text). Raises K.Failure."""
    if kind not in KINDS:
        raise K.Failure(K.BAD_INPUT, "kind must be images or audio")
    q = quote_plus(" ".join(str(query).split())[:120])
    url = f"{API}/{kind}/?q={q}&page_size={max(1, min(int(limit), 20))}" + ("&license_type=commercial" if licence_ok_for_ads else "") + "&mature=false"
    status, text = get(url, {"Authorization": f"Bearer {bearer}"} if bearer else {})
    if status == 429:
        raise K.Failure(K.RATE_LIMITED, "Openverse's limit was reached (a free registration raises it)", retry_after=3600)
    if status in (401, 403):
        raise K.Failure(K.NEED_LOGIN if bearer else K.PLATFORM_BLOCKED, f"Openverse refused the request (HTTP {status})")
    if status != 200:
        raise K.Failure(K.UPSTREAM_BROKEN, f"Openverse answered HTTP {status}")
    try:
        results = json.loads(text).get("results") or []
    except ValueError:
        raise K.Failure(K.UPSTREAM_BROKEN, "Openverse's answer was not JSON") from None
    return [_asset(kind, r) for r in results if isinstance(r, dict) and r.get("url")]


def _strip(html):
    return " ".join(re.sub(r"<[^>]+>", " ", str(html or "")).split())


def commons_search(get, query, kind="images", *, limit=10):
    """The platform's own backup: Wikimedia Commons pictures or sounds with their licence and author. Raises K.Failure."""
    status, text = get(COMMONS.format(n=max(1, min(int(limit), 20)), q=quote_plus(f"filetype:{COMMONS_FILETYPE.get(kind, 'bitmap')} " + " ".join(str(query).split())[:120])), {})
    if status != 200:
        raise K.Failure(K.UPSTREAM_BROKEN, f"Wikimedia Commons answered HTTP {status}")
    try:
        pages = (json.loads(text).get("query") or {}).get("pages") or {}
    except ValueError:
        raise K.Failure(K.UPSTREAM_BROKEN, "Wikimedia Commons' answer was not JSON") from None
    out = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = _strip((meta.get("LicenseShortName") or {}).get("value")).lower().replace(" ", "-")
        out.append({"kind": "audio" if kind == "audio" else "image", "title": str(page.get("title") or "").removeprefix("File:")[:140], "url": str(info.get("url") or ""), "thumbnail": str(info.get("thumburl") or ""),
                    "page": str(info.get("descriptionurl") or ""), "creator": _strip((meta.get("Artist") or {}).get("value"))[:100], "license": lic, "license_version": "",
                    "license_url": _strip((meta.get("LicenseUrl") or {}).get("value")), "attribution": _strip((meta.get("Attribution") or {}).get("value"))[:300], "source": "wikimedia commons",
                    "width": info.get("width"), "height": info.get("height"), "duration_ms": None, "via": "commons"})
    return [a for a in out if a["url"]]


def find(get, query, kind="images", *, limit=10, bearer="", use_openverse=True, licence_ok_for_ads=True):
    """(assets, which) with the backup as the second route: Openverse first when it is switched on, then Wikimedia Commons (pictures and sounds). licence_ok_for_ads=False lifts Openverse's advertising-use filter
    (the owner's choice; the licence and credit line still come with every result). Raises the last failure when both fail."""
    last = None
    if use_openverse:
        try:
            return search(get, query, kind, limit=limit, bearer=bearer, licence_ok_for_ads=licence_ok_for_ads), "openverse"
        except K.Failure as exc:
            last = exc
    try:
        return commons_search(get, query, kind, limit=limit), "commons"
    except K.Failure as exc:
        last = exc
    raise last


def credit_line(a):
    lic = (a["license"] or "").upper() + (f" {a['license_version']}" if a.get("license_version") else "")
    return a.get("attribution") or f"\"{a['title']}\" by {a['creator'] or 'unknown'} ({lic}) {a['page']}".strip()
