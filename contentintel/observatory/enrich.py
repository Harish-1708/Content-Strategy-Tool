"""Finding a competitor's website and social accounts from several independent sources, and saying how sure we are.

Sources (each optional, each recorded by name):
  collector        an Instagram post of theirs already collected by the existing Instagram collector
  homepage         the competitor's own website links to the account (read politely, or through the Scrapling worker when that engine is chosen)
  search           a Google-backed web search for "<name> instagram" (Serper, Tavily, SerpApi, Exa: whichever the search settings allow)
  agent_reach      Instagram's account search and the account's profile through OpenCLI, when it is installed and a person has logged in (never on a hosted runner)

Each source gives a piece of evidence with a weight for how much it proves. Weights from different sources combine as independent evidence (1 - the product of what each leaves
in doubt), so two weak, agreeing sources can pass the bar where one cannot, and one strong one (the site linking to its own account, or the account's bio linking back to the site)
passes alone. A match below `min_confidence` is not written to the competitor record: it is kept as a 'possible' with its confidence and evidence so a person can see it.
Nothing here can add a competitor; it only fills in who a candidate is.
"""
import re

from . import htmlx
from .config import host_of

WEIGHTS = {"collector": 0.85, "homepage": 0.90, "bio_link": 0.95, "search_1": 0.60, "search_2": 0.50, "search_3": 0.40, "search_n": 0.25, "agent_reach": 0.50}
_TOKEN = re.compile(r"[a-z0-9]+")


def _norm(text):
    return "".join(_TOKEN.findall(str(text or "").lower()))


def name_matches_handle(name, handle, title=""):
    """Does this account plausibly belong to this company? The name and the handle must resemble each other (one inside the other, at least four letters), or every word
    of a multi-word name must be in the account's display title. A single short word matching a title is not enough."""
    n, h = _norm(name), _norm(handle)
    words = _TOKEN.findall(str(name or "").lower())
    if len(n) >= 4 and len(h) >= 4 and (n in h or h in n):
        return len(words) >= 2 or abs(len(n) - len(h)) <= 3          # a single short word must be nearly the whole handle: "defi" is not "defi.finance"
    t = _norm(title)
    return len(words) >= 2 and all(w in t for w in words)


def combine(weights):
    """How sure, from independent pieces of evidence: 1 - the product of each one's doubt."""
    doubt = 1.0
    for w in weights:
        doubt *= 1 - max(0.0, min(1.0, w))
    return round(1 - doubt, 2)


def evidence_for_handle(name, domain, *, collected=None, homepage=None, search_results=None, agent_rows=None, profile_site=None):
    """{handle: [{source, weight, detail}]} from every source that was consulted. Inputs that are None were not consulted."""
    found = {}

    def add(handle, source, weight, detail):
        handle = str(handle or "").lstrip("@").lower()
        if handle:
            found.setdefault(handle, []).append({"source": source, "weight": WEIGHTS.get(weight, weight), "detail": detail})
    if collected:
        add(collected, "collector", "collector", "an Instagram post from this account was collected by the existing collector")
    if homepage:
        add(homepage, "homepage", "homepage", f"{domain or 'the competitor'}'s own site links to this account")
    for rank, r in enumerate(search_results or [], start=1):
        social = htmlx.social_links([{"href": r.get("url", "")}])
        handle = social.get("instagram")
        if handle and name_matches_handle(name, handle, r.get("title", "")):
            add(handle, "search", f"search_{rank}" if rank <= 3 else "search_n", f"search result {rank}: {str(r.get('title', ''))[:80]}")
    for row in agent_rows or []:
        handle = row.get("username") or ""
        if handle and name_matches_handle(name, handle, row.get("name", "")):
            add(handle, "agent_reach", "agent_reach", f"Instagram account search: {str(row.get('name') or handle)[:60]}")
    for handle, site in (profile_site or {}).items():
        if site and domain and host_of(site).removeprefix("www.") == host_of(domain).removeprefix("www."):
            add(handle, "agent_reach", "bio_link", "the account's profile links to this website")
    return found


def pick(found, min_confidence):
    """(chosen handle or '', confidence, evidence, possible) from the evidence. The best-supported handle wins; below the bar it is only a 'possible'."""
    if not found:
        return "", 0.0, [], None
    scored = sorted(((combine([e["weight"] for e in ev]), h, ev) for h, ev in found.items()), key=lambda t: (-t[0], t[1]))
    conf, handle, ev = scored[0]
    rival = scored[1][0] if len(scored) > 1 else 0.0
    if conf >= min_confidence and conf - rival >= 0.15:             # two accounts nearly as well supported means we are not sure which one it is
        return handle, conf, ev, None
    return "", conf, ev, {"handle": handle, "confidence": conf, "evidence": ev}


def agent_rows_for(cli, name, limit=5):
    """Account rows from Instagram's account search through OpenCLI: [{username, name, site}]; a Failure from the route is raised for the caller to note."""
    from .. import agent_reach_instagram as AR
    rows = []
    for raw in AR.rows_from(cli.call(["search", name, "--limit", limit]))[:limit]:
        handle = AR._pick(raw, "username", "handle", "author", "user")
        if handle:
            rows.append({"username": str(handle).lstrip("@"), "name": str(AR._pick(raw, "full_name", "name", "title", "displayName") or ""),
                         "site": str(AR._pick(raw, "external_url", "website", "bio_link", "link") or "")})
    return rows


def profile_site(cli, handle):
    """The website a profile links to, '' when it has none or the read fails with a non-fatal error."""
    from .. import agent_reach_instagram as AR
    rows = AR.rows_from(cli.call(["profile", handle]))
    return str(AR._pick(rows[0], "external_url", "website", "bio_link", "link", "url") or "") if rows else ""


def record(c, kind, handle, conf, evidence, possible, now):
    """Write the outcome onto a candidate. The handle goes on the record only above the bar; the evidence and confidence are always kept."""
    c[f"{kind}_confidence"] = conf
    c[f"{kind}_evidence"] = evidence
    c[f"{kind}_checked_at"] = now
    if handle:
        c[kind] = handle
        c.pop(f"{kind}_possible", None)
        return True
    if possible:
        c[f"{kind}_possible"] = possible
    return False
