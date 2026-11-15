"""Free AI capacity worth adding to the pool, read from OmniRoute's catalogue as DATA (OmniRoute itself is never run).

OmniRoute (https://github.com/cheaper-inference/OmniRoute, MIT licence, copyright 2026 diegosouzapw) keeps a researched catalogue of free tiers: for every provider and model, the kind of free
allowance (recurring daily, monthly, uncapped, keyless, one-time, discontinued), the token budget, whether the provider hard-stops instead of billing, whether it may train on prompts, and its own verdict
on the provider's terms of service (ok, caution, ambiguous, avoid, unknown). The one data file (open-sse/config/freeModelCatalog.data.ts) is read from the connection `omniroute_catalog` (its checkout), or from the
snapshot kept in ai/omniroute-free-catalog.json when the repository cannot be reached. This module turns it into a list of candidates the owner can choose from; it changes nothing by itself: the owner approves
each provider (ai_providers.yaml), as always.

The catalogue has NO field that says a provider is "official": its terms verdict (ok, caution, ambiguous, unknown, avoid) is the only judgement it gives. So the platform does not claim to filter to official providers. It shows every candidate with that verdict, puts the ones the catalogue calls `ok` first, and leaves the owner to check the provider's own terms before approving.

Nothing is hidden. Providers the catalogue itself marks `avoid` (usually access that borrows a consumer subscription or reverse-engineers a web page) are listed under their own heading with that reason, not removed,
and the owner decides.
"""
import json
import re
from pathlib import Path

DATA_FILE = Path("open-sse") / "config" / "freeModelCatalog.data.ts"
SNAPSHOT = Path("ai") / "omniroute-free-catalog.json"
FREE_KINDS = ("keyless", "recurring-daily", "recurring-monthly", "recurring-uncapped", "recurring-credit")
_ENTRY = re.compile(r"\{[^{}]*?provider:\s*\"([^\"]+)\"[^{}]*?modelId:\s*\"([^\"]+)\"[^{}]*?\}", re.S)


def _field(block, name):
    m = re.search(name + r":\s*\"?([A-Za-z0-9_\-\.]+)\"?", block)
    return m.group(1) if m else ""


def parse(text):
    """The catalogue entries in a freeModelCatalog.data.ts text: provider, model, display name, monthly tokens, free type, terms verdict, hard stop, trains on prompts, region-gated."""
    out = []
    for m in _ENTRY.finditer(text or ""):
        block = m.group(0)
        tokens = re.search(r"monthlyTokens:\s*([0-9_]+)", block)
        name = re.search(r"displayName:\s*\"([^\"]+)\"", block)
        out.append({"provider": m.group(1), "model": m.group(2), "name": name.group(1) if name else m.group(2), "monthly_tokens": int(tokens.group(1).replace("_", "")) if tokens else 0,
                    "free_type": _field(block, "freeType"), "tos": _field(block, "tos") or "unknown", "hard_stop": "hardStopGuaranteed: true" in block,
                    "trains_on_prompts": "trainsOnPrompts: true" in block, "gated": "eligibilityGate" in block})
    return out


def read(checkout=None, snapshot=None):
    """(entries, where from). The repository's own file first when its checkout exists, then the snapshot kept in this repository."""
    if checkout and (Path(checkout) / DATA_FILE).is_file():
        entries = parse((Path(checkout) / DATA_FILE).read_text(encoding="utf-8", errors="replace"))
        if entries:
            return entries, "the OmniRoute checkout"
    snap = Path(snapshot) if snapshot else SNAPSHOT
    if snap.is_file():
        data = json.loads(snap.read_text(encoding="utf-8"))
        return data.get("entries", []), f"the snapshot taken {data.get('taken', '?')} from OmniRoute commit {str(data.get('commit', '?'))[:10]}"
    return [], "nothing (no checkout and no snapshot)"


def make_snapshot(entries, commit, taken):
    return {"source": "https://github.com/cheaper-inference/OmniRoute (open-sse/config/freeModelCatalog.data.ts, MIT)", "commit": commit, "taken": taken, "entries": entries}


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


# The platforms the gateway itself serves (FreeLLMAPI v0.16.17, server/src/providers/index.ts), kept here so the check works without the gateway source; gateway_platforms() reads the real list when it is present.
GATEWAY_PLATFORMS = ("agnes", "aihorde", "ainative", "aion", "aisense", "anyapi", "bai", "bazaarlink", "cerebras", "cloudflare", "cohere", "github", "google", "groq", "halx", "huggingface", "kilo", "llm7",
                     "longcat", "mistral", "modelscope", "nara", "navy", "nvidia", "ollama", "opencode", "openrouter", "orcarouter", "ovh", "pollinations", "qianfan", "radeon", "reka", "requesty", "routeway",
                     "sealion", "siliconflow", "unorouter", "volcengine", "xfyun", "xkiro", "zhipu")


def gateway_platforms(gateway_dir=None):
    """The gateway's own provider ids: read from its source when the directory is given and readable, else the list above. Never empty."""
    if gateway_dir:
        f = Path(gateway_dir) / "server" / "src" / "providers" / "index.ts"
        if f.is_file():
            text = f.read_text(encoding="utf-8", errors="replace")
            found = set(re.findall(r"platform:\s*'([a-z0-9_-]+)'", text)) | set(re.findall(r"Keyless\w*\(\s*'([a-z0-9_-]+)'", text))
            found -= {"fake", "fakefusion", "custom", "custom-endpoint", "invalid_platform"}
            if len(found) >= 20:
                return tuple(sorted(found | set(GATEWAY_PLATFORMS)))
    return GATEWAY_PLATFORMS


def pool_names(providers, gateway=None):
    """The names already in the pool: ai_providers.yaml 'providers' (ids and labels) AND every provider the gateway itself serves, normalised for comparison."""
    names = {_norm(g) for g in (gateway if gateway is not None else GATEWAY_PLATFORMS)}
    for pid, spec in (providers or {}).items():
        names.add(_norm(pid))
        names.add(_norm((spec or {}).get("label", "")))
        names.add(_norm(str((spec or {}).get("label", "")).split(" (")[0]))
    return {n for n in names if n}


_ALIASES = {"cloudflareai": "cloudflare", "gemini": "google", "kilogateway": "kilo", "apiairforce": "airforce", "glm": "zhipu", "glmcn": "zhipu", "ollamacloud": "ollama", "nvidianim": "nvidia", "githubmodels": "github", "agnescn": "agnes", "ovhcloud": "ovh", "baidu": "qianfan"}


def in_pool(provider, names):
    n = _norm(provider)
    return n in names or _ALIASES.get(n, "\0") in names


def candidates(entries, providers, gateway=None):
    """{'official-looking', 'avoid', 'already in the pool', 'one-time only', 'discontinued'}: providers grouped, each with its models, budget and flags. Recurring and keyless free tiers only are candidates."""
    names = pool_names(providers, gateway)
    by = {}
    for e in entries:
        by.setdefault(e["provider"], []).append(e)
    groups = {"recommended": [], "avoid": [], "in_pool": [], "one_time_only": [], "discontinued_only": []}
    for provider, rows in sorted(by.items()):
        free = [r for r in rows if r["free_type"] in FREE_KINDS]
        summary = {"provider": provider, "models": len(free) or len(rows), "monthly_tokens": sum(r["monthly_tokens"] for r in free), "kinds": sorted({r["free_type"] for r in free}),
                   "tos": sorted({r["tos"] for r in rows}), "hard_stop": all(r["hard_stop"] for r in free) if free else False, "trains_on_prompts": any(r["trains_on_prompts"] for r in rows),
                   "gated": any(r["gated"] for r in rows), "examples": [r["model"] for r in sorted(free or rows, key=lambda r: -r["monthly_tokens"])[:3]]}
        if in_pool(provider, names):
            groups["in_pool"].append(summary)
        elif not free:
            groups["discontinued_only" if all(r["free_type"] == "discontinued" for r in rows) else "one_time_only"].append(summary)
        elif "avoid" in summary["tos"]:
            groups["avoid"].append(summary)
        else:
            groups["recommended"].append(summary)
    for key in groups:
        groups[key].sort(key=lambda s: (-s["monthly_tokens"], s["provider"]))
    return groups


def render(groups, where):
    lines = [f"Free-tier candidates for the AI pool, read from {where}.",
             "The catalogue has no 'official provider' field; its terms verdict is the only judgement it gives, so each candidate shows it. Check a provider's own terms before approving it."]
    ok = [s for s in groups.get("recommended") or [] if s["tos"] == ["ok"]]
    other = [s for s in groups.get("recommended") or [] if s["tos"] != ["ok"]]
    groups = dict(groups, recommended=ok + other)
    labels = (("recommended", f"Candidates (recurring or keyless free tier; the catalogue's terms verdict is not 'avoid'): {len(ok)} with verdict ok first, then {len(other)} with caution, ambiguous or unknown"),
              ("avoid", "Listed, not recommended: the catalogue's own terms verdict is 'avoid' (the owner decides)"),
              ("in_pool", "Already in the pool"), ("one_time_only", "One-time credit only (not recurring)"), ("discontinued_only", "Discontinued"))
    for key, title in labels:
        rows = groups.get(key) or []
        lines.append(f"{title}: {len(rows)}" if key != "recommended" else title)
        if key in ("recommended", "avoid"):
            for s in rows:
                flags = [f"{s['monthly_tokens']:,} tokens a month" if s["monthly_tokens"] else "uncapped or keyless", f"terms: {'/'.join(s['tos'])}"]
                flags += (["hard stop, never bills"] if s["hard_stop"] else []) + (["may train on prompts"] if s["trains_on_prompts"] else []) + (["needs a regional identity check"] if s["gated"] else [])
                lines.append(f"  - {s['provider']}: {s['models']} model(s) such as {', '.join(s['examples'])}; " + "; ".join(flags))
    return lines
