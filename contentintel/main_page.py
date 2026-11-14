"""Find a brand's MAIN official Facebook page when the Ad Library lookup could not (benchmarks.py): searched for with Serper, confirmed by a reviewer step through one of our connected AI tools.

The owner's standing rule (14 Nov 2026): always the main page, never a regional one (@ChobaniAu, @ChobaniNZ); a regional page only when the main one does not exist. When a brand's page is not found, or two pages have
the same name and neither is clearly the main one, this module looks for it with Serper and a reviewer confirms that the page is the main official one. It asks a person only when it is still genuinely unclear.
No page or id is written into this code: everything comes from the search, the reviewer and the page itself.

Three things are passed in, so the logic is tested without a network: `search(query) -> [{url, title, snippet}]`, `review(name, candidates) -> {choice, is_main, regional, reason}` and `read_id(url) -> page id or ''`."""
import re

from . import benchmarks as bench

RESERVED = {"pages", "groups", "watch", "ads", "marketplace", "people", "public", "profile.php", "sharer", "sharer.php", "share", "events", "posts", "photo", "photos", "videos", "reel", "reels", "story.php",
            "login", "help", "policies", "privacy", "business", "gaming", "hashtag", "stories", "permalink.php", "l.php", "dialog", "plugins", "tr", "legal", "about"}
REGIONS = {"au", "nz", "uk", "us", "usa", "ca", "in", "de", "fr", "es", "it", "br", "mx", "za", "ie", "sg", "my", "ph", "id", "jp", "kr", "ae", "sa", "nl", "se", "no", "dk", "fi", "pl", "tr", "ru", "cn", "hk", "tw", "th", "vn", "pk",
           "eu", "asia", "africa", "latam", "india", "canada", "australia", "newzealand", "britain", "ireland", "germany", "france", "spain", "italy", "brasil", "brazil", "mexico", "japan", "korea", "singapore", "malaysia", "global"}
REGION_WORDS = ("australia", "new zealand", "united kingdom", " uk", "india", "canada", "ireland", "deutschland", "france", "españa", "brasil", "méxico", "south africa", "singapore", "malaysia", "philippines", "japan", "korea")
SEARCHES = ('"{name}" official Facebook page', 'site:facebook.com "{name}"')
MAX_CANDIDATES = 5
FB_PAGE = re.compile(r"^https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9._\-]+)/?(?:[?#].*)?$", re.I)
PAGE_ID_PATTERNS = (r'"pageID"\s*:\s*"(\d{6,})"', r'"page_id"\s*:\s*"?(\d{6,})', r"fb://page/\?id=(\d{6,})", r'"entity_id"\s*:\s*"(\d{6,})"', r'content="fb://profile/(\d{6,})"')

REVIEW_SYSTEM = (
    "You confirm which Facebook page is a brand's MAIN official page. You are given the brand name and some candidate pages found by a web search (address, title, snippet). "
    "Rules: the main page is the brand's own primary, global page. A regional or country page (for example a page whose name or address adds Au, NZ, UK, India or a country) is NEVER the main page "
    "when a main page exists among the candidates; choose a regional page only if there is no main page among them at all. A fan page, a reseller, a news article or a page about a different brand is not the brand's page. "
    "If you cannot tell, say so: do not guess. Answer with JSON only: {\"choice\": the exact address of the chosen candidate, or null, \"is_main\": true or false, \"regional\": true or false, \"reason\": one short sentence}.")


def slug_of(url):
    m = FB_PAGE.match(str(url or "").strip())
    if not m:
        return ""
    slug = m.group(1)
    return "" if slug.lower() in RESERVED or slug.lower().endswith((".php", ".html")) else slug


def candidates_from(results):
    """Facebook PAGE addresses among search results, once each (a post, a video or a group is not a page)."""
    seen, out = set(), []
    for r in results or []:
        url = str(r.get("url") or r.get("link") or "")
        slug = slug_of(url)
        if not slug or slug.lower() in seen:
            continue
        seen.add(slug.lower())
        out.append({"url": f"https://www.facebook.com/{slug}", "slug": slug, "title": str(r.get("title") or "")[:140], "snippet": str(r.get("snippet") or "")[:240]})
    return out


def looks_regional(brand, candidate):
    """True when the page looks like a regional one: its address is the brand's with a region added, or its title names a region the brand name does not."""
    want, slug = bench.norm_name(brand).replace(" ", ""), re.sub(r"[^a-z0-9]", "", candidate["slug"].lower())
    if want and slug.startswith(want) and slug != want:
        tail = slug[len(want):]
        if tail in REGIONS:
            return True
    for sep in ("-", ".", "_"):
        parts = candidate["slug"].lower().split(sep)
        if len(parts) > 1 and parts[-1] in REGIONS and bench.norm_name(" ".join(parts[:-1])).replace(" ", "") == want:
            return True
    title = " " + candidate["title"].lower()
    return any(w in title and w.strip() not in bench.norm_name(brand) for w in REGION_WORDS)


def rank(brand, candidates):
    """Best first: the page whose address is the brand's own name, then the pages that do not look regional, then the rest, in the order the search gave them."""
    want = bench.norm_name(brand).replace(" ", "")
    return sorted(candidates, key=lambda c: (re.sub(r"[^a-z0-9]", "", c["slug"].lower()) != want, looks_regional(brand, c)))


def read_page_id(html):
    """The numeric page id inside a Facebook page's own HTML, or ''."""
    for pattern in PAGE_ID_PATTERNS:
        m = re.search(pattern, html or "")
        if m:
            return m.group(1)
    return ""


def clean_review(raw, candidates):
    """The reviewer's answer made safe: {choice (one of the candidates' addresses or ''), is_main, regional, reason}. Anything else is 'could not tell'."""
    if not isinstance(raw, dict):
        return {"choice": "", "is_main": False, "regional": False, "reason": "the reviewer did not answer in the expected form"}
    known = {c["url"].lower(): c["url"] for c in candidates}
    choice = known.get(str(raw.get("choice") or "").strip().rstrip("/").lower().replace("http://", "https://").replace("://facebook.com", "://www.facebook.com"), "")
    return {"choice": choice, "is_main": raw.get("is_main") is True, "regional": raw.get("regional") is True, "reason": " ".join(str(raw.get("reason") or "").split())[:200]}


def lookup_main_page(name, search, review, read_id, label="the reviewer"):
    """Resolve a brand's main official Facebook page, or say why not. Same result shape as benchmarks.resolve."""
    out = {"status": "unresolved", "page_id": "", "page_name": "", "page_likes": 0, "candidates": [], "reason": ""}
    found = []
    for query in SEARCHES:
        try:
            found += candidates_from(search(query.format(name=name)))
        except Exception as exc:                                       # a search that fails is a reason, never a crash
            out["reason"] = f"the search for this brand's Facebook page failed ({type(exc).__name__})"
            return out
        if len(candidates_from(found)) >= 3:
            break
    ranked = rank(name, candidates_from(found))[:MAX_CANDIDATES]
    if not ranked:
        out["reason"] = "searching for this brand's Facebook page found no Facebook page address"
        return out
    out["candidates"] = [{"page_id": "", "name": c["title"] or c["slug"], "likes": 0, "ads": 0, "url": c["url"]} for c in ranked]
    verdict = clean_review(review(name, ranked), ranked)
    if not verdict["choice"] or not verdict["is_main"]:
        out.update(status="ambiguous", reason="the search found Facebook pages, but " + (verdict["reason"] or "the reviewer could not confirm one as the main official page"))
        return out
    chosen = next(c for c in ranked if c["url"] == verdict["choice"])
    others_main = [c for c in ranked if c is not chosen and not looks_regional(name, c)]
    if (verdict["regional"] or looks_regional(name, chosen)) and others_main:
        out.update(status="ambiguous", reason=f"the reviewer chose {chosen['url']}, which looks regional, while a page that does not ({others_main[0]['url']}) was also found; the standing rule is the main page, so a person decides")
        return out
    others_regional = [c for c in ranked if c is not chosen and looks_regional(name, c)]
    if (verdict["regional"] or looks_regional(name, chosen)) and others_regional:
        out.update(status="ambiguous", reason=f"only regional pages were found ({chosen['url']} and {others_regional[0]['url']}) and none is the main one, so which region is meant is a person's decision")
        return out
    page_id = read_id(chosen["url"])
    if not page_id:
        out.update(status="ambiguous", reason=f"{chosen['url']} was confirmed as the main official page by {label} ({verdict['reason']}), but its page id could not be read from the page")
        return out
    out.update(status="resolved", page_id=page_id, page_name=chosen["title"] or chosen["slug"], page_likes=0,
               reason=f"found with Serper and confirmed as the main official page by {label}: {verdict['reason']} ({chosen['url']})")
    return out


def heal_benchmarks(state, manual, finder, now, limit):
    """Look again, with the finder, at every benchmark the Ad Library could not settle (ambiguous or unresolved) that a person has not decided. At most `limit` per run. Returns (new state, report lines)."""
    lines, used = [], 0
    manual_keys = {bench.norm_name(k) for k in (manual or {})}
    for key, entry in sorted(state.get("items", {}).items()):
        if used >= limit:
            break
        if entry.get("status") not in ("ambiguous", "unresolved") or key in manual_keys or entry.get("source") == "serper_review" and entry.get("status") == "ambiguous" and bench._age_days(entry.get("checked_at"), now) < 1:
            continue
        used += 1
        found = finder(entry["name"])
        if found is None:
            continue
        if found["status"] == "resolved":
            state = bench.record(state, entry["name"], entry.get("kind", "benchmark"), found, now)
            state["items"][key]["source"] = "serper_review"
            lines.append(f"{entry['name']}: resolved - {found['reason']} -> {found['page_name']} (id {found['page_id']})")
        else:
            lines.append(f"{entry['name']}: still {entry['status']} after a search for its Facebook page - {found['reason']}")
    return state, lines


def make_finder(serper, claude, get_html, label="the AI reviewer"):
    """The finder the collect command uses: (name) -> resolution, from a Serper provider, a Claude-like client with json_task (routed through the AI gateway when the owner set that) and a page fetcher."""
    def search(query):
        return [{"url": r.url, "title": r.title, "snippet": r.snippet} for r in serper.search(query, num=10)]

    def review(name, candidates):
        listing = "\n".join(f"- {c['url']} | title: {c['title']} | snippet: {c['snippet']}" for c in candidates)
        return claude.json_task(REVIEW_SYSTEM, f"Brand: {name}\nCandidates:\n{listing}", max_tokens=500)

    def read_id(url):
        try:
            return read_page_id(get_html(url))
        except Exception:
            return ""
    return lambda name: lookup_main_page(name, search, review, read_id, label)
