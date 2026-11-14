"""Task lanes (ai_lanes.yaml): the kinds of AI work the platform does, and for each one the fixed chain of routes to try.

A lane fixes the highest data class it may carry, the ordered provider/model chain, whether Claude leads or is only the
fallback, how the output is checked, and whether a person must review it. Routes are chosen by configuration and by
measured evaluation, never by a model's own say-so.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import data_class as dc

DEFAULT_PATH = "ai_lanes.yaml"
REVIEW = ("none", "required")


class LaneError(Exception):
    pass


@dataclass(frozen=True)
class Lane:
    name: str
    description: str
    max_data_class: str
    chain: tuple
    claude_fallback: bool
    claude_model: str
    claude_leads: bool
    output: dict
    forbid: tuple
    review: str
    review_sample_every: int
    allow_experimental: bool
    max_tokens: int
    min_confidence: float
    use_pool: bool = True                       # after its own preferred routes, the lane may draw on the shared pool (every approved model, available everywhere)


def load_lanes(path=DEFAULT_PATH, registry=None):
    p = Path(path)
    if not p.is_file():
        raise LaneError(f"{path} is missing. It defines the AI task lanes and their route chains.")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LaneError(f"{path} could not be read: {exc}") from None
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("lanes"), dict) or not raw["lanes"]:
        raise LaneError(f"{path}: needs version: 1 and a lanes list")
    out = {}
    for name, d in raw["lanes"].items():
        where = f"lanes.{name}"
        allowed = ("description", "max_data_class", "chain", "claude_fallback", "claude_leads", "output", "forbid", "review", "review_sample_every",
                   "allow_experimental", "max_tokens", "min_confidence", "claude_model", "use_pool")
        extra = sorted(set(d) - set(allowed))
        if extra:
            raise LaneError(f"{where}: unknown setting(s) {', '.join(extra)}")
        try:
            ceiling = dc.validate(d.get("max_data_class", dc.PUBLIC))
        except dc.DataClassError as exc:
            raise LaneError(f"{where}: {exc}") from None
        chain = []
        for i, r in enumerate(d.get("chain") or []):
            if not isinstance(r, dict) or set(r) - {"provider", "model", "gateway_model"} or not r.get("provider") or not r.get("model"):
                raise LaneError(f"{where}.chain[{i}] needs provider and model (and optionally gateway_model)")
            if registry is not None:
                if r["provider"] not in registry.providers:
                    raise LaneError(f"{where}.chain[{i}]: provider {r['provider']!r} is not in ai_providers.yaml")
                if registry.providers[r["provider"]].retired:
                    raise LaneError(f"{where}.chain[{i}]: {r['provider']} was retired by its provider ({registry.providers[r['provider']].retired['date']}); remove this route")
                if not registry.model_ok(r["provider"], str(r["model"])):
                    raise LaneError(f"{where}.chain[{i}]: model {r['model']!r} is not an approved model of {r['provider']} (allowlist in ai_providers.yaml)")
            chain.append({"provider": r["provider"], "model": str(r["model"]), "gateway_model": str(r.get("gateway_model") or r["model"])})
        review = d.get("review", "none")
        if review not in REVIEW:
            raise LaneError(f"{where}.review must be one of {', '.join(REVIEW)}")
        output = d.get("output") or {"format": "text"}
        if output.get("format") not in ("json", "text"):
            raise LaneError(f"{where}.output.format must be json or text")
        if d.get("claude_leads") and not any(r["provider"] == "claude" for r in chain):
            raise LaneError(f"{where}: claude_leads needs a claude route in the chain")
        claude_model = str(d.get("claude_model", "claude-haiku-5-5"))
        if registry is not None and d.get("claude_fallback") and not registry.model_ok("claude", claude_model):
            raise LaneError(f"{where}.claude_model {claude_model!r} is not an approved Claude model")
        if "use_pool" in d and not isinstance(d["use_pool"], bool):
            raise LaneError(f"{where}.use_pool must be true or false")
        mc = d.get("min_confidence", 0)
        if isinstance(mc, bool) or not isinstance(mc, (int, float)) or not 0 <= mc <= 1:
            raise LaneError(f"{where}.min_confidence must be between 0 and 1")
        out[name] = Lane(name=name, description=str(d.get("description", "")), max_data_class=ceiling, chain=tuple(chain),
                         claude_fallback=bool(d.get("claude_fallback", False)), claude_model=claude_model, claude_leads=bool(d.get("claude_leads", False)), output=output,
                         forbid=tuple(d.get("forbid") or ()), review=review, review_sample_every=int(d.get("review_sample_every", 0) or 0),
                         allow_experimental=bool(d.get("allow_experimental", False)), max_tokens=int(d.get("max_tokens", 1200)), min_confidence=float(mc),
                         use_pool=bool(d.get("use_pool", True)))
    return out


def load_pool(path=DEFAULT_PATH, registry=None):
    """The shared pool (ai_lanes.yaml `pool:`): one ordered list of routes, each an approved model, that every lane may draw on after its own preferred routes, and every future stage and tool with
    it. A model is approved once (in ai_providers.yaml) and listed once here; it is not the property of any lane. Returns a list of {provider, model, gateway_model}."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LaneError(f"{path} could not be read: {exc}") from None
    entries = raw.get("pool") or []
    if not isinstance(entries, list):
        raise LaneError(f"{path}: pool must be a list of routes")
    out, seen = [], set()
    for i, r in enumerate(entries):
        where = f"pool[{i}]"
        if not isinstance(r, dict) or set(r) - {"provider", "model", "gateway_model"} or not r.get("provider") or not r.get("model"):
            raise LaneError(f"{where} needs provider and model (and optionally gateway_model)")
        key = (r["provider"], str(r["model"]))
        if key in seen:
            raise LaneError(f"{where}: {r['provider']} / {r['model']} is listed twice")
        seen.add(key)
        if registry is not None:
            if r["provider"] not in registry.providers:
                raise LaneError(f"{where}: provider {r['provider']!r} is not in ai_providers.yaml")
            if registry.providers[r["provider"]].retired:
                raise LaneError(f"{where}: {r['provider']} was retired by its provider; remove this route")
            if r["provider"] == "claude":
                raise LaneError(f"{where}: Claude is a lane's lead or its fallback, never part of the free pool")
            if not registry.model_ok(r["provider"], str(r["model"])):
                raise LaneError(f"{where}: model {r['model']!r} is not an approved model of {r['provider']} (allowlist in ai_providers.yaml)")
        out.append({"provider": r["provider"], "model": str(r["model"]), "gateway_model": str(r.get("gateway_model") or r["model"])})
    return out
