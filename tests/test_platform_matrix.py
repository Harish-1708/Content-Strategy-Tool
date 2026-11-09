"""The integration matrix (docs/platform/integration-matrix.md) is checked against the code, so it cannot claim a connection that is not there or leave out a module that reaches the outside world."""
import re
from pathlib import Path

import pytest

from contentintel.observatory import config as OC

ROOT = Path(__file__).resolve().parents[1]
DOC = (ROOT / "docs" / "platform" / "integration-matrix.md").read_text(encoding="utf-8")
STATUSES = {"Connected", "Not applicable", "Not connected"}
SERVICE_WORDS = {"scrapling": "Scrapling", "agent_reach": "Agent-Reach", "search": "Search", "ai_gateway": "AI Gateway", "page_readers": "Page readers"}


def rows():
    out = []
    for line in DOC.splitlines():
        if line.startswith("| ") and not line.startswith("| Area") and "---" not in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            assert len(cells) == 7, line[:100]
            out.append(dict(zip(("area", "service", "status", "where", "tests", "live", "reason"), cells)))
    return out


ROWS = rows()


def test_every_row_has_a_known_status_and_the_columns_that_status_needs():
    assert len(ROWS) >= 30
    for r in ROWS:
        assert r["status"] in STATUSES, r["area"]
        if r["status"] == "Connected":
            assert r["where"] and r["tests"] and r["live"].startswith("Not live-tested: "), r["area"] + " / " + r["service"]
        else:
            assert r["reason"] and not r["where"] and not r["tests"], r["area"] + " / " + r["service"]


def test_no_row_claims_to_be_live_tested_because_nothing_has_been():
    assert "Live-tested" not in DOC.replace("Not live-tested", "") and all(r["live"] == "" or r["live"].startswith("Not live-tested") for r in ROWS)


@pytest.mark.parametrize("row", [r for r in ROWS if r["status"] == "Connected"], ids=lambda r: f"{r['area'][:30]}|{r['service'][:25]}")
def test_every_connected_row_names_a_real_function_and_real_tests(row):
    path, _, symbol = row["where"].partition("::")
    target = ROOT / path
    assert target.is_file() and symbol and symbol in target.read_text(encoding="utf-8"), row["where"]
    for token in re.split(r",\s*", row["tests"]):
        test_file, _, name = token.partition("::")
        text = (ROOT / test_file).read_text(encoding="utf-8")
        assert re.search(rf"def {re.escape(name)}\b", text), token


def test_every_built_in_service_is_connected_somewhere_and_the_matrix_mentions_each():
    connected = " ".join(r["service"] for r in ROWS if r["status"] == "Connected")
    for name in OC.SERVICE_REGISTRY:
        word = SERVICE_WORDS.get(name)
        if word:
            assert word in connected, name


OUTBOUND = re.compile(r"requests\.|urllib|http_get|default_http|httpx|build_providers|Claude\(|claude\.complete|json_task|playwright")


def test_every_file_that_reaches_the_outside_world_or_claude_is_accounted_for_in_the_matrix():
    missing = []
    for path in sorted((ROOT / "contentintel").rglob("*.py")):
        if path.name in ("__init__.py", "__main__.py"):
            continue
        if OUTBOUND.search(path.read_text(encoding="utf-8")):
            rel = path.relative_to(ROOT).as_posix()
            if rel not in DOC:
                missing.append(rel)
    assert not missing, f"add these to docs/platform/integration-matrix.md: {missing}"


def test_the_documented_limits_name_the_genuine_reasons_and_invent_no_restriction():
    text = DOC.lower()
    for phrase in ("agent-reach currently has no tiktok channel", "no connection has been live-tested", "never runs on a github-hosted runner", "makes no request without a recorded reddit approval"):
        assert phrase in text
