"""Break the Stage 3 code on purpose and confirm the tests notice (a faster companion to run_sabotage.py).

Each mutation gets a throwaway copy of the project with one deliberate bug, and only the Stage 3 test files run against it. CAUGHT means a test failed (good).
SURVIVED means every test passed, a gap: a real bug of that kind would slip through. The real files are never touched.

Run from the project root:   python tests/sabotage/run_observatory_sabotage.py
Set SCRAPLING_PYTHON first to include the real-library worker tests. Exit code 0 only if the clean copy passes and every mutation is caught.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OBS, AI = "contentintel/observatory/", "contentintel/ai/"
TESTS = ["tests/test_observatory_*.py", "tests/test_ai_github.py", "tests/test_ai_health_tab.py", "tests/test_ai_config.py", "tests/test_providers.py"]

# (id, file, text found exactly once, replacement, what the bug simulates)
MUTATIONS = [
    ("gate-ignores-method", OBS + "gatecheck.py", '    if method not in entry["methods"]:', "    if False:", "a host listed for one method may use them all"),
    ("gate-lookalike-host", OBS + "gatecheck.py", "        if host in by_host:\n            return by_host[host]",
     "        hit = [k for k in by_host if host.endswith(k)]\n        if hit:\n            return by_host[hit[0]]", "shop.example.evil.test is treated as shop.example"),
    ("worker-no-gate", OBS + "scrapling_worker.py", "            if need:\n                try:", "            if False:\n                try:", "advanced methods run for any host"),
    ("worker-google-referer", OBS + "scrapling_worker.py", '"google_search": False', '"google_search": True', "the stealth browser sets a Google referer"),
    ("worker-ignores-robots", OBS + "scrapling_worker.py", '                if mode == "obey":', "                if False:", "robots.txt is ignored under the default policy"),
    ("worker-dishonest-default-agent", OBS + "scrapling_worker.py", 'headers={"User-Agent": self.agent}, timeout=self.job.get("timeout", 20), follow_redirects=False, **extra)',
     'headers={}, timeout=self.job.get("timeout", 20), follow_redirects=False, **extra)', "robots.txt is fetched without our honest identity"),
    ("worker-keeps-hammering-after-challenge", OBS + "scrapling_worker.py", '                self._site(host)["rest"] = "a bot-check or challenge page was shown"', "                pass",
     "the site is not rested after a bot check"),
    ("worker-robots-around-the-proxy", OBS + "scrapling_worker.py", '**({"proxy": proxy} if proxy else {}))', "**{})", "robots.txt is fetched directly while the page goes through the proxy"),
    ("worker-private-addresses", OBS + "scrapling_worker.py", "        if not self.private_ok and not safety.resolves_public(host):", "        if False:", "a name that points inside our network is fetched"),
    ("client-leaks-environment", OBS + "worker_client.py", "    names = set(_PASS)", "    names = set(_PASS) | set(environ)", "the worker receives every secret of the main tool"),
    ("client-job-world-readable", OBS + "worker_client.py", "        job_path.chmod(0o600)", "        pass", "the job file is readable by other users"),
    ("client-advanced-not-checked", OBS + "worker_client.py", "    if need:\n        gate.require(comp[\"domain\"], *need)", "    if False:\n        gate.require(comp[\"domain\"], *need)", "the core builds jobs for unauthorised hosts"),
    ("polite-obeys-nothing", OBS + "polite.py", '                if mode == "obey":\n                    failure', '                if False:\n                    failure', "robots.txt is ignored by the default crawler"),
    ("polite-override-any-host", OBS + "polite.py", '                if entry["host"] == host:', "                if True:", "an override for one site applies to all"),
    ("config-advisory-needs-no-basis", OBS + "config.py", '    if base["robots"] == "advisory" and not base["basis"]:', '    if False:', "ignoring robots needs no recorded reason"),
    ("config-override-needs-no-approver", OBS + "config.py", 'for key in ("host", "basis", "approved_by", "date"):', 'for key in ("host", "basis", "date"):', "an exception need not name who decided"),
    ("config-unbuilt-ai-judge-accepted", OBS + "config.py", '    if out["discovery"]["use_ai"]:', "    if False:", "a setting that does nothing is accepted silently"),
    ("reader-credential-urls-sent", OBS + "readers.py", 'if _SECRET_QUERY.search("?" + parts.query) or', 'if False and _SECRET_QUERY.search("?" + parts.query) or', "an address carrying a token is sent to a third party"),
    ("reader-challenge-accepted", OBS + "readers.py", "    if title and any(title.group(1).strip().startswith(t) for t in _CHALLENGE_TITLES) and len(body_text) < 6000:",
     "    if False:", "a bot-check page is returned as if it were the content"),
    ("reader-obey-ignored", OBS + "readers.py", '            if mode == "obey":\n                failure = K.Failure(K.PLATFORM_BLOCKED, f"{clean} was not read', '            if False:\n                failure = K.Failure(K.PLATFORM_BLOCKED, f"{clean} was not read',
     "the reader's obey policy does nothing"),
    ("reader-no-rate-wait", OBS + "readers.py", "        if len(self.calls) >= limit:", "        if False:", "the free per-minute limit is exceeded"),
    ("exa-limit-not-recognised", "contentintel/providers.py", "            if _EXA_LIMIT.search(text):\n                raise QuotaError(f\"{self.label}: its free limit was reached ({self._clean(text)[:120]})\")",
     "            if False:\n                raise QuotaError('x')", "a hosted-search limit is treated as a generic error"),
    ("exa-requests-paid-contents", "contentintel/providers.py", 'body = {"query": query, "numResults": min(num, 10), "type": "auto"}', 'body = {"query": query, "numResults": min(num, 10), "type": "auto", "contents": {"text": True}}',
     "page contents (billed separately) are requested"),
    ("corpus-ranks-raw-views", OBS + "corpus.py", 'mult = v["views"] / med', 'mult = v["views"] / 1000', "videos are ranked by size, not by beating their own channel"),
    ("corpus-thin-channels-ranked", OBS + "corpus.py", "        if len(views) < min_videos:", "        if len(views) < 1:", "a channel with two videos gets a median"),
    ("corpus-quota-unchecked", OBS + "corpus.py", "        if self.used + units > self.cap:", "        if False:", "the tool's own YouTube ceiling does nothing"),
    ("corpus-history-always-grows", OBS + "corpus.py", "    if last is None or last[\"views\"] != point[\"views\"]", "    if True or last is None or last[\"views\"] != point[\"views\"]", "a view-count point is stored on every run"),
    ("corpus-claims-a-spoken-hook", OBS + "corpus.py", '"spoken_hook": None, "spoken_hook_note": SPOKEN_HOOK_NOTE,', '"spoken_hook": "n/a", "spoken_hook_note": "",', "the missing transcript is hidden"),
    ("corpus-search-ignores-titles", OBS + "corpus.py", '_terms(d["title"]) * 3', '_terms(d["title"]) * 1', "a title match no longer outranks a passing mention"),
    ("corpus-key-in-errors", OBS + "corpus.py", '            raise K.Failure(K.NEED_SETUP, "YouTube rejected the API key, or the YouTube Data API is not switched on for it")',
     '            raise K.Failure(K.NEED_SETUP, "YouTube rejected the API key " + self.key)', "the API key is put in an error message"),
    ("discovery-flat-records", OBS + "discovery.py", 'for rec in (batch.get("items") if isinstance(batch.get("items"), list) else [batch]):', "for rec in [batch]:", "collected batches are read as flat records"),
    ("discovery-short-names-match", OBS + "discovery.py", "    if len(words) < 2 and not (label", "    if False and not (label", "one short word matches any title containing it"),
    ("discovery-overrides-person", OBS + "discovery.py", '        if c.get("decided_by") == "person":\n            continue                                                  # a person\'s choice is final',
     '        if False:\n            continue', "automatic scoring undoes a person's decision"),
    ("discovery-no-auto-cap", OBS + "discovery.py", 'and auto < cfg["auto_monitor_max"] and watching < cfg["max_monitored"]):', "):", "more than 6 competitors are added automatically"),
    ("discovery-unprobed-added", OBS + "discovery.py", 'c.get("shopify") is not None and ai.get("relevant", True)', 'ai.get("relevant", True)', "a site never checked to be a store is added"),
    ("report-drops-websiteless-candidates", OBS + "report.py", 'if isinstance(c, dict) and c.get("key")]}', 'if isinstance(c, dict) and c.get("domain")]}', "candidates without a website vanish on save"),
    ("decisions-saved-in-dry-run", OBS + "report.py", "    if changed and save:", "    if changed:", "a dry run changes what is monitored"),
    ("changes-removal-unconfirmed", OBS + "changes.py", '            if pending[key]["count"] >= c["removal_confirm_runs"]:', "            if True:", "a product missing once is called removed"),
    ("changes-tiny-price-noise", OBS + "changes.py", '            pct = _pct(o["price_min"], n["price_min"])\n            if pct is not None and abs(pct) >= c["price_change_min_pct"]:', '            pct = _pct(o["price_min"], n["price_min"])\n            if pct is not None:', "sub-threshold price changes are reported as events"),
    ("monitor-keeps-going-after-block", OBS + "monitor.py", '        if isinstance(exc, K.Failure) and polite.is_fatal(exc):\n            self.stopped', '        if False:\n            self.stopped', "a blocked site is hammered for every remaining part"),
    ("direct-endpoint-unchecked", AI + "registry.py", "            if not any(base == ok or base.startswith(ok + \"/\") for ok in DIRECT_ENDPOINTS):", "            if False:", "prompts can be sent to an address typed into a file"),
    ("direct-token-leaks", AI + "clients.py", '        return str(text).replace(self.token, "***") if self.token else str(text)', "        return str(text)", "the GitHub token appears in an error message"),
    ("direct-413-misclassified", AI + "clients.py", "        if status in (400, 413, 422):", "        if status in (400, 422):", "a too-large request is treated as a provider outage"),
    ("gateway-gets-direct-providers", AI + "gateway.py", '        if p.id == "claude" or p.direct:', '        if p.id == "claude":', "GitHub Models is sent through the gateway"),
    ("github-silently-off", "ai_providers.yaml", "    label: GitHub Models\n    enabled: true", "    label: GitHub Models\n    enabled: false", "GitHub Models switched off without anyone noticing"),
    ("health-tab-ignores-rest", AI + "sheetrows.py", '        resting = pause.get("until") if pause.get("until", "") > now else ""', '        resting = ""', "a resting provider shows as ready"),
]
MUTATIONS.append(("decide-www-not-stripped", OBS + "commands.py", '        wanted = (OC.host_of(args.competitor) or "").removeprefix("www.")',
                  '        wanted = (OC.host_of(args.competitor) or "")', "a pasted www address does not match the candidate"))


def run_tests(workdir):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    files = [str(p.relative_to(workdir)) for pat in TESTS for p in sorted(workdir.glob(pat))]
    try:
        done = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *files], cwd=workdir, env=env, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return False, "the tests did not finish in 15 minutes"      # a hang is noticed, which counts as caught
    return done.returncode == 0, (done.stdout + done.stderr)[-600:]


def copy_project():
    tmp = Path(tempfile.mkdtemp(prefix="sabotage-"))
    skip = lambda d, names: [n for n in names if n in ("__pycache__", ".git", ".pytest_cache") or (Path(d) == ROOT and n == "data")]   # noqa: E731  (only the top-level data folder)
    shutil.copytree(ROOT, tmp / "p", ignore=skip)
    return tmp, tmp / "p"


def one(mutation):
    mid, rel, old, new, why = mutation
    tmp, work = copy_project()
    try:
        target = work / rel
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            return mid, "BAD", f"the text to break was found {text.count(old)} times in {rel}", why
        target.write_text(text.replace(old, new), encoding="utf-8")
        passed, tail = run_tests(work)
        return mid, ("SURVIVED" if passed else "CAUGHT"), tail, why
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    only = {x for x in os.environ.get("SABOTAGE_ONLY", "").split(",") if x}      # run chosen breaks only, for example after fixing a gap
    if only:
        MUTATIONS[:] = [m for m in MUTATIONS if m[0] in only]
    tmp, work = copy_project()
    try:
        ok, tail = run_tests(work)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not ok:
        print("The clean copy fails its own tests, so the check cannot start:\n" + tail)
        return 2
    print(f"clean copy passes; breaking the code {len(MUTATIONS)} ways ...")
    with ThreadPoolExecutor(max_workers=int(os.environ.get("SABOTAGE_WORKERS", "6"))) as pool:
        futures = [pool.submit(one, m) for m in MUTATIONS]
        results = []
        for fut in as_completed(futures):                           # each result is printed the moment it is known
            mid, status, tail, why = fut.result()
            results.append((mid, status, tail, why))
            print(f"{status:9} {mid:42} {why}", flush=True)
            if status in ("BAD", "SURVIVED"):
                print("          " + tail.replace("\n", "\n          ")[-300:], flush=True)
    bad = [r for r in results if r[1] != "CAUGHT"]
    print(f"\n{len(results) - len(bad)} of {len(results)} caught" + ("" if not bad else f"; NOT caught or broken: {', '.join(r[0] for r in bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
