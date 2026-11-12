"""The AI Gateway Health tab."""
import json

from contentintel import sheets
from contentintel.ai import registry as R
from contentintel.ai import sheetrows as SR
from contentintel.ai import usage as U
from contentintel.store import Store
from conftest import ROOT
from fake_sheets import FakeSpreadsheet, read

NOW = "2026-11-03T12:00:00Z"
REG = R.load_registry(ROOT / "ai_providers.yaml")


def ledger(tmp_path):
    u = U.UsageLedger(tmp_path / "ai_usage.json")
    g, c, gh = REG.provider("groq"), REG.provider("claude"), REG.provider("github")
    for _ in range(30):
        u.record(g, "research_extraction", NOW, ok=True, tokens_in=500, tokens_out=100, latency_ms=300)
    u.record(g, "research_extraction", NOW, ok=False)
    u.record(gh, "audience_voice", "2026-11-01T10:00:00Z", ok=True, tokens_in=200, tokens_out=50)
    u.record(c, "brief_generation", NOW, ok=True, tokens_in=10_000, tokens_out=2_000, model="claude-sonnet-5-5")
    return u


HEALTH = {"routes": {"ai:groq|gateway": {"last_success": "2026-11-03T11:00:00Z", "last_error": {"code": "TIMEOUT", "at": "2026-11-02T09:00:00Z"}, "pause": None},
                     "ai:mistral|gateway": {"last_success": "2026-11-01T11:00:00Z", "pause": {"until": "2026-11-03T13:00:00Z", "message": "429 daily limit"}}}}


def test_each_enabled_provider_has_a_row_with_free_quota_used_failures_and_rest(tmp_path):
    rows = SR.rows(REG, ledger(tmp_path), HEALTH, NOW)
    assert list(rows)[0] == "claude" and set(rows) == {"claude", "cloudflare", "github", "google", "groq", "mistral", "openrouter"}
    groq = rows["groq"]
    assert (groq["status"], groq["cost"], groq["requests_today"], groq["daily_limit"], groq["left_today"], groq["failures_this_month"]) == ("ready", "free", 31, 1000, 969, 1)
    assert groq["tokens_today"] == 18000 and groq["last_success"] == "2026-11-03T11:00:00Z" and groq["last_error"] == "TIMEOUT at 2026-11-02T09:00" and "nothing is bought" in groq["note"]
    assert rows["mistral"]["status"] == "resting" and rows["mistral"]["resting_until"] == "2026-11-03T13:00:00Z" and "429 daily limit" in rows["mistral"]["note"]
    gh = rows["github"]
    assert gh["status"] == "retired" and gh["requests_today"] == 0 and gh["requests_this_month"] == 1 and gh["note"] == "RETIRED: GitHub Models was withdrawn by its provider on 2026-07-30; it is never called (history only)" and gh["terms"] == "verified_secondary"    # old usage stays visible; it is never listed as ready or off
    assert rows["google"]["data_classes"] == "public, internal" and rows["groq"]["approved_models"] == 2


def test_claude_shows_its_spend_against_both_caps(tmp_path):
    claude = SR.rows(REG, ledger(tmp_path), HEALTH, NOW)["claude"]
    assert claude["cost"] == "paid, capped" and claude["spend_this_month_usd"] > 0 and claude["data_classes"] == "public, internal, restricted"
    assert "of $0.50 today" in claude["note"] and "of $10.00 this month" in claude["note"]


def test_a_rest_that_has_ended_is_not_shown_and_providers_that_are_off_and_unused_are_left_out(tmp_path):
    late = "2026-11-03T14:00:00Z"
    rows = SR.rows(REG, ledger(tmp_path), HEALTH, late)
    assert rows["mistral"]["status"] == "ready" and rows["mistral"]["resting_until"] == "" and "cerebras" not in rows and "kilo" not in rows


def test_nothing_enabled_and_nothing_used_means_no_tab(tmp_path):
    import copy
    data = {"providers": {k: copy.copy(v) for k, v in REG.providers.items()}}
    off = R.Registry(providers={k: type(v)(**{**v.__dict__, "enabled": False}) for k, v in data["providers"].items()}, daily_usd=0.5, monthly_usd=10.0)
    assert SR.tab(off, U.UsageLedger(tmp_path / "none.json"), {}, NOW) is None


def test_providers_switched_on_but_never_used_do_not_create_a_tab(tmp_path):
    assert SR.tab(REG, U.UsageLedger(tmp_path / "none.json"), {"routes": {"meta_ads|browser": {}}}, NOW) is None
    assert SR.tab(REG, U.UsageLedger(tmp_path / "none.json"), {"routes": {"ai:groq|gateway": {"last_success": NOW}}}, NOW)["rows_by_key"]["groq"]["last_success"] == NOW


def test_the_tab_reaches_the_sheet_typed_and_a_problem_never_stops_the_rest(tmp_path, brands, monkeypatch):
    store = Store(tmp_path / "data")
    shared = tmp_path / "data" / "_shared"
    shared.mkdir(parents=True)
    ledger(tmp_path).save()
    (shared / "ai_usage.json").write_text((tmp_path / "ai_usage.json").read_text())
    store.write_health(HEALTH)
    monkeypatch.chdir(ROOT)
    sp = FakeSpreadsheet("sid")
    sheets.update_brand_sheet(brands["duderobe"], store, sp, NOW)
    header, rows = read(sp.worksheet("AI Gateway Health"))
    by = {r["provider"]: r for r in rows}
    assert header[0] == "provider" and by["groq"]["requests_today"] == 31 and by["groq"]["left_today"] == 969 and by["claude"]["spend_this_month_usd"] > 0
    assert by["mistral"]["status"] == "resting" and "last_updated" in header
    monkeypatch.setattr(SR, "tab", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    sp2 = FakeSpreadsheet("sid2")
    rep = sheets.update_brand_sheet(brands["duderobe"], store, sp2, NOW)
    assert any("AI Gateway Health tab skipped: boom" in n for n in rep.notes) and "Products" in [w.title for w in sp2.worksheets()] and json.dumps(rep.notes)
