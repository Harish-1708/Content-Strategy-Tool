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
TESTS = ["tests/test_observatory_*.py", "tests/test_services.py", "tests/test_ytdlp_youtube.py", "tests/test_external_repository.py", "tests/test_platform_wiring.py", "tests/test_platform_matrix.py", "tests/test_reddit_routes.py", "tests/test_agent_reach_instagram.py", "tests/test_ai_github.py", "tests/test_ai_health_tab.py", "tests/test_ai_config.py", "tests/test_providers.py", "tests/test_agent_reach_catalog.py", "tests/test_owner_defaults.py", "tests/test_capability_matrix.py", "tests/test_research.py", "tests/test_sources.py", "tests/test_guards.py", "tests/test_connections.py", "tests/test_sheets.py", "tests/test_health.py", "tests/test_acceptance.py"]

# (id, file, text found exactly once, replacement, what the bug simulates)
MUTATIONS = [
    ("worker-google-referer", OBS + "scrapling_worker.py", '"google_search": False', '"google_search": True', "the stealth browser sets a Google referer"),
    ("worker-ignores-robots", OBS + "scrapling_worker.py", '                if mode == "obey":', "                if False:", "robots.txt is ignored under the default policy"),
    ("worker-keeps-hammering-after-challenge", OBS + "scrapling_worker.py", '                    self._site(host)["rest"] = why', "                    pass",
     "the site is not rested after a bot check"),
    ("worker-robots-around-the-proxy", OBS + "scrapling_worker.py", '**({"proxy": proxy} if proxy else {}))', "**{})", "robots.txt is fetched directly while the page goes through the proxy"),
    ("worker-private-addresses", OBS + "scrapling_worker.py", '        if guards.on("private_address_block") and not self.private_ok and not safety.resolves_public(host):', "        if False:", "a name that points inside our network is fetched"),
    ("client-leaks-environment", OBS + "worker_client.py", "    names = set(_PASS)", "    names = set(_PASS) | set(environ)", "the worker receives every secret of the main tool"),
    ("client-job-world-readable", OBS + "worker_client.py", "        job_path.chmod(0o600)", "        pass", "the job file is readable by other users"),
    ("polite-obeys-nothing", OBS + "polite.py", '                if mode == "obey":\n                    failure', '                if False:\n                    failure', "robots.txt is ignored by the default crawler"),
    ("polite-override-any-host", OBS + "polite.py", '                if entry["host"] == host:', "                if True:", "an override for one site applies to all"),
    ("config-advisory-needs-no-basis", OBS + "config.py", '    if base["robots"] == "advisory" and not base["basis"]:', '    if False:', "ignoring robots needs no recorded reason"),
    ("config-override-needs-no-approver", OBS + "config.py", 'for key in ("host", "basis", "approved_by", "date"):', 'for key in ("host", "basis", "date"):', "an exception need not name who decided"),
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
    # ---- the recorded decision for evasion methods, and the owner's switches (no host list) ----
    ("worker-evasion-switch-ignored", OBS + "scrapling_worker.py", '                if not self.controls.get("evasion"):', "                if False:", "the owner's evasion switch does nothing"),
    ("worker-cookies-switch-ignored", OBS + "scrapling_worker.py", '("stealth", "browser", "the browser"), ("cookies_env", "account_cookies", "account cookies")):', '("stealth", "browser", "the browser")):', "the account-cookies switch does nothing"),
    ("worker-browser-proxy-dropped", OBS + "scrapling_worker.py", '                    kw["extra_headers"] = extra\n                if proxy:\n                    kw["proxy"] = proxy', '                    kw["extra_headers"] = extra\n                if False:\n                    kw["proxy"] = proxy', "the browser goes out without the proxy that was asked for"),
    ("worker-browser-headers-dropped", OBS + "scrapling_worker.py", '                if extra:\n                    kw["extra_headers"] = extra', '                if False:\n                    kw["extra_headers"] = extra', "the browser sends no From: header or custom headers"),
    ("worker-browser-locale-dropped", OBS + "scrapling_worker.py", '        kw["locale"] = adv["locale"]', "        pass", "the browser ignores the locale that was set"),
    ("worker-dishonest-default-agent", OBS + "scrapling_worker.py", '        headers = {"User-Agent": agent or self.agent}\n        if self.contact:', '        headers = {"User-Agent": "Mozilla/5.0"}\n        if self.contact:', "the default request pretends to be a browser"),
    ("client-switch-not-checked", OBS + "worker_client.py", '        if adv.get(key) and not controls[switch]:', "        if False:", "the core builds jobs for controls the owner switched off"),
    # ---- the AI judge and its review ----
    ("judge-blocks-at-any-confidence", OBS + "judge.py", 'float(v.get("confidence", 1.0)) >= STOP_CONFIDENCE', "True", "an unsure 'not a competitor' blocks an addition"),
    ("judge-rejected-verdict-still-blocks", OBS + "judge.py", 'v.get("review") != "rejected" and float(', "float(", "a verdict a person rejected still blocks"),
    ("judge-invented-keys-kept", OBS + "judge.py", 'item.get("key") not in keys or ', "", "a verdict for a company that was not asked about is kept"),
    ("judge-confidence-unchecked", OBS + "judge.py", "not 0 <= conf <= 1", "False", "a confidence of 7 is accepted"),
    ("judge-batches-candidates", OBS + "judge.py", "MAX_PER_CALL = 1 ", "MAX_PER_CALL = 8 ", "verdicts share one review item"),
    ("judge-data-inside-instructions", OBS + "judge.py", "context=context_for(batch, entities), ", "context=(), ", "collected text is not passed as fenced data"),
    ("judge-decision-always-approved", OBS + "commands.py", '"approved" if what == "approve" else "rejected"', '"approved"', "rejecting a verdict records an approval"),
    ("judge-approved-verdict-replaced", OBS + "discovery.py", ' and (c.get("ai") or {}).get("review") != "approved":', ":", "a newer unreviewed verdict replaces an approved one"),
    ("judge-asks-again-after-rejection", OBS + "commands.py", ' and not (known.get(e["key"]) or {}).get("ai_rejected")][:cfg["judge_max"]]', "][:cfg[\"judge_max\"]]", "a rejected verdict is asked for again"),
    ("judge-verdict-ignored-by-merge", OBS + "discovery.py", "        blocked = judge.blocks_auto_add(c)", "        blocked = False", "the AI cannot stop an addition"),
    ("discovery-unprobed-added", OBS + "discovery.py", 'c.get("shopify") is not None and not blocked', "not blocked", "a site never checked is added"),
    # ---- multi-source discovery ----
    ("enrich-evidence-added-up", OBS + "enrich.py", "    return round(1 - doubt, 2)", "    return round(min(1.0, sum(weights)), 2)", "evidence is summed, so weak sources pass too easily"),
    ("enrich-no-rival-check", OBS + "enrich.py", "if conf >= min_confidence and conf - rival >= 0.15:", "if conf >= min_confidence:", "two nearly equal accounts: one is chosen anyway"),
    ("enrich-threshold-ignored", OBS + "enrich.py", "if conf >= min_confidence and conf - rival >= 0.15:", "if conf - rival >= 0.15:", "a weak match is written to the record"),
    ("enrich-short-name-matches", OBS + "enrich.py", "        return len(words) >= 2 or abs(len(n) - len(h)) <= 3", "        return True", "a short single-word name matches any account that contains it"),
    ("enrich-bio-link-any-site", OBS + "enrich.py", 'if site and domain and host_of(site).removeprefix("www.") == host_of(domain).removeprefix("www."):', "if site and domain:", "a profile that links elsewhere counts as proof"),
    ("enrich-search-name-unchecked", OBS + "enrich.py", 'if handle and name_matches_handle(name, handle, r.get("title", "")):', "if handle:", "any Instagram result for the search is accepted"),
    ("enrich-no-lookup-still-searches", OBS + "commands.py", """    queries = [f"{e['name']} instagram" for e in targets] if not args.no_lookup else []""", """    queries = [f"{e['name']} instagram" for e in targets]""", "--no-lookup still makes outside requests"),
    ("enrich-homepage-read-with-no-probe", OBS + "commands.py", '        if e["domain"] and contact and not args.no_probe:', '        if e["domain"] and contact:', "--no-probe still reads home pages"),
    ("settings-exa-switch-ignored-in-lookups", OBS + "commands.py", """    if not settings["readers"]["exa"]:                                    # readers.exa: false keeps Exa out of the website lookups""", "    if False:", "Exa is used for website lookups although readers.exa is off"),
    # ---- Reddit ----
    ("reddit-approval-ignored", "contentintel/reddit_routes.py", '        problem = approval_problem(self.approval)\n        if problem:', '        problem = ""\n        if problem:', "the Reddit API is called without a recorded approval"),
    ("reddit-rate-limit-not-respected", "contentintel/reddit_routes.py", "        if status == 429:\n            wait = int(", "        if False:\n            wait = int(", "a Reddit rate limit is treated as an ordinary error"),
    ("reddit-removed-posts-kept", "contentintel/reddit_routes.py", 'd.get("removed_by_category") in ("moderator", "deleted", "reddit")', "False", "posts Reddit removed are collected"),
    ("reddit-duplicates-kept", "contentintel/reddit_routes.py", '        if r["item_id"] not in seen:', "        if True:", "a post two targets returned is stored twice"),
    ("reddit-missing-community-stops-all", "contentintel/reddit_routes.py", "            if exc.code != K.NOT_FOUND:\n                break", "            if False:\n                break", "one missing community stops the rest"),
    ("reddit-bad-token-kept", "contentintel/reddit_routes.py", '            self.token = ""\n            raise K.Failure(K.NEED_SETUP, "Reddit rejected the access token', '            raise K.Failure(K.NEED_SETUP, "Reddit rejected the access token', "a rejected token is reused"),
    ("reddit-sleeps-through-long-reset", "contentintel/reddit_routes.py", "_num(reset) <= 60:", "True:", "a run waits ten minutes for a rate limit"),
    ("reddit-api-id-differs-from-agent-reach", "contentintel/reddit_routes.py", "_row(SOURCE, pid, handle=str(d.get(\"author\")", "_row(SOURCE, \"x\" + pid, handle=str(d.get(\"author\")", "the same post gets two ids, so it is stored twice"),
    ("reddit-title-only-post-unusable", "contentintel/agent_reach_instagram.py", '        return bool(row.get("text") or row.get("title"))', '        return bool(row.get("text"))', "a Reddit post with only a title counts as empty"),
    ("reliability-empty-rate-crashes", "contentintel/agent_reach_instagram.py", "    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None", '    return float(value) if value != "" else None', "a route that never ran breaks the report"),
    ("reddit-reports-agent-reach-as-hosted-ok", "contentintel/agent_reach_instagram.py", "            raise K.Failure(K.NEED_SETUP, f\"this route does not run on a GitHub-hosted runner", "            return\n            raise K.Failure(K.NEED_SETUP, f\"this route does not run on a GitHub-hosted runner", "Agent-Reach starts on a hosted runner"),
    # ---- every capability is an owner setting (no approval step) ----
    ("client-evasion-switch-not-checked", OBS + "worker_client.py", '    if asked and not controls["evasion"]:', "    if False:", "the core builds jobs for bot-detection options the owner switched off"),
    ("client-solve-flag-dropped", OBS + "worker_client.py", '    if adv.get("solve_challenges"):\n        options["solve_cloudflare"] = True', '    if False:\n        options["solve_cloudflare"] = True', "a challenge-solving setting is silently not applied"),
    ("client-humanize-dropped", OBS + "worker_client.py", '    if adv.get("humanize"):\n        options["humanize"] = True', '    if False:\n        options["humanize"] = True', "a cursor-simulation setting is silently not applied"),
    ("client-canvas-webrtc-merged", OBS + "worker_client.py", 'options["hide_canvas"], options["block_webrtc"] = bool(adv.get("hide_canvas")), bool(adv.get("block_webrtc"))', 'options["hide_canvas"], options["block_webrtc"] = True, True', "turning on one fingerprint change turns on both"),
    ("config-fingerprint-alias-ignored", OBS + "config.py", '        advanced["hide_canvas"] = advanced["block_webrtc"] = True', "        pass", "the older fingerprint setting silently does nothing"),
    ("config-challenge-alias-ignored", OBS + "config.py", '        advanced["solve_challenges"] = True', "        pass", "the older challenge setting silently does nothing"),
    ("controls-summary-hides-cookies", OBS + "report.py", '    if adv.get("cookies_env"):\n        parts.append(', '    if False:\n        parts.append(', "the sheet does not show that account cookies are in use"),
    ("controls-summary-hides-evasion", OBS + "report.py", '("impersonate", "TLS impersonation"), ("hide_canvas", "canvas change"),', '("hide_canvas", "canvas change"),', "the sheet does not show that TLS impersonation is on"),
    ("controls-command-hides-switched-off", OBS + "commands.py", '.get(k) and not on[switch]]', '.get(k) and False]', "a control the owner switched off is not reported as not applied"),
    # ---- Scrapling and Agent-Reach as shared platform services ----
    ('services-targets-own-settings-ignored', 'contentintel/services.py', '    explicit.update(target_advanced or {})', '    pass', "a target's own settings are overridden by the central ones"),
    ('services-override-www-pattern-unnormalised', 'contentintel/services.py', '    base = match.removeprefix("*.").removeprefix("www.")', '    base = match.removeprefix("*.")', 'an override written with www. never matches'),
    ('services-override-matches-lookalikes', 'contentintel/services.py', '    return host == base or host.endswith("." + base)', '    return host == base or host.endswith(base)', 'an override for shop.example also hits notshop.example'),
    ('services-purpose-override-matches-all', 'contentintel/services.py', '        return match.split(":", 1)[1] == purpose', '        return True', 'a purpose override applies to every purpose'),
    ('controls-default-not-removed', 'contentintel/services.py', '            if value == unset:\n                node.pop(name, None)\n                continue', '            if False:\n                node.pop(name, None)\n                continue', "a central default put back still masks a target's setting"),
    ('controls-unknown-key-loaded', 'contentintel/services.py', '    return {k: v for k, v in values.items() if k in BY_KEY}', '    return values', 'an unknown saved control is applied'),
    ('controls-stale-copy-counts-as-edit', 'contentintel/services.py', '        if raw.lower() == baseline.lower() or raw.lower() == now_text.lower():', '        if raw.lower() == now_text.lower():', 'a stale tab in another sheet undoes a change'),
    ('controls-record-not-per-sheet', 'contentintel/services.py', '    all_written[sheet_id] = written', '    all_written["shared"] = written', "one sheet's update hides another sheet's edit baseline"),
    ('controls-off-text-not-understood', 'contentintel/services.py', '        if text.lower() in ("false", "no", "off", "0", "n"):', '        if False:', 'typing off in a cell is rejected'),
    ('controls-range-unchecked', 'contentintel/services.py', '        if value < 0 or value > (3600 if defn["key"].endswith("delay") else 168):', '        if False:', 'a negative or huge delay is accepted'),
    ('commands-switch-rests-the-route', OBS + "commands.py", '                allowed, why = SV.agent_reach_allowed(settings, "reddit", "content_research")\n                if not allowed:', '                allowed, why = True, ""\n                if not allowed:', 'switching a channel off records a route failure and rests it'),
    ('commands-central-settings-not-applied', OBS + "commands.py", '            slot["comp"]["advanced"] = SV.resolve_scrapling(settings, slot["comp"]["domain"], "competitors", slot["comp"].get("advanced"))', '            pass', 'central defaults and overrides do not reach competitors'),
    ('commands-enrich-wrong-purpose', OBS + "commands.py", 'SV.agent_reach_client(settings, "instagram", "social_discovery",', 'SV.agent_reach_client(settings, "instagram", "competitors",', 'social discovery is governed by the competitors switch'),
    ('sheets-controls-not-read', 'contentintel/sheets.py', '    _read_controls(spreadsheet, brand, store, now, report, dry_run)', '    pass', 'edits in the Controls tab are ignored'),
    ('sheets-controls-tab-missing', 'contentintel/sheets.py', ' + _controls_tabs(brand, store, now, report) + _connection_tabs(store, now, report)', ' + _connection_tabs(store, now, report)', 'the Controls tab is not written'),
    ('acceptance-agent-reach-switch-ignored', OBS + "acceptance_checks.py", 'and SV.agent_reach_allowed(settings, "reddit", "content_research")[0]:', 'and True:', 'an acceptance check runs for a route the owner switched off'),
    # ---- the platform-wide shared interfaces and their connections ----
    ('hub-allowed-ignores-master', 'contentintel/services.py', '    if not sec["enabled"]:\n        return False, f"{OC.SERVICE_REGISTRY', '    if False:\n        return False, f"{OC.SERVICE_REGISTRY', "a service's master switch does nothing"),
    ('hub-allowed-ignores-purpose', 'contentintel/services.py', '    if not sec["use_for"][purpose]:', '    if False:', 'a purpose switch does nothing'),
    ('hub-allowed-ignores-channel', 'contentintel/services.py', '    if channel is not None and not sec.get("channels", {}).get(channel, False):', '    if False:', 'a channel or provider switch does nothing'),
    ('hub-search-provider-switch-ignored', 'contentintel/services.py', '    return {name: prov for name, prov in build(environ).items() if enabled.get(name, True)}', '    return dict(build(environ))', 'a search provider the owner switched off is still used'),
    ('hub-search-purpose-ignored', 'contentintel/services.py', '    ok, _ = allowed(settings, "search", purpose)\n    if not ok:\n        return {}', '    ok = True\n    if not ok:\n        return {}', 'search runs for a purpose it is switched off for'),
    ('hub-ai-route-ignored', 'contentintel/services.py', '    if not ok or route_for(settings, "ai_gateway", purpose) != "gateway_first":', '    if not ok:', 'every purpose goes through the gateway whatever the owner chose'),
    ('hub-ai-switch-ignored', 'contentintel/services.py', '    if not ok or route_for(settings, "ai_gateway", purpose) != "gateway_first":', '    if route_for(settings, "ai_gateway", purpose) != "gateway_first":', 'the gateway is used though its switch is off'),
    ('hub-ai-no-fallback', 'contentintel/services.py', '        self.calls.append("claude")\n        return self.inner.json_task(system, user, max_tokens)', '        return {}', 'a module gets nothing when the gateway has no answer'),
    ('hub-ai-wrong-data-class', 'contentintel/services.py', 'data_class="internal", instructions=user, system=system, now=self._now(), item_ids=(self.purpose,))', 'data_class="restricted", instructions=user, system=system, now=self._now(), item_ids=(self.purpose,))', 'module data is sent at the wrong data class'),
    ('hub-ai-gateway-error-escapes', 'contentintel/services.py', '            except Exception as exc:                # noqa: BLE001  (the gateway is an extra; the module must go on without it)', '            except KeyError as exc:', 'a gateway that cannot start stops the module'),
    ('hub-ai-no-question-rule-lost', 'contentintel/services.py', '            return "" if res["text"].strip().upper().startswith(NO_QUESTION) else res["text"]', '            return res["text"]', 'a reply to Gemini is sent when nothing was asked'),
    ('hub-page-refusal-escalates', 'contentintel/services.py', '            if choice != "auto" or (exc.code in REFUSALS and _GUARDS.on("rest_after_refusal")):', '            if choice != "auto":', 'another reader is tried after a site refused'),
    ('hub-page-auto-order', 'contentintel/services.py', '"external_first": ["scrapling", "jina", "core"],', '"external_first": ["core", "jina", "scrapling"],', 'auto tries the readers in the wrong order'),
    ('hub-page-scrapling-switch-ignored', 'contentintel/services.py', '            if not ok:\n                skipped.append(f"scrapling: {why}")', '            if False:\n                skipped.append(f"scrapling: {why}")', 'Scrapling is used inside auto although switched off'),
    ('hub-page-purpose-switch-ignored', 'contentintel/services.py', '    ok, why = allowed(settings, "page_readers", purpose)\n    if not ok:\n        raise K.Failure(K.BAD_INPUT, why)', '    ok, why = True, ""\n    if not ok:\n        raise K.Failure(K.BAD_INPUT, why)', 'pages are read for a purpose the readers are switched off for'),
    ('hub-engine-legacy-switch-everywhere', 'contentintel/services.py', ' and purpose in _PAGE_PURPOSES_OLD_ENGINE and scrapling_installed:', ' and scrapling_installed:', "the older switch changes every purpose's reader"),
    ('hub-registry-controls-not-refreshed', 'contentintel/services.py', '    OC.register_service(name, **kwargs)\n    CONTROLS[:] = _build()', '    OC.register_service(name, **kwargs)\n    pass', 'a newly registered service does not appear in the controls'),
    ('config-route-choice-unchecked', OBS + "config.py", '                if choice not in spec["routes"]["choices"]:', '                if False:', 'a route that is not one of the choices is accepted'),
    ('config-route-setting-dropped', OBS + "config.py", '                out[name]["routes"][purpose] = choice', '                pass', "the owner's route choice is ignored"),
    ('config-unknown-service-accepted', OBS + "config.py", '    _only(given, tuple(SERVICE_REGISTRY), f"{where} services")', '    pass', 'an unregistered service is accepted'),
    ('providers-agent-reach-rank-gap', 'contentintel/providers.py', 'out.append(Result(rank=len(out) + 1,', 'out.append(Result(rank=len(out) + 2,', 'results are numbered with gaps'),
    ('providers-agent-reach-login-misclassified', 'contentintel/providers.py', '            cls = AuthError if exc.code == K.NEED_LOGIN else ProviderError', '            cls = ProviderError', 'a login wall is reported as a generic failure'),
    ('cli-search-ignores-controls', 'contentintel/cli.py', '    return services.search_providers(hub, purpose, os.environ)', '    return providers_mod.build_providers(os.environ)', "research search ignores the owner's switches"),
    ('cli-routed-ignores-controls', 'contentintel/cli.py', '    return services.ai_client(hub, purpose, claude, stack_factory=stack, now_fn=sync_mod.utc_now)', '    return claude', 'no module can use the gateway'),
    ('cli-brief-not-routed', 'contentintel/cli.py', 'claude_for=lambda model: _routed(_claude(csettings, model), "brief", args, store)', 'claude_for=lambda model: _claude(csettings, model)', 'brief generation bypasses the shared interface'),
    ('cli-ideabank-not-routed', 'contentintel/cli.py', '    claude = _routed(_claude(csettings), "idea_bank", args, store)', '    claude = _claude(csettings)', 'the idea bank bypasses the shared interface'),
    ('cli-planning-wrong-purpose', 'contentintel/cli.py', '    claude = _routed(_claude(csettings), "planning", args, store)', '    claude = _routed(_claude(csettings), "brief", args, store)', 'planning is governed by the brief switch'),
    ('collectors-deep-check-wrong-purpose', 'contentintel/collectors.py', '_for_purpose(claude, "deep_check")', '_for_purpose(claude, "autoseeds")', 'the deep check is governed by the seed-words switch'),
    ('collectors-prompt-not-routed', 'contentintel/collectors.py', '            prompt = _for_purpose(claude, "collectors").prepare_prompt(question, context)', '            prompt = claude.prepare_prompt(question, context)', 'the Gemini prompt bypasses the shared interface'),
    ('commands-deepread-repeats', OBS + "commands.py", 'if u.startswith(("http://", "https://")) and u not in done and u not in urls:', 'if u.startswith(("http://", "https://")) and u not in urls:', 'pages already read are read again'),
    ('commands-jina-switch-recorded-as-failure', OBS + "commands.py", '        if not settings["readers"]["jina"]:                                   # the owner', '        if False:                                   # the owner', 'switching Jina off rests its route'),
    ('commands-judge-gateway-switch-ignored', OBS + "commands.py", '    ok, why = SV.allowed(settings, "ai_gateway", "discovery")\n    if not ok:\n        return {}, [f"AI judge not used: {why}"]', '    ok, why = True, ""\n    if not ok:\n        return {}, [f"AI judge not used: {why}"]', 'the competitor judge ignores the AI Gateway switch'),
    ('commands-monitor-route-ignored', OBS + "commands.py", '    wish = SV.effective_engine(settings, "competitors", scrapling_installed=True) ', '    wish = "scrapling" if settings["engine"]["pages"] == "scrapling" else "core" ', 'competitor monitoring ignores the page-reader route'),
    ('settings-exa-switch-ignored-in-enrichment', OBS + "commands.py", '                 if not k.startswith("agent_reach")}                  # nor an Instagram account\n    if not settings["readers"]["exa"]:', '                 if not k.startswith("agent_reach")}                  # nor an Instagram account\n    if False:', 'Exa is used for account searches although readers.exa is off'),
    ('commands-engine-gate-ignored', OBS + "commands.py", '    if wish == "scrapling" and not SV.scrapling_allowed(settings, "competitors")[0]:', '    if False:', 'Scrapling is used for competitors although switched off'),
    ('commands-read-page-wrong-purpose', OBS + "commands.py", 'args.url, "content_research", engine=args.engine or None)', 'args.url, "competitors", engine=args.engine or None)', 'content research is governed by the competitors switch'),
    ('config-services-match-unvalidated', OBS + "config.py", '                if not isinstance(item, dict) or not _match_ok(item.get("match", "")):', '                if not isinstance(item, dict):', 'an override with a nonsense match is accepted'),
    ('config-services-defaults-unvalidated', OBS + "config.py", '            out[name]["defaults"] = {**SCRAPLING_ON_DEFAULTS, **_advanced_options(section.get("defaults") or {}, f"{here}.defaults")}', '            out[name]["defaults"] = {**SCRAPLING_ON_DEFAULTS, **dict(section.get("defaults") or {})}', "a central default is not validated like a competitor's setting"),
    ('hub-agent-reach-channel-switch-ignored', 'contentintel/services.py', '    ok, why = allowed(settings, "agent_reach", purpose, channel)\n    return (ok, why) if ok else (False, why.replace("Agent-Reach is switched off for", "Agent-Reach is switched off for"))', '    ok, why = allowed(settings, "agent_reach", purpose)\n    return (ok, why) if ok else (False, why)', 'an Agent-Reach channel switch does nothing'),
    # ---- the YouTube workflow, general research reading and reuse by other repositories ----
    ('ytdlp-bot-check-not-a-refusal', 'contentintel/ytdlp_youtube.py', '    if _BOT.search(text):', '    if False:', 'a YouTube bot check is reported as a generic failure'),
    ('ytdlp-rate-limit-unclassified', 'contentintel/ytdlp_youtube.py', '    if _LIMIT.search(text):', '    if False:', 'a YouTube rate limit is reported as a generic failure'),
    ('ytdlp-search-limit-ignored', 'contentintel/ytdlp_youtube.py', 'f"ytsearch{max(1, int(limit))}:{query}"', 'f"ytsearch5:{query}"', 'the number of results asked for is ignored'),
    ('ytdlp-claims-it-needs-a-browser', 'contentintel/ytdlp_youtube.py', '    needs_browser = False\n\n    def __init__(self, *, run=subprocess.run', '    needs_browser = True\n\n    def __init__(self, *, run=subprocess.run', 'YouTube is refused on a hosted runner though it needs no login'),
    ('ytdlp-repeated-caption-lines-kept', 'contentintel/ytdlp_youtube.py', '        if line and line != last:', '        if line:', 'automatic captions come back with every line twice'),
    ('ytdlp-missing-install-not-reported', 'contentintel/ytdlp_youtube.py', '        if not self.available():\n            raise K.Failure(K.NEED_SETUP, "yt-dlp is not installed here', '        if False:\n            raise K.Failure(K.NEED_SETUP, "yt-dlp is not installed here', 'a missing yt-dlp surfaces as an odd crash'),
    ('channels-found-twice-listed-twice', OBS + "corpus.py", '        if ref and ref.lower() not in seen:', '        if ref:', 'a channel found by two searches is added twice'),
    ('channels-handle-ignored', OBS + "corpus.py", '        ref = cid if cid.startswith("UC") else handle', '        ref = cid', 'channels that only have a handle are lost'),
    ('youtube-channelpages-explicit-engine-ignored', OBS + "commands.py", '"youtube_research", engine=args.engine or None, sink="channel_pages.jsonl")', '"youtube_research", sink="channel_pages.jsonl")', 'an explicit reader choice is ignored for channel pages'),
    ('youtube-deepread-explicit-engine-ignored', OBS + "commands.py", 'u, "general_research", engine=args.engine or None)', 'u, "general_research")', 'an explicit reader choice is ignored for deepread'),
    ('youtube-channel-accounts-not-recorded', OBS + "commands.py", 'recorded = _record_page_social(comp, page["social"], snaps, now) if _same_channel(comp.get("youtube"), ch) else {}', 'recorded = {}', 'accounts a channel links to never reach the competitor'),
    ('youtube-channel-accounts-wrong-competitor', OBS + "commands.py", 'if _same_channel(comp.get("youtube"), ch) else {}', 'if True else {}', "a channel's accounts are recorded on every competitor"),
    ('youtube-discovery-adds-nothing', OBS + "commands.py", '        corpus["channels"].setdefault(ref, {"id": ref, "title": title, "added_at": now, "found_by": f"agent_reach: {q}"})', '        pass', 'channels found through Agent-Reach are not kept'),
    ('youtube-agentreach-refused-on-hosted-runner', OBS + "commands.py", '    if getattr(cli, "needs_browser", True) and AR.hosted_runner(', '    if AR.hosted_runner(', 'YouTube research is refused on a hosted runner'),
    ('corpus-about-pages-not-searchable', OBS + "corpus.py", '"corpus" / "channel_pages.jsonl"', '"corpus" / "channel_pages_unused.jsonl"', 'channel About pages cannot be found by search'),
    ('hub-youtube-client-is-browser-based', 'contentintel/services.py', '    if channel == "youtube":                        # Agent-Reach\'s YouTube channel is yt-dlp', "    if False:                        # Agent-Reach's YouTube channel is yt-dlp", 'the YouTube channel is built as an OpenCLI client'),
    ('providers-youtube-search-needs-browser', 'contentintel/providers.py', '    name, label, channel = "agent_reach_youtube", "YouTube search (Agent-Reach, yt-dlp)", "youtube"\n    needs_browser = False', '    name, label, channel = "agent_reach_youtube", "YouTube search (Agent-Reach, yt-dlp)", "youtube"\n    needs_browser = True', 'YouTube search is withheld on a hosted runner'),
    ('hub-failing-plugin-stops-the-rest', 'contentintel/services.py', '        except Exception as exc:                    # noqa: BLE001  (a plugin that fails is reported and skipped)', '        except KeyError as exc:', "one repository's broken plugin stops another's"),
    ('hub-plugins-reloaded-every-time', 'contentintel/services.py', '    if _PLUGINS_DONE and not force:', '    if False:', 'plugins are loaded again on every settings read'),
    ('hub-bad-declaration-stops-the-rest', 'contentintel/services.py', '        except (ValueError, KeyError, TypeError) as exc:\n            NOTES.append(f"{path}: service', '        except KeyError as exc:\n            NOTES.append(f"{path}: service', 'one bad declaration stops the others'),
    ('hub-new-purpose-not-on-every-service', OBS + "config.py", '    return tuple(PURPOSES) if spec.get("all_purposes") else spec["purposes"]', '    return spec["purposes"]', 'a new purpose gets no switch on the existing services'),
    ('hub-builtin-purpose-removable', OBS + "config.py", '    if name in BUILT_IN_PURPOSES:', '    if False:', 'a built-in purpose can be removed'),
    ('hub-settings-skip-registrations', 'contentintel/services.py', '    load_plugins()\n    load_declared()\n    settings = OC.load_settings(path)', '    settings = OC.load_settings(path)', "another repository's registrations are not applied"),
    ('workflow-research-picks-the-engine', '.github/workflows/research.yml', 'args=(corpus --what deepread --top "$READ_PAGES")', 'args=(corpus --what deepread --engine core --top "$READ_PAGES")', "the research workflow overrides the owner's reader choice"),
    ('workflow-observatory-always-passes-engine', '.github/workflows/observatory.yml', 'if [ "$ENGINE" != "central" ]; then args+=(--engine "$ENGINE"); fi', 'args+=(--engine "$ENGINE")', "the observatory workflow overrides the owner's reader choice"),
    ('commands-hosted-runner-recorded', OBS + "commands.py", '    if getattr(cli, "needs_browser", True) and AR.hosted_runner(cli.environ if getattr(cli, "environ", None) is not None else os.environ):', '    if False:', 'a hosted-runner refusal rests the route'),
    ('providers-agent-reach-on-hosted', 'contentintel/providers.py', '            if key.lower() in ("on", "1", "true", "yes") and not (cls.needs_browser and hosted_runner(env)):', '            if key.lower() in ("on", "1", "true", "yes"):', 'Reddit search is built on a hosted runner'),
    ('providers-agent-reach-always', 'contentintel/providers.py', '            if key.lower() in ("on", "1", "true", "yes") and not (cls.needs_browser and hosted_runner(env)):', '            if not (cls.needs_browser and hosted_runner(env)):', "Agent-Reach search is built without the owner's opt-in"),
    ('commands-deepread-wrong-purpose', OBS + "commands.py", 'u, "general_research", engine=args.engine or None)', 'u, "content_research", engine=args.engine or None)', 'general research reading is governed by the content research switch'),
    ('commands-channelpages-wrong-purpose', OBS + "commands.py", '"youtube_research", engine=args.engine or None, sink="channel_pages.jsonl")', '"content_research", engine=args.engine or None, sink="channel_pages.jsonl")', 'YouTube channel pages are governed by the content research switch'),
    ('commands-read-central-route-ignored', OBS + "commands.py", 'args.url, "content_research", engine=args.engine or None)', 'args.url, "content_research", engine=args.engine or "jina")', "the owner's reader choice is ignored by corpus read"),
]
NEW = [   # update 40: request controls, the Instagram route, social links, trend speed, wired settings
    ("worker-declared-identity-ignored", OBS + "scrapling_worker.py", 'return adv.get("identity") if adv.get("identity") and not adv.get("impersonate") else self.agent', "return self.agent", "a declared identity is never sent"),
    ("worker-from-header-dropped", OBS + "scrapling_worker.py", '        headers = {"User-Agent": self._agent_for(adv)}\n        if self.contact:', '        headers = {"User-Agent": self._agent_for(adv)}\n        if False:', "the From header naming us disappears when an identity is declared"),
    ("worker-robots-for-wrong-identity", OBS + "scrapling_worker.py", "parser.can_fetch(clean, self._token(agent))", 'parser.can_fetch(clean, "KelsonResearchBot")', "robots.txt is checked for our honest name while another identity is used"),
    ("worker-refusal-does-not-rest-the-host", OBS + "scrapling_worker.py", 'raise _Refused(code, f"{host} still refused after {tries} attempt(s) ({why}); marked failed and moved on")', 'raise _Refused(code, "refused")',
     "a 403 can be retried with another identity in the same run"),
    ("worker-switches-ignored", OBS + "scrapling_worker.py", '                if adv.get(flag) and not self.controls.get(control):', "                if False:", "the global access_controls switches do nothing"),
    ("worker-session-forgets-cookies", OBS + "scrapling_worker.py", "        held = self.sessions.get(key)\n        if held is None:", "        held = None\n        if held is None:", "a session starts afresh on every request"),
    ("worker-pace-can-be-lowered", OBS + "scrapling_worker.py", 'site["delay"] = max(site["delay"], min(float(adv["delay"]), self.max_delay))', 'site["delay"] = min(float(adv["delay"]), self.max_delay)', "a person's delay can go below the site's Crawl-delay"),
    ("config-cookie-header-allowed", OBS + "config.py", '_FORBIDDEN_HEADERS = {"cookie", "authorization",', '_FORBIDDEN_HEADERS = {"authorization",', "a Cookie header can be set from a file"),
    ("config-switch-accepts-text", OBS + "config.py", '                if not isinstance(value, bool):\n                    raise ObservatoryConfigError(f"{path} access_controls.', '                if False:\n                    raise ObservatoryConfigError(f"{path} access_controls.', "an access control can be set to the word no"),
    ("client-resting-site-asked-anyway", OBS + "worker_client.py", '            rest = self.pauses.paused(self.comp["domain"], self.now)\n            if rest:', '            rest = self.pauses.paused(self.comp["domain"], self.now)\n            if False:', "a resting site is read again"),
    ("client-identity-switch-ignored", OBS + "worker_client.py", '        if adv.get(key) and not controls[switch]:', "        if False:", "the core builds jobs for controls that are switched off"),
    ("instagram-runs-on-hosted-runner", "contentintel/agent_reach_instagram.py", "        if hosted_runner(self.environ):", "        if False:", "the logged-in route runs on a GitHub-hosted runner"),
    ("instagram-new-route-overwrites", "contentintel/agent_reach_instagram.py", '        if a in (None, "") and b not in (None, ""):', '        if b not in (None, ""):', "the newer route overwrites the established route's values"),
    ("instagram-disagreement-threshold", "contentintel/agent_reach_instagram.py", "DISAGREE = 0.10 ", "DISAGREE = 0.90 ", "large count disagreements go unnoticed"),
    ("instagram-abbreviated-counts-lost", "contentintel/agent_reach_instagram.py", '{"": 1, "k": 1_000,', '{"": 1, "k": 1,', "1.2K likes becomes 1"),
    ("instagram-login-not-recognised", "contentintel/agent_reach_instagram.py", 'LOGIN_WORDS = ("login",', 'LOGIN_WORDS = ("zzz",', "a login wall is reported as a generic failure"),
    ("instagram-block-not-recognised", "contentintel/agent_reach_instagram.py", 'BLOCK_WORDS = ("captcha",', 'BLOCK_WORDS = ("zzz",', "a captcha is not recognised as a security check"),
    ("instagram-keeps-going-after-login-wall", "contentintel/agent_reach_instagram.py", "            if exc.code != K.NOT_FOUND:\n                break", "            if False:\n                break", "every account is tried after a login wall or block"),
    ("social-igtv-links-are-accounts", OBS + "htmlx.py", '_IG_RESERVED = {"p", "reel", "reels", "tv",', '_IG_RESERVED = {"p", "reel", "reels",', "a link to a single IGTV video is taken for an account"),
    ("social-short-channel-ids", OBS + "htmlx.py", "(UC[0-9A-Za-z_\\-]{22})", "(UC[0-9A-Za-z_\\-]{2})", "any UC-word is taken for a YouTube channel id"),
    ("social-file-does-not-win", OBS + "monitor.py", "    wanted = {k: v for k, v in found.items() if v and not own.get(k)}", "    wanted = {k: v for k, v in found.items() if v}", "a page's account overrides what a person wrote"),
    ("social-announced-every-run", OBS + "monitor.py", "    new = {k: v for k, v in wanted.items() if not stored.get(k)}", "    new = dict(wanted)", "the same accounts are announced on every run"),
    ("trend-short-gaps-count", OBS + "corpus.py", "            if hours >= min_hours and gain >= 0:", "            if True:", "two readings minutes apart, or a falling count, give a speed"),
    ("trend-old-videos-estimated", OBS + "corpus.py", "                if 0 < age <= window_days:", "                if True:", "a years-old video's lifetime average is called its current speed"),
    ("trend-tiny-channels-ranked", OBS + "corpus.py", "        if len(recs) < 3:\n            thin.append", "        if len(recs) < 1:\n            thin.append", "a channel with two videos gets a usual pace"),
    ("settings-auto-prune-off", OBS + "commands.py", '    if settings["retention"]["auto_prune"]:', "    if False:", "old snapshots are never thinned by a run"),
    ("settings-discovery-switch-ignored", OBS + "commands.py", '    if not settings["discovery"]["enabled"]:', "    if False:", "discovery runs although it is switched off"),
    ("settings-min-appearances-ignored", OBS + "commands.py", '["signals"]["kept_items"] >= floor}', '["signals"]["kept_items"] >= 0}', "a one-off advertiser becomes a candidate"),
    ("cloudflare-quota-guessed", "ai_providers.yaml", "QUOTA UNKNOWN until verified live", "quota is fine", "an unverified Cloudflare quota is presented as known"),
    ("health-tab-hides-unknown-quota", AI + "sheetrows.py", "            if daily is None:", "            if False:", "an unknown free quota shows as a blank"),
]
MUTATIONS.extend(NEW)
MUTATIONS.append(("decide-www-not-stripped", OBS + "commands.py", '        wanted = (OC.host_of(args.competitor) or "").removeprefix("www.")',
                  '        wanted = (OC.host_of(args.competitor) or "")', "a pasted www address does not match the candidate"))


U40B = [   # update 40, later: every Agent-Reach channel, GitHub, the on-by-default routes, the site crawl (and four older checks re-pointed at refactored code)
    ('client-rest-hours-ignored', 'contentintel/observatory/worker_client.py', '            longer = float((comp.get("advanced") or {}).get("rest_hours") or 0) * 3600', '            longer = 0.0', "the owner's longer rest after a challenge is ignored"),
    ('client-rest-not-remembered', 'contentintel/observatory/worker_client.py', '        if info.get("rest"):\n            code = K.RATE_LIMITED if "slow down"', '        if False:\n            code = K.RATE_LIMITED if "slow down"', 'a rate limit met by the worker is forgotten before the next run'),
    ('catalogue-youtube-wrong-purpose', 'contentintel/agent_reach_catalog.py', '"xiaohongshu": "social_discovery", "youtube": "youtube_research"}', '"xiaohongshu": "social_discovery", "youtube": "content_research"}', 'YouTube research is governed by the content research switch'),
    ('hub-engine-legacy-switch-ignored', 'contentintel/services.py', 'if choice == "auto" and settings["engine"]["pages"] == "scrapling" and purpose in _PAGE_PURPOSES_OLD_ENGINE and scrapling_installed:', 'if False:', 'the older engine.pages switch stops working'),
    ('cat-instagram-posts-wrong-command', 'contentintel/agent_reach_catalog.py', '["user", "{username}", "--limit", "{limit}"]', '["posts", "{username}", "--limit", "{limit}"]', 'the Instagram posts action runs a command OpenCLI does not have'),
    ('cat-username-at-kept', 'contentintel/agent_reach_catalog.py', 'clean["username"] = clean["username"].lstrip("@")', 'pass', 'an @ stays on a handle'),
    ('cat-extra-input-loses-to-default', 'contentintel/agent_reach_catalog.py', 'if k not in act["params"] and v not in (None, ""):', 'if k not in clean and v not in (None, ""):', "a caller's city is overridden by the default"),
    ('cat-optional-location-dropped', 'contentintel/agent_reach_catalog.py', '        if clean.get(key):', '        if False:', "LinkedIn's location is never sent"),
    ('cat-reddit-read-unvalidated', 'contentintel/agent_reach_catalog.py', 'if action == "read" and not (re.fullmatch(', 'if False and not (re.fullmatch(', 'anything is accepted as a Reddit post'),
    ('cat-unconnected-channel-accepted', 'contentintel/agent_reach_catalog.py', 'if not spec or spec["integration"] == "none":', 'if not spec:', 'a channel that is not connected is run'),
    ('cat-twitter-thread-url-kept', 'contentintel/agent_reach_catalog.py', 'clean["target"] = clean["target"].rstrip("/").split("/")[-1].split("?")[0]', 'pass', 'a tweet link is passed whole instead of its id'),
    ('cat-linkedin-job-id-not-url', 'contentintel/agent_reach_catalog.py', 'clean["target"] = f"https://www.linkedin.com/jobs/view/{clean[\'target\']}/"', 'pass', 'a LinkedIn job id is not turned into its link'),
    ('cat-missing-input-accepted', 'contentintel/agent_reach_catalog.py', '        if not raw:\n            raise ValueError(f"{channel} {action} needs {name}")', '        if False:\n            raise ValueError(f"{channel} {action} needs {name}")', 'an action runs with no query'),
    ('cat-unknown-action-accepted', 'contentintel/agent_reach_catalog.py', '    if act is None:', '    if False:', 'an action the channel does not have is run'),
    ('cfg-channels-not-from-catalogue', 'contentintel/observatory/config.py', 'AR_CHANNELS = _AR_CATALOG.integrated()', 'AR_CHANNELS = ("instagram", "reddit", "youtube")', 'the new channels have no owner switch'),
    ('gh-token-always-sent', 'contentintel/github_reach.py', '        if self.token:\n            headers["Authorization"]', '        if True:\n            headers["Authorization"]', 'an empty bearer token is sent'),
    ('gh-rate-limit-forgotten', 'contentintel/github_reach.py', 'if exc.code in (403, 429):', 'if exc.code in (429,):', "GitHub's rate limit is reported as a broken service"),
    ('gh-readme-missing-is-fatal', 'contentintel/github_reach.py', '                if exc.code != K.NOT_FOUND:\n                    raise', '                if True:\n                    raise', 'a repository without a README cannot be read'),
    ('gh-search-limit-ignored', 'contentintel/github_reach.py', '"per_page": max(1, min(limit, 30))', '"per_page": 30', 'the search ignores how many were asked for'),
    ('gh-issue-comments-dropped', 'contentintel/github_reach.py', 'comments = json.loads(self._get(f"/repos/{owner}/{repo}/issues/{number}/comments?per_page=50"))', 'comments = []', 'an issue is read without its comments'),
    ('svc-github-token-not-passed', 'contentintel/services.py', 'token=(environ or {}).get("GITHUB_TOKEN", "")', 'token=""', 'GITHUB_TOKEN never raises the rate limit'),
    ('svc-web-channel-gets-a-client', 'contentintel/services.py', '    if channel == "web":\n        raise K.Failure(K.BAD_INPUT', '    if False:\n        raise K.Failure(K.BAD_INPUT', 'the web channel is run as a social site'),
    ('svc-crawl-owner-limit-ignored', 'contentintel/services.py', 'max_pages=max_pages or settings["crawl"]["max_pages"]', 'max_pages=max_pages or 25', "the owner's page limit is ignored"),
    ('svc-crawl-owner-depth-ignored', 'contentintel/services.py', 'depth=settings["crawl"]["depth"] if depth is None else depth', 'depth=settings["crawl"]["depth"]', "a caller's depth is ignored"),
    ('cfg-gateway-default-claude', 'contentintel/observatory/config.py', '"choices": ("claude", "gateway_first"), "default": "gateway_first"})', '"choices": ("claude", "gateway_first"), "default": "claude"})', 'the gateway is not tried first by default'),
    ('cfg-readers-default-core', 'contentintel/observatory/config.py', '"default": {**PAGE_ENGINE_DEFAULTS, "*": "auto"}})', '"default": {**PAGE_ENGINE_DEFAULTS, "*": "core"}})', 'Scrapling is not tried first by default'),
    ('svc-workflow-box-overrides-owner-route', 'contentintel/services.py', 'if choice == "auto" and settings["engine"]["pages"] == "scrapling"', 'if settings["engine"]["pages"] == "scrapling"', "the workflow's box beats an owner's explicit route"),
    ('cli-gateway-tried-with-no-provider', 'contentintel/cli.py', 'if not any(name != "claude" for name in getattr(router, "clients", {})):', 'if False:', 'the gateway is tried (and its state written) with no provider'),
    ('research-agent-reach-not-routed', 'contentintel/research.py', ', "agent_reach_youtube", "agent_reach_reddit"],       # the Agent-Reach channels last', '],       # the Agent-Reach channels last', 'research never uses the Agent-Reach providers'),
    ('yaml-gateway-route-back-to-claude', 'observatory.yaml', 'routes: {general_research: gateway_first,', 'routes: {general_research: claude,', 'the shipped file silently overrides the code default'),
    ('crawl-leaves-the-site', 'contentintel/observatory/scrapling_worker.py', 'if key not in seen and len(seen) < 5000 and _same_site(link, root):', 'if key not in seen and len(seen) < 5000:', 'the crawl follows links to other sites'),
    ('crawl-repeats-pages', 'contentintel/observatory/scrapling_worker.py', 'if key not in seen and len(seen) < 5000 and _same_site(link, root):', 'if len(seen) < 5000 and _same_site(link, root):', 'pages are read again and again'),
    ('crawl-continues-after-a-refusal', 'contentintel/observatory/scrapling_worker.py', 'if err.get("code") in ("PLATFORM_BLOCKED", "RATE_LIMITED") and not err.get("robots_path"):', 'if False:', 'a rate limit does not stop the crawl'),
    ('crawl-depth-ignored', 'contentintel/observatory/scrapling_worker.py', '                if level < depth:', '                if True:', 'the crawl goes deeper than asked'),
    ('crawl-offsite-redirect-read', 'contentintel/observatory/scrapling_worker.py', 'if res.get("ok") and not _same_site(res.get("final_url") or url, root):', 'if False:', 'a redirect to another site is read as part of this one'),
    ('crawl-never-gives-up-on-errors', 'contentintel/observatory/scrapling_worker.py', 'if failures >= 5:', 'if failures >= 5000:', 'a dead site is asked for every link'),
    ('crawl-client-limits-unclamped', 'contentintel/observatory/worker_client.py', 'max_pages, depth = max(1, min(int(max_pages), 200)), max(0, min(int(depth), 6))', 'max_pages, depth = int(max_pages), int(depth)', 'a caller can ask for any number of pages'),
    ('crawl-refusal-not-remembered', 'contentintel/observatory/worker_client.py', '            pauses.pause(host, max(settings["politeness"]["rate_limit_pause_seconds"], longer), info["rest"], now, code)', '            pass', 'a refused site is asked again next run'),
    ('crawl-command-limit-ignored', 'contentintel/observatory/commands.py', 'max_pages=args.max_pages or None,', 'max_pages=None,', '--max-pages does nothing'),
    ('agentreach-param-ignored', 'contentintel/observatory/commands.py', 'extra = dict(p.split("=", 1) for p in (args.param or []) if "=" in p)', 'extra = {}', '--param does nothing'),
    ('agentreach-web-not-shared', 'contentintel/observatory/commands.py', '        return _agentreach_web(args, store, ledger, settings, brands, now, target)', '        return 0', 'the web channel reads nothing'),
]
MUTATIONS.extend(U40B)

U40C = [   # update 40, last: sitemap seeding, pause and resume, parser tools, auto-throttle, feed discovery, provider switches, the extra Agent-Reach actions
    ('sitemap-offsite-addresses-followed', 'contentintel/observatory/scrapling_worker.py', 'urls.extend(u for u in locs if urlsplit(u).scheme in ("http", "https") and _same_site(u, root))', 'urls.extend(u for u in locs if urlsplit(u).scheme in ("http", "https"))', 'the sitemap leads the crawl to other sites'),
    ('sitemap-index-not-followed', 'contentintel/observatory/scrapling_worker.py', '                queue.extend(locs[:5])', '                pass', 'a sitemap index is read as if it listed pages'),
    ('sitemap-entities-not-decoded', 'contentintel/observatory/scrapling_worker.py', 'html_lib.unescape(m.strip())', 'm.strip()', 'addresses with &amp; are asked for wrongly'),
    ('sitemap-seed-ignored', 'contentintel/observatory/scrapling_worker.py', '            if spec.get("sitemap"):', '            if False:', 'the sitemap switch does nothing'),
    ('resume-ignored', 'contentintel/observatory/scrapling_worker.py', '        if resume.get("queue"):', '        if False:', 'a paused crawl starts again from the beginning'),
    ('resume-state-not-returned', 'contentintel/observatory/scrapling_worker.py', '        if queue:                                                                                 # what is left,', '        if False:                                                                                 # what is left,', 'nothing is left to resume from'),
    ('resume-state-kept-after-finishing', 'contentintel/services.py', '        elif path.is_file():\n            path.unlink()', '        elif False:\n            path.unlink()', 'a finished crawl is resumed later and read again'),
    ('resume-without-asking', 'contentintel/services.py', 'if resume and path is not None and path.is_file():', 'if path is not None and path.is_file():', 'every crawl silently resumes an old one'),
    ('resume-state-not-saved', 'contentintel/services.py', '        if got.get("state") and got["stopped"] != "no more pages to follow":', '        if False:', 'an early stop cannot be resumed'),
    ('extract-regex-ignored', 'contentintel/observatory/scrapling_worker.py', '                elif spec.get("regex"):', '                elif False:', 'regex extraction returns nothing'),
    ('extract-error-loses-the-page', 'contentintel/observatory/scrapling_worker.py', '            except Exception as exc:                                                              # one bad selector never loses the page', '            except KeyError as exc:                                                               # one bad selector never loses the page', 'one bad selector loses the whole page'),
    ('extract-similar-only-first', 'contentintel/observatory/scrapling_worker.py', 'hits = [first[0], *first[0].find_similar()] if first else []', 'hits = [first[0]] if first else []', 'find_similar finds nothing'),
    ('autothrottle-never-slows', 'contentintel/observatory/scrapling_worker.py', 'return min(self.max_delay, 2.0 * site["lat"]) if self.autothrottle and site.get("lat") else 0.0', 'return 0.0', 'a slow site is asked just as often'),
    ('feeds-probe-not-verified', 'contentintel/observatory/worker_client.py', 'if probe.get("ok") and re.search(r"<(rss|feed|rdf:rdf)\\b", (probe.get("raw") or "")[:3000], re.I) and probe["url"] not in seen:', 'if probe.get("ok") and probe["url"] not in seen:', 'any page at a usual address is called a feed'),
    ('feeds-relative-links-unresolved', 'contentintel/observatory/worker_client.py', 'urljoin(page.get("final_url") or url, link.get("href", ""))', 'link.get("href", "")', 'a relative feed link is not a usable address'),
    ('feeds-service-ignores-switch', 'contentintel/services.py', '    ok, why = allowed(settings, "scrapling", purpose)\n    if not ok:\n        raise K.Failure(K.NEED_SETUP, why)\n    parts = urlsplit(url)', '    ok, why = True, ""\n    if not ok:\n        raise K.Failure(K.NEED_SETUP, why)\n    parts = urlsplit(url)', 'feed discovery runs while Scrapling is switched off'),
    ('command-sitemap-flag-ignored', 'contentintel/observatory/commands.py', 'sitemap=args.sitemap, extract=extract,', 'sitemap=False, extract=extract,', '--sitemap does nothing'),
    ('command-extract-dropped', 'contentintel/observatory/commands.py', 'sitemap=args.sitemap, extract=extract,', 'sitemap=args.sitemap, extract=[],', '--extract does nothing'),
    ('command-resume-dropped', 'contentintel/observatory/commands.py', 'state_dir=args.data_dir, resume=args.resume)', 'state_dir=args.data_dir, resume=False)', '--resume does nothing'),
    ('ai-provider-switch-ignored', 'contentintel/services.py', 'if not on and provider is not None and provider.enabled:', 'if False:', "an owner's switch for a free provider does nothing"),
    ('cat-xiaohongshu-comments-replies-dropped', 'contentintel/agent_reach_catalog.py', '["comments", "{target}", "--limit", "{limit}", "--with-replies", "true"]', '["comments", "{target}", "--limit", "{limit}"]', 'comments come without their replies'),
    ('cat-boss-job-id-not-extracted', 'contentintel/agent_reach_catalog.py', '        clean["job_id"] = found.group(1)', '        clean["job_id"] = clean["target"]', 'Boss gets a whole link where it wants an id'),
    ('svc-crawl-ignores-switch', 'contentintel/services.py', '    if not ok:\n        raise K.Failure(K.NEED_SETUP, why)\n    host = (urlsplit(url).hostname or "").lower()\n    comp = {"id": OC.competitor_id(host), "name": host, "domain": host, "advanced": resolve_scrapling(settings, host, purpose, target_advanced), "selectors": {}}\n    extra = {}\n    if environ is not None:\n        extra["environ"] = environ\n    if runner is not None:\n        extra["runner"] = runner\n    state = None', '    if False:\n        raise K.Failure(K.NEED_SETUP, why)\n    host = (urlsplit(url).hostname or "").lower()\n    comp = {"id": OC.competitor_id(host), "name": host, "domain": host, "advanced": resolve_scrapling(settings, host, purpose, target_advanced), "selectors": {}}\n    extra = {}\n    if environ is not None:\n        extra["environ"] = environ\n    if runner is not None:\n        extra["runner"] = runner\n    state = None', 'a crawl runs while Scrapling is switched off'),
    ('crawl-client-asks-a-resting-site', 'contentintel/observatory/worker_client.py', 'rests the site."""\n    if pauses:', 'rests the site."""\n    if False:', 'a site that said slow down is crawled anyway'),
    ('feeds-client-asks-a-resting-site', 'contentintel/observatory/worker_client.py', 'One page and a few guesses, through the same guarded fetch."""\n    import re\n    from urllib.parse import urljoin\n    if pauses:', 'One page and a few guesses, through the same guarded fetch."""\n    import re\n    from urllib.parse import urljoin\n    if False:', 'a resting site is probed for feeds anyway'),
    ('crawl-command-stores-nothing', 'contentintel/observatory/commands.py', '        for page in got["pages"]:\n            found = ', '        for page in []:\n            found = ', 'crawled pages are not searchable'),
]
MUTATIONS.extend(U40C)

U40D = [   # update 40, final: every per-request Scrapling option on by default, with graceful handling
    ('on-default-solve-challenges-off', 'contentintel/observatory/config.py', '"humanize": True, "solve_challenges": True,', '"humanize": True, "solve_challenges": False,', 'challenge solving is off by default'),
    ('on-default-lost-when-owner-sets-one', 'contentintel/observatory/config.py', '{**SCRAPLING_ON_DEFAULTS, **_advanced_options(', '{**_advanced_options(', 'setting one option centrally switches the others off'),
    ('on-default-refuses-when-permission-off', 'contentintel/services.py', '        if merged.get(key) and not explicit.get(key) and not controls.get(switch, True):', '        if False:', 'a switched-off permission breaks every page read'),
    ('on-default-explicit-request-quietly-dropped', 'contentintel/services.py', '        if merged.get(key) and not explicit.get(key) and not controls.get(switch, True):', '        if merged.get(key) and not controls.get(switch, True):', "a person's own request for a switched-off option is silently ignored"),
    ('on-default-proxy-without-proxies', 'contentintel/services.py', '    if merged.get("proxy") and not explicit.get("proxy") and not wc.proxy_env_names(settings):', '    if False:', 'every page read fails when no proxy is listed'),
    ('on-default-explicit-proxy-dropped', 'contentintel/services.py', '    if merged.get("proxy") and not explicit.get("proxy") and not wc.proxy_env_names(settings):', '    if merged.get("proxy") and not wc.proxy_env_names(settings):', "a person's own proxy request is silently ignored"),
    ('worker-no-browser-no-fallback', 'contentintel/observatory/scrapling_worker.py', '                    if _browser_missing(exc):', '                    if False:', 'a missing browser fails every page read'),
    ('worker-fallback-hides-real-errors', 'contentintel/observatory/scrapling_worker.py', '                    if _browser_missing(exc):', '                    if True:', 'every browser failure is hidden as a missing browser'),
    ('yaml-on-defaults-off', 'observatory.yaml', '    defaults: {browser: true, session: true,', '    defaults: {browser: false, session: true,', 'the shipped file turns the browser default off'),
    ('controls-default-off', 'contentintel/services.py', 'OC.SCRAPLING_ON_DEFAULTS.get(opt, unset) if kind == "bool" else unset', 'unset', 'the Controls tab shows the options as off'),
]
MUTATIONS.extend(U40D)

U41 = [   # update 41: guards as owner switches, the connections layer, the external Agent-Reach client
    ('g-master-ignored', 'contentintel/guards.py', '_state = {g: bool(master and channels.get(g, legacy())) for g in GUARDS}', '_state = {g: bool(channels.get(g, legacy())) for g in GUARDS}', 'the master guard switch does nothing'),
    ('g-unknown-guard-on', 'contentintel/guards.py', '    if name not in GUARDS:\n        return False', '    if name not in GUARDS:\n        return True', 'an unknown guard name blocks'),
    ('g-default-flipped', 'contentintel/guards.py', 'os.environ.get("CONTENTINTEL_LEGACY_GUARDS", "") == "1"', 'os.environ.get("CONTENTINTEL_LEGACY_GUARDS", "") != "1"', 'guards start ON when they should start OFF'),
    ('g-dict-default-on', 'contentintel/guards.py', '_state = {g: bool(source.get(g, legacy())) for g in GUARDS}', '_state = {g: bool(source.get(g, True)) for g in GUARDS}', 'a guard the worker was not told about is ON'),
    ('g-snapshot-all-off', 'contentintel/guards.py', 'return {g: on(g) for g in GUARDS}', 'return {g: False for g in GUARDS}', "the worker is never told the owner's guards"),
    ('cfg-guard-default-on', 'contentintel/observatory/config.py', 'channel_default=_GUARDS.legacy())', 'channel_default=True)', 'the Controls tab shows every guard ON by default'),
    ('pause-read-guard', 'contentintel/observatory/pauses.py', '        if not guards.on("rest_after_refusal"):                  # the owner has not asked sites to rest: never blocks\n            return None', '        if False:                  # the owner has not asked sites to rest: never blocks\n            return None', 'a site written down as resting blocks even with the guard off'),
    ('pause-write-guard', 'contentintel/observatory/pauses.py', '        if not guards.on("rest_after_refusal"):\n            return\n', '        if False:\n            return\n', 'a site is rested with the guard off'),
    ('health-pause-set-guard', 'contentintel/health.py', 'if seconds > 0 and guards.on("rest_after_refusal"):', 'if seconds > 0:', 'a route is paused after failures with the guard off'),
    ('health-pause-read-guard', 'contentintel/health.py', 'if not guards.on("rest_after_refusal") or not pause or', 'if not pause or', 'a paused route blocks with the guard off'),
    ('cooldown-guard', 'contentintel/collectors.py', '    if not guards.on("rest_after_refusal"):\n        return out', '    if False:\n        return out', 'an account rested earlier still blocks with the guard off'),
    ('hosted-runner-guard', 'contentintel/agent_reach_instagram.py', 'return guards.on("hosted_runner_block") and (', 'return (', 'browser channels refuse a hosted runner with the guard off'),
    ('reddit-approval-guard', 'contentintel/reddit_routes.py', '    if not guards.on("reddit_approval_required"):', '    if False:', 'the Reddit approval gate blocks with the guard off'),
    ('safety-fetch-scheme', 'contentintel/safety.py', '    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:\n        raise UnsafeURL("only http(s) addresses are allowed")\n    return candidate', '    if not parsed.hostname:\n        raise UnsafeURL("only http(s) addresses are allowed")\n    return candidate', 'any scheme is fetched when the private-address guard is off'),
    ('w-robots-guard', 'contentintel/observatory/scrapling_worker.py', '            if guards.on("robots_txt"):\n                parser, closed, note', '            if True:\n                parser, closed, note', 'robots.txt is consulted with the guard off'),
    ('w-private-guard', 'contentintel/observatory/scrapling_worker.py', 'if guards.on("private_address_block") and not self.private_ok and not safety.resolves_public(host):', 'if not self.private_ok and not safety.resolves_public(host):', 'local addresses are refused with the guard off'),
    ('w-rest-guard', 'contentintel/observatory/scrapling_worker.py', '                if guards.on("rest_after_refusal"):                                          # the owner\'s rest guard', "                if True:                                          # the owner's rest guard", 'a refusing host rests with the guard off'),
    ('w-no-retry', 'contentintel/observatory/scrapling_worker.py', 'if tries < int(self.retry["attempts"]):', 'if tries < 1:', 'a refusing site is not retried'),
    ('w-guards-from-job-ignored', 'contentintel/observatory/scrapling_worker.py', '        if "guards" in job:', '        if False:', "the worker ignores the owner's guards"),
    ('p-robots-guard', 'contentintel/observatory/polite.py', 'rb = self._robots_for(current) if guards.on("robots_txt") else None', 'rb = self._robots_for(current)', 'the polite reader asks for robots.txt with the guard off'),
    ('p-no-retry', 'contentintel/observatory/polite.py', 'attempts = 1 if guards.on("rest_after_refusal") else max(1, int(self.retry["attempts"]))', 'attempts = 1', 'the polite reader never retries a refusing site'),
    ('p-private-guard', 'contentintel/observatory/polite.py', 'if guards.on("private_address_block") and not site.public:', 'if not site.public:', 'the polite reader refuses a private address with the guard off'),
    ('c-secrets-switch', 'contentintel/connections.py', '    if decide(settings, conn_id)["secrets"]:\n        for name in', '    if True:\n        for name in', 'declared secrets are handed over although the owner withheld them'),
    ('c-secrets-everything', 'contentintel/connections.py', '        for name in CONNECTIONS[conn_id]["secrets"]:', '        for name in environ:', 'every secret is handed to the repository'),
    ('c-score-unweighted', 'contentintel/connections.py', 'weights = list(range(1, len(window) + 1))', 'weights = [1] * len(window)', 'old attempts count as much as recent ones'),
    ('c-down-threshold', 'contentintel/connections.py', '"degraded" if score >= 40', '"degraded" if score >= 30', 'a failing connection is called degraded, not down'),
    ('c-backup-counted-as-attempt', 'contentintel/connections.py', '            entry["builtin_used"] = entry.get("builtin_used", 0) + 1\n            return', '            entry["builtin_used"] = entry.get("builtin_used", 0) + 1', 'using the backup is scored as a repository attempt'),
    ('c-off-still-runs', 'contentintel/connections.py', '    if not d["on"]:\n        order = tuple(r for r in order if r == "builtin")', '    if False:\n        order = tuple(r for r in order if r == "builtin")', 'a switched-off connection is still run'),
    ('c-builtin-first-order', 'contentintel/connections.py', '"builtin_first": ("builtin", "external")', '"builtin_first": ("external", "builtin")', 'builtin_first runs the repository first'),
    ('c-external-only-falls-back', 'contentintel/connections.py', '"external_only": ("external",)', '"external_only": ("external", "builtin")', 'external_only quietly uses the backup'),
    ('c-failure-not-recorded', 'contentintel/connections.py', '                note(settings, conn_id, False, now, code=getattr(exc, "code", "UPSTREAM_BROKEN"), message=str(exc), ledger=ledger)', '                pass', 'a repository failure never lowers its health'),
    ('c-install-off', 'contentintel/connections.py', '    if not d["on"]:\n        return False, f"{conn_id}: switched off, nothing installed"', '    if False:\n        return False, f"{conn_id}: switched off, nothing installed"', 'a switched-off connection is installed'),
    ('c-install-not-latest', 'contentintel/connections.py', 'sh([pip, "install", "-q", "--upgrade", spec["pip"], *spec.get("pip_extra", ())])', 'sh([pip, "install", "-q", spec["pip"], *spec.get("pip_extra", ())])', 'latest does not upgrade'),
    ('c-node-not-newest-tag', 'contentintel/connections.py', 'ref = tags[0] if tags else "HEAD"', 'ref = "HEAD"', 'latest does not pick the newest release'),
    ('c-probe-success-not-recorded', 'contentintel/connections.py', '    note(settings, conn_id, True, now, ledger=ledger)\n    return True, f"{conn_id}: {message}"', '    return True, f"{conn_id}: {message}"', 'a working probe is not recorded'),
    ('arx-code-lost', 'contentintel/agent_reach_external.py', 'code if code in K.CODES else K.UPSTREAM_BROKEN', 'K.UPSTREAM_BROKEN', "Agent-Reach's error codes are all reported as broken"),
    ('arx-optional-dropped', 'contentintel/agent_reach_external.py', '            if name in OPTIONAL and values.get(name):', '            if False:', '--limit and the other options are never sent'),
    ('arx-env-leak', 'contentintel/agent_reach_external.py', 'env=CONN.external_env(self.settings, "agent_reach", self.environ)', 'env=self.environ', 'every secret is handed to Agent-Reach'),
    ('sv-ar-always-builtin', 'contentintel/services.py', '    if not exe:\n        return builtin\n    return ARX.RoutedAgentReach', '    if False:\n        return builtin\n    return ARX.RoutedAgentReach', 'Agent-Reach is run although it is not installed'),
    ('sv-ar-builtin-only-ignored', 'contentintel/services.py', 'exe = CONN.binary_path("agent_reach") if d["on"] and d["route"] != "builtin_only" else ""', 'exe = CONN.binary_path("agent_reach") if d["on"] else ""', 'builtin_only still runs the repository'),
    ('sv-chain-refusal-guard', 'contentintel/services.py', '(exc.code in REFUSALS and _GUARDS.on("rest_after_refusal"))', 'exc.code in REFUSALS', 'a refusal ends the reader chain with the guard off'),
    ('sv-scrapling-off-ignored', 'contentintel/services.py', '        if not conn["on"]:\n            skipped.append("scrapling: the Scrapling connection', '        if False:\n            skipped.append("scrapling: the Scrapling connection', 'a switched-off Scrapling connection is still used'),
    ('sv-refusal-counts-against-scrapling', 'contentintel/services.py', 'if name == "scrapling" and exc.code not in REFUSALS:', 'if name == "scrapling":', "a site's refusal lowers Scrapling's health"),
    ('sv-builtin-first-chain', 'contentintel/services.py', '"builtin_first": ["core", "jina", "scrapling"]', '"builtin_first": ["scrapling", "jina", "core"]', 'builtin_first still reads with Scrapling first'),
    ('wc-version-pin-swapped', 'contentintel/observatory/worker_client.py', '.get("scrapling", "latest") == "pinned" else ""', '.get("scrapling", "latest") == "latest" else ""', 'latest pins the version and pinned does not'),
    ('cmd-connections-bad-key', 'contentintel/observatory/commands.py', '    if args.key and args.key not in CONN.CONNECTIONS:', '    if False:', 'a mistyped connection name is accepted'),
    ('sheets-connections-tab', 'contentintel/sheets.py', '+ _connection_tabs(store, now, report) + _ai_tabs(store, now, report):', '+ _ai_tabs(store, now, report):', 'the Connections Health tab is not written'),
]
MUTATIONS.extend(U41)

U42 = [   # update 42: the Acceptance workflow passes the contact address
    ('acceptance-workflow-no-contact', '.github/workflows/acceptance.yml', 'OBSERVATORY_CONTACT: ${{ secrets.OBSERVATORY_CONTACT }}', 'OBSERVATORY_CONTACT_UNUSED: ${{ secrets.OBSERVATORY_CONTACT }}', 'the Acceptance workflow does not pass the contact address, so 14 checks skip'),
    ('acceptance-workflow-no-reddit-secret', '.github/workflows/acceptance.yml', 'REDDIT_CLIENT_SECRET: ${{ secrets.REDDIT_CLIENT_SECRET }}', 'REDDIT_CLIENT_SECRET_UNUSED: ${{ secrets.REDDIT_CLIENT_SECRET }}', 'the Acceptance workflow does not pass a documented secret the Reddit check reads'),
]
MUTATIONS.extend(U42)

def run_tests(workdir):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    patterns = os.environ.get("SABOTAGE_TESTS", "").split() or TESTS                # SABOTAGE_TESTS narrows the run to chosen test files, for a slow machine
    files = [str(p.relative_to(workdir)) for pat in patterns for p in sorted(workdir.glob(pat))]
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
