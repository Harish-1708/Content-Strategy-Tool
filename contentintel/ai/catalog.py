"""The providers' own model lists, read and compared with ours (`ai catalog`; Actions > AI gateway > catalog). READ-ONLY: nothing in ai_providers.yaml or ai_lanes.yaml is ever changed by it.

For each provider with a list address we know, it reports three things: which of OUR approved models the provider no longer lists (probably withdrawn: confirm with the live check before switching one off),
which models the provider lists that the pinned gateway CAN route and that we have not approved (candidates), and which listed models the pinned gateway does NOT know (they need a newer gateway pin, a
separate decision). A list that cannot be read (no key, an HTTP error, an answer that is not a list) is said so and never turned into a claim about our models. A key is never printed."""
import re

from . import clients as C
from . import gateway as gw

# provider id -> (address, how the key goes). 'none': the list is public. 'bearer': Authorization header. 'query': ?key= (never printed).
SOURCES = {
    "kilo": ("https://api.kilo.ai/api/gateway/models", "none"),
    "openrouter": ("https://openrouter.ai/api/v1/models", "none"),
    "nvidia": ("https://integrate.api.nvidia.com/v1/models", "bearer"),
    "groq": ("https://api.groq.com/openai/v1/models", "bearer"),
    "mistral": ("https://api.mistral.ai/v1/models", "bearer"),
    "cohere": ("https://api.cohere.com/v1/models?page_size=200", "bearer"),
    "ollama": ("https://ollama.com/api/tags", "bearer"),
    "zhipu": ("https://api.z.ai/api/paas/v4/models", "bearer"),
    "google": ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", "query"),
}
SHOWN = 8                                              # names listed per line before '(+N more)'


def normalise(model_id):
    """A model id as comparable text: lower case, no 'models/' prefix (Google), no ':cloud' or '-cloud' suffix (Ollama)."""
    text = str(model_id or "").strip().lower()
    text = re.sub(r"^models/", "", text)
    return re.sub(r"[:\-]cloud$", "", text)


def parse_models(parsed):
    """The model ids in a provider's answer: {'data': [...]}, {'models': [...]} or a plain list, each item a string or an object with id, name or model. [] when it is not a list."""
    items = parsed
    if isinstance(parsed, dict):
        items = parsed.get("data") if isinstance(parsed.get("data"), list) else parsed.get("models")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        value = it if isinstance(it, str) else (it.get("id") or it.get("name") or it.get("model") or it.get("modelId") or "") if isinstance(it, dict) else ""
        if value and str(value) not in out:
            out.append(str(value))
    return out


def fetch(provider, environ, http=C.default_http):
    """(ids, note): the provider's listed models, or [] and why not. Never raises, never shows a key."""
    spec = SOURCES.get(provider.id)
    if spec is None:
        return [], "no model-list address is known for this provider"
    url, auth = spec
    headers = {"Accept": "application/json", "User-Agent": "content-strategy-tool"}
    if auth != "none":
        key = next((str(environ.get(name, "")).strip() for name in provider.credential_env if str(environ.get(name, "")).strip()), "")
        if not key:
            return [], "no key in this run (" + ", ".join(provider.credential_env) + "), so its list was not read"
        if auth == "bearer":
            headers["Authorization"] = "Bearer " + key
        else:
            url += ("&" if "?" in url else "?") + "key=" + key
    try:
        status, parsed, _ = http("GET", url, headers=headers, body=None, timeout=25)
    except Exception as exc:                          # a network error: the text of it can carry the address (and the key in it), so only its type is shown
        return [], f"the list could not be fetched ({type(exc).__name__})"
    if status != 200:
        return [], f"the list answered HTTP {status}"
    ids = parse_models(parsed)
    return (ids, "") if ids else ([], "the answer was not a list of models")


def run(registry, environ, inventory, http=C.default_http):
    """[row] for every enabled provider except Claude: id, ok (the list was read), note, listed (count), approved_missing, candidates, unroutable."""
    rows = []
    for p in registry.providers.values():
        if not p.enabled or p.id == "claude":
            continue
        ids, note = fetch(p, environ, http)
        row = {"id": p.id, "ok": bool(ids), "note": note, "listed": len(ids), "approved_missing": [], "candidates": [], "known_dead": [], "unroutable": [], "free_unknown": []}
        if ids:
            live = {normalise(i): i for i in ids}
            known = {normalise(m) for m in inventory.get(p.gateway_platform, [])}
            approved = [m for m in p.models if m.approved]
            row["approved_missing"] = [m.id for m in approved if normalise(m.id) not in live]
            ours = {normalise(m.id): m for m in p.models}
            row["candidates"] = [i for n, i in live.items() if n in known and not (n in ours and (ours[n].approved or _dead(ours[n])))]
            row["known_dead"] = [ours[n].id for n in live if n in ours and not ours[n].approved and _dead(ours[n])]      # listed by the provider, yet a live check of ours proved it unusable for this account: not offered again
            row["unroutable"] = [i for n, i in live.items() if n not in known and n not in ours]
            row["free_unknown"] = [i for i in row["unroutable"] if "free" in normalise(i)]                               # the ones worth a look: free, and not in the pinned catalogue (the gateway's add-model call can add one)
        rows.append(row)
    return rows


def _dead(model):
    note = (model.note or "").lower()
    return "switched off" in note or "no longer available" in note


def _names(items, shown=SHOWN):
    text = ", ".join(items[:shown])
    return text + (f" (+{len(items) - shown} more)" if len(items) > shown else "")


def format_report(rows):
    lines = ["Provider model lists (READ-ONLY: nothing is changed; compared with ai_providers.yaml and the pinned gateway's catalogue):"]
    for r in rows:
        if not r["ok"]:
            lines.append(f"  {r['id']:<11} NOT READ: {r['note']}")
            continue
        lines.append(f"  {r['id']:<11} {r['listed']} listed")
        lines.append("      ours, approved, NOT listed (probably withdrawn: confirm with the live check): " + (_names(r["approved_missing"]) if r["approved_missing"] else "none"))
        lines.append("      listed, in the pinned gateway's catalogue, NOT approved (candidates): " + (_names(r["candidates"]) if r["candidates"] else "none"))
        lines.append("      listed, but switched off by a live check of ours (not offered again): " + (_names(r.get("known_dead", [])) if r.get("known_dead", []) else "none"))
        lines.append("      listed, not in the pinned gateway's catalogue (the gateway's add-model call can add one; no newer pin needed): " + (_names(r["unroutable"]) if r["unroutable"] else "none"))
        lines.append("      of those, the ones named free (all of them): " + (_names(r.get("free_unknown", []), 80) if r.get("free_unknown", []) else "none"))
    read = sum(1 for r in rows if r["ok"])
    lines.append(f"  {read} of {len(rows)} provider list(s) read. An approved model that is not listed is switched off by hand (approved: false and its lines in ai_lanes.yaml) only after a live check says it is gone.")
    return "\n".join(lines)


def inventory():
    return gw.baseline_from_inventory()
