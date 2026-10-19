"""Break the code on purpose and confirm the tests notice.

For every mutation below, a throwaway copy of the project gets one deliberate bug
and the full test suite runs against it. CAUGHT means at least one test failed
(good). SURVIVED means every test still passed, so a real regression of that
kind would slip through unnoticed. The real source files are never modified.

Run from the project root:   python tests/sabotage/run_sabotage.py
Exit code 0 only if the clean copy passes and every mutation is caught.
"""
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (id, file, text to find exactly once, replacement, what the bug simulates)
MUTATIONS = [
    # ---- fetching -----------------------------------------------------------------
    ("fetch-first-page-only", "contentintel/shopify_fetch.py",
     "        sleep(delay)\n    stamp",
     "        sleep(delay)\n        break\n    stamp",
     "paging stops after page 1 (the original 30-product bug)"),
    ("fetch-stop-on-short-page", "contentintel/shopify_fetch.py",
     "        if not batch:\n            terminal = True",
     "        if not batch or len(batch) < limit:\n            terminal = True",
     "stops when a page is shorter than the requested limit"),
    ("fetch-no-loop-guard", "contentintel/shopify_fetch.py",
     "        if fresh == 0:\n            loop = True",
     "        if False:\n            loop = True",
     "a server repeating one page is never detected"),
    ("fetch-trailing-slash", "contentintel/shopify_fetch.py",
     'url = f"https://{domain}/products.json"',
     'url = f"https://{domain}/products.json/"',
     "URL gains a trailing slash (the Defi 404)"),
    ("fetch-retry-404", "contentintel/shopify_fetch.py",
     "if resp.status_code == 404:", "if resp.status_code == 999:",
     "a 404 is retried instead of explained"),
    ("fetch-duplicates-lost", "contentintel/shopify_fetch.py",
     "                duplicates.append(pid)", "                pass",
     "duplicate ids across pages are silently dropped"),
    # ---- completeness gate -----------------------------------------------------------
    ("gate-no-baseline-floor", "contentintel/gate.py",
     'if baseline_count and count < cfg["baseline_floor"] * baseline_count:',
     'if False and baseline_count and count < cfg["baseline_floor"] * baseline_count:',
     "stepwise catalogue erosion is no longer caught"),
    ("gate-never-fails-on-drop", "contentintel/gate.py",
     'if drop > cfg["fail_drop"] and gap >= min_abs:',
     "if drop > 1.0 and gap >= min_abs:",
     "a truncated feed only warns"),
    ("gate-unbaselined-passes", "contentintel/gate.py",
     "    elif not baseline_count:", "    elif False:",
     "an unconfirmed catalogue is treated as trusted"),
    ("gate-no-default-page-size-check", "contentintel/gate.py",
     "if not confirmed_end and count in DEFAULT_PAGE_SIZES:",
     "if False and count in DEFAULT_PAGE_SIZES:",
     "a count equal to a page size no longer raises a warning"),
    ("gate-sitemap-direction-reversed", "contentintel/gate.py",
     "missing = sorted(set(sitemap_handles) - set(feed_handles or []))",
     "missing = sorted(set(feed_handles or []) - set(sitemap_handles))",
     "sitemap cross-check compares the wrong way round"),
    ("gate-empty-catalogue-ok", "contentintel/gate.py",
     "    if count == 0:", "    if count == -1:",
     "zero products is not treated as a failure"),
    ("gate-fail-ignored-for-status", "contentintel/gate.py",
     '    if "fail" in levels:\n        status = "fail"',
     '    if False:\n        status = "fail"',
     "failures no longer produce a failed status"),
    # ---- normalising and hashing ---------------------------------------------------------
    ("hash-includes-updated-at", "contentintel/normalize.py",
     '"title": record["title"], "handle": record["handle"],',
     '"title": record["title"], "handle": record["handle"], "updated_at": record["updated_at"],',
     "every product looks changed whenever Shopify touches updated_at"),
    ("hash-ignores-price", "contentintel/normalize.py",
     '([v["id"], v["title"], v["sku"], v["price"], v["compare_at_price"]]',
     '([v["id"], v["title"], v["sku"], None, v["compare_at_price"]]',
     "price changes go unnoticed"),
    ("hash-image-version-counts", "contentintel/normalize.py",
     'return (src or "").split("?", 1)[0]', 'return (src or "")',
     "CDN version strings look like image changes"),
    ("hash-ignores-facts", "contentintel/normalize.py",
     '"facts": {k: [f["value"], f["source"]] for k, f in sorted(record["facts"].items())},',
     '"facts": {},',
     "manual fact changes go unnoticed"),
    # ---- classification ----------------------------------------------------------------------
    ("classify-no-exclusions", "contentintel/classify.py",
     "    if product_type and product_type.strip().lower() in {t.lower() for t in ex[\"product_types\"]}:",
     "    if False and product_type:",
     "gift cards and personalization are no longer excluded by type"),
    ("classify-no-title-exclusions", "contentintel/classify.py",
     '    if _matches(ex["title_patterns"], title):', "    if False:",
     "gift cards are no longer excluded by title"),
    ("classify-no-tag-exclusions", "contentintel/classify.py",
     '    if _has_tag(ex["tags"], tag_set):', "    if False:",
     "products tagged hidden are no longer excluded"),
    ("classify-sale-flag-inverted", "contentintel/classify.py",
     'v["compare_at_price"] > v["price"]', 'v["compare_at_price"] < v["price"]',
     "sale products are not recognised"),
    ("classify-inactive-inverted", "contentintel/classify.py",
     "if active_tag and active_tag.strip().lower() not in tag_set:",
     "if active_tag and active_tag.strip().lower() in tag_set:",
     "active products become inactive and vice versa"),
    ("classify-coming-soon-title-ignored", "contentintel/classify.py",
     'if rule and (_has_tag(rule["tags"], tag_set) or _matches(rule["title_patterns"], title)):',
     'if rule and (_has_tag(rule["tags"], tag_set)):',
     "coming-soon products identified by title are missed"),
    ("classify-flags-ignore-tags", "contentintel/classify.py",
     'if _has_tag(rule["tags"], tag_set) or _matches(rule["title_patterns"], title):\n            flags.append(name)',
     'if _matches(rule["title_patterns"], title):\n            flags.append(name)',
     "limited-edition and new flags from tags are lost"),
    ("classify-family-no-split", "contentintel/classify.py",
     '    if " - " in clean:\n        clean = clean.rsplit(" - ", 1)[0].strip()',
     "    pass",
     "colour variants sold as separate products are no longer grouped"),
    # ---- facts ---------------------------------------------------------------------------------
    ("facts-pocket-false-positive", "contentintel/facts.py",
     r"\s+(?:[A-Za-z]+\s+)?pockets?\b", r"\s+(?:[A-Za-z]+\s+){0,3}pockets?\b",
     "'one holds a little pocket' is read as a pocket count"),
    ("facts-materials-double-count", "contentintel/facts.py",
     "found.setdefault((fibre.lower(), int(pct)), label)",
     "found[(label.lower(), int(pct))] = label",
     "the same blend is counted twice and totals exceed 100"),
    ("facts-manual-does-not-outrank", "contentintel/facts.py",
     'merged[name] = _fact(value, source="manual", status="stated_by_user")',
     'merged.setdefault(name, _fact(value, source="manual", status="stated_by_user"))',
     "the website overrides a person's stated fact"),
    ("facts-nothing-required", "contentintel/facts.py",
     "missing_required = [n for n in brand.required_facts if n not in facts]",
     "missing_required = []",
     "products with no facts are reported as ready"),
    ("facts-allergens-in-apparel", "contentintel/facts.py",
     '    elif schema == "food":', '    if True:',
     "food facts are extracted for every category"),
    # ---- change detection ---------------------------------------------------------------------------
    ("diff-removals-ignored", "contentintel/diff.py",
     '        if pid in cur_records or entry["presence"] != "present":\n            continue',
     "        if True:\n            continue",
     "a product that vanishes is never flagged"),
    ("diff-stock-ignored", "contentintel/diff.py",
     "            if stock:\n                events.append", "            if False:\n                events.append",
     "sold-out and restock events disappear"),
    ("diff-returns-ignored", "contentintel/diff.py",
     '        if entry["presence"] != "present":\n            events.append(_event("returned"',
     '        if False:\n            events.append(_event("returned"',
     "a returning product is not reported"),
    ("diff-confirm-anything", "contentintel/diff.py",
     '    if entry["presence"] != "suspected_removed":\n        raise ValueError',
     '    if False:\n        raise ValueError',
     "a present product can be marked removed"),
    ("diff-price-not-reported", "contentintel/diff.py",
     'if a["price"] != b["price"]:', "if False:",
     "price changes are detected but not described"),
    ("diff-new-products-ignored", "contentintel/diff.py",
     '            if not initial:\n                events.append(_event("new", rec))',
     "            pass",
     "new products are not reported"),
    # ---- sync, baseline and state ----------------------------------------------------------------------
    ("sync-failed-run-accepted", "contentintel/sync.py",
     '    if gate.status == "fail":\n        return _blocked(brand, gate, now, store, meta, len(records))',
     '    if gate.status == "fail" and False:\n        return _blocked(brand, gate, now, store, meta, len(records))',
     "a failed sync still replaces the last good snapshot"),
    ("sync-unbaselined-accepted", "contentintel/sync.py",
     '    if gate.status == "unbaselined":\n        store.save_pending', '    if False:\n        store.save_pending',
     "an unconfirmed catalogue is promoted without confirmation"),
    ("sync-stale-failures-ignored", "contentintel/sync.py",
     'f.get("after_snapshot") == snap_at', 'f.get("after_snapshot") == "never"',
     "a failed sync no longer makes the catalogue unusable"),
    ("sync-baseline-count-unchecked", "contentintel/sync.py",
     'if snapshot["count"] != int(confirmed_count):', "if False:",
     "any number can confirm the baseline"),
    ("sync-baseline-reconfirm-allowed", "contentintel/sync.py",
     "    if store.load_baseline(slug):\n        raise BaselineError(\"A baseline is already",
     "    if False:\n        raise BaselineError(\"A baseline is already",
     "a confirmed baseline can be silently replaced"),
    ("sync-drops-unknown-handle-warning", "contentintel/sync.py",
     "unknown = sorted(h for h in brand.manual_facts if h not in handles)", "unknown = []",
     "manual facts for a missing product are not reported"),
    # ---- brand isolation -----------------------------------------------------------------------------------
    ("store-cross-brand-write", "contentintel/store.py",
     '        if snapshot.get("brand") != slug:', "        if False:",
     "one brand's snapshot can be saved under another brand"),
    ("brands-unsafe-slug", "contentintel/brands.py",
     "if not isinstance(slug, str) or not SLUG_RE.match(slug):", "if not isinstance(slug, str):",
     "unsafe brand names are accepted"),
    ("brands-gate-unknown-key", "contentintel/brands.py",
     '        if key not in DEFAULT_GATE:\n            raise BrandError(f"{slug}: unknown gate setting {key!r}")',
     "        pass",
     "a typo in a gate setting is silently ignored"),
    # ---- brief rules ----------------------------------------------------------------------------------------
    ("rules-order-reversed", "contentintel/rules.py",
     "(i_first != -1 and i_first > i_then)", "(i_first != -1 and i_first < i_then)",
     "word-order rule fires on the correct order"),
    ("rules-phrase-substring", "contentintel/rules.py",
     'if re.search(r"\\b" + re.escape(item["phrase"]) + r"\\b", text, re.I):',
     'if re.search(re.escape(item["phrase"]), text, re.I):',
     "'spa' is flagged inside 'space'"),
    ("rules-allowlist-inverted", "contentintel/rules.py",
     'if approved and record.get("handle") not in approved:', 'if approved and record.get("handle") in approved:',
     "approved products are rejected and others accepted"),
    ("rules-limited-edition-allowed", "contentintel/rules.py",
     '        if flag in record.get("flags", []):', "        if False:",
     "limited-edition prints are no longer blocked"),
    ("rules-banned-char-ignored", "contentintel/rules.py",
     "        count = text.count(item[\"char\"])\n        if count:", "        count = 0\n        if count:",
     "em dashes and exclamation points slip through"),
    # ---- command line ----------------------------------------------------------------------------------------
    ("cli-blocked-exits-zero", "contentintel/cli.py", '"blocked": 2', '"blocked": 0',
     "a blocked sync looks successful to the scheduler"),
]


def prepare_copy(dest):
    shutil.copytree(
        ROOT, dest,
        ignore=shutil.ignore_patterns(".git", "data", "__pycache__", ".pytest_cache", "*.pyc"))


def run_tests(workdir):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"],
        cwd=workdir, capture_output=True, text=True, timeout=300)
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    return proc.returncode, tail


def apply(workdir, rel, old, new):
    path = Path(workdir) / rel
    text = path.read_text(encoding="utf-8")
    hits = text.count(old)
    if hits != 1:
        return f"text found {hits} times in {rel} (expected exactly once)"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return None


def one(mutation):
    mid, rel, old, new, what = mutation
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "p"
        prepare_copy(work)
        problem = apply(work, rel, old, new)
        if problem:
            return mid, "ERROR", problem, what
        code, tail = run_tests(work)
        return mid, ("CAUGHT" if code != 0 else "SURVIVED"), tail, what


def main():
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "p"
        prepare_copy(work)
        code, tail = run_tests(work)
    print(f"Clean copy: {'PASS' if code == 0 else 'FAIL'}  ({tail})")
    if code != 0:
        print("The unmodified project fails its own tests; fix that before sabotage testing.")
        return 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, MUTATIONS))
    width = max(len(r[0]) for r in results)
    for mid, status, detail, what in results:
        mark = {"CAUGHT": "ok  ", "SURVIVED": "!!  ", "ERROR": "??  "}[status]
        extra = "" if status == "CAUGHT" else f"   <- {detail}"
        print(f"{mark}{mid:<{width}}  {status:<8} {what}{extra}")
    caught = sum(1 for r in results if r[1] == "CAUGHT")
    print(f"\n{caught} of {len(results)} deliberate bugs were caught by the tests.")
    bad = [r for r in results if r[1] != "CAUGHT"]
    if bad:
        print("Not caught: " + ", ".join(r[0] for r in bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
