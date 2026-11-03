"""The provider registry (ai_providers.yaml) and the approved-model allowlist: what exists, what is on, what each may receive.

Nothing is on by default. A provider carries the terms found when it was checked, and a ceiling set by those terms. A person
may raise the ceiling only through an explicit, dated owner_override naming who decided and why; that choice is the owner's,
it is recorded in version control, and the loader refuses an override that has no author, date or reason. Restricted data may
reach Claude only. A model is routable only if it is listed and approved for its provider: the remote gateway catalogue can
add models, but it can never make one routable.
"""
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import data_class as dc

DEFAULT_PATH = "ai_providers.yaml"
TERMS_STATUS = ("verified", "verified_secondary", "upstream_documented", "declared_only", "unverified", "owner_decision")
CLAUDE = "claude"


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class Model:
    id: str
    approved: bool
    capabilities: tuple
    price: dict
    note: str = ""


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    enabled: bool
    experimental: bool
    paid: bool
    ceiling: str
    override: dict
    approved_classes: tuple
    credential_env: tuple
    gateway_platform: str
    terms: dict
    limits: dict
    price_per_million: dict
    models: tuple

    @property
    def effective_ceiling(self):
        return self.override.get("ceiling", self.ceiling) if self.override else self.ceiling

    def can_receive(self, data_class):
        return data_class in self.approved_classes and dc.within(self.effective_ceiling, data_class)

    def model(self, model_id):
        return next((m for m in self.models if m.id == model_id), None)

    def price_for(self, model_id):
        m = self.model(model_id)
        return (m.price if m and m.price else None) or self.price_per_million


@dataclass(frozen=True)
class Registry:
    providers: dict
    daily_usd: float
    monthly_usd: float

    def provider(self, pid):
        try:
            return self.providers[pid]
        except KeyError:
            raise RegistryError(f"{pid!r} is not in ai_providers.yaml") from None

    def model_ok(self, pid, model_id):
        m = self.provider(pid).model(model_id)
        return bool(m and m.approved)

    def eligibility(self, pid, data_class, *, experimental_ok=False, model=None):
        """(ok, reason): may this provider and model be used for data of this class, by configuration alone."""
        p = self.provider(pid)
        if not p.enabled:
            return False, "disabled (a person has not approved it)"
        if p.experimental and not experimental_ok:
            return False, "experimental route: not allowed in this lane"
        if model is not None and not self.model_ok(pid, model):
            return False, f"model {model!r} is not in the approved-model allowlist"
        if not p.can_receive(data_class):
            return False, f"not approved for {data_class} data"
        return True, ""


def _only(data, allowed, where):
    extra = sorted(set(data) - set(allowed))
    if extra:
        raise RegistryError(f"{where}: unknown setting(s) {', '.join(extra)} (allowed: {', '.join(allowed)})")


def _num(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise RegistryError(f"{where} must be a number of 0 or more")
    return value


def _prices(price, where):
    _only(price, ("input", "output"), where)
    for k, v in price.items():
        _num(v, f"{where}.{k}")
    return dict(price)


def load_registry(path=DEFAULT_PATH):
    p = Path(path)
    if not p.is_file():
        raise RegistryError(f"{path} is missing. It lists the AI providers, which are on, and what each may receive.")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path} could not be read: {exc}") from None
    if not isinstance(raw, dict):
        raise RegistryError(f"{path} must be a list of settings")
    _only(raw, ("version", "budgets", "providers"), path)
    if raw.get("version") != 1:
        raise RegistryError(f"{path}: version must be 1")
    budgets = raw.get("budgets") or {}
    _only(budgets, ("daily_usd", "monthly_usd"), f"{path} budgets")
    out = {}
    for pid, d in (raw.get("providers") or {}).items():
        where = f"providers.{pid}"
        if not isinstance(d, dict):
            raise RegistryError(f"{where} must be a list of settings")
        _only(d, ("label", "enabled", "experimental", "paid", "ceiling", "owner_override", "approved_classes", "credentials", "gateway_platform",
                  "terms", "limits", "price_per_million", "models"), where)
        ceiling = d.get("ceiling", dc.PUBLIC)
        try:
            dc.validate(ceiling)
            approved = tuple(dc.validate(c) for c in (d.get("approved_classes") or []))
        except dc.DataClassError as exc:
            raise RegistryError(f"{where}: {exc}") from None
        terms = d.get("terms") or {}
        _only(terms, ("trains_on_data", "retention", "business_use", "status", "date", "note"), f"{where}.terms")
        status = terms.get("status", "unverified")
        if status not in TERMS_STATUS:
            raise RegistryError(f"{where}.terms.status must be one of {', '.join(TERMS_STATUS)}")
        override = d.get("owner_override") or {}
        if override:
            _only(override, ("ceiling", "approved_by", "date", "reason"), f"{where}.owner_override")
            if not all(str(override.get(k, "")).strip() for k in ("ceiling", "approved_by", "date", "reason")):
                raise RegistryError(f"{where}.owner_override needs ceiling, approved_by, date and reason: an override is a recorded decision")
            try:
                dc.validate(override["ceiling"])
            except dc.DataClassError as exc:
                raise RegistryError(f"{where}.owner_override: {exc}") from None
            if status in ("unverified", "declared_only"):
                raise RegistryError(f"{where}: terms that are {status} cannot be overridden: check the provider's terms first")
        eff = override.get("ceiling", ceiling) if override else ceiling
        too_high = [c for c in approved if not dc.within(eff, c)]
        if too_high:
            raise RegistryError(f"{where}: approved_classes {too_high} are above the ceiling ({eff}); raise it only with an owner_override")
        if dc.RESTRICTED in approved and pid != CLAUDE:
            raise RegistryError(f"{where}: Restricted data may be approved for claude only")
        for flag in ("enabled", "experimental", "paid"):
            if flag in d and not isinstance(d[flag], bool):
                raise RegistryError(f"{where}.{flag} must be true or false")
        if d.get("enabled") and status in ("unverified", "declared_only"):
            raise RegistryError(f"{where}: a provider whose terms are {status} cannot be enabled")
        if d.get("experimental") and eff != dc.PUBLIC:
            raise RegistryError(f"{where}: an experimental route may only be Public")
        limits = d.get("limits") or {}
        _only(limits, ("requests_per_day", "tokens_per_day", "requests_per_minute"), f"{where}.limits")
        for k, v in limits.items():
            _num(v, f"{where}.limits.{k}")
        price = _prices(d.get("price_per_million") or {}, f"{where}.price_per_million")
        models = []
        for i, m in enumerate(d.get("models") or []):
            if not isinstance(m, dict) or not m.get("id"):
                raise RegistryError(f"{where}.models[{i}] needs an id")
            _only(m, ("id", "approved", "capabilities", "price_per_million", "note"), f"{where}.models[{i}]")
            if "approved" in m and not isinstance(m["approved"], bool):
                raise RegistryError(f"{where}.models[{i}].approved must be true or false")
            models.append(Model(id=str(m["id"]), approved=bool(m.get("approved", False)), capabilities=tuple(m.get("capabilities") or ()),
                                price=_prices(m.get("price_per_million") or {}, f"{where}.models[{i}].price_per_million"), note=str(m.get("note", ""))))
        ids = [m.id for m in models]
        if len(ids) != len(set(ids)):
            raise RegistryError(f"{where}: a model is listed twice")
        paid = bool(d.get("paid", False))
        if paid and not (price or all(m.price for m in models if m.approved)):
            raise RegistryError(f"{where}: a paid provider needs price_per_million, for the provider or for every approved model")
        out[pid] = Provider(id=pid, label=str(d.get("label") or pid), enabled=bool(d.get("enabled", False)), experimental=bool(d.get("experimental", False)),
                            paid=paid, ceiling=ceiling, override=override, approved_classes=approved,
                            credential_env=tuple((d.get("credentials") or {}).get("env", [])), gateway_platform=str(d.get("gateway_platform") or pid),
                            terms=terms, limits=limits, price_per_million=price, models=tuple(models))
    if not out:
        raise RegistryError(f"{path}: providers must name at least one provider")
    return Registry(providers=out, daily_usd=_num(budgets.get("daily_usd", 0), "budgets.daily_usd"), monthly_usd=_num(budgets.get("monthly_usd", 0), "budgets.monthly_usd"))
