"""Builders for the AI tests: a small registry and lanes written to disk, and a router wired to scripted clients."""
import copy

import yaml

from contentintel.ai import lanes as L
from contentintel.ai import registry as R
from contentintel.ai import review as RV
from contentintel.ai import router as RT
from contentintel.ai import usage as U
from contentintel.health import HealthLedger

from stage1_helpers import NOW, registry as sources_registry

OWNER = {"ceiling": "internal", "approved_by": "Owner", "date": "2026-11-03", "reason": "test"}

PROVIDERS = {
    "version": 1, "budgets": {"daily_usd": 0.05, "monthly_usd": 0.2},
    "providers": {
        "alpha": {"label": "Alpha", "enabled": True, "ceiling": "public", "approved_classes": ["public"], "credentials": {"env": ["ALPHA_KEY"]}, "gateway_platform": "alpha",
                  "terms": {"status": "verified"}, "limits": {"requests_per_day": 3}, "models": [{"id": "a-1", "approved": True}, {"id": "a-2", "approved": False}]},
        "beta": {"label": "Beta", "enabled": True, "ceiling": "public", "owner_override": OWNER, "approved_classes": ["public", "internal"], "credentials": {"env": ["BETA_KEY"]},
                 "gateway_platform": "beta", "terms": {"status": "verified"}, "models": [{"id": "b-1", "approved": True}]},
        "gamma": {"label": "Gamma", "enabled": True, "experimental": True, "ceiling": "public", "approved_classes": ["public"], "credentials": {"env": []},
                  "gateway_platform": "gamma", "terms": {"status": "upstream_documented"}, "models": [{"id": "g-1", "approved": True}]},
        "delta": {"label": "Delta", "enabled": False, "ceiling": "public", "approved_classes": [], "credentials": {"env": ["DELTA_KEY"]}, "gateway_platform": "delta",
                  "terms": {"status": "unverified"}, "models": [{"id": "d-1", "approved": True}]},
        "claude": {"label": "Claude", "enabled": True, "paid": True, "ceiling": "restricted", "approved_classes": ["public", "internal", "restricted"], "credentials": {"env": ["ANTHROPIC_API_KEY"]},
                   "gateway_platform": "claude", "terms": {"status": "owner_decision"},
                   "models": [{"id": "claude-haiku-5-5", "approved": True, "price_per_million": {"input": 0.1, "output": 0.5}},
                              {"id": "claude-sonnet-5-5", "approved": True, "price_per_million": {"input": 2.0, "output": 10.0}},
                              {"id": "claude-opus-5-5", "approved": False, "price_per_million": {"input": 4.0, "output": 20.0}}]},
    },
}
JSON_ANSWER = {"type": "json", "schema": {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string", "minLength": 1}}}}
LANES = {
    "version": 1,
    "lanes": {
        "pub": {"max_data_class": "public", "chain": [{"provider": "alpha", "model": "a-1"}], "claude_fallback": True, "output": {"format": "json", "schema": JSON_ANSWER["schema"]}, "max_tokens": 100},
        "internal": {"max_data_class": "internal", "chain": [{"provider": "alpha", "model": "a-1"}, {"provider": "beta", "model": "b-1"}], "claude_fallback": True,
                     "output": {"format": "text", "min_chars": 3}, "max_tokens": 100},
        "secret": {"max_data_class": "restricted", "chain": [{"provider": "claude", "model": "claude-sonnet-5-5"}], "claude_leads": True, "review": "required",
                   "output": {"format": "text", "min_chars": 3}, "forbid": ["guaranteed results"], "max_tokens": 3000},
        "exp": {"max_data_class": "public", "chain": [{"provider": "gamma", "model": "g-1"}], "allow_experimental": True, "output": {"format": "text", "min_chars": 1}, "max_tokens": 50},
        "noexp": {"max_data_class": "public", "chain": [{"provider": "gamma", "model": "g-1"}], "output": {"format": "text", "min_chars": 1}, "max_tokens": 50},
        "sampled": {"max_data_class": "public", "chain": [{"provider": "alpha", "model": "a-1"}], "review_sample_every": 1, "output": {"format": "text", "min_chars": 1}, "max_tokens": 50},
        "confident": {"max_data_class": "public", "chain": [{"provider": "alpha", "model": "a-1"}], "min_confidence": 0.6,
                      "output": {"format": "json", "schema": {"type": "object", "required": ["confidence"], "properties": {"confidence": {"type": "number"}}}}, "max_tokens": 50},
    },
}


def write(tmp_path, providers=None, lanes=None):
    p, l = tmp_path / "providers.yaml", tmp_path / "lanes.yaml"
    p.write_text(yaml.safe_dump(providers if providers is not None else PROVIDERS), encoding="utf-8")
    l.write_text(yaml.safe_dump(lanes if lanes is not None else LANES), encoding="utf-8")
    return p, l


def edited(base, fn):
    data = copy.deepcopy(base)
    fn(data)
    return data


def stack(tmp_path, clients, providers=None, lanes=None):
    p, l = write(tmp_path, providers, lanes)
    registry = R.load_registry(p)
    lane_map = L.load_lanes(l, registry)
    usage, review = U.UsageLedger(tmp_path / "usage.json"), RV.ReviewQueue(tmp_path / "review.json")
    health = HealthLedger({}, sources_registry(tmp_path))
    router = RT.Router(registry, lane_map, usage, health, clients, review)
    return router, registry, usage, review, health


__all__ = ["NOW", "stack", "write", "edited", "PROVIDERS", "LANES", "OWNER"]
