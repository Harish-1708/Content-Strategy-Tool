"""Stage 11: the search fallback (RustXNG or any SearXNG-compatible metasearch), off until its address is set."""
import io
import json
import urllib.error

import pytest

from contentintel import search_fallback as SF


def test_the_fallback_is_asked_only_when_the_primary_fails_or_finds_nothing():
    asked, notes = [], []
    primary_ok = lambda q: [{"url": "https://primary.example/a", "title": "A"}]                                       # noqa: E731

    def fallback(base, q):
        asked.append((base, q))
        return [{"url": "https://fallback.example/b", "title": "B"}]
    search = SF.with_fallback(primary_ok, "https://rust.example", notes=notes, searcher=fallback)
    assert search("q1")[0]["url"] == "https://primary.example/a" and asked == []                                      # the primary is always asked first
    empty = SF.with_fallback(lambda q: [], "https://rust.example", notes=notes, searcher=fallback)
    assert empty("q2")[0]["url"] == "https://fallback.example/b" and asked == [("https://rust.example", "q2")]

    def broken(q):
        raise RuntimeError("Serper down")
    failing = SF.with_fallback(broken, "https://rust.example", notes=notes, searcher=fallback)
    assert failing("q3")[0]["url"] == "https://fallback.example/b" and any("primary search failed (RuntimeError)" in n for n in notes)
    missing = SF.with_fallback(None, "https://rust.example", notes=notes, searcher=fallback)
    assert missing("q4") and len(asked) == 3                                                                          # no primary at all: the fallback answers


def test_with_no_address_nothing_changes_and_a_fallback_that_fails_too_returns_nothing_and_says_so():
    primary = lambda q: []                                                                                            # noqa: E731
    assert SF.with_fallback(primary, "") is primary and SF.with_fallback(None, "") is None
    notes = []

    def down(base, q):
        raise urllib.error.URLError("refused")
    both = SF.with_fallback(primary, "https://rust.example", notes=notes, searcher=down)
    assert both("q") == [] and notes == ["search: the fallback failed too (URLError)"]
    assert SF.configured({"RUSTXNG_URL": " https://rust.example "}) == "https://rust.example" and SF.configured({}) == ""


def test_the_metasearch_reply_is_read_as_url_and_title_pairs_and_a_bad_address_is_refused():
    seen = []

    def opener(req, timeout=0):
        seen.append(req.full_url)
        return io.BytesIO(json.dumps({"results": [{"url": "https://a.example", "title": "A"}, {"title": "no url"}, {"url": "https://b.example"}]}).encode())
    got = SF.searx_search("https://rust.example/", "soft overalls & more", opener=lambda req, timeout=0: type("R", (), {"__enter__": lambda s: opener(req), "__exit__": lambda s, *a: None})())
    assert got == [{"url": "https://a.example", "title": "A"}, {"url": "https://b.example", "title": ""}]
    assert seen[0] == "https://rust.example/search?q=soft+overalls+%26+more&format=json"
    with pytest.raises(ValueError):
        SF.searx_search("ftp://rust.example", "q")
    with pytest.raises(ValueError):
        SF.searx_search("rust.example", "q")
