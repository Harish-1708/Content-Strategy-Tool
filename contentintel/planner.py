"""Request mode: type an idea, Claude plans which tools to use, you confirm or change it, then it runs.

    1. PLAN   You write what you want ("Father's Day gifting ideas for Swoveralls") and choose the tools yourself or leave
              it to Claude. Claude reads the request and the brand's catalogue and proposes a plan: the Meta ad searches,
              the Instagram and TikTok hashtags, the web searches (Serper.dev, Tavily and SerpApi, always as an extra beside
              the collectors), and any Deep Research questions, plus the creator and influencer angles to look for, and what it
              will cost. Nothing is searched yet. The plan is saved as RQ-0001 and shown in the Requests tab.
    2. RUN    You run the request. Without changes it runs as planned. With changes ("drop TikTok, add a gift guide angle") Claude
              revises the plan first. The run collects, searches, then puts what it found in the Idea Bank (scored, with its
              files in Drive) for a human to review.

Claude only plans. The plan is checked and trimmed by code (allowed tools only, limits on how many searches and hashtags), and the
tools themselves are the ones the rest of the system already trusts.
"""
import json
import math
import re

from .providers import ProviderError

TOOLS = ("meta_ads", "instagram", "tiktok", "web_search", "deep_research")
COLLECT_TOOLS = ("meta_ads", "instagram", "tiktok", "deep_research")
CREATOR_SITES = ("tiktok.com", "instagram.com")
STATUSES = ("planned", "running", "done", "partial", "failed")


class PlanError(Exception):
    pass


# The kinds of content a request can ask for. The first words in each list are what a person may type; all are read case-insensitively.
FORMATS = {
    "ugc": "UGC: real customers and creators on camera or in photos (reviews, hauls, try-ons, unboxings, testimonials)",
    "ai": "AI-made or AI-assisted content (AI images, AI video ads, AI avatars and voices)",
    "influencer": "influencer and creator partnerships",
    "static": "static posts and single-image ads",
    "carousel": "carousels and slide posts",
    "video": "short-form video: Reels, TikToks and video ads",
    "meme": "memes, humour and trend formats",
}
FORMAT_WORDS = {
    "ugc": "ugc", "user generated": "ugc", "user generated content": "ugc", "customer content": "ugc", "customer videos": "ugc", "testimonial": "ugc",
    "testimonials": "ugc", "reviews": "ugc", "unboxing": "ugc", "hauls": "ugc",
    "ai": "ai", "ai content": "ai", "ai generated": "ai", "ai made": "ai", "ai ads": "ai", "ai video": "ai", "ai images": "ai", "ai influencer": "ai",
    "influencer": "influencer", "influencers": "influencer", "creator": "influencer", "creators": "influencer", "partnerships": "influencer",
    "static": "static", "static post": "static", "static posts": "static", "post": "static", "posts": "static", "image": "static", "images": "static",
    "image ads": "static", "photo": "static", "photos": "static",
    "carousel": "carousel", "carousels": "carousel", "slides": "carousel", "slideshow": "carousel",
    "video": "video", "videos": "video", "short video": "video", "short videos": "video", "reel": "video", "reels": "video", "video ads": "video",
    "meme": "meme", "memes": "meme", "humour": "meme", "humor": "meme", "trends": "meme",
}
# Searches that go looking for each format, added to the web searches of a request that asks for it.
FORMAT_QUERIES = {
    "ugc": ["UGC ad examples apparel brand", "UGC creator try-on haul loungewear"],
    "ai": ["AI generated ad examples fashion brand", "AI influencer clothing brand campaign"],
    "influencer": ["influencer partnership campaign loungewear brand", "creator collaboration clothing brand ad"],
    "static": ["static image ad examples apparel brand", "best performing static social posts fashion brand"],
    "carousel": ["carousel ad examples fashion brand", "carousel post ideas clothing brand"],
    "video": ["short video ad examples loungewear brand", "best Reels ideas clothing brand"],
    "meme": ["meme marketing examples apparel brand", "funny relatable social posts clothing brand"],
}


# A phrase that contains one of these words (and no other format's word) means that format: "AI generated ads" is AI content.
FORMAT_MARKERS = {"ai": "ai", "ugc": "ugc", "carousel": "carousel", "carousels": "carousel", "meme": "meme", "memes": "meme", "reel": "video", "reels": "video",
                  "influencer": "influencer", "influencers": "influencer"}


def parse_formats(value):
    """The formats a person asked for, as a list of the names in FORMATS (in the order given, no repeats). Takes text such as
    'UGC, AI content and posts' or a list. An unknown word is an error that lists what is understood."""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    out = []
    for word in re.split(r"[,;/\n]|\band\b|&", str(value or "").lower()):
        word = " ".join(re.sub(r"[^a-z0-9 -]+", " ", word).replace("-", " ").split())
        if not word:
            continue
        name = FORMAT_WORDS.get(word) or (word if word in FORMATS else "")
        if not name:
            found = {FORMAT_MARKERS[w] for w in word.split() if w in FORMAT_MARKERS}
            name = found.pop() if len(found) == 1 else ""
        if not name:
            raise PlanError(f"'{word}' is not a content format I know. Use any of: " + "; ".join(f"{k} ({v})" for k, v in FORMATS.items()))
        if name not in out:
            out.append(name)
    return out


PLAN_SYSTEM = (
    "You plan content research for a marketing agency. Read the request and the brand, then propose which tools to use and what "
    "to search. Only use the tools listed as allowed. Reply with JSON only, in this shape: {\"interpretation\": \"one sentence on what "
    "the request really asks for\", \"occasion\": \"the occasion or theme, or empty\", \"audience\": \"who the content is for\", "
    "\"goal\": \"what the brief should achieve\", \"tools\": {\"meta_ads\": {\"why\": \"...\", \"keywords\": [\"search phrases for "
    "competitor and similar-brand ads\"]}, \"instagram\": {\"why\": \"...\", \"hashtags\": [\"no # sign\"]}, \"tiktok\": {\"why\": "
    "\"...\", \"hashtags\": [\"no # sign\"]}, \"web_search\": {\"why\": \"...\", \"queries\": [\"web search phrases\"]}, "
    "\"deep_research\": {\"why\": \"...\", \"questions\": [\"one broad question\"]}}, \"creator_angles\": [\"short phrases describing "
    "content that creators and influencers (not brands) post about this, for example 'what I bought my dad for Father's Day haul'\"], "
    "\"notes\": \"anything the person should know or decide\"}. Include only the tools that will help. Look for ideas well beyond the "
    "brand's direct competitors: other brands in adjacent categories, creators, influencers, gift guides, trends. Use the requested "
    "region only to choose wording and sources; never put a country code in a search phrase. Keep phrases short and concrete. Do not "
    "invent facts about the brand. If the request lists formats wanted, shape the keywords, hashtags and creator angles to surface that kind "
    "of content: for UGC, hauls, try-ons, reviews and get-ready-with-me posts; for AI content, AI-generated ads, AI influencers and AI video; "
    "for influencer content, creator partnerships and gifted posts; and so on.")


# ---------------------------------------------------------------------------------------------------------------------
# turning Claude's plan into a safe one
# ---------------------------------------------------------------------------------------------------------------------

def _clean_list(values, limit, transform=lambda s: s, maximum_chars=120):
    out = []
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str):
            continue
        text = transform(" ".join(value.split())[:maximum_chars])
        if text and text.lower() not in (o.lower() for o in out):
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _hashtag(text):
    return re.sub(r"[^\w]", "", text.lstrip("#").lower())


def creator_queries(angles, limit, sites=CREATOR_SITES):
    """Web searches aimed at what creators and influencers post: each angle on each of the given sites (the sites of the platforms the plan
    uses, so a dropped TikTok is not searched for). With no sites, each angle is searched on its own."""
    out = []
    for angle in angles:
        for site in sites or (None,):
            if len(out) >= limit:
                return out
            out.append(f"{angle} site:{site}" if site else angle)
    return out


def format_queries(formats, limit):
    """One search for each wanted format, then a second for each, and so on, up to the limit."""
    pools = [list(FORMAT_QUERIES.get(f, [])) for f in formats]
    out = []
    for rank in range(max((len(p) for p in pools), default=0)):
        for pool in pools:
            if rank < len(pool) and len(out) < limit:
                out.append(pool[rank])
    return out


def clean_plan(raw, allowed, settings, request_text="", formats=()):
    """A plan the system can run, and notes on anything that was dropped or added. Raises PlanError if nothing usable is left."""
    if not isinstance(raw, dict) or not isinstance(raw.get("tools"), dict):
        raise PlanError("Claude's plan was not in the expected shape")
    notes, tools = [], {}
    given = raw["tools"]
    for name in given:
        if name not in TOOLS:
            notes.append(f"left out '{name}': not a tool this system has")
        elif name not in allowed:
            notes.append(f"left out {name}: you did not allow it")
    angles = _clean_list(raw.get("creator_angles"), 4, maximum_chars=80)

    def why(name):
        entry = given.get(name)
        return " ".join(str((entry or {}).get("why", "")).split())[:200] if isinstance(entry, dict) else ""

    for name in ("meta_ads",):
        if name in allowed and isinstance(given.get(name), dict):
            keywords = _clean_list(given[name].get("keywords"), settings.plan_max_keywords)
            pages = _clean_list(given[name].get("pages"), settings.plan_max_pages, maximum_chars=300)
            pages = [p for p in pages if p.startswith("https://www.facebook.com/ads/library/")]
            if keywords or pages:
                tools[name] = {"why": why(name), "keywords": keywords, "pages": pages}
    for name in ("instagram", "tiktok"):
        if name in allowed and isinstance(given.get(name), dict):
            tags = _clean_list(given[name].get("hashtags"), settings.plan_max_hashtags, _hashtag, maximum_chars=40)
            if tags:
                tools[name] = {"why": why(name), "hashtags": tags}
    if "web_search" in allowed:                      # the web searches always run beside the collectors: they are the extra
        own = given.get("web_search") if isinstance(given.get("web_search"), dict) else {}
        sites = tuple(site for name, site in (("tiktok", "tiktok.com"), ("instagram", "instagram.com")) if name in tools)
        creators = creator_queries(angles, max(0, settings.plan_max_queries // 2), sites)
        room = max(0, settings.plan_max_queries - len(creators) - 1)                 # always leave room for one of Claude's own searches
        wanted = format_queries(formats, min(max(len(formats), settings.plan_max_queries // 4), room))
        base = _clean_list(own.get("queries"), settings.plan_max_queries)
        queries = list(dict.fromkeys(creators + wanted + [q for q in base if q not in creators]))[:settings.plan_max_queries]
        if not queries and request_text.strip():
            queries = [" ".join(request_text.split())[:120]]
            notes.append("added the request itself as a web search, because the plan had none")
        if queries:
            tools["web_search"] = {"why": why("web_search"), "queries": queries}
    if "deep_research" in allowed and isinstance(given.get("deep_research"), dict):
        questions = _clean_list(given["deep_research"].get("questions"), settings.plan_max_questions, maximum_chars=400)
        if questions:
            tools["deep_research"] = {"why": why("deep_research"), "questions": questions}
    if not tools:
        raise PlanError("the plan has no usable tool: check which tools you allowed, or describe the request more fully")
    plan = {"interpretation": " ".join(str(raw.get("interpretation", "")).split())[:300],
            "occasion": " ".join(str(raw.get("occasion", "")).split())[:80],
            "audience": " ".join(str(raw.get("audience", "")).split())[:200],
            "goal": " ".join(str(raw.get("goal", "")).split())[:300],
            "creator_angles": angles, "formats": list(formats), "tools": tools,
            "notes": " ".join(str(raw.get("notes", "")).split())[:400], "dropped": notes}
    return plan, notes


def plan_summary(plan):
    """One line: what will be searched."""
    tools = (plan or {}).get("tools") or {}
    parts = []
    if (plan or {}).get("formats"):
        parts.append("formats: " + ", ".join(plan["formats"]))
    if "meta_ads" in tools:
        parts.append("ads: " + ", ".join(tools["meta_ads"].get("keywords", [])[:4]))
    for name in ("instagram", "tiktok"):
        if name in tools:
            parts.append(f"{name}: " + ", ".join("#" + t for t in tools[name]["hashtags"][:4]))
    if "web_search" in tools:
        parts.append(f"web: {len(tools['web_search']['queries'])} searches")
    if "deep_research" in tools:
        parts.append(f"deep research: {len(tools['deep_research']['questions'])}")
    return " | ".join(parts)


def estimate(plan, settings, collect_settings=None):
    """What the plan will use, in the units the caps are in."""
    tools = plan["tools"]
    pages = len(tools.get("meta_ads", {}).get("keywords", [])) + len(tools.get("meta_ads", {}).get("pages", []))
    pages += sum(len(tools[t]["hashtags"]) for t in ("instagram", "tiktok") if t in tools)
    scoring = min(settings.max_scoring_calls, math.ceil(settings.max_new_per_run / settings.score_batch))
    return {"web_searches": len(tools.get("web_search", {}).get("queries", [])), "browser_pages": pages,
            "deep_research_reports": len(tools.get("deep_research", {}).get("questions", [])),
            "claude_calls_at_most": 1 + scoring + len(tools.get("deep_research", {}).get("questions", [])) * 2,
            "new_items_at_most": settings.max_new_per_run}


def render_plan(request, est=None):
    """The plan as plain text, for the log and the run summary."""
    plan = request["plan"]
    lines = [f"Request {request['id']} ({request['brand']}), region: {request['region']}", f"  You asked: {request['text']}",
             f"  Understood as: {plan['interpretation']}"]
    for key, label in (("occasion", "Occasion"), ("audience", "Audience"), ("goal", "Goal")):
        if plan.get(key):
            lines.append(f"  {label}: {plan[key]}")
    if request.get("formats"):
        lines.append("  Formats wanted: " + ", ".join(request["formats"]))
    for name, entry in plan["tools"].items():
        lines.append(f"  {name}: {entry.get('why', '')}".rstrip())
        for key in ("keywords", "hashtags", "pages", "queries", "questions"):
            for value in entry.get(key, []):
                lines.append(f"      {'#' if key == 'hashtags' else ''}{value}")
    if plan["creator_angles"]:
        lines.append("  Creator and influencer angles: " + "; ".join(plan["creator_angles"]))
    for note in plan["dropped"]:
        lines.append(f"  Note: {note}")
    if plan["notes"]:
        lines.append(f"  Claude's note: {plan['notes']}")
    if est:
        lines.append(f"  It will use at most: {est['web_searches']} web searches, {est['browser_pages']} browser pages, "
                     f"{est['deep_research_reports']} Deep Research report(s), about {est['claude_calls_at_most']} Claude calls, "
                     f"and add up to {est['new_items_at_most']} items to the Idea Bank.")
    return lines


# ---------------------------------------------------------------------------------------------------------------------
# planning and revising
# ---------------------------------------------------------------------------------------------------------------------

def resolve_allowed(mode, ticked):
    """The tools a request may use: all of them (auto), or just the ticked ones (manual)."""
    if mode not in ("auto", "manual"):
        raise PlanError(f"tools must be auto or manual, got {mode!r}")
    if mode == "auto":
        return list(TOOLS)
    chosen = [t for t in TOOLS if t in ticked]
    if not chosen:
        raise PlanError("you chose to pick the tools yourself but ticked none")
    return chosen


def _ask(request, context, previous=None, changes=""):
    lines = [f"Brand context: {context}", f"Allowed tools: {', '.join(request['allowed'])}",
             f"Region: {request['region']}", f"Request: {request['text']}"]
    if request.get("formats"):
        lines.append("Formats wanted: " + ", ".join(f"{f} = {FORMATS[f]}" for f in request["formats"]))
    if previous:
        lines += ["Current plan (JSON): " + json.dumps(previous, ensure_ascii=False),
                  f"Change it as the person asks, keeping what they did not mention: {changes}"]
    return "\n".join(lines)


def plan_request(store, brand, claude, budget, settings, *, text, mode, ticked, region, now, apify_extra=False, context="", formats=()):
    """Make and save a new request with Claude's plan. Returns the request."""
    text = " ".join(str(text or "").split())
    if len(text) < 8:
        raise PlanError("describe the request in a sentence or two")
    allowed = resolve_allowed(mode, ticked)
    wanted_formats = parse_formats(formats)
    if claude is None:
        raise PlanError("planning needs the ANTHROPIC_API_KEY secret")
    if not budget.take():
        raise PlanError(f"the monthly Claude limit ({budget.cap} calls) is reached")
    request = {"id": store.next_request_id(brand.slug), "brand": brand.slug, "text": text, "created": now, "status": "planned",
               "tools_mode": mode, "allowed": allowed, "region": region or "worldwide", "apify_extra": bool(apify_extra),
               "formats": wanted_formats, "changes": [], "run": {}}
    try:
        raw = claude.json_task(PLAN_SYSTEM, _ask(request, context), max_tokens=2500)
    except ProviderError as exc:
        raise PlanError(f"Claude could not make a plan: {exc}") from exc
    request["plan"], _ = clean_plan(raw, allowed, settings, text, wanted_formats)
    request["occasion"] = request["plan"]["occasion"]
    store.write_request(brand.slug, request)
    return request


def revise_request(store, brand, request, claude, budget, settings, *, changes, now, context=""):
    """Change a planned request as the person asked. Returns the request."""
    changes = " ".join(str(changes or "").split())
    if not changes:
        return request
    if request["status"] not in ("planned", "partial", "failed"):
        raise PlanError(f"{request['id']} is {request['status']}; a request can only be changed before it runs")
    if claude is None or not budget.take():
        raise PlanError("changing the plan needs Claude: the ANTHROPIC_API_KEY secret and some of this month's calls")
    try:
        raw = claude.json_task(PLAN_SYSTEM, _ask(request, context, previous=request["plan"], changes=changes), max_tokens=2500)
    except ProviderError as exc:
        raise PlanError(f"Claude could not change the plan: {exc}") from exc
    plan, _ = clean_plan(raw, request["allowed"], settings, request["text"], request.get("formats") or [])
    request["plan"] = plan
    request["occasion"] = plan["occasion"]
    request["changes"].append({"at": now, "text": changes})
    store.write_request(brand.slug, request)
    return request


# ---------------------------------------------------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------------------------------------------------

def collect_config(request):
    """The collect settings for a request: only what the plan asked for, and none of the always-on extras."""
    tools, cfg = request["plan"]["tools"], {"track_own_brand": False}
    ads = tools.get("meta_ads")
    if ads:
        cfg["ads"] = {"keywords": ads["keywords"], "pages": ads["pages"], "country": request["region"]}
    cfg["social"] = {name: {"hashtags": tools[name]["hashtags"]} for name in ("instagram", "tiktok") if name in tools}
    if "deep_research" in tools:
        cfg["deep_research"] = {"questions": tools["deep_research"]["questions"]}
    return cfg


def collect_sources(request):
    return [t for t in COLLECT_TOOLS if t in request["plan"]["tools"]]


def research_queries(request):
    return list(request["plan"]["tools"].get("web_search", {}).get("queries", []))


def web_sources_for(sources, queries):
    """The Research tab rows that were found by any of this request's searches."""
    wanted = {" ".join(q.lower().split()) for q in queries}
    out = {}
    for sid, row in sources.items():
        found = {" ".join(q.lower().split()) for q in str(row.get("queries", "")).split(" | ")}
        if found & wanted:
            out[sid] = row
    return out


def execute_request(store, brand, request, *, now, collect_runner, research_runner, ideabank_runner):
    """Run a planned request. The runners do the real work and return (lines): collect_runner(cfg, sources),
    research_runner(queries), ideabank_runner(request, queries) -> (new ids, lines). A failing step is recorded and the
    others still run, so a Meta hiccup does not lose the web searches."""
    if request["status"] == "running":
        raise PlanError(f"{request['id']} is already running")
    if request["status"] == "done":
        raise PlanError(f"{request['id']} has already run; make a new request to search again")
    request["status"], request["run"] = "running", {"started": now, "notes": [], "items_added": []}
    store.write_request(brand.slug, request)
    failures = 0
    steps = []
    sources = collect_sources(request)
    if sources:
        steps.append(("collect", lambda: collect_runner(collect_config(request), sources)))
    queries = research_queries(request)
    if queries:
        steps.append(("web search", lambda: research_runner(queries)))
    for label, step in steps:
        try:
            request["run"]["notes"] += [f"{label}: {line}" for line in (step() or [])][:6]
        except Exception as exc:                                  # a failed step must not stop the others or lose the plan
            failures += 1
            request["run"]["notes"].append(f"{label} failed: {str(exc)[:200] or exc.__class__.__name__}")
    try:
        new, lines = ideabank_runner(request, queries)
        request["run"]["items_added"] = list(new)
        request["run"]["notes"] += [f"idea bank: {line}" for line in lines][:6]
    except Exception as exc:
        failures += 1
        request["run"]["notes"].append(f"idea bank failed: {str(exc)[:200] or exc.__class__.__name__}")
    request["status"] = "failed" if failures and failures == len(steps) + 1 else "partial" if failures else "done"
    request["run"]["finished"] = now
    store.write_request(brand.slug, request)
    return request
