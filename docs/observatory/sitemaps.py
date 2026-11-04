"""Sitemaps: what a site says exists and when each page last changed.

Scrapling's own sitemap spider keeps only the addresses; the observatory also needs `lastmod` (the site's own statement of when a page
changed), so this parser keeps it. XML is read with defusedxml (no entity or DTD tricks), compressed files are unpacked within a size
limit, and an address that is not a public address on the same site is dropped.
"""
import re
from urllib.parse import urlsplit

from defusedxml import ElementTree as DET
from defusedxml.common import DefusedXmlException

from .. import contract as K
from .. import safety
from . import polite

_PATH_KINDS = (("product", re.compile(r"/products?/[^/]+/?$")), ("collection", re.compile(r"/collections?/[^/]+/?$")), ("article", re.compile(r"/(?:blogs?|news|journal|stories|articles?)/.+")),
               ("page", re.compile(r"/pages?/[^/]+/?$")), ("policy", re.compile(r"/policies/")))


def categorize(url):
    """product, collection, article, page, policy, home or other, judged from the address alone."""
    path = urlsplit(url).path or "/"
    if path in ("/", ""):
        return "home"
    for kind, pattern in _PATH_KINDS:
        if pattern.search(path):
            return kind
    return "other"


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _same_site(url, host):
    other = (urlsplit(url).hostname or "").lower()
    return bool(other) and (other == host or other.endswith("." + host) or host.endswith("." + other))


def parse_sitemap(body, site_host, max_urls=50000):
    """{'kind': 'index'|'urlset'|'text', 'children': [...], 'urls': [{loc, lastmod, changefreq, priority, images}]} from a sitemap file."""
    stripped = body.lstrip()[:200]
    if not stripped.startswith(b"<"):
        urls, truncated = [], False
        for line in body.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("http") and safety.is_public_url(line) and _same_site(line, site_host):
                if len(urls) >= max_urls:
                    truncated = True
                    break
                urls.append({"loc": line, "lastmod": "", "changefreq": "", "priority": "", "images": 0})
        if not urls:
            raise K.Failure(K.UPSTREAM_BROKEN, "the sitemap is neither XML nor a list of addresses")
        return {"kind": "text", "children": [], "urls": urls, "truncated": truncated}
    try:
        root = DET.fromstring(body, forbid_dtd=True)
    except (DET.ParseError, DefusedXmlException, ValueError):
        raise K.Failure(K.UPSTREAM_BROKEN, "the sitemap is not valid XML (or uses features that are refused for safety)") from None
    kind = _local(root.tag)
    if kind == "sitemapindex":
        children = []
        for node in root:
            loc = next((" ".join((c.text or "").split()) for c in node if _local(c.tag) == "loc"), "")
            if loc and safety.is_public_url(loc) and _same_site(loc, site_host):
                children.append(loc)
        return {"kind": "index", "children": children, "urls": [], "truncated": False}
    if kind != "urlset":
        raise K.Failure(K.UPSTREAM_BROKEN, f"unexpected sitemap root <{kind}>")
    urls, truncated = [], False
    for node in root:
        if _local(node.tag) != "url":
            continue
        if len(urls) >= max_urls:
            truncated = True                       # more addresses exist than the ceiling allows: the caller must not call this complete
            break
        fields = {_local(c.tag): " ".join((c.text or "").split()) for c in node if _local(c.tag) in ("loc", "lastmod", "changefreq", "priority")}
        loc = fields.get("loc", "")
        if loc and safety.is_public_url(loc) and _same_site(loc, site_host):
            urls.append({"loc": loc, "lastmod": fields.get("lastmod", ""), "changefreq": fields.get("changefreq", ""), "priority": fields.get("priority", ""),
                         "images": sum(1 for c in node if _local(c.tag) == "image")})
    return {"kind": "urlset", "children": [], "urls": urls, "truncated": truncated}


def walk_sitemaps(client, start_urls, settings):
    """Read sitemaps and their children politely. Returns {'urls': {loc: {lastmod, cat}}, 'files': [...], 'truncated': bool, 'errors': [...]}."""
    cfg = settings["sitemaps"]
    host = (urlsplit(start_urls[0]).hostname or "").lower()
    queue, seen, files, errors, urls, truncated = list(start_urls), set(), [], [], {}, False
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        if len(files) >= cfg["max_child_sitemaps"]:
            truncated = True
            break
        seen.add(url)
        try:
            got = client.get(url, accept="application/xml,text/xml,text/plain,*/*;q=0.5", max_bytes=cfg["max_decompressed_bytes"])
            parsed = parse_sitemap(polite.decompress(got.body, got.headers, url, cfg["max_decompressed_bytes"]), host, cfg["max_urls"])
        except K.Failure as exc:
            if polite.is_fatal(exc):
                raise
            errors.append({"url": url, "code": exc.code, "message": str(exc)})
            continue
        files.append({"url": url, "kind": parsed["kind"], "urls": len(parsed["urls"]), "children": len(parsed["children"])})
        truncated = truncated or parsed.get("truncated", False)
        queue.extend(c for c in parsed["children"] if c not in seen)
        for entry in parsed["urls"]:
            if len(urls) >= cfg["max_urls"]:
                truncated = True
                break
            urls.setdefault(entry["loc"], {"lastmod": entry["lastmod"], "cat": categorize(entry["loc"])})
    if not files and errors:
        first = errors[0]
        raise K.Failure(first["code"], first["message"])
    return {"urls": urls, "files": files, "truncated": truncated, "errors": errors}


def default_candidates(base_url, from_robots):
    """Where to look for a site's sitemap: what robots.txt lists, otherwise the usual address."""
    base = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"
    return list(from_robots) or [base + "/sitemap.xml"]
