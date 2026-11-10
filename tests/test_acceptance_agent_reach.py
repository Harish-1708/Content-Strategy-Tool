"""The nine Agent-Reach research channels in the acceptance run: a real check where one can run here, the true reason where it cannot."""
import pytest

from contentintel import acceptance as A
from contentintel import contract as K
from contentintel import guards, health as HL
from contentintel import services as SV
from contentintel import sources as SRC
from contentintel.observatory import acceptance_checks as AK
from contentintel.store import Store
from conftest import ROOT

NOW = "2026-11-10T12:00:00Z"
KEYS = {("agent_reach_research", c) for c in AK.AGENT_REACH_CHANNELS}
SITE = {"instagram": "Instagram", "reddit": "Reddit", "twitter": "X", "facebook": "Facebook", "linkedin": "LinkedIn", "xiaohongshu": "Xiaohongshu", "boss": "Boss"}


class FakeCLI:
    def __init__(self, ready=True, rows=None):
        self.ready, self.rows, self.calls = ready, [{"title": "a result", "url": "https://x.example/1"}, {"title": "b", "url": "https://x.example/2"}] if rows is None else rows, []

    def available(self):
        return self.ready

    def call(self, argv):
        self.calls.append(argv)
        return self.rows


class GitHub:
    def call(self, args):
        return [{"title": "acme/widgets", "url": "https://github.com/acme/widgets"}]


class Web:
    def get(self, url):
        return type("Got", (), {"body": b"<html>Example Domain</html>"})()


def s():
    return SV.load_settings("observatory.yaml", "no-such-dir")


def test_every_channel_that_can_run_here_has_a_real_check_and_the_opencli_ones_try_their_own_small_search():
    clis = {}
    factory = lambda ch: clis.setdefault(ch, FakeCLI())                                                 # noqa: E731
    got = AK.agent_reach_checks(s(), "a@b.example", NOW, environ={}, cli=factory, github=GitHub(), web_client=Web())
    assert set(got) == KEYS and AK.agent_reach_missing(s(), "a@b.example", environ={}, cli=factory) == {}
    for (_, channel), fn in got.items():
        items, detail = fn()
        assert items >= 1 and (channel in detail if channel in SITE else ("GitHub" in detail if channel == "github" else "public page" in detail))
    assert clis["instagram"].calls == [["search", "marketing", "--limit", "2"]] and clis["linkedin"].calls == [["search", "marketing", "--limit", "2", ]]
    assert clis["boss"].calls[0][:2] == ["search", "marketing"]


def test_without_opencli_the_seven_browser_channels_report_the_true_reason_and_github_and_web_still_run():
    factory = lambda ch: FakeCLI(ready=False)                                                           # noqa: E731
    got = AK.agent_reach_checks(s(), "a@b.example", NOW, environ={}, cli=factory, github=GitHub(), web_client=Web())
    assert set(got) == {("agent_reach_research", "github"), ("agent_reach_research", "web")}
    missing = AK.agent_reach_missing(s(), "a@b.example", environ={}, cli=factory)
    assert set(missing) == KEYS - set(got)
    for (_, channel), why in missing.items():
        assert why == f"cannot run: OpenCLI is not installed on this machine (it needs a browser logged in to {SITE[channel]}, on a machine you control)"
        assert "OBSERVATORY_CONTACT" not in why                                                          # never the contact address when that is not the reason


def test_on_a_hosted_runner_with_the_guard_on_the_reason_names_the_runner_and_with_it_off_the_check_is_tried():
    hosted = {"RUNNER_ENVIRONMENT": "github-hosted"}
    guards.configure({g: g == "hosted_runner_block" for g in guards.GUARDS})
    why = AK.agent_reach_missing(s(), "a@b.example", environ=hosted, cli=lambda ch: FakeCLI())[("agent_reach_research", "twitter")]
    assert why.startswith("cannot run on a GitHub-hosted runner") and "logged in to X" in why
    assert ("agent_reach_research", "twitter") not in AK.agent_reach_checks(s(), "a@b.example", NOW, environ=hosted, cli=lambda ch: FakeCLI(), github=GitHub(), web_client=Web())
    guards.configure({g: False for g in guards.GUARDS})
    assert ("agent_reach_research", "twitter") in AK.agent_reach_checks(s(), "a@b.example", NOW, environ=hosted, cli=lambda ch: FakeCLI(), github=GitHub(), web_client=Web())


def test_the_web_check_needs_the_contact_address_and_says_so_only_for_web_and_a_switched_off_channel_says_that():
    got = AK.agent_reach_checks(s(), "", NOW, environ={}, cli=lambda ch: FakeCLI(), github=GitHub(), web_client=Web())
    assert ("agent_reach_research", "web") not in got and ("agent_reach_research", "github") in got
    missing = AK.agent_reach_missing(s(), "", environ={}, cli=lambda ch: FakeCLI())
    assert missing == {("agent_reach_research", "web"): "no contact address: set OBSERVATORY_CONTACT (the polite reader puts it in the request)"}
    st = s()
    st["services"]["agent_reach"]["channels"]["linkedin"] = False
    got = AK.agent_reach_checks(st, "a@b.example", NOW, environ={}, cli=lambda ch: FakeCLI(), github=GitHub(), web_client=Web())
    assert ("agent_reach_research", "linkedin") not in got
    assert AK.agent_reach_missing(st, "a@b.example", environ={}, cli=lambda ch: FakeCLI())[("agent_reach_research", "linkedin")].startswith("switched off: ")


def test_a_check_that_gets_nothing_back_fails_with_a_clear_code():
    got = AK.agent_reach_checks(s(), "a@b.example", NOW, environ={}, cli=lambda ch: FakeCLI(rows=[]), github=GitHub(), web_client=Web())
    with pytest.raises(K.Failure) as seen:
        got[("agent_reach_research", "reddit")]()
    assert seen.value.code == K.UPSTREAM_BROKEN and "reddit search" in str(seen.value)


def test_in_the_acceptance_run_a_channel_that_cannot_run_shows_the_true_reason_not_the_contact_message(tmp_path):
    reg = SRC.load_sources(ROOT / "sources.yaml")
    ctx = A.Context(registry=reg)
    ctx.observatory_checks = AK.agent_reach_checks(s(), "a@b.example", NOW, environ={}, cli=lambda ch: FakeCLI(ready=False), github=GitHub(), web_client=Web())
    ctx.missing.update(AK.agent_reach_missing(s(), "a@b.example", environ={}, cli=lambda ch: FakeCLI(ready=False)))
    probes = {(p.source, p.route): p for p in A.build_probes(ctx, wanted=["agent_reach_research"])}
    for channel in SITE:
        p = probes[("agent_reach_research", channel)]
        assert p.skip.startswith("cannot run: OpenCLI is not installed") and "OBSERVATORY_CONTACT" not in p.skip
    assert not probes[("agent_reach_research", "github")].skip and not probes[("agent_reach_research", "web")].skip
    store, health = Store(str(tmp_path)), HL.HealthLedger.load(Store(str(tmp_path)), reg)
    results = A.run_acceptance([probes[("agent_reach_research", "github")], probes[("agent_reach_research", "web")]], health=health, store=store, now=NOW, version="t", run_id="r", platform="other")
    assert [r["status"] for r in results] == ["passed", "passed"]


def test_the_cli_builds_the_agent_reach_checks_beside_the_competitor_ones_and_the_reasons_for_the_rest(tmp_path, monkeypatch):
    import argparse
    import inspect

    from contentintel import cli
    monkeypatch.setenv("OBSERVATORY_CONTACT", "research@kelson.example")
    monkeypatch.setenv("OPENCLI_BIN", "/no/such/opencli")
    args = argparse.Namespace(brand="", brands_dir=str(ROOT / "brands"), data_dir=str(tmp_path))
    checks = cli._observatory_checks(args, NOW)
    assert {("agent_reach_research", "github"), ("agent_reach_research", "web")} <= set(checks) and ("competitor_pages", "feed") in checks and ("competitor_stores", "sitemap") in checks
    assert not {("agent_reach_research", c) for c in SITE} & set(checks)                                   # no OpenCLI here: those are reported with their reason instead
    assert "_oc.agent_reach_missing(_s, _contact)" in inspect.getsource(cli)
