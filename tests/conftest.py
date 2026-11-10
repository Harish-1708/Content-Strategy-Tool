import copy
import os
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONTENTINTEL_LEGACY_GUARDS", "1")       # the existing tests were written for the guards-on behaviour; tests of the shipped (OFF) default run in a clean process
sys.path.insert(0, str(ROOT))

from contentintel.brands import load_all  # noqa: E402
from contentintel.normalize import normalize_all  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BRANDS_DIR = ROOT / "brands"
BRIEFS_DIR = ROOT / "briefs"

_CACHE = {}


def _no_real_download(url, dest, cookies, max_mb, timeout):
    return None, "no real downloads in tests"


def _no_real_browser(browser_cfg, creds, debug_dir):
    def factory(site):
        raise RuntimeError("no real browser in tests")
    return factory


def _no_real_ytdlp(*args, **kwargs):
    raise ConnectionError("no real yt-dlp in tests")


def _no_real_google(key):
    raise ConnectionError("no real Google in tests")


@pytest.fixture(autouse=True)
def tests_never_touch_the_real_tools(monkeypatch):
    """GitHub has yt-dlp, a network and, in the repository, your real sheet id. A test that did not set up its own stand-in must never reach any of
    them: not the real yt-dlp (it would ask Instagram), not a real browser, not real Google, and not the repository's own sheets.yaml."""
    from contentintel import cli
    from contentintel import sheets as sheets_mod
    monkeypatch.setattr(cli, "_VIDEO_RUNNER", _no_real_download)
    monkeypatch.setattr(cli, "_BROWSER_FACTORY", _no_real_browser)
    monkeypatch.setattr(cli, "_SHEETS_CONNECT", _no_real_google)
    monkeypatch.setattr(cli, "_YTDLP_RUNNER", _no_real_ytdlp)
    monkeypatch.setattr(cli, "_YT_API_GET", _no_real_ytdlp)
    real = sheets_mod.load_sheet_config
    monkeypatch.setattr(sheets_mod, "load_sheet_config", lambda path, slugs: real(FIXTURES / "sheets_blank.yaml", slugs) if path == "sheets.yaml" else real(path, slugs))

# Saved catalogues and brief hooks the tests read. They must be uploaded to tests/fixtures/.
REQUIRED_FIXTURES = (
    "swoveralls_full_72.json", "swoveralls_truncated_30.json",
    "duderobe_fetch_a.json", "duderobe_fetch_b.json", "defi_snacks.json",
    "swoveralls_brief_hooks.json", "duderobe_brief_hooks.json",
)


def pytest_collection_modifyitems(config, items):
    """One clear message instead of a hundred file-not-found failures."""
    missing = [n for n in REQUIRED_FIXTURES if not (FIXTURES / n).is_file()]
    if missing:
        pytest.exit(
            "Test data is missing from tests/fixtures/: " + ", ".join(missing) + ". "
            "Upload those files to the tests/fixtures/ folder (README, one-time set-up, step 2).",
            returncode=4)


def load_json(name):
    if name not in _CACHE:
        with open(FIXTURES / name, encoding="utf-8") as fh:
            _CACHE[name] = json.load(fh)
    return copy.deepcopy(_CACHE[name])


def raw_products(name):
    """Fresh copy of the products list from a saved catalogue fixture."""
    return load_json(name)["products"]


@pytest.fixture(scope="session")
def brands():
    return load_all(BRANDS_DIR)


@pytest.fixture
def swov(brands):
    return brands["swoveralls"]


@pytest.fixture
def dude(brands):
    return brands["duderobe"]


@pytest.fixture
def defi(brands):
    return brands["defi_snacks"]


@pytest.fixture
def swov_records(swov):
    records, problems = normalize_all(raw_products("swoveralls_full_72.json"), swov)
    assert not problems
    return records


def by_title(records, title):
    matches = [r for r in records.values() if r["title"] == title]
    assert len(matches) == 1, f"{title!r}: {len(matches)} matches"
    return matches[0]


def by_handle(records, handle):
    matches = [r for r in records.values() if r["handle"] == handle]
    assert len(matches) == 1, f"{handle!r}: {len(matches)} matches"
    return matches[0]


class FakeServer:
    """Pretends to be /products.json. `page_size` caps each page like Shopify's default."""

    def __init__(self, products, page_size=30, repeat_first_page=False):
        self.products = products
        self.page_size = page_size
        self.repeat_first_page = repeat_first_page
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        page = params["page"]
        size = min(params["limit"], self.page_size)
        if self.repeat_first_page:
            page = 1
        start = (page - 1) * size
        return {"products": self.products[start:start + size]}


@pytest.fixture(autouse=True)
def fresh_sheet_write_pacer(monkeypatch):
    """The sheet write pacer counts writes over a real minute. Every test starts with an empty count, so one test's writes can never make
    another wait, and no test really sleeps (a test that wants to see the waiting builds its own WritePacer)."""
    from contentintel import sheets
    monkeypatch.setattr(sheets, "_PACER", sheets.WritePacer())
    monkeypatch.setattr(sheets, "_SLEEP", lambda seconds: None)           # no test really waits; a test that checks the waits records them itself


@pytest.fixture(autouse=True)
def _reset_guards():
    """Guard switches are process-wide: every test starts from the environment's default and leaves nothing behind."""
    from contentintel import guards
    guards.configure(None)
    yield
    guards.configure(None)
