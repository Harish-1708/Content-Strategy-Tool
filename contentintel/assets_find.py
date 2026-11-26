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


def split_derived(query, brand, store, research):
    """(own, competitors): other queries to try, derived from the brand and kept apart. `own` is what the brand says about itself: its keywords, topics and queries from the research file and its catalogue's product terms.
    `competitors` is the competitors' names. A competitor's name finds the COMPETITOR's pictures (Allbirds shoes for 'sweatpant overalls'), so those results are only used when they also pass the relevance floor against
    the ORIGINAL query, and the brand's own site is tried before them."""
    from . import voice as VOICE
    research = research or {}
    own, competitors = [], []
    try:
        own += [t for t in VOICE.topics_for(brand, store, limit=6) if t]
    except Exception:                                                                       # noqa: BLE001 - no catalogue yet: only the names below
        pass
    ads = research.get("ads") if isinstance(research.get("ads"), dict) else {}
    own += list(ads.get("keywords") or []) + list(research.get("topics") or []) + list(research.get("queries") or [])
    pages = ads.get("benchmark_pages")
    names = list(pages) if isinstance(pages, dict) else list(pages or [])
    for e in names + list(research.get("benchmark_pages") or []) + list(research.get("competitors") or []):
        competitors.append(e.get("name") if isinstance(e, dict) else e)
    seen = {query.strip().lower(), brand.name.strip().lower()}                                 # the query itself and the brand's own name are never tried again
    def clean(items):
        out = []
        for q in items:
            if isinstance(q, str) and q.strip() and q.strip().lower() not in seen:
                seen.add(q.strip().lower())
                out.append(q.strip())
        return out[:MAX_EXPANSIONS]
    return clean(own), clean(competitors)


def derived_queries(query, brand, store, research):
    """The own terms then the competitors' names (kept for older callers); at most MAX_EXPANSIONS in all."""
    own, competitors = split_derived(query, brand, store, research)
    return (own + competitors)[:MAX_EXPANSIONS]


def brand_terms(brand, store, research):
    """The words that make a picture the brand's to use: its name and its OWN terms, never a competitor's name."""
    own, _competitors = split_derived(brand.name, brand, store, research)
    return [brand.name] + own


def terms_from_files(brands_dir, slug, store, include_competitors=False):
    """The words that make something the brand's world, read from the brand's own files at run time (never a list in the code, so a new brand works with no code change): its name, the keywords, topics and queries of
    its research file, its catalogue's product terms and, when asked, its competitors' names. A brand that cannot be read: its own slug as words."""
    from pathlib import Path
    import yaml
    from .brands import load_brand
    try:
        brand = load_brand(slug, brands_dir)
        rp = Path(brands_dir) / slug / "research.yaml"
        research = yaml.safe_load(rp.read_text(encoding="utf-8")) if rp.is_file() else None
        own, competitors = split_derived(brand.name, brand, store, research)
        return [brand.name] + own + (competitors if include_competitors else [])
    except Exception:                                                                       # noqa: BLE001
        return [slug.replace("_", " ")]


def on_brand(asset, terms, floor=0.5):
    """A picture is the brand's when it is the brand's own site media (licence brand-own) or its title and credit line mention at least `floor` of the words of one of the brand's own terms."""
    if str(asset.get("license", "")).lower() == "brand-own":
        return True
    return any(words(t) and relevance(asset, t) >= floor for t in terms)


def clean_saved(assets, terms, floor=0.5):
    """(kept, removed): the assets already saved for a brand with the pictures that are not the brand's taken out (a parliament, a coat of arms, a competitor's shoes)."""
    kept, removed = [], []
    for a in assets:
        (kept if on_brand(a, terms, floor) else removed).append(a)
    return kept, removed


_IMG = re.compile(r"<img\b[^>]*>", re.I)
_ATTR = re.compile(r"""(\w[\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_OG = re.compile(r"""<meta[^>]+property=["']og:image["'][^>]*content=["']([^"']+)["']""", re.I)


def site_media(get, domain, query, *, limit=8, front_page=True):
    """The brand's own pictures from its own front page (og:image and <img> with a name or description that mentions the query's words). They are the brand's, so the licence is `brand-own`: the owner is the
    rights holder. Returns [] with the reason in the report when the page cannot be read."""
    from urllib.parse import urljoin
    base = domain if domain.startswith("http") else "https://" + domain
    html = get(base)
    html = html if isinstance(html, str) else str(html or "")
    out, seen = [], set()
    want = set(words(query))
    for m in (_OG.finditer(html) if front_page else ()):                                    # the front page image is the brand's own picture of ITSELF: offered only when the topic is about the brand
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


def pixabay_search(get, key, query, limit=8):
    """The FALLBACK image source for when Openverse and Wikimedia Commons return nothing usable: Pixabay's free API (a free key, no card; the Pixabay Content Licence allows commercial use without attribution). Results are assets
    in the same shape as Openverse's, with the picture's tags as its title so the same relevance floor applies. Raises K.Failure."""
    import json
    from urllib.parse import quote_plus
    from . import contract as K2
    q = quote_plus(" ".join(str(query).split())[:100])
    status, text = get(f"https://pixabay.com/api/?key={key}&q={q}&image_type=photo&per_page={max(3, min(int(limit), 20))}&safesearch=true", {})
    if status == 429:
        raise K2.Failure(K2.RATE_LIMITED, "Pixabay's limit was reached (100 requests a minute)", retry_after=60)
    if status in (400, 401, 403):
        raise K2.Failure(K2.NEED_LOGIN, f"Pixabay refused the key (HTTP {status})")
    if status != 200:
        raise K2.Failure(K2.UPSTREAM_BROKEN, f"Pixabay answered HTTP {status}")
    try:
        hits = json.loads(text).get("hits") or []
    except ValueError:
        raise K2.Failure(K2.UPSTREAM_BROKEN, "Pixabay's answer was not JSON") from None
    out = []
    for h in hits:
        url = h.get("largeImageURL") or h.get("webformatURL")
        if not url:
            continue
        tags = [x.strip() for x in str(h.get("tags", "")).split(",") if x.strip()]
        out.append({"url": url, "thumbnail": h.get("webformatURL") or h.get("previewURL") or url, "title": " ".join(tags) or "pixabay picture", "tags": tags, "license": "pixabay-content-license", "attribution": f"{h.get('user', 'a photographer')} on Pixabay",
                    "page": h.get("pageURL", ""), "kind": "image", "source": "pixabay"})
    return out[:limit]


def find_usable(get, query, kind, *, brand=None, store=None, research=None, limit=8, bearer="", use_openverse=True, licence_ok_for_ads=True, pixabay_key=""):
    """(kept, report): usable assets for `query`, or an empty list and a report that says what was tried. Order: the query itself; the brand's OWN terms; the brand's own site; and last the competitors' names, whose
    results must also pass the relevance floor against the ORIGINAL query (an expansion is scored against what it searched for, which is why 'Allbirds' once returned eight Allbirds shoes for 'sweatpant overalls').
    Every kept asset carries `relevance` against the ORIGINAL query and `found_with` (the query that found it). report = {tried: [{query, source, returned, kept, dropped: [(title, why)]}], used: ''}."""
    report = {"tried": [], "used": ""}

    def attempt(q, strict=False):
        try:
            found, which = OV.find(get, q, kind, limit=limit, bearer=bearer, use_openverse=use_openverse, licence_ok_for_ads=licence_ok_for_ads)
        except K.Failure as exc:
            report["tried"].append({"query": q, "source": "openverse/commons", "returned": 0, "kept": 0, "dropped": [], "error": f"{exc.code}: {exc.message}"})
            return []
        kept, dropped = filter_relevant(found, q) if kind == "images" else (found, [])
        if kind == "images":
            scored = []
            for a in kept:
                orig = round(relevance(a, query), 2)
                if strict and orig < 0.5:
                    dropped.append((a.get("title") or a.get("url", "")[:60], f"off-target for the ORIGINAL query {query!r}: its title and credit line mention {int(orig * 100)}% of its words (it matched {q!r}, a competitor's name)"))
                else:
                    scored.append(dict(a, relevance=orig, found_with=q))
            kept = scored
        report["tried"].append({"query": q, "source": which, "returned": len(found), "kept": len(kept), "dropped": dropped})
        return kept
    kept = attempt(query)
    if kept:
        report["used"] = query
        return kept, report
    own, competitors = split_derived(query, brand, store, research) if brand is not None else ([], [])
    for q in own:
        kept = attempt(q)
        if kept:
            report["used"] = q
            return kept, report
    if brand is not None and kind == "images" and getattr(brand, "domain", ""):
        about_brand = bool(set(words(query)) & (set(words(brand.name)) | {w for t in own for w in words(t)}))          # the topic names the brand or one of its own terms
        try:
            site = site_media(get, brand.domain, query if words(query) else brand.name, limit=limit, front_page=about_brand or not words(query))   # an unrelated topic no longer returns the generic front-page picture, so the Pixabay fallback can be reached (Research tools #43)
        except Exception as exc:                                                            # noqa: BLE001
            report["tried"].append({"query": query, "source": "brand site", "returned": 0, "kept": 0, "dropped": [], "error": f"{type(exc).__name__}: {str(exc)[:100]}"})
            site = []
        else:
            report["tried"].append({"query": query, "source": "brand site", "returned": len(site), "kept": len(site), "dropped": []})
        if site:
            report["used"] = "the brand's own site"
            return [dict(a, relevance=1.0, found_with="the brand's own site") for a in site], report
    if pixabay_key and kind == "images":                                                    # the fallback source: after the brand's own site, before any competitor's name
        for q in [query] + own[:3]:
            try:
                found = pixabay_search(get, pixabay_key, q, limit=limit)
            except K.Failure as exc:
                report["tried"].append({"query": q, "source": "pixabay", "returned": 0, "kept": 0, "dropped": [], "error": f"{exc.code}: {exc.message}"})
                break
            kept, dropped = filter_relevant(found, q)
            kept = [dict(a, relevance=round(relevance(a, query), 2), found_with=q) for a in kept]
            report["tried"].append({"query": q, "source": "pixabay", "returned": len(found), "kept": len(kept), "dropped": dropped})
            if kept:
                report["used"] = f"{q} (Pixabay)"
                return kept, report
    for q in competitors:
        kept = attempt(q, strict=True)
        if kept:
            report["used"] = q
            return kept, report
    return [], report


def report_lines(report):
    lines = []
    for t in report["tried"]:
        lines.append(f"  tried {t['query']!r} on {t['source']}: {t['returned']} returned, {t['kept']} kept, {len(t['dropped'])} dropped" + (f" ({t['error']})" if t.get("error") else ""))
        for title, why in t["dropped"][:5]:
            lines.append(f"    dropped {title[:70]!r}: {why}")
    return lines
