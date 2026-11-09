"""The Agent-Reach catalogue (every channel, how each action is run here, why any is not) and the native GitHub client."""
import importlib
import json
import os
import pkgutil
import sys
import urllib.error
from pathlib import Path

import pytest

from contentintel import agent_reach_catalog as CAT
from contentintel import contract as K
from contentintel import github_reach as GH
from contentintel import services as SV
from contentintel.observatory import config as OC

SRC = Path(os.environ.get("AGENT_REACH_SRC", "/tmp/repos/Agent-Reach"))


@pytest.mark.skipif(not (SRC / "agent_reach" / "channels").is_dir(), reason="Agent-Reach's source is not on this machine")
def test_the_catalogue_lists_exactly_the_channels_actions_and_levels_agent_reach_declares():
    sys.path.insert(0, str(SRC))
    try:
        channels = importlib.import_module("agent_reach.channels")
        found = {}
        for info in pkgutil.iter_modules(channels.__path__):
            if info.name.startswith("_") or info.name == "base":
                continue
            mod = importlib.import_module(f"agent_reach.channels.{info.name}")
            for cls in vars(mod).values():
                if isinstance(cls, type) and getattr(cls, "actions", None) and cls.__module__ == mod.__name__:
                    found[info.name] = (cls.level, {a.name: tuple(p.name for p in a.params) for a in cls.actions})
    finally:
        sys.path.remove(str(SRC))
    assert sorted(found) == sorted(CAT.CHANNELS)
    for name, (level, actions) in found.items():
        spec = CAT.CHANNELS[name]
        assert spec["level"] == level
        assert CAT.DECLARED[name] == {a: tuple(p) for a, p in actions.items()}                  # the catalogue lists every action Agent-Reach declares, with its inputs
        connected = {a: v["params"] for a, v in spec["actions"].items()}
        declared_here = {**connected, **{a: None for a in spec.get("unconnected_actions", {})}}
        if spec["integration"] == "none":
            assert actions and not connected                                                  # every action of an unconnected channel is accounted for by the channel's `why`
        else:
            assert set(declared_here) == set(actions)                                         # connected + deliberately unconnected = everything Agent-Reach offers


def test_every_channel_is_either_connected_with_a_reason_to_be_or_unconnected_with_the_exact_reason():
    assert CAT.integrated()[:3] == ("instagram", "reddit", "youtube") and len(CAT.CHANNELS) == 14
    for name, spec in CAT.CHANNELS.items():
        assert spec["relevance"] and spec["needs"] and spec["level"] in ("default", "optional", "experimental")
        if spec["integration"] == "none":
            assert spec["why"] and not spec["actions"] and name not in CAT.integrated()
        else:
            assert spec["actions"] and name in CAT.integrated() and name in OC.AR_CHANNELS
            for action, why in spec.get("unconnected_actions", {}).items():
                assert action not in spec["actions"] and why
    assert set(OC.AR_CHANNELS) == set(CAT.integrated())
    keys = {c["key"] for c in SV.CONTROLS}
    assert all(f"services.agent_reach.channels.{c}" in keys for c in CAT.integrated())          # each connected channel has its own owner switch, ON by default
    assert not any(f"services.agent_reach.channels.{c}" in keys for c, s in CAT.CHANNELS.items() if s["integration"] == "none")


@pytest.mark.parametrize("channel, action, values, expected", [
    ("instagram", "search", {"query": "protein bars"}, ["search", "protein bars", "--limit", "10"]),
    ("instagram", "profile", {"username": "@acme"}, ["profile", "acme"]),
    ("instagram", "posts", {"username": "acme"}, ["user", "acme", "--limit", "10"]),
    ("reddit", "search", {"query": "snacks"}, ["search", "snacks", "--limit", "10"]),
    ("reddit", "read", {"target": "https://www.reddit.com/r/x/comments/abc/t/"}, ["read", "https://www.reddit.com/r/x/comments/abc/t/", "--limit", "20"]),
    ("reddit", "read", {"target": "abc123"}, ["read", "abc123", "--limit", "20"]),
    ("reddit", "subreddit", {"name": "r/snacks"}, ["subreddit", "snacks", "--limit", "10"]),
    ("twitter", "search", {"query": "brand"}, ["search", "brand", "--limit", "10"]),
    ("twitter", "profile", {"username": "@Acme"}, ["profile", "Acme"]),
    ("twitter", "tweets", {"username": "acme"}, ["tweets", "acme", "--limit", "10"]),
    ("twitter", "thread", {"target": "https://x.com/acme/status/1234567890?s=20"}, ["thread", "1234567890"]),
    ("facebook", "search", {"query": "brand"}, ["search", "brand", "--limit", "10"]),
    ("facebook", "profile", {"username": "acme"}, ["profile", "acme"]),
    ("linkedin", "jobs", {"query": "marketing"}, ["search", "marketing", "--limit", "10"]),
    ("linkedin", "jobs", {"query": "marketing", "location": "Austin"}, ["search", "marketing", "--limit", "10", "--location", "Austin"]),
    ("linkedin", "read", {"target": "3912345678"}, ["job-detail", "https://www.linkedin.com/jobs/view/3912345678/"]),
    ("xiaohongshu", "search", {"query": "x"}, ["search", "x", "--limit", "10"]),
    ("xiaohongshu", "read", {"target": "https://www.xiaohongshu.com/explore/1?xsec_token=t"}, ["note", "https://www.xiaohongshu.com/explore/1?xsec_token=t"]),
    ("xiaohongshu", "comments", {"target": "https://www.xiaohongshu.com/explore/1?xsec_token=t"}, ["comments", "https://www.xiaohongshu.com/explore/1?xsec_token=t", "--limit", "10", "--with-replies", "true"]),
    ("boss", "read", {"target": "https://www.zhipin.com/web/geek/job_detail/abc_12-3.html?x=1"}, ["detail", "abc_12-3"]),
    ("boss", "search", {"query": "designer"}, ["search", "designer", "--city", "全国", "--limit", "10"]),
    ("boss", "search", {"query": "designer", "city": "上海"}, ["search", "designer", "--city", "上海", "--limit", "10"]),
])
def test_each_connected_opencli_action_becomes_the_command_agent_reach_itself_runs(channel, action, values, expected):
    argv, clean = CAT.prepare(channel, action, values)
    assert argv == expected and clean["limit"] == "10"


@pytest.mark.parametrize("channel, action, values, message", [
    ("xueqiu", "quote", {"symbol": "x"}, "not connected here: stock quotes"),
    ("bilibili", "search", {"query": "x"}, "bili-cli"),
    ("twitter", "read", {"target": "x"}, "has no connected action 'read'"),
    ("youtube", "transcribe", {"target": "x"}, "Whisper"),
    ("linkedin", "jobs", {}, "needs query"),
    ("reddit", "read", {"target": "not a reddit link"}, "post link or a post id"),
    ("xiaohongshu", "read", {"target": "https://example.com/x"}, "xsec_token"),
    ("xiaohongshu", "comments", {"target": "http://www.xiaohongshu.com/explore/1"}, "https Xiaohongshu note link"),
    ("boss", "read", {"target": "https://www.zhipin.com/other"}, "direct zhipin.com job link"),
    ("nosuch", "search", {"query": "x"}, "not connected here"),
])
def test_a_bad_channel_action_or_input_is_refused_with_the_real_reason(channel, action, values, message):
    with pytest.raises(ValueError, match=message):
        CAT.prepare(channel, action, values)


def test_non_opencli_channels_get_clean_inputs_and_no_opencli_command():
    assert CAT.prepare("youtube", "search", {"query": "q"}, limit=5) == (None, {"query": "q", "limit": "5"})
    assert CAT.prepare("github", "read", {"target": "yt-dlp/yt-dlp"})[0] is None


# ---- GitHub through its public API ---------------------------------------------------------------------------------------------------------------

class Net:
    def __init__(self, routes=None, error=None):
        self.routes, self.error, self.calls = routes or {}, error, []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        if self.error:
            raise self.error
        for part, body in self.routes.items():
            if url.split("?")[0].endswith(part):
                return body if isinstance(body, bytes) else json.dumps(body).encode()
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)


REPO = {"full_name": "acme/widgets", "html_url": "https://github.com/acme/widgets", "description": "Widgets", "owner": {"login": "acme"}, "stargazers_count": 120,
        "language": "Python", "forks_count": 4, "pushed_at": "2026-10-01T00:00:00Z", "topics": ["ecommerce", "ads"]}


def test_a_repository_search_returns_rows_and_the_readme_and_issue_reads_return_their_text():
    net = Net({"/search/repositories": {"items": [REPO, {**REPO, "full_name": "acme/other"}]}, "/repos/acme/widgets/readme": b"# Widgets\nA readme", "/repos/acme/widgets/issues/7/comments": [{"user": {"login": "bob"}, "body": "me too"}],
               "/repos/acme/widgets/issues/7": {"title": "Bug", "html_url": "https://github.com/acme/widgets/issues/7", "body": "It breaks", "user": {"login": "amy"}, "created_at": "t", "comments": 1}, "/repos/acme/widgets": REPO})
    gh = GH.GitHubPublic(fetch=net)
    rows = gh.call(["search", "widgets ads", "--limit", 1])
    assert len(rows) == 1 and rows[0]["title"] == "acme/widgets" and rows[0]["likes"] == 120 and rows[0]["topics"] == "ecommerce, ads" and "q=widgets+ads" in net.calls[0][0] and "per_page=1" in net.calls[0][0]
    repo = gh.call(["read", "https://github.com/acme/widgets"])[0]
    assert repo["content"].startswith("# Widgets") and repo["url"] == "https://github.com/acme/widgets"
    issue = gh.call(["read", "acme/widgets#7"])[0]
    assert issue["title"] == "Bug" and "It breaks" in issue["content"] and "@bob: me too" in issue["content"]


def test_a_repository_with_no_readme_is_still_read_and_every_failure_has_its_real_code():
    net = Net({"/repos/acme/widgets": REPO})
    assert GH.GitHubPublic(fetch=net).call(["read", "acme/widgets"])[0]["content"] == ""
    for exc, code in ((urllib.error.HTTPError("u", 404, "n", {}, None), K.NOT_FOUND), (urllib.error.HTTPError("u", 403, "n", {}, None), K.RATE_LIMITED), (urllib.error.HTTPError("u", 429, "n", {}, None), K.RATE_LIMITED),
                      (urllib.error.HTTPError("u", 500, "n", {}, None), K.UPSTREAM_BROKEN), (TimeoutError(), K.TIMEOUT), (urllib.error.URLError("down"), K.UPSTREAM_BROKEN)):
        with pytest.raises(K.Failure) as seen:
            GH.GitHubPublic(fetch=Net(error=exc)).call(["search", "x"])
        assert seen.value.code == code
    with pytest.raises(K.Failure, match="GITHUB_TOKEN"):
        GH.GitHubPublic(fetch=Net(error=urllib.error.HTTPError("u", 403, "n", {}, None))).call(["search", "x"])
    with pytest.raises(K.Failure) as seen:
        GH.GitHubPublic(fetch=Net(error=urllib.error.HTTPError("u", 403, "n", {}, None)), token="t").call(["search", "x"])
    assert "GITHUB_TOKEN" not in str(seen.value)
    for bad in (["read", "not a target"], ["frobnicate", "x"]):
        with pytest.raises(K.Failure) as seen:
            GH.GitHubPublic(fetch=Net()).call(bad)
        assert seen.value.code == K.BAD_INPUT


def test_the_token_is_sent_only_when_set_and_only_to_the_github_api():
    net = Net({"/search/repositories": {"items": []}})
    GH.GitHubPublic(fetch=net).call(["search", "x"])
    assert "Authorization" not in net.calls[0][1] and net.calls[0][0].startswith("https://api.github.com/")
    GH.GitHubPublic(fetch=net, token="secret").call(["search", "x"])
    assert net.calls[1][1]["Authorization"] == "Bearer secret" and net.calls[1][0].startswith("https://api.github.com/")
    assert [GH.parse_target(t) for t in ("o/r", "https://github.com/o/r", "https://github.com/o/r.git", "https://github.com/o/r/issues/5", "https://github.com/o/r/pull/9", "o/r#3")] == [
        ("o", "r", None), ("o", "r", None), ("o", "r", None), ("o", "r", 5), ("o", "r", 9), ("o", "r", 3)]


# ---- the shared client factory ---------------------------------------------------------------------------------------------------------------------

def test_the_shared_client_factory_gives_each_channel_its_own_client_under_the_owners_switches(tmp_path):
    s = SV.load_settings("observatory.yaml", str(tmp_path))
    assert isinstance(SV.agent_reach_client(s, "github", "general_research", environ={"GITHUB_TOKEN": "t"}), GH.GitHubPublic)
    assert SV.agent_reach_client(s, "github", "general_research", environ={"GITHUB_TOKEN": "t"}).token == "t"
    tw = SV.agent_reach_client(s, "twitter", "social_discovery", environ={})
    assert tw.site == "twitter" and tw.needs_browser if hasattr(tw, "needs_browser") else tw.site == "twitter"
    with pytest.raises(K.Failure) as seen:
        SV.agent_reach_client(s, "web", "general_research", environ={})
    assert seen.value.code == K.BAD_INPUT and "shared page readers" in str(seen.value)
    for channel in ("twitter", "github", "linkedin"):
        SV.save_overrides(str(tmp_path), {f"services.agent_reach.channels.{channel}": False}, "owner", "2026-11-04T10:00:00Z", "off")
        with pytest.raises(K.Failure) as seen:
            SV.agent_reach_client(SV.load_settings("observatory.yaml", str(tmp_path)), channel, "general_research", environ={})
        assert seen.value.code == K.NEED_SETUP and f"services.agent_reach.channels.{channel}" in str(seen.value)
        SV.save_overrides(str(tmp_path), {}, "owner", "2026-11-04T10:01:00Z", "on")
        SV.agent_reach_client(SV.load_settings("observatory.yaml", str(tmp_path)), channel, "general_research", environ={})
