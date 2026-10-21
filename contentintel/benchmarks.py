"""Benchmark brands: find each one's Meta advertiser page once, then follow that page.

Searching the Ad Library for a brand's NAME returns every ad that mentions it (a gym opening that hashtags
Gymshark, an ebook ad that talks about it), so benchmark brands are followed through their own advertiser page
instead. Claude suggests the names; this module turns a name into a page using what the Ad Library itself says
(each ad carries its advertiser's page id, name and follower count), and it never guesses:

  * exactly one page with that name                      -> resolved
  * several pages with that name, one clearly the main   -> resolved (the evidence is written down)
  * several pages with that name, none clearly the main  -> ambiguous: listed for a person to choose
  * no page with that name, but similar names            -> ambiguous: listed for a person to choose
  * nothing usable                                       -> unresolved: tried again after a while

A person can always decide: `benchmark_pages` in a brand's research.yaml maps a name to a page id, an Ad Library
link, or `skip`, and that is never overwritten by anything automatic.
"""
import collections
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

_TS = "%Y-%m-%dT%H:%M:%SZ"
NOISE_WORDS = {"the", "official", "inc", "llc", "ltd", "co", "us", "usa"}
SKIP_WORDS = {"skip", "none", "ignore"}
STATE_VERSION = 1
DOMINANCE = 5            # one page must have this many times the followers of the next to be the clear main page
MAX_CANDIDATES = 5


class BenchmarkError(Exception):
    """A benchmark setting a person can fix in a research file."""


def norm_name(text):
    """A name reduced to what identifies it: lower case, letters and digits only, filler words dropped."""
    plain = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii").lower()
    return "".join(w for w in re.findall(r"[a-z0-9]+", plain) if w not in NOISE_WORDS)


def page_id_from(value):
    """A Meta page id from a bare number or an Ad Library link; None when it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"\d{5,20}", value):
        return value
    if value.startswith("https://www.facebook.com/"):
        found = parse_qs(urlsplit(value).query).get("view_all_page_id", [""])[0]
        return found if re.fullmatch(r"\d{5,20}", found) else None
    return None


def parse_manual(mapping, where):
    """research.yaml `benchmark_pages` -> {name: page id or 'skip'}. Raises BenchmarkError on anything unusable."""
    if mapping in (None, {}):
        return {}
    if not isinstance(mapping, dict):
        raise BenchmarkError(f"{where} must be a list of name: page-id pairs, for example  Gymshark: 123456789")
    out, seen = {}, {}
    for name, value in mapping.items():
        if not isinstance(name, str) or not norm_name(name):
            raise BenchmarkError(f"{where}: {name!r} is not a usable brand name")
        if isinstance(value, str) and value.strip().lower() in SKIP_WORDS:
            chosen = "skip"
        else:
            chosen = page_id_from(value)
            if chosen is None:
                raise BenchmarkError(f"{where}: {name!r} must be a Meta page id (digits), an Ad Library link containing "
                                     f"view_all_page_id=..., or skip; got {value!r}")
        key = norm_name(name)
        if key in seen:
            raise BenchmarkError(f"{where}: {name!r} and {seen[key]!r} are the same brand written twice")
        seen[key] = name
        out[name.strip()] = chosen
    return out


# ---------------------------------------------------------------------------------------
# turning ads into candidate pages, and candidates into a decision
# ---------------------------------------------------------------------------------------

def candidates_from_items(items, label):
    """Advertiser pages behind the ads that one search (named by `label`) found, most followed first."""
    wanted = str(label).strip().lower()
    pages = {}
    for item in items:
        page_id = str(item.get("page_id") or "").strip()
        if not page_id or wanted not in [p.strip().lower() for p in str(item.get("found_via") or "").split(" | ")]:
            continue
        entry = pages.setdefault(page_id, {"names": collections.Counter(), "likes": 0, "ads": set()})
        if item.get("handle"):
            entry["names"][item["handle"].strip()] += 1
        entry["likes"] = max(entry["likes"], item.get("page_likes") or 0)
        entry["ads"].add(item.get("item_id"))
    out = [{"page_id": pid, "name": e["names"].most_common(1)[0][0] if e["names"] else "", "likes": e["likes"],
            "ads": len(e["ads"])} for pid, e in pages.items()]
    return sorted(out, key=lambda c: (-c["likes"], -c["ads"], c["name"].lower(), c["page_id"]))


def _followers(n):
    n = n or 0
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e4 else f"{n:,}"


def describe_candidate(c):
    return f"\"{c['name'] or '(no name)'}\" (id {c['page_id']}, {_followers(c['likes'])} followers, {c['ads']} ads seen)"


def resolve(name, candidates):
    """Decide which advertiser page a brand name means, or say why a person must. Returns a dict with status,
    page_id, page_name, page_likes, reason and the candidates considered."""
    want = norm_name(name)
    ranked = sorted(candidates, key=lambda c: (-(c.get("likes") or 0), -c["ads"], c["name"].lower(), c["page_id"]))
    shown = [dict(c) for c in ranked[:MAX_CANDIDATES]]
    out = {"status": "unresolved", "page_id": "", "page_name": "", "page_likes": 0, "candidates": shown, "reason": ""}
    if not ranked:
        out["reason"] = "searching for this name turned up no ads that name an advertiser page"
        return out
    exact = [c for c in ranked if want and norm_name(c["name"]) == want]
    if len(exact) == 1:
        out.update(status="resolved", reason="the only advertiser page named exactly this", **_chosen(exact[0]))
        return out
    if len(exact) > 1:
        top, second = exact[0], exact[1]
        if (top["likes"] or 0) >= DOMINANCE * max(second["likes"] or 0, 1):
            out.update(status="resolved", **_chosen(top),
                       reason=f"the largest of {len(exact)} pages with this name "
                              f"({_followers(top['likes'])} followers against {_followers(second['likes'])})")
        else:
            out.update(status="ambiguous", candidates=[dict(c) for c in exact[:MAX_CANDIDATES]],
                       reason=f"{len(exact)} pages have this name and none is clearly the main one")
        return out
    related = [c for c in ranked if want and len(norm_name(c["name"])) >= 3
               and (want in norm_name(c["name"]) or norm_name(c["name"]) in want)]
    if related:
        out.update(status="ambiguous", candidates=[dict(c) for c in related[:MAX_CANDIDATES]],
                   reason="no page is named exactly this; the closest names are listed")
        return out
    out["reason"] = "other advertisers turned up, but none has a name like this one"
    return out


def _chosen(c):
    return {"page_id": c["page_id"], "page_name": c["name"], "page_likes": c["likes"]}


# ---------------------------------------------------------------------------------------
# what is remembered between runs
# ---------------------------------------------------------------------------------------

def empty_state():
    return {"version": STATE_VERSION, "items": {}}


def clean_state(raw):
    """What was saved, made safe to use: anything unreadable is treated as 'not decided yet'."""
    items = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items, dict):
        return empty_state()
    good = {}
    for key, entry in items.items():
        if isinstance(entry, dict) and entry.get("status") in ("resolved", "ambiguous", "unresolved") \
                and isinstance(entry.get("name"), str) and (entry["status"] != "resolved" or page_id_from(entry.get("page_id"))):
            good[key] = entry
    return {"version": STATE_VERSION, "items": good}


def record(state, name, kind, resolution, now):
    """A copy of `state` with this brand's decision filed. Earlier evidence is replaced only by newer evidence."""
    out = {"version": STATE_VERSION, "items": dict(state.get("items", {}))}
    out["items"][norm_name(name)] = {
        "name": name, "kind": kind, "status": resolution["status"], "page_id": resolution["page_id"],
        "page_name": resolution["page_name"], "page_likes": resolution["page_likes"],
        "reason": resolution["reason"], "candidates": resolution["candidates"], "checked_at": now, "source": "auto"}
    return out


def _age_days(checked_at, now):
    try:
        return (datetime.strptime(now, _TS) - datetime.strptime(checked_at, _TS)).total_seconds() / 86400
    except (TypeError, ValueError):
        return float("inf")


@dataclass
class FollowPlan:
    pages: list = field(default_factory=list)       # {name, page_id, kind (own|benchmark|manual), page_name, page_likes, source}
    discover: list = field(default_factory=list)    # {name, kind}: to be searched by name this run
    waiting: list = field(default_factory=list)     # {name, kind}: not searched yet (per-run limit)
    review: list = field(default_factory=list)      # {name, kind, status, reason, candidates, retry_after}
    held: list = field(default_factory=list)        # resolved, but over the limit on followed pages
    skipped: list = field(default_factory=list)


def plan_follow(own_name, benchmark_names, manual, state, now, *, max_pages, discovery_per_run, retry_days):
    """Which advertiser pages to follow, which names to look up, and which need a person."""
    plan, items = FollowPlan(), state.get("items", {})
    skip = {norm_name(n) for n, v in manual.items() if v == "skip"}
    manual_keys = {norm_name(n) for n, v in manual.items() if v != "skip"}
    for name, value in manual.items():
        if value != "skip":
            plan.pages.append({"name": name, "page_id": value, "kind": "manual", "page_name": "", "page_likes": 0,
                               "source": "manual"})
    names, seen = [], set()
    for kind, name in [("own", own_name)] + [("benchmark", n) for n in benchmark_names]:
        key = norm_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        if key in skip:
            plan.skipped.append(name)
        elif key not in manual_keys:
            names.append((kind, name, key))
    followed = 0
    for kind, name, key in names:
        entry = items.get(key)
        if entry and entry["status"] == "resolved":
            if kind == "benchmark" and followed >= max_pages:
                plan.held.append(name)
                continue
            followed += kind == "benchmark"
            plan.pages.append({"name": name, "page_id": entry["page_id"], "kind": kind, "page_name": entry.get("page_name", ""),
                               "page_likes": entry.get("page_likes", 0), "source": "auto"})
        elif entry and entry["status"] == "ambiguous":
            plan.review.append({"name": name, "kind": kind, "status": "ambiguous", "reason": entry["reason"],
                                "candidates": entry.get("candidates", []), "retry_after": ""})
        elif entry and _age_days(entry.get("checked_at"), now) < retry_days:
            plan.review.append({"name": name, "kind": kind, "status": "unresolved", "reason": entry["reason"],
                                "candidates": entry.get("candidates", []),
                                "retry_after": f"checked {entry['checked_at'][:10]}, tried again after {retry_days} days"})
        else:
            (plan.discover if len(plan.discover) < discovery_per_run else plan.waiting).append({"name": name, "kind": kind})
    return plan


def format_follow_plan(plan, slug, own_name):
    """The review list for a run report or a seeds printout."""
    lines = ["Benchmark brands (each is followed through its own Meta advertiser page, never by searching its name):"]
    for p in plan.pages:
        who = " (your brand)" if p["kind"] == "own" else ""
        label = f"page \"{p['page_name']}\", " if p["page_name"] else ""
        followers = f", {_followers(p['page_likes'])} followers" if p["page_likes"] else ""
        how = "you chose" if p["source"] == "manual" else "found automatically"
        lines.append(f"  following   {p['name']}{who}: {label}id {p['page_id']}{followers} ({how})")
    for d in plan.discover:
        lines.append(f"  to look up  {d['name']}: its advertiser page is found by searching the name, once")
    for w in plan.waiting:
        lines.append(f"  queued      {w['name']}: after the names above (a limited number are looked up per run)")
    for r in plan.review:
        lines.append(f"  NEEDS YOU   {r['name']}: {r['reason']}" if r["status"] == "ambiguous"
                     else f"  not found   {r['name']}: {r['reason']} ({r['retry_after']})")
        for c in r["candidates"]:
            lines.append(f"                - {describe_candidate(c)}")
        if r["status"] == "ambiguous":
            lines.append(f"              to decide, add under ads: in brands/{slug}/research.yaml ->   "
                         f"benchmark_pages: {{ {r['name']}: PAGE_ID }}    (or  {r['name']}: skip)")
    for name in plan.held:
        lines.append(f"  held        {name}: page known, not followed this run (over the limit on followed pages)")
    for name in plan.skipped:
        lines.append(f"  skipped     {name}: you set it to skip")
    if len(lines) == 1:
        lines.append("  none yet: no benchmark brands suggested (run Collect with mode seeds)")
    return "\n".join(lines)


def sheet_rows(state):
    """One row per benchmark brand for the Benchmarks tab: what was decided and, when it is not decided, who the
    candidates are. A person decides in research.yaml (or by adding a page link there), not in the sheet."""
    rows = {}
    for key, e in sorted(state.get("items", {}).items()):
        rows[key] = {"name": e["name"], "kind": e.get("kind", "benchmark"), "status": e["status"],
                     "page_id": e.get("page_id", ""), "page_name": e.get("page_name", ""),
                     "page_followers": e.get("page_likes") or "", "reason": e.get("reason", ""),
                     "candidates": " | ".join(describe_candidate(c) for c in e.get("candidates", [])),
                     "checked_at": e.get("checked_at", ""), "source": e.get("source", "auto")}
    return rows
