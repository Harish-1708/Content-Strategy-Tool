"""The two-tier filter for discovery seeds (the trending lane of `voice --mode discover`, which feeds the seed suggestions).

Tier 1, priority: an item that shares a word (four letters or more) with the brand's OWN terms (its name, research keywords, topics, product terms, competitors: all read from the brand's own files at run time) is kept at once. No model is asked.
Tier 2: every other item is judged by a pool model through its own ranked lane (`seed_judge_json`, the lane's chain is the ranked list with backups behind the first choice): could this topic help the brand's marketing? Yes -> it joins the
list, marked 'judged'; no -> it is dropped. There is no word list anywhere in this file: the brand's terms are the only brand knowledge the judge is given.

It is built to share the pool, not drain it. The judge is asked at most CALLS_PER_RUN times a run (one batch of up to ITEMS_PER_CALL topics each, a few hundred tokens), every answer is remembered for CACHE_DAYS days so
a topic is never judged twice, a month's calls are counted and capped, and there is NO fallback to Claude for this job: when the pool has no answer the items stay unjudged (dropped for this run, never assumed useful, never cached, asked
again next time). The router's own pacing, rests and shared usage ledger apply to the call exactly as they do to every other feature.
"""
CALLS_PER_RUN = 2
ITEMS_PER_CALL = 12
CACHE_DAYS = 30
CACHE_MAX = 300
MONTH_CALL_CAP = 60
COUNTER = "seed_judge_calls"

SYSTEM = ("You help a marketing team decide which trending topics could be useful to ONE brand. For each numbered topic say whether a marketer for this brand could turn it into content, an angle or a search idea "
          "that reaches the brand's audience. Say yes only when there is a real connection to what the brand sells, who it sells to or the interests of that audience; say no for unrelated news, politics, "
          "technology or sport with no link to the brand. Answer ONLY with JSON: {\"verdicts\": [{\"i\": 0, \"could_help\": true or false}]} with one entry per topic and no other text.")


def key_of(item):
    return " ".join(str(item.get("title") or item.get("topic") or "").lower().split())[:160]


def brand_context(slug, terms):
    """What the judge is told about the brand: only what the brand's own files say (its terms)."""
    seen, out = set(), []
    for t in terms:
        t = " ".join(str(t).split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return f"Brand: {slug}. Its own terms (what it is called, sells and researches): " + "; ".join(out[:24])


def _days(a, b):
    from datetime import datetime
    try:
        return (datetime.strptime(str(b)[:10], "%Y-%m-%d") - datetime.strptime(str(a)[:10], "%Y-%m-%d")).days
    except ValueError:
        return 10 ** 6


def load_cache(store, slug, now):
    data = store.read_extra(slug, "seed_judgments") or {}
    return {k: v for k, v in (data.get("verdicts") or {}).items() if isinstance(v, dict) and "yes" in v and _days(v.get("at", ""), now) <= CACHE_DAYS}


def save_cache(store, slug, cache):
    keep = dict(sorted(cache.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:CACHE_MAX])
    store.write_extra(slug, "seed_judgments", {"note": "what the pool judge said about each discovered topic (could it help this brand), kept 30 days so a topic is never judged twice", "verdicts": keep})


def parse(out, n):
    """{index: True|False} for the topics the answer covers; a topic it does not cover, or covers unclearly, is absent (never assumed yes)."""
    verdicts = {}
    rows = out.get("verdicts") if isinstance(out, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("i"), int) and 0 <= row["i"] < n and isinstance(row.get("could_help"), bool):
            verdicts[row["i"]] = row["could_help"]
    return verdicts


def judge_batch(judge, context, items):
    """One call: {index: bool} for the items (may be partial). Raises whatever the client raises."""
    lines = [f"{i}. {str(it.get('title') or it.get('topic') or '')[:140]}" for i, it in enumerate(items)]
    return parse(judge.json_task(SYSTEM, context + "\n\nTopics:\n" + "\n".join(lines), 600), len(items))


def run(slug, items, terms, lane_ok, judge_for, cache, now, *, calls_left, usage=None):
    """Split discovered items that are not about the brand into the two tiers.
    lane_ok(item, terms) -> bool is the shared-word test (tier 1). judge_for() -> a client or None, called only when tier 2 has something left to ask. `calls_left` is a one-item list shared by every topic of a run, so the per-run
    limit holds across topics; `usage` is the research ledger (month counters). `cache` is updated in place with the new verdicts.
    Returns {'priority': [...], 'judged': [...], 'dropped': n, 'unjudged': n, 'cached': n, 'calls': n, 'why': ''}."""
    out = {"priority": [], "judged": [], "dropped": 0, "unjudged": 0, "cached": 0, "calls": 0, "why": ""}
    rest = []
    for it in items:
        (out["priority"] if lane_ok(it, terms) else rest).append(it)
    waiting = []
    for it in rest:
        known = cache.get(key_of(it))
        if known is None:
            waiting.append(it)
            continue
        out["cached"] += 1
        if known["yes"]:
            out["judged"].append(it)
        else:
            out["dropped"] += 1
    if not waiting:
        return out
    if usage is not None and usage.month_used(COUNTER) >= MONTH_CALL_CAP:
        out["unjudged"], out["why"] = len(waiting), f"this month's limit of {MONTH_CALL_CAP} judge calls was used"
        return out
    if calls_left[0] <= 0:
        out["unjudged"], out["why"] = len(waiting), f"this run's limit of {CALLS_PER_RUN} judge call(s) was used"
        return out
    judge = judge_for()
    if judge is None:
        out["unjudged"], out["why"] = len(waiting), "no pool model is available to judge them"
        return out
    context = brand_context(slug, terms)
    for start in range(0, len(waiting), ITEMS_PER_CALL):
        if calls_left[0] <= 0 or (usage is not None and usage.month_used(COUNTER) >= MONTH_CALL_CAP):
            out["unjudged"] += len(waiting) - start
            out["why"] = f"this run's limit of {CALLS_PER_RUN} judge call(s) (or the month's limit of {MONTH_CALL_CAP}) was used"
            break
        chunk = waiting[start:start + ITEMS_PER_CALL]
        calls_left[0] -= 1
        out["calls"] += 1
        if usage is not None:
            usage.charge(COUNTER, 1)
        try:
            verdicts = judge_batch(judge, context, chunk)
        except Exception as exc:                                                            # noqa: BLE001
            out["unjudged"] += len(waiting) - start
            out["why"] = f"the pool had no answer ({type(exc).__name__}: {str(exc)[:100]})"
            break
        for i, it in enumerate(chunk):
            if i not in verdicts:
                out["unjudged"] += 1
                continue
            cache[key_of(it)] = {"yes": verdicts[i], "at": str(now)[:10]}
            if verdicts[i]:
                out["judged"].append(it)
            else:
                out["dropped"] += 1
    return out
