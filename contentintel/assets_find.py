"""Finding pictures a brief can use, without ever saving junk. The raw search (Openverse, then Wikimedia Commons) matches loosely, so a brand name gives nothing and a product term gives pictures of something else.
This layer: (1) scores every result against the words of the query (its title and credit line must mention them) and DROPS what is off-target; (2) when nothing usable is left, derives other queries from the brand itself,
its catalogue's product terms and its competitors' names (nothing hardcoded); (3) falls back to the brand's own site media, which is the primary source for the brand's own pictures; (4) reports what was tried,
what was kept and what was dropped and why. The result is usable pictures, or a plain statement that none were found."""
import re

from . import contract as K
from . import openverse as OV

STOP = {"the", "and", "for", "with", "from", "that", "this", "your", "our", "his", "her", "its", "are", "was", "were", "has", "have", "not", "but", "you", "all", "any", "can", "new", "photo", "image", "picture", "pictures", "images"}
MAX_EXPANSIONS = 4


def words(text):
    return [w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOP]


def relevance(asset, query):
    """The share of the query's words that the picture's own title, credit line and tags mention (0 to 1); a query with no usable word scores 0."""
    want = set(words(query))
    if not want:
        return 0.0
    have = set(words(" ".join([asset.get("title", ""), asset.get("attribution", ""), " ".join(asset.get("tags") or [])])))
    return len(want & have) / len(want)


def filter_relevant(assets, query, *, floor=0.5):
    """(kept, dropped): a picture stays when at least `floor` of the query's words (and at least one) appear in what the picture says about itself. dropped is [(title, reason)]."""
    kept, dropped = [], []
    for a in assets:
        score = relevance(a, query)
        if score >= floor and score > 0:
            kept.append(dict(a, relevance=round(score, 2)))
        else:
            dropped.append((a.get("title") or a.get("url", "")[:60], f"off-target: its title and credit line mention {int(score * 100)}% of the words of {query!r}"))
    return kept, dropped


def derived_queries(query, brand, store, research):
    """Other queries to try when the first gives nothing usable, derived from the brand: its name (when the query was something else), its catalogue's product terms and its competitors' names."""
    from . import voice as VOICE
    out = []
    try:
        out += [t for t in VOICE.topics_for(brand, store, limit=6) if t]
    except Exception:                                                                       # noqa: BLE001 - no catalogue yet: only the names below
        pass
    research = research or {}
    ads = research.get("ads") if isinstance(research.get("ads"), dict) else {}
    pool = list(ads.get("keywords") or []) + list(research.get("topics") or []) + list(research.get("queries") or [])                 # what the brand says it sells and is known for
    pages = ads.get("benchmark_pages")
    names = list(pages) if isinstance(pages, dict) else list(pages or [])
    for e in names + list(research.get("benchmark_pages") or []) + list(research.get("competitors") or []):                           # competitor names, however the research file lists them
        pool.append(e.get("name") if isinstance(e, dict) else e)
    for name in pool:
        if isinstance(name, str) and name.strip() and name.strip().lower() != brand.name.lower():
            out.append(name.strip())
    seen = {query.strip().lower(), brand.name.strip().lower()}                                 # the query itself and the brand's own name are never tried again
    ordered = []
    for q in out:
        if q.strip().lower() not in seen:
            seen.add(q.strip().lower())
            ordered.append(q.strip())
    return ordered[:MAX_EXPANSIONS]


_IMG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r"""(\w[\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_OG = re.compile(r"""<meta[^>]+property=["']og:image["'][^>]*content=["']([^"']+)["']""", re.I)


def site_media(get, domain, query, *, limit=8):
    """The brand's own pictures from its own front page (og:image and <img> with a name or description that mentions the query's words). They are the brand's, so the licence is `brand-own`: the owner is the
    rights holder. Returns [] with the reason in the report when the page cannot be read."""
    from urllib.parse import urljoin
    base = domain if domain.startswith("http") else "https://" + domain
    html = get(base)
    html = html if isinstance(html, str) else str(html or "")
    out, seen = [], set()
    want = set(words(query))
    for m in _OG.finditer(html):
        url = urljoin(base, m.group(1))
        if url not in seen:
            seen.add(url)
            out.append({"kind": "image", "title": "front page image", "url": url, "thumbnail": url, "page": base, "creator": domain, "license": "brand-own", "license_version": "", "license_url": "", "attribution": f"{domain} (the brand's own site)", "source": "brand site", "via": "brand_site"})
    for tag in _IMG.findall(html):
        attrs = {k.lower(): (a or b) for k, a, b in _ATTR.findall(tag)}
        src = attrs.get("src") or attrs.get("data-src") or ""
        if not src or src.startswith("data:") or url_is_icon(src):
            continue
        url = urljoin(base, src)
        label = " ".join([attrs.get("alt", ""), re.sub(r"[-_./]", " ", src.rsplit("/", 1)[-1])])
        if url in seen or not (want & set(words(label))):
            continue
        seen.add(url)
        out.append({"kind": "image", "title": " ".join((attrs.get("alt") or src.rsplit("/", 1)[-1]).split())[:140], "url": url, "thumbnail": url, "page": base, "creator": domain, "license": "brand-own", "license_version": "",
                    "license_url": "", "attribution": f"{domain} (the brand's own site)", "source": "brand site", "via": "brand_site"})
    return out[:limit]


def url_is_icon(src):
    return bool(re.search(r"(?i)(sprite|favicon|logo-?\d|icon|pixel|spacer|1x1|\.svg(\?|$)|\.gif(\?|$))", src))


def find_usable(get, query, kind, *, brand=None, store=None, research=None, limit=8, bearer="", use_openverse=True, licence_ok_for_ads=True):
    """(kept, report): usable assets for `query`, or an empty list and a report that says what was tried. report = {tried: [{query, source, returned, kept, dropped: [(title, why)]}], used: query-or-source or ''}."""
    report = {"tried": [], "used": ""}

    def attempt(q):
        try:
            found, which = OV.find(get, q, kind, limit=limit, bearer=bearer, use_openverse=use_openverse, licence_ok_for_ads=licence_ok_for_ads)
        except K.Failure as exc:
            report["tried"].append({"query": q, "source": "openverse/commons", "returned": 0, "kept": 0, "dropped": [], "error": f"{exc.code}: {exc.message}"})
            return []
        kept, dropped = filter_relevant(found, q) if kind == "images" else (found, [])
        report["tried"].append({"query": q, "source": which, "returned": len(found), "kept": len(kept), "dropped": dropped})
        return kept
    kept = attempt(query)
    if kept:
        report["used"] = query
        return kept, report
    if brand is not None:
        for q in derived_queries(query, brand, store, research):
            kept = attempt(q)
            if kept:
                report["used"] = q
                return kept, report
        if kind == "images" and getattr(brand, "domain", ""):
            try:
                own = site_media(get, brand.domain, query if words(query) else brand.name, limit=limit)
                if not own and brand.name.lower() not in query.lower():
                    own = site_media(get, brand.domain, brand.name, limit=limit)
            except Exception as exc:                                                        # noqa: BLE001
                report["tried"].append({"query": query, "source": "brand site", "returned": 0, "kept": 0, "dropped": [], "error": f"{type(exc).__name__}: {str(exc)[:100]}"})
                return [], report
            report["tried"].append({"query": query, "source": "brand site", "returned": len(own), "kept": len(own), "dropped": []})
            if own:
                report["used"] = "the brand's own site"
                return own, report
    return [], report


def report_lines(report):
    lines = []
    for t in report["tried"]:
        lines.append(f"  tried {t['query']!r} on {t['source']}: {t['returned']} returned, {t['kept']} kept, {len(t['dropped'])} dropped" + (f" ({t['error']})" if t.get("error") else ""))
        for title, why in t["dropped"][:5]:
            lines.append(f"    dropped {title[:70]!r}: {why}")
    return lines
