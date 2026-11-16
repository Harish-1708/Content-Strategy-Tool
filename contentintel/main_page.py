"""Find a brand's MAIN official Facebook page when the Ad Library lookup could not (benchmarks.py): searched for with Serper, confirmed by a reviewer step through one of our connected AI tools.

The owner's standing rule (14 Nov 2026): always the main page, never a regional one (@ChobaniAu, @ChobaniNZ); a regional page only when the main one does not exist. When a brand's page is not found, or two pages have
the same name and neither is clearly the main one, this module looks for it with Serper and a reviewer confirms that the page is the main official one. It asks a person only when it is still genuinely unclear.
No page or id is written into this code: everything comes from the search, the reviewer and the page itself.

Three things are passed in, so the logic is tested without a network: `search(query) -> [{url, title, snippet}]`, `review(name, candidates) -> {choice, is_main, regional, reason}` and `read_id(url) -> page id or ''`."""
import re
from urllib.parse import quote

from . import benchmarks as bench
from . import page_match as PM

RESERVED = {"pages", "groups", "watch", "ads", "marketplace", "people", "public", "profile.php", "sharer", "sharer.php", "share", "events", "posts", "photo", "photos", "videos", "reel", "reels", "story.php",
            "login", "help", "policies", "privacy", "business", "gaming", "hashtag", "stories", "permalink.php", "l.php", "dialog", "plugins", "tr", "legal", "about"}
REGIONS = {"au", "nz", "uk", "us", "usa", "ca", "in", "de", "fr", "es", "it", "br", "mx", "za", "ie", "sg", "my", "ph", "id", "jp", "kr", "ae", "sa", "nl", "se", "no", "dk", "fi", "pl", "tr", "ru", "cn", "hk", "tw", "th", "vn", "pk",
           "eu", "asia", "africa", "latam", "india", "canada", "australia", "newzealand", "britain", "ireland", "germany", "france", "spain", "italy", "brasil", "brazil", "mexico", "japan", "korea", "singapore", "malaysia", "global"}
REGION_WORDS = ("australia", "new zealand", "united kingdom", " uk", "india", "canada", "ireland", "deutschland", "france", "españa", "brasil", "méxico", "south africa", "singapore", "malaysia", "philippines", "japan", "korea")
SEARCHES = ('"{name}" official Facebook page', 'site:facebook.com "{name}"')
MAX_CANDIDATES = 5
FB_PEOPLE = re.compile(r"^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:people/([^/]+)/(\d{6,})|profile\.php\?(?:[^#]*&)?id=(\d{6,}))", re.I)
FB_PAGE = re.compile(r"^https?://(?:www\.|m\.|web\.)?facebook\.com/([A-Za-z0-9._\-]+)/?(?:[?#].*)?$", re.I)
PAGE_ID_PATTERNS = (r'"pageID"\s*:\s*"(\d{6,})"', r'"page_id"\s*:\s*"?(\d{6,})', r"fb://page/\?id=(\d{6,})", r'"entity_id"\s*:\s*"(\d{6,})"', r'content="fb://profile/(\d{6,})"',
                    r'"delegate_page_id"\s*:\s*"?(\d{6,})', r'"profile_id"\s*:\s*"?(\d{6,})', r"facebook\.com/profile\.php\?id=(\d{6,})")

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
    """Facebook PAGE addresses among search results, once each (a post, a video or a group is not a page). An address that already holds the page id (facebook.com/people/Name/123..., profile.php?id=123...) carries it."""
    seen, out = set(), []
    for r in results or []:
        url = str(r.get("url") or r.get("link") or "")
        people = FB_PEOPLE.match(url.strip())
        if people:
            name, page_id = people.group(1) or "", people.group(2) or people.group(3)
            slug = re.sub(r"[^A-Za-z0-9]", "", (name or "").replace("-", "")) or page_id
            key = "id:" + page_id
            if key in seen:
                continue
            seen.add(key)
            out.append({"url": f"https://www.facebook.com/profile.php?id={page_id}", "slug": slug, "id": page_id, "title": str(r.get("title") or "")[:140], "snippet": str(r.get("snippet") or "")[:240]})
            continue
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
    """Best first: the page whose address is the brand's own name, then the pages that do not look regional, then the page whose name matches the brand most strongly (exact, then a shared word, then a contained one;
    never a page that only shares a category word ahead of one that carries the brand's whole name), then the rest, in the order the search gave them. Each candidate gets a 'match' note (contentintel/page_match.py)."""
    want = bench.norm_name(brand).replace(" ", "")
    for c, verdict in zip(candidates, PM.judge(brand, [c["title"] or c["slug"] for c in candidates])):
        c["match"] = verdict
    return sorted(candidates, key=lambda c: (re.sub(r"[^a-z0-9]", "", c["slug"].lower()) != want, looks_regional(brand, c), PM.sort_key(c["match"])))


COMMON_WORD_REGIONS = {"in", "it", "no", "my", "us", "id", "me"}        # region codes that are also ordinary words: written as a Capitalised tail (MadeIn, DoItNo) they are far more likely the word than the country


def regional_hint(slug, brand=""):
    """The region a page address names, or ''. A hint only. With the brand's name it is exact: the address is the brand's name plus a region in any letter case (chobaniau, ChobaniAu, CHOBANINZ -> 'au' / 'nz'), and the
    brand's own plain address is never regional. Without the name, the address cannot be told from a plain name that happens to end in region letters (a lower-case 'snuggie' or 'theoodie' end in 'ie'), so only these
    are flagged: a region after a separator (chobani-nz), and an upper-case or Capitalised tail on a lower-case stem (ChobaniAu, DudeWipesUS), except a Capitalised tail that is also an ordinary word (MadeIn)."""
    s = slug or ""
    want, have = re.sub(r"[^a-z0-9]", "", (brand or "").lower()), re.sub(r"[^a-z0-9]", "", s.lower())
    if want and have == want:                                          # the brand's own address is never regional, however it is punctuated (made-in, made.in for the brand 'Made In')
        return ""
    for sep in ("-", ".", "_"):
        parts = s.lower().split(sep)
        if len(parts) > 1 and parts[-1] in REGIONS:
            return parts[-1]
    if want and have.startswith(want) and have[len(want):] in REGIONS:
        return have[len(want):]
    for r in sorted(REGIONS, key=len, reverse=True):
        stem, tail = s[:-len(r)], s[-len(r):]
        if len(stem) >= 4 and tail.lower() == r and stem[-1].islower() and (tail == tail.upper() or (tail == tail.capitalize() and r not in COMMON_WORD_REGIONS)):
            return r
    return ""


def page_id_addresses(url):
    """The places a page id is looked for, in order: the page, its mobile address, its basic address and Facebook's public Page Plugin (the embed any website may show)."""
    slug = slug_of(url)
    if not slug:
        return [url]
    plugin = "https://www.facebook.com/plugins/page.php?href=" + quote(f"https://www.facebook.com/{slug}", safe="") + "&tabs&width=340&height=130&small_header=true&adapt_container_width=true&hide_cover=true&show_facepile=false"
    return list(dict.fromkeys([url, f"https://m.facebook.com/{slug}", f"https://mbasic.facebook.com/{slug}", plugin]))


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


ID_LINKS = (r"facebook\.com/people/[^/\s?]+/(\d{6,})", r"facebook\.com/profile\.php\?id=(\d{6,})", r"view_all_page_id=(\d{6,})", r"facebook\.com/pages/[^/\s?]+/(\d{6,})")
ID_SEARCHES = ('"{name}" facebook.com/people', '"{name}" facebook page', '"{name}" Facebook Ad Library view_all_page_id')
ID_REVIEW_SYSTEM = (
    "You confirm which numeric Facebook page id belongs to a brand's MAIN official page. You are given the brand name, the address of the page already confirmed as its main official page, and candidate ids found in web search results "
    "(the id, the address it came from, the title and snippet). A candidate id belongs to that page only if its address, title or snippet clearly names the SAME page: the brand's own primary, global page. A regional or country page (a name or "
    "address that adds Au, NZ, UK, India or a country), a fan page, a reseller or a different brand is NOT the same page. If no candidate clearly matches, or two different ids both could, say so: do not guess. Answer with JSON only: "
    "{\"choice\": the exact id of the chosen candidate, or null, \"same_page\": true or false, \"regional\": true or false, \"reason\": one short sentence}.")


def id_candidates(results):
    """Numeric page ids found in search results: in a people or pages address, a profile.php address, or an Ad Library view_all_page_id link. Each id once, with where it came from."""
    seen, out = set(), []
    for r in results:
        blob = " ".join(str(r.get(k) or "") for k in ("url", "snippet", "title"))
        for pattern in ID_LINKS:
            for m in re.finditer(pattern, blob):
                if m.group(1) not in seen:
                    seen.add(m.group(1))
                    out.append({"id": m.group(1), "url": r.get("url", ""), "title": r.get("title", ""), "snippet": r.get("snippet", "")})
    return out


def find_id_by_search(name, chosen_url, search, review_id):
    """When the page's own HTML has no id: look for the numeric id in the search results about the page (people addresses, profile.php addresses, Ad Library links) and have the reviewer confirm it is the SAME main page.
    Returns (id, reason) with the id empty when none is found or confirmed; the reason says why. The id must be one of the candidates found: a reviewer's invented id is refused."""
    found, failed = [], ""
    for query in ID_SEARCHES:                                                       # all three: the Ad Library query is never skipped because the first two found something
        try:
            found += list(search(query.format(name=name)) or [])
        except Exception as exc:                                                    # a failed search is a reason, never a crash
            failed = f"the search for the page's numeric id failed ({type(exc).__name__})"
    cands = id_candidates(found)[:8]
    if not cands:
        return "", failed or "a search for the page's numeric id found no address carrying one"
    try:
        verdict = review_id(name, chosen_url, cands) or {}                           # the reviewer sees exactly the candidates the answer is checked against
    except Exception as exc:                                                        # a reviewer that fails leaves the brand unresolved, never crashes the run
        return "", f"a search found {len(cands)} numeric id(s) but the reviewer could not be asked ({type(exc).__name__})"
    choice = str(verdict.get("choice") or "")
    if not choice or not verdict.get("same_page") or verdict.get("regional") or choice not in {c["id"] for c in cands}:
        return "", f"a search found {len(cands)} numeric id(s) but the reviewer could not confirm one as this page" + (f" ({verdict.get('reason')})" if verdict.get("reason") else "")
    return choice, f"the numeric id came from a search ({next(c['url'] for c in cands if c['id'] == choice)}) and the reviewer confirmed it is the same page: {verdict.get('reason') or 'no reason given'}"


def lookup_main_page(name, search, review, read_id, label="the reviewer", review_id=None):
    """Resolve a brand's main official Facebook page, or say why not. Same result shape as benchmarks.resolve."""
    out = {"status": "unresolved", "page_id": "", "page_name": "", "page_likes": 0, "candidates": [], "reason": ""}
    found = []
    for query in SEARCHES:
        try:
            found += list(search(query.format(name=name)) or [])                   # the raw results: they are turned into candidates once, below
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
    try:
        verdict = clean_review(review(name, ranked), ranked)
    except Exception as exc:                                                       # a reviewer that fails leaves the brand unresolved, never crashes the run (as for the id reviewer)
        out.update(status="ambiguous", reason=f"the search found Facebook pages, but the reviewer could not be asked ({type(exc).__name__})")
        return out
    if not verdict["choice"] or not verdict["is_main"]:
        out.update(status="ambiguous", reason="the search found Facebook pages, but " + (verdict["reason"] or "the reviewer could not confirm one as the main official page"))
        return out
    chosen = next(c for c in ranked if c["url"] == verdict["choice"])
    better = [c for c in ranked if c is not chosen and c.get("match") and c["match"]["strength"] in (PM.EXACT, PM.TOKEN) and not c["match"]["category_only"]]
    cm = chosen.get("match") or {}
    if better and cm and (cm.get("strength") == PM.NONE or cm.get("category_only")) and not (chosen.get("id") and cm.get("strength") != PM.NONE):
        out.update(status="ambiguous", reason=f"the reviewer chose {chosen['url']} ({PM.explain(cm)}), while {better[0]['url']} carries the brand's name ({PM.explain(better[0]['match'])}); a person should decide which is the brand's own page")
        return out
    others_main = [c for c in ranked if c is not chosen and not looks_regional(name, c)]
    if (verdict["regional"] or looks_regional(name, chosen)) and others_main:
        out.update(status="ambiguous", reason=f"the reviewer chose {chosen['url']}, which looks regional, while a page that does not ({others_main[0]['url']}) was also found; the standing rule is the main page, so a person decides")
        return out
    others_regional = [c for c in ranked if c is not chosen and looks_regional(name, c)]
    if (verdict["regional"] or looks_regional(name, chosen)) and others_regional:
        out.update(status="ambiguous", reason=f"only regional pages were found ({chosen['url']} and {others_regional[0]['url']}) and none is the main one, so which region is meant is a person's decision")
        return out
    page_id = chosen.get("id") or read_id(chosen["url"])
    how = ""
    if not page_id and review_id is not None:
        page_id, how = find_id_by_search(name, chosen["url"], search, review_id)
    elif not page_id:
        how = "no reviewer is available to confirm a numeric id found by search, so none was looked for"
    if not page_id:
        why = getattr(read_id, "why", "")
        out.update(status="ambiguous", reason=f"{chosen['url']} was confirmed as the main official page by {label} ({verdict['reason']}), but its page id could not be read from the page" + (f" ({why})" if why else "") + (f"; {how}" if how else ""))
        return out
    out.update(status="resolved", page_id=page_id, page_name=chosen["title"] or chosen["slug"], page_likes=0,
               reason=f"found with Serper and confirmed as the main official page by {label}: {verdict['reason']} ({chosen['url']})" + (f"; {how}" if how else ""))
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
        """The page id from the page's own HTML. The page itself is asked first with the tool's own identity and, only if that gives no id or fails, with the earlier browser identity that read the ids of Defi Snacks,
        Magic Spoon and Chobani; then its mobile address, its basic address and Facebook's public Page Plugin (the embed any website may show: it needs no login and names the page it draws).
        read_id.why says what each one did, in the order they were tried."""
        tried = []
        alt = getattr(get_html, "alt", None)

        def attempt(fetch, variant, label):
            try:
                html = fetch(variant) or ""
            except Exception as exc:                                       # the kind of error only
                tried.append(f"{label}: {type(exc).__name__}")
                return ""
            found = read_page_id(html)
            if not found:
                tried.append(f"{label}: no page id in the page ({len(html):,} characters)")
            return found

        for variant in page_id_addresses(url):
            host = variant.split("/")[2] + ("/plugins" if "/plugins/page.php" in variant else "")
            found = attempt(get_html, variant, host)
            if not found and alt is not None and variant == url:           # a failed honest fetch is also 'no id read': the browser identity gets its turn either way
                found = attempt(alt, variant, f"{host} as a browser")
            if found:
                read_id.why = ""
                return found
        read_id.why = "tried " + "; ".join(tried)
        return ""
    read_id.why = ""

    def review_id(name, chosen_url, cands):
        listing = "\n".join(f"- id {c['id']} | from {c['url']} | title: {c['title']} | snippet: {c['snippet']}" for c in cands)
        return claude.json_task(ID_REVIEW_SYSTEM, f"Brand: {name}\nConfirmed main page: {chosen_url}\nCandidate ids:\n{listing}", max_tokens=400)
    return lambda name: lookup_main_page(name, search, review, read_id, label, review_id)
