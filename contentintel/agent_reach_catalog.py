"""Everything Agent-Reach declares, read from its own channel files (14 channels), with how this platform runs each action and why any is not connected.

This module holds data and small pure functions only, so the service registry, the commands, the documentation checks and other repositories can all read the same list.
`integration` says how an action is run here:
  opencli  Agent-Reach's own OpenCLI command in a logged-in browser on a machine you control (never a hosted runner)
  ytdlp    yt-dlp, no login (YouTube may bot-check data-centre addresses)
  github   GitHub's public REST API, no login (60 requests an hour without a token; GITHUB_TOKEN, if set, raises that)
  shared   the platform's own shared page readers / search providers (nothing is duplicated)
  none     not connected, with the reason in `why`
"""
from __future__ import annotations

import re

OPENCLI_NEEDS = "OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner"


def _a(params, argv=(), *, optional=(), defaults=None):
    return {"params": tuple(params), "argv": tuple(argv), "optional": tuple(optional), "defaults": dict(defaults or {})}


CHANNELS = {
    "instagram": {"level": "experimental", "integration": "opencli", "needs": OPENCLI_NEEDS, "site": "instagram",
                  "relevance": "high: competitor and creator accounts, posts and search (competitor collection, discovery, social discovery)",
                  "actions": {"search": _a(["query"], ["search", "{query}", "--limit", "{limit}"]), "profile": _a(["username"], ["profile", "{username}"]),
                              "posts": _a(["username"], ["user", "{username}", "--limit", "{limit}"])}},
    "reddit": {"level": "optional", "integration": "opencli", "needs": OPENCLI_NEEDS, "site": "reddit",
               "relevance": "high: what audiences say about a category or competitor (content research, discovery)",
               "actions": {"search": _a(["query"], ["search", "{query}", "--limit", "{limit}"]), "read": _a(["target"], ["read", "{target}", "--limit", "20"]),
                           "subreddit": _a(["name"], ["subreddit", "{name}", "--limit", "{limit}"])}},
    "twitter": {"level": "experimental", "integration": "opencli", "needs": OPENCLI_NEEDS, "site": "twitter",
                "relevance": "high: competitor and creator accounts on X, their posts and threads, and searches (social discovery, content research)",
                "actions": {"search": _a(["query"], ["search", "{query}", "--limit", "{limit}"]), "profile": _a(["username"], ["profile", "{username}"]),
                            "tweets": _a(["username"], ["tweets", "{username}", "--limit", "{limit}"]), "thread": _a(["target"], ["thread", "{target}"])}},
    "facebook": {"level": "experimental", "integration": "opencli", "needs": OPENCLI_NEEDS, "site": "facebook",
                 "relevance": "medium: competitor pages and profiles (social discovery)",
                 "actions": {"search": _a(["query"], ["search", "{query}", "--limit", "{limit}"]), "profile": _a(["username"], ["profile", "{username}"])}},
    "linkedin": {"level": "optional", "integration": "opencli", "needs": OPENCLI_NEEDS, "site": "linkedin",
                 "relevance": "medium: job postings are a hiring signal about a competitor (general research); the channel reads jobs only, not company pages or posts",
                 "actions": {"jobs": _a(["query"], ["search", "{query}", "--limit", "{limit}"], optional=[("--location", "{location}")]),
                             "read": _a(["target"], ["job-detail", "{target}"])}},
    "xiaohongshu": {"level": "optional", "integration": "opencli", "needs": OPENCLI_NEEDS, "site": "xiaohongshu",
                    "relevance": "low unless a brand sells to Chinese-language audiences: notes and search on a Chinese social platform",
                    "actions": {"search": _a(["query"], ["search", "{query}", "--limit", "{limit}"]), "read": _a(["target"], ["note", "{target}"]),
                                "comments": _a(["target"], ["comments", "{target}", "--limit", "{limit}", "--with-replies", "true"])}},
    "boss": {"level": "optional", "integration": "opencli", "needs": OPENCLI_NEEDS, "site": "boss",
             "relevance": "low: a Chinese job site (job search by city)",
             "actions": {"search": _a(["query"], ["search", "{query}", "--city", "{city}", "--limit", "{limit}"], defaults={"city": "全国"}),
                         "read": _a(["target"], ["detail", "{job_id}"])}},
    "youtube": {"level": "default", "integration": "ytdlp", "needs": "yt-dlp (installed by the workflow); no login; YouTube may bot-check data-centre addresses", "site": "",
                "relevance": "high: video search, pages and subtitles (YouTube research, channel discovery); the official Data API stays the main video collector",
                "actions": {"search": _a(["query"]), "read": _a(["target"]), "subtitle": _a(["target"])},
                "unconnected_actions": {"transcribe": "needs a Whisper speech model and ffmpeg (compute a hosted runner should not spend); subtitle already returns the video's own captions"}},
    "github": {"level": "default", "integration": "github", "needs": "nothing (public REST API: 60 requests an hour without a token; GITHUB_TOKEN raises it)", "site": "",
               "relevance": "medium: repositories, READMEs and issues for tool, library and competitor-technology research (general research)",
               "actions": {"search": _a(["query"]), "read": _a(["target"])}},
    "web": {"level": "default", "integration": "shared", "needs": "nothing extra: served by the platform's shared page readers and search providers", "site": "",
            "relevance": "high, and already covered: Agent-Reach's web channel is Jina Reader and Exa, which the platform has as shared services",
            "actions": {"read": _a(["target"])},
            "unconnected_actions": {"search": "use the shared search providers (the research command); Agent-Reach's web search is Exa, which is one of them"}},
    "bilibili": {"level": "default", "integration": "none", "needs": "the bili-cli program (or the bilibili-api package)", "site": "",
                 "relevance": "none for the brands served: a Chinese video platform",
                 "why": "it needs the separate bili-cli program that this platform does not install, and the platform's brands and languages have no use for it; add it as a service from services_extra.yaml if one ever does",
                 "actions": {}},
    "v2ex": {"level": "default", "integration": "none", "needs": "nothing (public API)", "site": "",
             "relevance": "none for the brands served: a Chinese-language developer forum",
             "why": "not connected because it has no use for the brands served; its public API needs no login, so it can be added later through services_extra.yaml",
             "actions": {}},
    "xueqiu": {"level": "optional", "integration": "none", "needs": "a Xueqiu web session token for most actions", "site": "",
               "relevance": "none for the brands served: Chinese stock quotes and investor posts",
               "why": "stock quotes and investor chatter on a Chinese exchange have no use for the brands served, and most actions need a site session token",
               "actions": {}},
    "xiaoyuzhou": {"level": "optional", "integration": "none", "needs": "ffmpeg and a Groq key for Whisper transcription", "site": "",
                   "relevance": "none for the brands served: Chinese podcasts",
                   "why": "it transcribes Chinese-podcast audio with Groq's Whisper, which needs a Groq key and ffmpeg and has no use for the brands served",
                   "actions": {}},
}

# Every action Agent-Reach itself declares for each channel, with its inputs, read from its channel files; a test compares this to those files, so the table in the documentation is complete.
DECLARED = {
    "bilibili": {"search": ("query", "limit"), "read": ("target",), "hot": ("limit",), "subtitle": ("target",)},
    "boss": {"search": ("query", "city", "limit"), "read": ("target",)},
    "facebook": {"search": ("query", "limit"), "profile": ("username",)},
    "github": {"search": ("query", "limit"), "read": ("target",)},
    "instagram": {"search": ("query", "limit"), "profile": ("username",), "posts": ("username", "limit")},
    "linkedin": {"jobs": ("query", "location", "limit"), "read": ("target",)},
    "reddit": {"search": ("query", "limit"), "read": ("target",), "subreddit": ("name", "limit")},
    "twitter": {"search": ("query", "limit"), "profile": ("username",), "tweets": ("username", "limit"), "thread": ("target",)},
    "v2ex": {"hot": ("limit",), "node": ("node_name", "limit"), "read": ("target",), "user": ("username",)},
    "web": {"search": ("query", "limit"), "read": ("target",)},
    "xiaohongshu": {"search": ("query", "limit"), "read": ("target",), "comments": ("target", "limit")},
    "xiaoyuzhou": {"transcribe": ("target",)},
    "xueqiu": {"quote": ("symbol",), "search": ("query", "limit"), "hot_posts": ("limit",), "hot_stocks": ("limit",)},
    "youtube": {"read": ("target",), "search": ("query", "limit"), "subtitle": ("target", "lang"), "transcribe": ("target",)},
}
RUNS_AS = {"opencli": "OpenCLI", "ytdlp": "yt-dlp", "github": "GitHub public API", "shared": "shared page readers"}


def action_rows():
    """One row per action Agent-Reach declares: (channel, action, inputs, connected 'Yes'/'No', how it runs here or '-', needs, why it is not connected or '-'). The documentation's table is this, and a test checks it."""
    rows = []
    for channel in sorted(DECLARED):
        spec = CHANNELS[channel]
        for action, params in DECLARED[channel].items():
            if action in spec["actions"]:
                rows.append((channel, action, ", ".join(params), "Yes", RUNS_AS[spec["integration"]], spec["needs"], "-"))
            else:
                why = spec.get("unconnected_actions", {}).get(action) or spec.get("why") or "not connected"
                rows.append((channel, action, ", ".join(params), "No", "-", spec["needs"], why))
    return rows


PURPOSE = {"instagram": "social_discovery", "twitter": "social_discovery", "facebook": "social_discovery", "xiaohongshu": "social_discovery", "youtube": "youtube_research"}


def integrated():
    """The channels the platform runs, in a stable order (the original three first)."""
    first = ("instagram", "reddit", "youtube")
    return first + tuple(n for n, c in CHANNELS.items() if c["integration"] != "none" and n not in first)


def actions_of(channel):
    return tuple(CHANNELS[channel]["actions"]) if channel in CHANNELS else ()


def every_action_name():
    return tuple(sorted({a for c in CHANNELS.values() for a in c["actions"]}))


def purpose_for(channel):
    return PURPOSE.get(channel, "general_research" if channel in ("github", "linkedin", "boss", "web") else "content_research")


def prepare(channel, action, values, limit=10):
    """Check the inputs for one action and return (argv for OpenCLI or None, cleaned values). Raises ValueError with a plain message for a bad input."""
    spec = CHANNELS.get(channel)
    if not spec or spec["integration"] == "none":
        raise ValueError(f"{channel!r} is not connected here" + (f": {spec['why']}" if spec else ""))
    act = spec["actions"].get(action)
    if act is None:
        why = spec.get("unconnected_actions", {}).get(action)
        raise ValueError(f"{channel} has no connected action {action!r}" + (f" ({why})" if why else "") + f"; the connected ones are {', '.join(spec['actions']) or 'none'}")
    clean = dict(act["defaults"])
    for name in act["params"]:
        raw = str(values.get(name, "") or "").strip()
        if not raw:
            raise ValueError(f"{channel} {action} needs {name}")
        clean[name] = raw
    for k, v in values.items():
        if k not in act["params"] and v not in (None, ""):                                    # extra inputs the caller gave win over the channel's defaults (a city, a location)
            clean[k] = str(v).strip()
    if "username" in clean:
        clean["username"] = clean["username"].lstrip("@")
    if channel == "reddit":
        if action == "read" and not (re.fullmatch(r"[A-Za-z0-9]+", clean["target"]) or re.search(r"(^|//|\.)reddit\.com/", clean["target"])):
            raise ValueError("reddit read: target must be a reddit.com post link or a post id")
        if action == "subreddit":
            clean["name"] = clean["name"].removeprefix("r/")
    if channel == "twitter" and action == "thread":
        clean["target"] = clean["target"].rstrip("/").split("/")[-1].split("?")[0]
    if channel == "linkedin" and action == "read" and re.fullmatch(r"\d+", clean["target"]):
        clean["target"] = f"https://www.linkedin.com/jobs/view/{clean['target']}/"
    if channel == "xiaohongshu" and action == "read" and "xiaohongshu.com" not in clean["target"] and "xhslink.com" not in clean["target"]:
        raise ValueError("xiaohongshu read: target must be a full Xiaohongshu note link including its xsec_token")
    if channel == "xiaohongshu" and action == "comments" and not re.match(r"https://([\w-]+\.)*xiaohongshu\.com/", clean["target"]):
        raise ValueError("xiaohongshu comments: target must be an https Xiaohongshu note link")
    if channel == "boss" and action == "read":
        found = re.search(r"zhipin\.com/.*?/job_detail/([A-Za-z0-9_-]+)\.html", clean["target"])
        if not found:
            raise ValueError("boss read: target must be a direct zhipin.com job link")
        clean["job_id"] = found.group(1)
    clean.setdefault("limit", str(int(limit)))
    if spec["integration"] != "opencli":
        return None, clean
    argv = []
    for token in act["argv"]:
        argv.append(token.format(**clean))
    for flag, template in act["optional"]:
        key = re.findall(r"{(\w+)}", template)[0]
        if clean.get(key):
            argv += [flag, template.format(**clean)]
    return argv, clean
