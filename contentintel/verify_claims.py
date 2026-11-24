"""Verifying a claim from public sources, with a model from the free pool: the tool finds the sources ITSELF (its Serper search), reads the pages, and a reviewer model says, page by page, whether the page
supports the claim. Nothing is hardcoded: no address, no site list, no award or brand name is written in this file.

The rule (the same for every brand, enforced here and again where the ledger is read, in canon.py). Every source is judged by a pool model, and the CODE checks that the quotation really is in the page:
  * sources are judged by whether they are CLOSE TO THE FACT (the brand about its own founder, launch date, products and specs; the body that granted the award; a person in an interview; a partner; a trade
    publication reporting it first-hand) and whether they have a REASON TO EXAGGERATE this claim, never by the medium: a web page, a social post, an interview, a podcast or video transcript, a newsletter
    and a community post all count the same way;
  * VERIFIED: ONE source of any kind, close to the fact, with an exact quotation that is really in the page and the brand named, and nothing contradicting it, verifies the claim. When only one source
    stands behind it the claim is marked VERIFIED FROM ONE SOURCE so the owner can spot-check it, and a source that may have a reason to exaggerate is flagged too. A claim on one site only is never blocked for that;
  * CONFLICTED (blocked, the owner decides): only when sources contradict each other, or a source directly contradicts the claim;
  * NO SOURCE close to the fact (none found, or only far-away repeats) and nothing contradicting it: a claim that was blocked is left UNVERIFIED for the owner to decide;
  * the owner's own decision on a claim is final: such a claim is never touched;
  * nothing is deleted or rewritten: the check only APPENDS an evidence event, and the owner can change any status afterwards in the Claims tab."""
import re

from . import canon as C
from . import story_refresh as SR

QUERY_SYSTEM = ('You help check ONE claim about a brand against public sources. Reply with JSON only: {"queries": ["<web search query>", ...]} with at most 3 queries that would find a source close to the fact, in ANY medium: '
                'the brand\'s own page for its own facts, the body that granted or recorded it, an interview, a podcast or video, a newsletter, a social or community post, a partner, or a trade publication. Use the claim\'s own key '
                'words: names, years, figures. Never put a web address in a query and never invent a fact.')
CHECK_SYSTEM = ('You check ONE claim about a brand against the text of ONE source (a web page, post, transcript or newsletter). Reply with JSON only: {"verdict": "supports|contradicts|silent", "quote": "<the EXACT sentence copied from the text>", '
                '"about_brand": true|false, "close_to_fact": true|false, "exaggeration_risk": "low|high", "medium": "page|press|social post|interview|podcast|video|newsletter|community post|other"}. '
                'Judge the source by how close it is to the fact, never by its medium: close_to_fact is true when the source is the party the fact is about (the brand on its own founder, launch date, products or specs), the body that '
                'granted or recorded it, a participant, a partner, or someone reporting it first-hand; false for an anonymous repost or a vague echo. exaggeration_risk is high when this source has a reason to exaggerate THIS claim '
                '(for example a brand on its own awards, press or revenue) and low when it does not (a brand on its own launch date). '
                'supports only if the quoted sentence itself states the claim with the same names, years and numbers (a finalist is not a winner; a different year is not support). '
                'contradicts only if the quoted sentence states something that cannot be true together with the claim. Otherwise silent. Copy the quotation exactly; never paraphrase it.')
MAX_QUERIES = 3
MAX_PER_RUN = 60                                                                          # claims one run takes when no number is given
CALLS_PER_CLAIM, CALL_FLOOR, CALL_CEILING = 5, 10, 400


def auto_budget(waiting):
    """The calls a run may use, sized to the claims waiting: about five a claim (one for the queries, a few for the pages), never below a floor and never above a ceiling."""
    return max(CALL_FLOOR, min(CALL_CEILING, int(waiting) * CALLS_PER_CLAIM + CALL_FLOOR))
PER_QUERY = 6


def domain_of(url):
    """The registrable-ish domain of an address: the host without www, and its last two labels (three for a two-letter country suffix under a short second level, e.g. co.uk)."""
    host = re.sub(r"^https?://(www\.)?", "", (url or "").lower()).split("/")[0].split(":")[0]
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "net", "gov", "ac"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def source_id(url):
    """Who stands behind a source: its registrable domain, and for a platform with many authors (a social network, a video site) the domain plus the account or channel in the address, so two posts by two accounts are two voices."""
    domain = domain_of(url)
    if any(domain == h or domain.endswith("." + h) for h in SR.SKIP_HOSTS):
        path = re.sub(r"^https?://[^/]+/?", "", (url or "").split("?")[0]).strip("/").split("/")
        return domain + "/" + path[0].lower() if path and path[0] else domain
    return domain


def decide(supports, contradicts):
    """The outcome of one check: 'conflicted' when a source contradicts, 'verified' with ONE source close to the fact (in any medium), otherwise 'unverified'."""
    if contradicts:
        return "conflicted"
    if any(s["close"] for s in supports):
        return "verified"
    return "unverified"


def verify_claim(reg, name, claim, *, search, get, claude, now, report, max_pages=6, max_calls=None, recheck=False):
    """Check ONE claim (a dict from the ledger). Returns 'verified', 'conflicted', 'unverified' or 'skipped'. `report` is {'calls','errors','notes'} shared across claims."""
    cid = claim["id"]
    if claim.get("owner_decided"):
        report["notes"].append(f"{cid}: the owner decided this claim himself, so it was not checked")
        return "skipped"
    judge = SR.for_job(claude, "claim_verify")
    queries = []
    if not (max_calls and report["calls"] >= max_calls):
        try:
            report["calls"] += 1
            got = (judge.json_task(QUERY_SYSTEM, f"Brand: {name}\nClaim: {claim['text']}", 300) or {}).get("queries") or []
            queries = [str(q).strip() for q in got if str(q).strip() and "http" not in str(q).lower()][:MAX_QUERIES]
        except Exception as exc:                                                         # noqa: BLE001 - no queries from the model: the claim's own words are used
            report["errors"] += 1
            report["notes"].append(f"{cid}: the search queries could not be made ({SR._why(exc)}); the claim's own words were searched")
    if not queries:
        queries = [f"{name} {claim['text']}"[:140]]
    urls, seen = [], set()
    for q in queries:
        try:
            results = search(q) or []
        except Exception as exc:                                                         # noqa: BLE001
            report["errors"] += 1
            report["notes"].append(f"{cid}: search '{q[:60]}' failed ({type(exc).__name__})")
            continue
        for r in results[:PER_QUERY]:
            url = (r.get("url") or "").strip()
            if url and url not in seen and url.startswith("http"):
                seen.add(url)
                urls.append(url)
    supports, contradicts, pages, read_urls = [], [], 0, []
    for url in urls:
        if pages >= max_pages or (max_calls and report["calls"] >= max_calls):
            break
        if any(s["domain"] == source_id(url) for s in supports + contradicts):
            continue                                                                     # one voice per source: a second page of the same site or account is not independent
        try:
            html = get(url)
        except Exception as exc:                                                         # noqa: BLE001
            report["errors"] += 1
            report["notes"].append(f"{cid}: a page could not be read ({type(exc).__name__})")
            continue
        if not html:
            continue
        pages += 1
        read_urls.append(url)
        text = SR.page_text(html)
        if SR._norm(name) not in SR._norm(text):
            continue                                                                     # the brand is not named on the page: it says nothing about the brand
        try:
            report["calls"] += 1
            verdict = judge.json_task(CHECK_SYSTEM, f"Brand: {name}\nClaim: {claim['text']}\n\nPAGE TEXT:\n{text[:12000]}", 400) or {}
        except Exception as exc:                                                         # noqa: BLE001
            report["errors"] += 1
            report["notes"].append(f"{cid}: a page could not be judged ({SR._why(exc)})")
            continue
        quote, kind = str(verdict.get("quote") or ""), verdict.get("verdict")
        if kind in ("supports", "contradicts") and verdict.get("about_brand") is True and SR.quote_in_page(quote, text):
            entry = {"url": url, "domain": source_id(url), "quote": quote[:400], "close": verdict.get("close_to_fact") is True, "risk": "low" if verdict.get("exaggeration_risk") == "low" else "high",
                     "medium": verdict.get("medium") if verdict.get("medium") in C.MEDIA else "other"}
            (supports if kind == "supports" else contradicts).append(entry)
        if len({s["domain"] for s in supports if s["close"]}) >= 2 and not contradicts:
            break                                                                        # two close sources agree and nothing disagrees: enough
    outcome = decide(supports, contradicts)
    report.setdefault("log", []).append({"id": cid, "text": claim["text"][:120], "queries": queries, "found": len(urls), "read": read_urls, "outcome": outcome,
                                         "supports": [(s["url"], s["medium"], "close" if s["close"] else "far", s["risk"]) for s in supports], "contradicts": [(c["url"], c["medium"]) for c in contradicts]})
    reg.record_evidence(cid, supports, contradicts, outcome, now, recheck=recheck)                       # always recorded, so a claim that was checked is not checked again and the owner can see what was found
    return outcome


def log_lines(report, prefix="canon:   "):
    """One block per claim that was checked, so the run's evidence is in the log itself: the verdict, the search queries, how many pages came back, which were read, and every source with its medium, closeness and risk."""
    lines = []
    for e in report.get("log", []):
        lines.append(f"{prefix}claim {e['id']} -> {e['outcome'].upper()}: {e['text']}")
        lines.append(f"{prefix}  searched: " + " | ".join(e["queries"]) + f"  ({e['found']} page(s) came back, {len(e['read'])} read)")
        for url, medium, close, risk in e["supports"]:
            lines.append(f"{prefix}  supports: {url} ({medium}, {close} to the fact, exaggeration risk {risk})")
        for url, medium in e["contradicts"]:
            lines.append(f"{prefix}  CONTRADICTS: {url} ({medium})")
        if not e["supports"] and not e["contradicts"]:
            lines.append(f"{prefix}  no usable source was found")
    return lines


def verify_ids(reg, name, ids, *, search, get, claude, now, max_calls=None, max_pages=6, report=None, tried=None, progress=None, deadline=None, clock=None, recheck=False):
    """Check the named claims (once each per `tried` set). Returns the ids whose status the check CHANGED away from blocking (verified, or a blocked claim left unverified), which is what lets a section through;
    `report['outcomes']` holds every claim's outcome. `report` collects calls, errors and notes."""
    report = report if report is not None else {"calls": 0, "errors": 0, "notes": []}
    report.setdefault("outcomes", {})
    tried = tried if tried is not None else set()
    out = []
    import time as _time
    clock = clock or _time.monotonic
    total = len(ids)
    for position, cid in enumerate(ids, 1):
        claim = reg.state()["claims"].get(cid)
        if not claim or cid in tried or claim["status"] == "verified":
            continue
        if deadline is not None and clock() > deadline:
            left = len([i for i in ids[position - 1:] if i not in tried])
            report["notes"].append(f"the time budget was reached: {left} claim(s) were not checked in this run; they are picked up by the next run")
            break
        tried.add(cid)
        if progress:
            progress("start", cid, claim, position, total)
        if max_calls and report["calls"] >= max_calls:
            report["notes"].append("the call limit was reached: the remaining claim(s) were not checked")
            break
        before = claim["status"]
        outcome = verify_claim(reg, name, claim, search=search, get=get, claude=claude, now=now, report=report, max_pages=max_pages, max_calls=max_calls, recheck=recheck)
        report["outcomes"][cid] = outcome
        if progress:
            progress("done", cid, outcome, position, total)
        after = reg.state()["claims"][cid]["status"]
        if after != before and after in ("verified", "unverified"):
            out.append(cid)
    return out


def targets(reg, limit):
    """The claims worth checking, most useful first: blocked and conflicted (they stop sections), then unverified; never one the owner decided or one already checked."""
    order = {"blocked": 0, "conflicted": 1, "unverified": 2}
    claims = [c for c in reg.state()["claims"].values() if c["status"] in order and not c.get("owner_decided") and not c.get("evidence")]
    return [c["id"] for c in sorted(claims, key=lambda c: (order[c["status"]], c["id"]))][:limit]
