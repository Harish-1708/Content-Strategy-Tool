"""The story-bank refresh: public-source research that keeps the claims ledger current WITHOUT ever deleting or rewriting anything.

The rules (the owner's, and enforced by the code, not by good intentions):
  * Old facts are protected. The registry is append-only: this module can only ADD events. A verified claim is never touched, and the owner's Decision and Notes columns are never written by it.
  * A new fact enters ONLY as an UNVERIFIED claim, with its source address, the date it was read and the exact sentence it rests on. Nothing is auto-verified and nothing becomes canon.
  * If new research disagrees with a claim already in the ledger, BOTH stay: the new one is added as CONFLICTED and linked to the old one, for the owner to decide. The tool never resolves it.
  * Every fact is checked before it goes in: it needs a source address, an exact quotation that really is in the page the tool fetched (checked by the code, not by a model), and a reviewer that
    reads only the fact and the quotation must confirm it. A fact without a source, with a quotation not found in the page, or that the reviewer cannot confirm does not go in (it is counted and named).
The watchlist for the first refresh is the brand's own Gaps (its claims seed). Searching uses the tool's Serper search; the extraction and the review go through the tool's routed AI client.
"""
import re
from . import canon as C

BASE_QUERIES = ("{name} award", "{name} featured in", "{name} press coverage", "{name} funding", "{name} revenue sales", "{name} reviews customers", "{name} founder interview")
SKIP_HOSTS = ("facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "youtube.com", "pinterest.com", "reddit.com", "linkedin.com", "threads.net", "bsky.app", "snapchat.com", "discord.com",
              "whatsapp.com", "t.me", "tumblr.com", "mastodon.social")      # social pages are read by the collectors, not as story-bank sources
EXTRACT_SYSTEM = ("You extract checkable facts about ONE brand from the text of a web page. Reply with JSON only: "
                  '{"facts": [{"fact": "<one plain sentence>", "quote": "<the EXACT sentence copied from the page that supports it>", "category": "award|press|ranking|figure|customer|origin|health|other"}]}. '
                  "Only facts about the named brand. Never invent, round, merge or paraphrase a number. If the page says nothing checkable about the brand, reply {\"facts\": []}.")
RELATION_SYSTEM = ('Two statements about the same brand. Reply with JSON only: {"relation": "same|contradicts|unrelated"}. same = they state the same fact (wording or rounding may differ). contradicts = they cannot both be true '
                   "(for example two different values for the same thing: a year, a price, a count, a ranking). unrelated = they are about different things, even if both contain numbers. When in doubt, answer unrelated.")
REVIEW_SYSTEM = ('You are a strict reviewer. For each item you get a fact about a brand and the exact quotation it rests on. Reply with JSON only: {"verdicts": [{"i": <number>, "supported": true|false, "about_brand": true|false}]}. '
                 "supported is true only if the quotation, read on its own, clearly states the fact (same numbers, same names). about_brand is true only if the quotation is about the named brand, not about another company.")


def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%#.' ]+", " ", (text or "").lower())).strip()


def page_text(html, limit=30000):
    """The readable text of a page (scripts and styles dropped, tags removed), cut at `limit` characters."""
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def quote_in_page(quote, text):
    """True only when the quotation really is in the page text (compared without case, punctuation spacing or line breaks). A quotation shorter than 25 characters is not accepted: it proves nothing."""
    q = _norm(quote)
    return len(q) >= 25 and q in _norm(text)


def watch_queries(name, seed_titles, limit):
    """The searches of one refresh: the standing questions about the brand, then one per Gap on its watchlist, at most `limit`."""
    out = [q.format(name=name) for q in BASE_QUERIES]
    for title in seed_titles:
        key = C.claim_key(title)
        if key and len(key.split()) <= 8:
            out.append(f"{name} {key}")
    seen, unique = set(), []
    for q in out:
        if q.lower() not in seen:
            seen.add(q.lower())
            unique.append(q)
    return unique[:limit]


def _numbers(text):
    return set(re.findall(r"\$\s?\d[\d,.]*\s?[kmb]?|\d[\d,.]*\s?(?:%|million|billion|k\b)|\b\d{4,}\b", (text or "").lower()))


def _content_words(text, name, gate):
    brand = set(C._words(name))
    return {w for w in C._words(text) if w not in brand and w not in set(gate["generic_words"]) and len(w) >= 3}


def related(reg, name, fact, category=""):
    """The claims in the ledger that are about the same THING as the fact: they share at least two content words with it (the brand's own name and common words do not count), or a distinctive word of the
    claim's name. At most three, the closest first."""
    gate = reg.gate_settings
    mine = _content_words(fact, name, gate)
    scored = []
    for claim in reg.state()["claims"].values():
        theirs = _content_words(claim["text"] + " " + (claim.get("reason") or ""), name, gate)
        shared = mine & theirs
        anchors = mine & {w for w in C._anchors(claim, gate) if w not in set(C._words(name))}
        if len(shared) >= 2 or anchors:
            scored.append((len(shared) + 2 * len(anchors), claim))
    scored.sort(key=lambda x: -x[0])
    chosen = [c for _score, c in scored[:3]]
    if len(chosen) < 3 and _numbers(fact):                                             # a figure may be about the same thing in other words (sold / sales): the same kind of claim with numbers goes to the reviewer too
        for claim in reg.state()["claims"].values():
            if len(chosen) >= 3:
                break
            if claim not in chosen and claim["category"] == category and _numbers(claim["text"] + " " + (claim.get("reason") or "")):
                chosen.append(claim)
    return chosen


def compare(reg, fact, category, *, name="", claude=None, report=None):
    """('duplicate'|'conflict'|'new', [existing claim ids]). A duplicate is decided without any model: almost the same words and the same numbers. A conflict needs the claims to be about the same thing AND a
    reviewer to say the two statements cannot both be true: two facts that both contain numbers are NOT a conflict (a rating and a return window are different things). Without a reviewer nothing is called
    a conflict, so the owner is never asked to decide something that is not one."""
    words = C._words(fact)
    ids = []
    for claim in related(reg, name, fact, category):
        mine, theirs = _numbers(fact), _numbers(claim["text"] + " " + (claim.get("reason") or ""))
        if C._share(words, claim) >= 0.8 and (not mine or mine <= theirs):
            return "duplicate", [claim["id"]]
        if claude is None:
            continue
        try:
            if report is not None:
                report["calls"] += 1
            relation = (claude.json_task(RELATION_SYSTEM, f"Brand: {name}\nA (already in the ledger): {claim['text']} {claim.get('reason') or ''}\nB (new): {fact}", 200) or {}).get("relation")
        except Exception as exc:                                                       # noqa: BLE001 - no answer: not a conflict, and it is named
            if report is not None:
                report["errors"] += 1
                report["notes"].append(f"could not compare with {claim['id']} ({type(exc).__name__}): treated as unrelated")
            continue
        if relation == "same":
            return "duplicate", [claim["id"]]
        if relation == "contradicts":
            ids.append(claim["id"])
    return ("conflict", ids) if ids else ("new", [])


def _short(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def run(reg, name, seed_titles, *, search, get, claude, now, max_queries=12, max_pages=12, max_new=20, today=None):
    """One refresh for one brand. Returns a report dict. `search(query)` -> [{url,title}], `get(url)` -> page html or None, `claude.json_task(system, user)` -> dict.
    Everything it writes is appended to the registry; nothing is ever removed or changed. `errors` counts every search, fetch, extraction, review or comparison that failed, so a refresh in which nothing
    worked can be told from one that found nothing."""
    today = today or now[:10]
    seen = reg.refresh_seen()
    report = {"queries": 0, "pages": 0, "pages_ok": 0, "facts_seen": 0, "added": 0, "conflicts": 0, "duplicates": 0, "rejected": [], "calls": 0, "errors": 0, "notes": []}
    candidates, urls_this_run = [], set()
    for query in watch_queries(name, seed_titles, max_queries):
        report["queries"] += 1
        try:
            results = search(query) or []
        except Exception as exc:                                                       # noqa: BLE001 - one failed search never stops the refresh
            report["errors"] += 1
            report["notes"].append(f"search '{query}' failed ({type(exc).__name__})")
            continue
        for r in results[:8]:
            url = (r.get("url") or "").strip()
            host = re.sub(r"^https?://(www\.)?", "", url.lower()).split("/")[0]
            if not url.startswith("http") or url in seen or url in urls_this_run or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
                continue
            urls_this_run.add(url)
            candidates.append((url, r.get("title") or ""))
    capped = False
    for url, title in candidates[:max_pages]:
        if capped:
            break                                                                                   # no more pages are fetched, extracted or reviewed: no more AI calls are spent
        try:
            html = get(url)
        except Exception as exc:                                                       # noqa: BLE001
            report["errors"] += 1
            report["notes"].append(f"{url}: could not be fetched ({type(exc).__name__})")
            continue
        report["pages"] += 1
        record = True
        try:
            text = page_text(html)
            if len(text) < 200 or _norm(name) not in _norm(text):
                report["rejected"].append(f"{url}: the page does not mention {name}")
                report["pages_ok"] += 1
                continue
            try:
                report["calls"] += 1
                facts = (claude.json_task(EXTRACT_SYSTEM, f"Brand: {name}\nPage address: {url}\nPage text:\n{text}", 1500) or {}).get("facts") or []
            except Exception as exc:                                                   # noqa: BLE001 - an AI failure is named, never fatal
                report["errors"] += 1
                report["notes"].append(f"{url}: the extraction failed ({_why(exc)})")
                continue
            checked = []
            for f in facts[:10] if isinstance(facts, list) else []:
                report["facts_seen"] += 1
                fact, quote = str((f or {}).get("fact") or "").strip(), str((f or {}).get("quote") or "").strip()
                cat = (f or {}).get("category") if (f or {}).get("category") in C.CATEGORIES else "other"
                if not fact or not quote:
                    report["rejected"].append(f"{url}: a fact without a quotation was dropped")
                elif not quote_in_page(quote, text):
                    report["rejected"].append(f"{url}: the quotation is not in the page, so \"{fact[:60]}\" was dropped")
                elif _norm(name) not in _norm(quote) and _norm(name) not in _norm(fact):
                    report["rejected"].append(f"{url}: neither the fact nor its quotation names {name}: \"{fact[:60]}\" was dropped")
                else:
                    checked.append((fact, quote, cat))
            if not checked:
                report["pages_ok"] += 1
                continue
            try:
                report["calls"] += 1
                items = "\n".join(f"{i}. FACT: {fact}\n   QUOTATION: {quote}" for i, (fact, quote, _c) in enumerate(checked))
                verdicts = (claude.json_task(REVIEW_SYSTEM, f"Brand: {name}\nSource: {url}\n{items}", 800) or {}).get("verdicts") or []
            except Exception as exc:                                                   # noqa: BLE001 - no reviewer, no entry
                report["errors"] += 1
                report["notes"].append(f"{url}: the reviewer could not be asked ({_why(exc)}), so nothing from this page was added")
                continue
            report["pages_ok"] += 1
            ok = {v.get("i") for v in verdicts if isinstance(v, dict) and v.get("supported") is True and v.get("about_brand") is True}
            for i, (fact, quote, cat) in enumerate(checked):
                if i not in ok:
                    report["rejected"].append(f"{url}: the reviewer did not confirm \"{fact[:60]}\"")
                    continue
                if report["added"] + report["conflicts"] >= max_new:
                    capped, record = True, False                                         # this page was not finished: it is NOT marked as read, so the next refresh takes it again
                    break
                kind, existing = compare(reg, fact, cat, name=name, claude=claude, report=report)
                reason = f"read {today}: \"{_short(quote, 1000)}\""
                if kind == "duplicate":
                    report["duplicates"] += 1
                elif kind == "conflict":
                    reg.add_conflict(_short(fact, 400), cat, url, reason + " (cannot both be true with " + ", ".join(existing) + ")", existing, now)
                    report["conflicts"] += 1
                else:
                    reg.add_claim(_short(fact, 400), cat, "unverified", url, reason, C.TOOL, now)
                    report["added"] += 1
        except BaseException:
            record = False                                                             # a page that crashed the run half-way is NOT marked as read; the pages handled before it are
            raise
        finally:
            if record:
                reg.record_sources([url], now)                                             # recorded as soon as the page was handled: an exception later cannot make the next refresh read it again
    if capped:
        report["notes"].append(f"the limit of {max_new} new entries for one refresh was reached: the rest (the facts not yet added and the pages not yet read) is left for the next refresh, and no more AI calls were spent")
    return report


def _why(exc):
    """The kind of error and its message, short and with anything that looks like a key removed."""
    text = re.sub(r"(?i)(bearer\s+|key[=: ]+|token[=: ]+)[A-Za-z0-9._\-]{12,}", r"\1[hidden]", str(exc))
    return f"{type(exc).__name__}: {text[:240]}"
