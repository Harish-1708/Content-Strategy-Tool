"""Brand profiles: one folder per brand, validated on load.

Everything brand-specific (domain, lifecycle tags, required facts, thresholds)
lives in brands/<slug>/brand.yaml. Code never hard-codes a brand.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

DEFAULT_GATE = {
    "warn_change": 0.10,        # warn when the count drops more than this vs last good run
    "warn_growth": 0.50,        # warn when the count grows more than this vs last good run
    "fail_drop": 0.25,          # fail when count drops more than this vs last good run
    "baseline_floor": 0.70,     # fail when count falls below this share of the confirmed baseline
    "min_abs_change": 2,        # ignore percentage moves smaller than this many products
    "sitemap_warn_missing": 0.05,
    "sitemap_fail_missing": 0.25,
}

_TOP_KEYS = {"slug", "name", "platform", "domain", "category_schema",
             "catalogue", "facts", "gate"}
_CATALOGUE_KEYS = {"exclude", "active_tag", "status_rules", "flags",
                   "category_by_type", "audience", "family"}
_EXCLUDE_KEYS = {"product_types", "title_patterns", "tags"}
_STATUS_NAMES = {"coming_soon", "pre_order"}


class BrandError(Exception):
    """Raised for an invalid brand profile or an unsafe brand name."""


def validate_slug(slug):
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise BrandError(f"Invalid brand slug: {slug!r}")
    return slug


@dataclass(frozen=True)
class Brand:
    slug: str
    name: str
    platform: str
    domain: str
    category_schema: str
    catalogue: dict
    facts: dict
    gate: dict
    manual_facts: dict = field(default_factory=dict)
    claims: list = field(default_factory=list)

    @property
    def required_facts(self):
        return list(self.facts.get("required", []))

    @property
    def expected_facts(self):
        return list(self.facts.get("expected", []))


def _read_yaml(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise BrandError(f"{path}: invalid YAML: {exc}") from exc
    return data if data is not None else {}


def _check_patterns(patterns, where):
    for pat in patterns:
        try:
            re.compile(pat)
        except re.error as exc:
            raise BrandError(f"{where}: bad pattern {pat!r}: {exc}") from exc


def _normalise_catalogue(cat, slug):
    unknown = set(cat) - _CATALOGUE_KEYS
    if unknown:
        raise BrandError(f"{slug}: unknown catalogue keys {sorted(unknown)}")
    exclude = cat.get("exclude") or {}
    bad = set(exclude) - _EXCLUDE_KEYS
    if bad:
        raise BrandError(f"{slug}: unknown exclude keys {sorted(bad)}")
    status_rules = cat.get("status_rules") or {}
    bad = set(status_rules) - _STATUS_NAMES
    if bad:
        raise BrandError(f"{slug}: unknown status rules {sorted(bad)}")
    out = {
        "exclude": {
            "product_types": list(exclude.get("product_types") or []),
            "title_patterns": list(exclude.get("title_patterns") or []),
            "tags": list(exclude.get("tags") or []),
        },
        "active_tag": cat.get("active_tag"),
        "status_rules": {
            name: {"tags": list((rule or {}).get("tags") or []),
                   "title_patterns": list((rule or {}).get("title_patterns") or [])}
            for name, rule in status_rules.items()
        },
        "flags": {
            name: {"tags": list((rule or {}).get("tags") or []),
                   "title_patterns": list((rule or {}).get("title_patterns") or [])}
            for name, rule in (cat.get("flags") or {}).items()
        },
        "category_by_type": dict(cat.get("category_by_type") or {}),
        "audience": {
            "tags": dict((cat.get("audience") or {}).get("tags") or {}),
            "title_patterns": dict((cat.get("audience") or {}).get("title_patterns") or {}),
        },
        "family": dict(cat.get("family") or {}),
    }
    _check_patterns(out["exclude"]["title_patterns"], f"{slug} exclude.title_patterns")
    for name, rule in list(out["status_rules"].items()) + list(out["flags"].items()):
        _check_patterns(rule["title_patterns"], f"{slug} {name}.title_patterns")
    _check_patterns(list(out["audience"]["title_patterns"]), f"{slug} audience.title_patterns")
    return out


def load_brand(slug, brands_dir="brands"):
    validate_slug(slug)
    root = Path(brands_dir) / slug
    path = root / "brand.yaml"
    if not path.is_file():
        raise BrandError(f"No brand profile at {path}")
    raw = _read_yaml(path)
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise BrandError(f"{slug}: unknown keys {sorted(unknown)}")
    for key in ("slug", "name", "platform", "domain", "category_schema"):
        if not raw.get(key):
            raise BrandError(f"{slug}: missing required key '{key}'")
    if raw["slug"] != slug:
        raise BrandError(f"{slug}: slug in file is {raw['slug']!r}, folder is {slug!r}")
    if raw["platform"] != "shopify":
        raise BrandError(f"{slug}: platform {raw['platform']!r} is not supported yet")

    facts = raw.get("facts") or {}
    facts = {"required": list(facts.get("required") or []),
             "expected": list(facts.get("expected") or [])}

    gate = dict(DEFAULT_GATE)
    for key, value in (raw.get("gate") or {}).items():
        if key not in DEFAULT_GATE:
            raise BrandError(f"{slug}: unknown gate setting {key!r}")
        gate[key] = value

    manual = {}
    manual_path = root / "manual_facts.yaml"
    if manual_path.is_file():
        manual = (_read_yaml(manual_path).get("products") or {})
    claims = []
    claims_path = root / "claims.yaml"
    if claims_path.is_file():
        claims = list(_read_yaml(claims_path).get("claims") or [])

    return Brand(
        slug=slug, name=raw["name"], platform=raw["platform"], domain=raw["domain"],
        category_schema=raw["category_schema"],
        catalogue=_normalise_catalogue(raw.get("catalogue") or {}, slug),
        facts=facts, gate=gate, manual_facts=manual, claims=claims,
    )


def list_brand_slugs(brands_dir="brands"):
    base = Path(brands_dir)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if (p / "brand.yaml").is_file() and SLUG_RE.match(p.name))


def load_all(brands_dir="brands"):
    return {slug: load_brand(slug, brands_dir) for slug in list_brand_slugs(brands_dir)}


def brand_data_dir(data_dir, slug):
    """Per-brand data folder. The resolved path must stay inside data_dir."""
    validate_slug(slug)
    base = Path(data_dir).resolve()
    target = (base / slug).resolve()
    if base != target.parent:
        raise BrandError(f"Unsafe data path for brand {slug!r}")
    return target
