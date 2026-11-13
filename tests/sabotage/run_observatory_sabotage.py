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
TESTS = ["tests/test_observatory_*.py", "tests/test_sessions.py", "tests/test_session_live.py", "tests/test_contract.py", "tests/test_services.py", "tests/test_ytdlp_youtube.py", "tests/test_external_repository.py", "tests/test_platform_wiring.py", "tests/test_platform_matrix.py", "tests/test_reddit_routes.py", "tests/test_agent_reach_instagram.py", "tests/test_ai_github.py", "tests/test_ai_health_tab.py", "tests/test_ai_config.py", "tests/test_providers.py", "tests/test_agent_reach_catalog.py", "tests/test_owner_defaults.py", "tests/test_capability_matrix.py", "tests/test_research.py", "tests/test_sources.py", "tests/test_guards.py", "tests/test_connections.py", "tests/test_sheets.py", "tests/test_health.py", "tests/test_acceptance.py", "tests/test_acceptance_agent_reach.py", "tests/test_acceptance_fixes.py", "tests/test_ai_gateway.py", "tests/test_ai_commands.py", "tests/test_ai_github.py", "tests/test_ai_config.py", "tests/test_ai_health_tab.py"]

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
    ('providers-agent-reach-on-hosted', 'contentintel/providers.py', '            if key.lower() in ("on", "1", "true", "yes") and not cls.unavailable_reason(env, which):', '            if key.lower() in ("on", "1", "true", "yes"):', 'Reddit search is built on a hosted runner or without OpenCLI'),
    ('providers-agent-reach-always', 'contentintel/providers.py', '            if key.lower() in ("on", "1", "true", "yes") and not cls.unavailable_reason(env, which):', '            if not cls.unavailable_reason(env, which):', "Agent-Reach search is built without the owner's opt-in"),
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
    ('sv-refusal-counts-against-scrapling', 'contentintel/services.py', '                _note_scrapling(settings, exc)\n            if choice != "auto"', '                pass\n            if choice != "auto"', 'a failed Scrapling page read is not recorded in its health'),
    ('sv-builtin-first-chain', 'contentintel/services.py', '"builtin_first": ["core", "jina", "scrapling"]', '"builtin_first": ["scrapling", "jina", "core"]', 'builtin_first still reads with Scrapling first'),
    ('wc-version-pin-swapped', 'contentintel/observatory/worker_client.py', '.get("scrapling", "latest") == "pinned" else ""', '.get("scrapling", "latest") == "latest" else ""', 'latest pins the version and pinned does not'),
    ('cmd-connections-bad-key', 'contentintel/observatory/commands.py', '    if args.key and args.key not in CONN.CONNECTIONS:', '    if False:', 'a mistyped connection name is accepted'),
    ('sheets-connections-tab', 'contentintel/sheets.py', '+ _connection_tabs(store, now, report) + _ai_tabs(store, now, report):', '+ _ai_tabs(store, now, report):', 'the Connections Health tab is not written'),
]
MUTATIONS.extend(U41)

U42 = [   # update 42: the Acceptance workflow passes the contact address
    ('acceptance-workflow-no-contact', '.github/workflows/acceptance.yml', 'OBSERVATORY_CONTACT: ${{ secrets.OBSERVATORY_CONTACT }}           # the contact address', 'OBSERVATORY_CONTACT_UNUSED: ${{ secrets.OBSERVATORY_CONTACT }}           # the contact address', 'the Acceptance workflow does not pass the contact address, so 14 checks skip'),
    ('acceptance-workflow-no-reddit-secret', '.github/workflows/acceptance.yml', 'REDDIT_CLIENT_SECRET: ${{ secrets.REDDIT_CLIENT_SECRET }}', 'REDDIT_CLIENT_SECRET_UNUSED: ${{ secrets.REDDIT_CLIENT_SECRET }}', 'the Acceptance workflow does not pass a documented secret the Reddit check reads'),
]
MUTATIONS.extend(U42)

U43 = [   # update 43: the Agent-Reach acceptance checks and true skip reasons, and the feed check
    ('ar-check-github-dropped', 'contentintel/observatory/acceptance_checks.py', '    if SV.agent_reach_allowed(settings, "github", "general_research")[0]:\n        def github_check', '    if False:\n        def github_check', 'the GitHub channel has no acceptance check'),
    ('ar-check-web-without-contact', 'contentintel/observatory/acceptance_checks.py', '    if contact and SV.agent_reach_allowed(settings, "web", "general_research")[0]:', '    if SV.agent_reach_allowed(settings, "web", "general_research")[0]:', 'the web check runs with no contact address'),
    ('ar-check-hosted-runner-ignored', 'contentintel/observatory/acceptance_checks.py', 'or AR.hosted_runner(environ) or not client.available():', 'or not client.available():', 'a browser channel is checked on a hosted runner although the guard says not to'),
    ('ar-check-opencli-unavailable-ignored', 'contentintel/observatory/acceptance_checks.py', 'or AR.hosted_runner(environ) or not client.available():', 'or AR.hosted_runner(environ) or False:', 'a browser channel is checked although OpenCLI is not installed'),
    ('ar-check-empty-answer-passes', 'contentintel/observatory/acceptance_checks.py', '            if not rows:\n                raise K.Failure(K.UPSTREAM_BROKEN, f"the {ch} {act}', '            if False:\n                raise K.Failure(K.UPSTREAM_BROKEN, f"the {ch} {act}', 'a channel that returns nothing passes'),
    ('ar-check-limit', 'contentintel/observatory/acceptance_checks.py', '"marketing")}, limit=2)', '"marketing")}, limit=20)', 'the check asks for 20 results instead of a small sample'),
    ('ar-reason-is-the-contact-message', 'contentintel/observatory/acceptance_checks.py', 'out[key] = f"cannot run: OpenCLI is not installed on this machine (it needs a browser logged in to {site}, on a machine you control)"', 'out[key] = "no contact address or no brand website: set OBSERVATORY_CONTACT"', 'a channel that cannot run blames the contact address'),
    ('ar-reason-switched-off-lost', 'contentintel/observatory/acceptance_checks.py', '            out[key] = f"switched off: {why}"\n        elif channel == "web"', '            pass\n        elif channel == "web"', 'a switched-off channel has no reason'),
    ('ar-reason-hosted-runner-lost', 'contentintel/observatory/acceptance_checks.py', '            if AR.hosted_runner(environ):\n                out[key]', '            if False:\n                out[key]', 'a hosted runner is not named as the reason'),
    ('feed-no-feed-fails', 'contentintel/observatory/acceptance_checks.py', 'raise Skipped(f"no feed to read:', 'raise K.Failure(K.NOT_FOUND, f"no feed to read:', 'a site with no feed fails the route'),
    ('feed-first-brand-only', 'contentintel/observatory/acceptance_checks.py', 'for n, dom in enumerate([domain, *other_domains]):', 'for n, dom in enumerate([domain]):', 'the feed is only ever tried on the first brand'),
    ('feed-unreadable-other-site-aborts', 'contentintel/observatory/acceptance_checks.py', '                if n == 0:\n                    raise\n                continue', '                raise', "another brand's unreadable site stops the feed check"),
    ('cli-agent-reach-checks-not-added', 'contentintel/cli.py', '        checks.update(obs_checks.agent_reach_checks(settings, contact, now))\n', '', 'the Agent-Reach checks are never added to the acceptance run'),
    ('cli-agent-reach-reasons-not-wired', 'contentintel/cli.py', '{**_oc.reddit_missing(_s, _contact), **_oc.agent_reach_missing(_s, _contact)}', '_oc.reddit_missing(_s, _contact)', 'the reasons for the Agent-Reach channels never reach the run'),
]
MUTATIONS.extend(U43)

U44 = [   # update 44: non-JSON answers, per-provider routing, skipped providers, the model catalogue
    ('ai-notjson-answer-lost', 'contentintel/ai/clients.py', '            except ValueError:                                           # an answer that is not JSON', '            except KeyError:                                           # an answer that is not JSON', 'an answer that is not JSON crashes instead of being kept'),
    ('ai-gateway-notjson-ignored', 'contentintel/ai/clients.py', '        if isinstance(body, dict) and "_not_json" in body:\n            raise Failure(contract.UPSTREAM_BROKEN, f"the gateway answered', '        if False:\n            raise Failure(contract.UPSTREAM_BROKEN, f"the gateway answered', "the gateway's non-JSON answer is not reported"),
    ('ai-direct-notjson-ignored', 'contentintel/ai/clients.py', '        if isinstance(answer, dict) and "_not_json" in answer:                 # what the provider really sent back', '        if False:                 # what the provider really sent back', "GitHub Models' non-JSON answer is not reported"),
    ('ai-direct-notjson-token-shown', 'contentintel/ai/clients.py', 'raise Failure(contract.UPSTREAM_BROKEN, self._clean(f"the provider answered {status} with something that is not JSON', 'raise Failure(contract.UPSTREAM_BROKEN, (f"the provider answered {status} with something that is not JSON', 'the token can appear in the report of a non-JSON answer'),
    ('ai-isolate-includes-disabled', 'contentintel/ai/gateway.py', '(r.get("model_id") or r.get("modelId")) and r.get("enabled")})', '(r.get("model_id") or r.get("modelId")) and True})', 'disabled models are split into groups too'),
    ('ai-isolate-includes-custom', 'contentintel/ai/gateway.py', 'r.get("platform") != "custom" and (r.get("model_id")', 'True and (r.get("model_id")', 'custom endpoints are split into groups too'),
    ('ai-isolate-wrong-endpoint', 'contentintel/ai/gateway.py', 'self.base_url + "/api/settings/unify"', 'self.base_url + "/api/settings/unifyx"', 'the grouping change goes to the wrong address'),
    ('ai-isolate-unify-left-on', 'contentintel/ai/gateway.py', 'body={"enabled": False, "overrides"', 'body={"enabled": True, "overrides"', 'grouping stays switched on'),
    ('ai-isolate-raises', 'contentintel/ai/gateway.py', '        except Exception as exc:                                  # noqa: BLE001 - never stops a run\n            return False, f"{exc.__class__.__name__}', '        except KeyError as exc:                                  # noqa: BLE001 - never stops a run\n            return False, f"{exc.__class__.__name__}', 'a gateway that cannot be reached stops the run while isolating'),
    ('ai-cmd-isolate-not-called', 'contentintel/ai/commands.py', 'isolated, why = side.isolate_models(environ["FREEAPI_ADMIN_PASSWORD"])', 'isolated, why = True, ""', 'models are never made their own group in a live run'),
    ('ai-cmd-isolate-failure-silent', 'contentintel/ai/commands.py', '                if not isolated:\n                    notes.append("NOTE: could not', '                if False:\n                    notes.append("NOTE: could not', 'a gateway that could not isolate models says nothing'),
    ('ai-cmd-direct-missing-credential-fails', 'contentintel/ai/commands.py', 'notes.append("SKIPPED: " + p.id + ": no credential in the environment (" + ", ".join(p.credential_env)', 'notes.append("APPLICATION: " + p.id + ": no credential in the environment (" + ", ".join(p.credential_env)', 'a direct provider with no key fails the run'),
    ('ai-cmd-gateway-missing-credential-fails', 'contentintel/ai/commands.py', 'notes.extend("SKIPPED: " + s["provider"] + ": no credential', 'notes.extend("APPLICATION: " + s["provider"] + ": no credential', 'a gateway provider with no key fails the run'),
    ('ai-cmd-skip-note-everywhere', 'contentintel/ai/commands.py', 'if skipped_for and only and skipped_for not in only:', 'if False:', 'every run lists providers it is not trying'),
    ('ai-cmd-skip-fails-the-run', 'contentintel/ai/commands.py', '        app_fail += n.startswith("APPLICATION")\n    targets = []', '        app_fail += n.startswith(("APPLICATION", "SKIPPED"))\n    targets = []', 'a skipped provider turns the run red'),
    ('ai-svc-switched-off-is-a-problem', 'contentintel/services.py', 'notes.append(f"SKIPPED: {pid}: switched off by the owner', 'notes.append(f"APPLICATION: {pid}: switched off by the owner', 'a provider the owner switched off fails the run'),
    ('ai-catalogue-retired-groq-approved', 'ai_providers.yaml', '    - id: llama-3.3-70b-versatile\n      approved: false', '    - id: llama-3.3-70b-versatile\n      approved: true', 'a retired Groq model is approved again'),
    ('ai-catalogue-retired-google-in-a-lane', 'ai_lanes.yaml', '{provider: google, model: gemini-3.5-flash}', '{provider: google, model: gemini-2.5-flash}', 'a lane routes to the retired Gemini model'),
]
MUTATIONS.extend(U44)

U45 = [   # update 44, later: the Reddit search is skipped, not failed, where OpenCLI is not installed
    ('rs-build-ignores-opencli', 'contentintel/providers.py', 'key.lower() in ("on", "1", "true", "yes") and not cls.unavailable_reason(env, which):', 'key.lower() in ("on", "1", "true", "yes"):', 'the Reddit search is built where OpenCLI is not installed'),
    ('rs-youtube-blamed-for-opencli', 'contentintel/providers.py', '        if not cls.needs_browser:\n            return ""\n        import shutil', '        if False:\n            return ""\n        import shutil', 'YouTube search is held back for want of OpenCLI'),
    ('rs-hosted-guard-ignored', 'contentintel/providers.py', '        if AR.hosted_runner(env):\n            return f"cannot run on a GitHub-hosted runner: it needs OpenCLI', '        if False:\n            return f"cannot run on a GitHub-hosted runner: it needs OpenCLI', 'the hosted-runner guard is ignored for the Reddit search'),
    ('rs-opencli-check-skipped', 'contentintel/providers.py', '        if not (which or shutil.which)(env.get("OPENCLI_BIN", "opencli")):\n            return f"cannot run: OpenCLI', '        if False:\n            return f"cannot run: OpenCLI', 'a missing OpenCLI is not noticed'),
    ('rs-reason-names-instagram', 'contentintel/providers.py', '(it needs a browser logged in to {AR.site_name(cls.channel)}, on a machine you control)"\n        return ""', '(it needs a browser logged in to Instagram, on a machine you control)"\n        return ""', 'the Reddit search blames Instagram'),
    ('rs-missing-reason-not-wired', 'contentintel/cli.py', '    ctx.missing.update(providers_mod.agent_reach_search_missing(os.environ, providers))', '    pass', 'the acceptance run never gets the true reason'),
    ('rs-missing-ignores-optin', 'contentintel/providers.py', 'cls.name not in built and (env.get(cls.env_var) or "").strip().lower() in ("on", "1", "true", "yes"):', 'cls.name not in built:', 'a search the owner never turned on is explained as unable to run'),
    ('rs-opencli-message-instagram', 'contentintel/agent_reach_instagram.py', 'a browser logged in to {site_name(self.site)}, which a hosted runner does not have")', 'a browser logged in to Instagram, which a hosted runner does not have")', 'every OpenCLI channel is said to need Instagram'),
]
MUTATIONS.extend(U45)

U46 = [   # update 45: the plan wording
    ('plan-says-cannot-run-twice', 'contentintel/cli.py', '((p.skip if p.skip.startswith("cannot run") else f"cannot run: {p.skip}") if p.skip else "will run")', '(f"cannot run: {p.skip}" if p.skip else "will run")', 'the plan says cannot run twice'),
]
MUTATIONS.extend(U46)

U47 = [   # update 46: competitor filter, empty searches, page-read summary, YouTube corpus, report and tab fallbacks, the Observatory workflow
    ('u46-filter-id-only', 'contentintel/observatory/commands.py', '            if low in names or host in names:\n                hits.add(sid)', '            if low == sid.lower():\n                hits.add(sid)', 'a competitor can only be named by its internal id'),
    ('u46-filter-unknown-silent', 'contentintel/observatory/commands.py', '    if unknown:\n        planned = ', '    if False:\n        planned = ', 'a name that matches nothing is quietly dropped'),
    ('u46-monitor-no-sites-succeeds', 'contentintel/observatory/commands.py', 'brands/<brand>/observatory.yaml.")\n        return 1\n    if not contact:', 'brands/<brand>/observatory.yaml.")\n        return 0\n    if not contact:', 'a monitor run with nothing to monitor is a success'),
    ('u46-ar-empty-search-fails', 'contentintel/observatory/commands.py', 'empty_ok=(what == "search"))', 'empty_ok=False)', 'a search with no matches is a broken route'),
    ('u46-ar-empty-search-silent', 'contentintel/observatory/commands.py', '        if not env["items"]:\n            print(f"=== {brand.name}: Agent-Reach', '        if False:\n            print(f"=== {brand.name}: Agent-Reach', 'an empty search is recorded and shown as if it had results'),
    ('u46-read-menu-shown', 'contentintel/observatory/commands.py', '        if len(seen) < 50 or len(seen.split()) < 8 or seen.lower().startswith("quick view"):', '        if seen.lower().startswith("quick view"):', 'short menu labels are shown as page content'),
    ('u46-read-links-shown', 'contentintel/observatory/commands.py', '        if len(seen) < 50 or len(seen.split()) < 8 or seen.lower().startswith("quick view"):', '        if len(seen) < 50 or seen.lower().startswith("quick view"):', 'a long unbroken string is shown as page text'),
    ('u46-read-prices-dropped', 'contentintel/observatory/commands.py', '    if prices:\n        print(f"  prices seen', '    if False:\n        print(f"  prices seen', 'prices on the page are not shown'),
    ('u46-yt-nothing-succeeds', 'contentintel/observatory/commands.py', '    if args.what == "youtube" and empty_brands and len(empty_brands) == len(brands):', '    if False:', 'a YouTube corpus with no channel anywhere is a success'),
    ('u46-report-no-fallback', 'contentintel/observatory/commands.py', '        if not sites:                                           # no monitor run has finished yet', '        if False:                                           # no monitor run has finished yet', 'the report ignores the competitors discovery chose'),
    ('u46-tab-drops-monitoring', 'contentintel/observatory/report.py', '        if c.get("domain") and c["domain"] in monitored_domains:          # already shown', '        if c.get("status") == "monitoring" or (c.get("domain") and c["domain"] in monitored_domains):          # already shown', 'a competitor chosen for monitoring vanishes from the Competitors tab'),
    ('u46-wf-ai-secret-dropped', '.github/workflows/observatory.yml', '          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}           # the AI layer', '          ANTHROPIC_API_KEY_UNUSED: ${{ secrets.ANTHROPIC_API_KEY }}           # the AI layer', 'the workflow does not pass the Claude key'),
    ('u46-wf-scrapling-crawl', '.github/workflows/observatory.yml', " || (inputs.action == 'corpus' && (inputs.what == 'crawl' || inputs.what == 'deepread'))", '', 'a crawl run does not install Scrapling by itself'),
    ('u46-wf-gateway-blocks', '.github/workflows/observatory.yml', '        continue-on-error: true\n        shell: bash\n        run: |\n          # the FreeLLMAPI connection: the newest release by default; the recorded', '        shell: bash\n        run: |\n          # the FreeLLMAPI connection: the newest release by default; the recorded', 'a gateway that cannot be built stops the run'),
    ('u46-wf-gateway-always', '.github/workflows/observatory.yml', "        if: ${{ inputs.action == 'discover' || inputs.action == 'judge' }}\n        continue-on-error: true\n        shell: bash\n        run: |\n          # the FreeLLMAPI", '        if: ${{ true }}\n        continue-on-error: true\n        shell: bash\n        run: |\n          # the FreeLLMAPI', 'the gateway is built for every action'),
]
MUTATIONS.extend(U47)

U48 = [   # update 46: GitHub Models API versions and redirects, the OpenRouter catalogue
    ('u46-gh-follows-redirects', 'contentintel/ai/clients.py', '    def redirect_request(self, *args, **kwargs):\n        return None', '    def redirect_request(self, *args, **kwargs):\n        return super().redirect_request(*args, **kwargs)', "a redirect is followed behind the caller's back"),
    ('u46-gh-redirect-any-host', 'contentintel/ai/clients.py', 'if status in (307, 308) and hop == 0 and self._same_host(where):', 'if status in (307, 308) and hop == 0:', 'the token is sent to whatever host a redirect names'),
    ('u46-gh-redirect-unreported', 'contentintel/ai/clients.py', '        if 300 <= status < 400:\n            raise Failure(contract.UPSTREAM_BROKEN, self._clean(f"the provider redirected', '        if False:\n            raise Failure(contract.UPSTREAM_BROKEN, self._clean(f"the provider redirected', 'a redirect is not reported'),
    ('u46-or-dead-model-approved', 'ai_providers.yaml', '    - id: openai/gpt-oss-120b:free\n      approved: false', '    - id: openai/gpt-oss-120b:free\n      approved: true', 'the dead OpenRouter free model is approved again'),
]
MUTATIONS.extend(U48)

U49 = [   # update 46: the live check's token limit
    ('u46-canary-too-small', 'contentintel/ai/commands.py', 'CANARY_MAX_TOKENS = 512 ', 'CANARY_MAX_TOKENS = 24 ', 'a thinking model has no room left to answer in the live check'),
]
MUTATIONS.extend(U49)

U50 = [   # update 47: page-read summary, Sheets reads, Scrapling health, cannot run here, acceptance GitHub, OpenRouter and GitHub clients
    ('u47-gh-no-user-agent', 'contentintel/ai/clients.py', '"Content-Type": "application/json", "Accept": "application/json", "User-Agent": USER_AGENT}', '"Content-Type": "application/json", "Accept": "application/json"}', "the provider is called under urllib's own name"),
    ('u47-or-paid-completion-allowed', 'contentintel/ai/clients.py', ' and str(price.get("completion", "1")) in ("0", "0.0") and "text" in modalities:', ' and "text" in modalities:', 'a model with a priced completion is treated as free'),
    ('u47-or-image-model-allowed', 'contentintel/ai/clients.py', 'in ("0", "0.0") and str(price.get("completion", "1")) in ("0", "0.0") and "text" in modalities:', 'in ("0", "0.0") and str(price.get("completion", "1")) in ("0", "0.0"):', 'an image model is asked for text'),
    ('u47-or-suffix-ignored', 'contentintel/ai/clients.py', 'if str(m.get("id", "")).endswith(":free") and', 'if True and', 'a model without the free suffix is accepted'),
    ('u47-or-router-dropped', 'contentintel/ai/clients.py', 'chain = lead[: self.MAX_MODELS - 1] + [self.ROUTER]', 'chain = lead[: self.MAX_MODELS - 1]', "OpenRouter's own free router is never asked"),
    ('u47-or-list-read-every-call', 'contentintel/ai/clients.py', '        if self._free is not None:\n            return self._free', '        if False:\n            return self._free', 'the free list is read on every call'),
    ('u47-or-too-many-models', 'contentintel/ai/clients.py', 'sorted(found, reverse=True)[:3]', 'sorted(found, reverse=True)[:10]', 'more than three free models are listed'),
    ('u47-or-smallest-first', 'contentintel/ai/clients.py', 'sorted(found, reverse=True)[:3]', 'sorted(found)[:3]', 'the smallest free models lead'),
    ('u47-or-gone-model-unexplained', 'contentintel/ai/clients.py', 'if status == 404 and "free" in text_error.lower():', 'if False:', 'a model withdrawn from free use is not reported as gone'),
    ('u47-cmd-openrouter-uses-github-client', 'contentintel/ai/commands.py', 'klass = cl.OpenRouterClient if str(p.direct["base_url"]).startswith("https://openrouter.ai") else cl.DirectClient', 'klass = cl.DirectClient', 'OpenRouter is called with the GitHub client'),
    ('u47-registry-openrouter-endpoint', 'contentintel/ai/registry.py', 'DIRECT_ENDPOINTS = ("https://openrouter.ai/api/v1",)', 'DIRECT_ENDPOINTS = ()', 'OpenRouter is not an allowed direct endpoint'),
    ('u47-yaml-openrouter-via-gateway', 'ai_providers.yaml', '    gateway_platform: openrouter\n    direct:\n      base_url: https://openrouter.ai/api/v1\n    terms:', '    gateway_platform: openrouter\n    terms:', 'OpenRouter goes back through the gateway'),
    ('u47-gateway-log-folder', 'contentintel/ai/gateway.py', '        self.log_path.parent.mkdir(parents=True, exist_ok=True)          # the scratch folder', '        pass          # the scratch folder', "the gateway's log folder is not made"),
    ('u47-sheets-decisions-list-no-retry', 'contentintel/sheets.py', 'if "Competitors" not in [w.title for w in _call(spreadsheet.worksheets)]:', 'if "Competitors" not in [w.title for w in spreadsheet.worksheets()]:', 'a refused tab list is not retried when reading decisions'),
    ('u47-sheets-decisions-read-no-retry', 'contentintel/sheets.py', '_call(_call(spreadsheet.worksheet, "Competitors").get_all_values)', 'spreadsheet.worksheet("Competitors").get_all_values()', 'a refused read of the decisions is not retried'),
    ('u47-sheets-controls-list-no-retry', 'contentintel/sheets.py', 'if services.CONTROLS_TAB not in [w.title for w in _call(spreadsheet.worksheets)]:', 'if services.CONTROLS_TAB not in [w.title for w in spreadsheet.worksheets()]:', 'a refused tab list is not retried when reading controls'),
    ('u47-sheets-controls-read-no-retry', 'contentintel/sheets.py', '_call(_call(spreadsheet.worksheet, services.CONTROLS_TAB).get_all_values)', 'spreadsheet.worksheet(services.CONTROLS_TAB).get_all_values()', 'a refused read of the controls is not retried'),
    ('u47-scrapling-crawl-success-unrecorded', 'contentintel/services.py', '    if got.get("pages"):                                                  # pages came back through it', '    if False:                                                  # pages came back through it', 'a crawl that worked is not recorded'),
    ('u47-scrapling-crawl-failure-unrecorded', 'contentintel/services.py', '        _note_scrapling(settings, exc)\n        raise\n    if got.get("pages"):', '        raise\n    if got.get("pages"):', 'a crawl that failed is not recorded'),
    ('u47-scrapling-feeds-success-unrecorded', 'contentintel/services.py', '    CONN.note(settings, "scrapling", True, _utc_now())\n    return found', '    return found', 'a feed search that worked is not recorded'),
    ('u47-scrapling-setup-counted', 'contentintel/services.py', 'if exc.code not in REFUSALS and exc.code != K.NEED_SETUP:', 'if exc.code not in REFUSALS:', "a missing environment lowers Scrapling's health"),
    ('u47-scrapling-refusal-counted', 'contentintel/services.py', 'if exc.code not in REFUSALS and exc.code != K.NEED_SETUP:', 'if exc.code != K.NEED_SETUP:', "a site's refusal lowers Scrapling's health"),
    ('u47-health-cannot-run-ignored', 'contentintel/health.py', 'if cannot_run_here(source, route) and not (fresh and acc.get("passed")):', 'if False:', 'routes that need OpenCLI look unproven instead of unable to run'),
    ('u47-health-cannot-run-hides-a-pass', 'contentintel/health.py', 'if cannot_run_here(source, route) and not (fresh and acc.get("passed")):', 'if cannot_run_here(source, route):', 'a route that really passed is hidden as unable to run'),
    ('u47-health-cannot-run-set-shrunk', 'contentintel/health.py', '| {("instagram", "agent_reach"), ("reddit", "agent_reach"), ("web_search", "agent_reach_reddit")})', '| {("instagram", "agent_reach"), ("reddit", "agent_reach")})', 'the Reddit search is not marked as unable to run here'),
    ('u47-health-cannot-run-no-note', 'contentintel/health.py', '(f" | {NEEDS_YOUR_MACHINE_NOTE}" if cannot_run_here(src.id, rt.name) else "")', '""', 'the reason it cannot run is not shown'),
    ('u47-acceptance-github-one-query', 'contentintel/observatory/acceptance_checks.py', 'for query in (environ.get("GITHUB_CHECK_QUERY", "agent reach"), "python"):', 'for query in (environ.get("GITHUB_CHECK_QUERY", "agent reach"),):', 'the GitHub check gives up after one query'),
    ('u47-acceptance-github-vague', 'contentintel/observatory/acceptance_checks.py', '(the workflow passes GITHUB_TOKEN to lift that)', '(see the workflow)', 'the GitHub check does not say what to do'),
    ('u47-acceptance-yml-github-token', '.github/workflows/acceptance.yml', '          GITHUB_TOKEN: ${{ github.token }}                  # lifts', '          GITHUB_TOKEN_UNUSED: ${{ github.token }}                  # lifts', "the acceptance run is not given the job's token"),
    ('u47-read-printed-per-brand', 'contentintel/observatory/commands.py', '    if page["url"] in shown:', '    if False:', 'the same page is described once per brand'),
    ('u47-read-zero-price-shown', 'contentintel/observatory/commands.py', '            if amount > 0 and len(re.findall', '            if len(re.findall', 'a $0.00 placeholder is shown as a price'),
    ('u47-read-links-kept-in-names', 'contentintel/observatory/commands.py', '    text = re.sub(r"\\[([^\\]]*)\\]\\([^)]*\\)", r"\\1", text)', '    text = text', 'link addresses are read as page text'),
    ('u47-read-images-kept', 'contentintel/observatory/commands.py', '    text = re.sub(r"!\\[[^\\]]*\\]\\([^)]*\\)", " ", str(line))', '    text = str(line)', 'picture links are read as page text'),
    ('u47-read-menu-rows-kept', 'contentintel/observatory/commands.py', '        if line.count("](") >= 2:\n            continue\n        seen = _visible(line)', '        seen = _visible(line)', 'a row of menu links is shown as content'),
    ('u47-read-store-prices-not-asked', 'contentintel/observatory/commands.py', '        store = _store_prices(page["url"], settings, contact, now)', '        store = None', "the store's real prices are never looked up"),
]
MUTATIONS.extend(U50)

U52 = [   # update 48: OpenRouter cap, GitHub OK hint and Models token, connection health history, read dedupe, Sheets audit lines, workflow wiring
    ('u48-or-list-uncapped', 'contentintel/ai/clients.py', 'chain = lead[: self.MAX_MODELS - 1] + [self.ROUTER]', 'chain = lead + [self.ROUTER]', 'OpenRouter is sent more than three models'),
    ('u48-or-router-first', 'contentintel/ai/clients.py', 'chain = lead[: self.MAX_MODELS - 1] + [self.ROUTER]', 'chain = [self.ROUTER] + lead[: self.MAX_MODELS - 1]', 'the free router is asked before the free models'),
    ('u48-or-max-five', 'contentintel/ai/clients.py', '    MAX_MODELS = 3 ', '    MAX_MODELS = 5 ', 'the cap is raised above what OpenRouter accepts'),
    ('u48-conn-nonfault-scored', 'contentintel/connections.py', '        if not ok and code in NOT_THE_REPOSITORYS_FAULT:\n            return', '        if False:\n            return', "a missing environment or a refusal lowers a connection's health"),
    ('u48-conn-ratelimit-scored', 'contentintel/connections.py', '"PLATFORM_BLOCKED", "RATE_LIMITED", "NOT_FOUND", "BAD_INPUT"})', '"PLATFORM_BLOCKED", "NOT_FOUND", "BAD_INPUT"})', "a site's rate limit lowers a connection's health"),
    ('u48-conn-migration-off', 'contentintel/connections.py', 'if isinstance(entry, dict) and entry.get("v", 1) < HISTORY_VERSION:', 'if False:', 'the old failures stay in the history'),
    ('u48-conn-migration-drops-successes', 'contentintel/connections.py', 'entry["attempts"] = [a for a in entry.get("attempts") or [] if a.get("ok")]', 'entry["attempts"] = []', 'the cleanup also throws away the successes'),
    ('u48-conn-migration-every-time', 'contentintel/connections.py', 'entry.get("v", 1) < HISTORY_VERSION', 'entry.get("v", 1) < 99', 'an up-to-date history is cleaned again on every load'),
    ('u48-conn-new-entry-unversioned', 'contentintel/connections.py', '"builtin_used": 0, "v": HISTORY_VERSION})', '"builtin_used": 0})', 'a new history is not marked up to date'),
    ('u48-health-no-inline-reason', 'contentintel/health.py', '        if r["status"] == "cannot run here":\n            extra.append(', '        if False:\n            extra.append(', 'cannot run here is shown without its reason'),
    ('u48-read-not-deduped', 'contentintel/observatory/commands.py', '    if page["url"] in shown:                                                   # several brands read the same address in one run: it is described once, then listed at the end', '    if False:                                                   # several brands read the same address in one run: it is described once, then listed at the end', 'the page is described again for every brand'),
    ('u48-read-no-recorded-line', 'contentintel/observatory/commands.py', '            if brand is brands[-1]:', '            if False:', 'the brands the page was recorded for are not listed'),
    ('u48-sheets-decisions-silent', 'contentintel/sheets.py', 'report.notes.append(f"Competitor decisions read from the sheet: {n}', '_ = (f"Competitor decisions read from the sheet: {n}', 'the Competitors tab read leaves no line in the log'),
    ('u48-sheets-controls-silent', 'contentintel/sheets.py', 'report.notes.append(f"Controls read from the sheet:', '_ = (f"Controls read from the sheet:', 'the Controls tab read leaves no line in the log'),
    ('u48-wf-contact-annotation', '.github/workflows/observatory.yml', 'echo "::er""ror::The secret OBSERVATORY_CONTACT is missing.', 'echo "::error::The secret OBSERVATORY_CONTACT is missing.', 'the step header prints a line that looks like an error'),
    ('u48-wf-scrapling-connections', '.github/workflows/observatory.yml', " || (inputs.action == 'connections' && (inputs.what == 'check' || inputs.what == 'install'))", '', 'a connections check does not install Scrapling'),
]
MUTATIONS.extend(U52)

U53 = [   # update 48a: the Models token is renamed (GitHub reserves the GITHUB_ prefix)
    ('u48a-wf-reserved-secret-name', '.github/workflows/observatory.yml', 'REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}', 'REDDIT_CLIENT_ID: ${{ secrets.GITHUB_REDDIT_ID }}', 'a secret name GitHub does not allow is used'),
]
MUTATIONS.extend(U53)

U54 = [   # update 49: GitHub Models retired, PROVIDER_RETIRED, the status list
    ('u49-code-not-final', 'contentintel/contract.py', 'RATE_LIMITED, NEED_LOGIN, PROVIDER_RETIRED})', 'RATE_LIMITED, NEED_LOGIN})', 'a retired provider is treated as an ordinary failure to try another route for'),
    ('u49-code-no-hint', 'contentintel/contract.py', '    PROVIDER_RETIRED: "The provider has been withdrawn', '    NOT_FOUND: "The provider has been withdrawn', 'the retired code has no explanation'),
    ('u49-reg-retired-enabled-allowed', 'contentintel/ai/registry.py', '            if d.get("enabled") or d.get("direct") or (d.get("credentials") or {}).get("env"):', '            if False:', 'a retired provider can be switched back on'),
    ('u49-reg-retired-record-unchecked', 'contentintel/ai/registry.py', '            if not all(str(retired.get(k, "")).strip() for k in ("date", "source", "reason")):', '            if False:', 'a retirement without a date, source and reason is accepted'),
    ('u49-reg-retired-eligible', 'contentintel/ai/registry.py', '        if p.retired:\n            return False, f"RETIRED:', '        if False:\n            return False, f"RETIRED:', 'a retired provider is described as merely disabled'),
    ('u49-reg-github-endpoint-allowed', 'contentintel/ai/registry.py', 'DIRECT_ENDPOINTS = ("https://openrouter.ai/api/v1",)', 'DIRECT_ENDPOINTS = ("https://openrouter.ai/api/v1", "https://models.github.ai/inference")', 'the retired address is an allowed endpoint again'),
    ('u49-lanes-retired-route-allowed', 'contentintel/ai/lanes.py', '                if registry.providers[r["provider"]].retired:\n                    raise LaneError', '                if False:\n                    raise LaneError', 'a lane may route to a retired provider'),
    ('u49-client-retired-host-allowed', 'contentintel/ai/clients.py', '        for retired_host, why in RETIRED_HOSTS.items():', '        for retired_host, why in {}.items():', 'the client calls the retired address'),
    ('u49-client-retired-subdomain', 'contentintel/ai/clients.py', 'if host == retired_host or host.endswith("." + retired_host):', 'if host == retired_host:', 'a sub-domain of the retired address is called'),
    ('u49-client-retired-message', 'contentintel/ai/clients.py', 'so no token, permission or request change can bring it back.', 'so it may come back later.', 'the refusal suggests the retired service can be restored'),
    ('u49-client-retired-code', 'contentintel/ai/clients.py', 'raise Failure(contract.PROVIDER_RETIRED, why)', 'raise Failure(contract.UPSTREAM_BROKEN, why)', 'the refusal is reported as an ordinary upstream failure'),
    ('u49-cmd-mock-is-live', 'contentintel/ai/commands.py', 'word = "LIVE" if mode == "live" else "MOCK"', 'word = "LIVE"', 'a scripted stand-in is listed as a live provider'),
    ('u49-cmd-retired-ignored', 'contentintel/ai/commands.py', '    if provider.retired:\n        return "RETIRED"', '    if False:\n        return "RETIRED"', 'a retired provider is listed as disabled'),
    ('u49-cmd-failed-is-live', 'contentintel/ai/commands.py', '    if slot and slot["ok"]:', '    if slot:', 'a provider that only failed is listed as live'),
    ('u49-cmd-switched-off-hidden', 'contentintel/ai/commands.py', 'switched_off = {n.split(":")[1].strip() for n in notes if n.startswith("SKIPPED:") and "switched off by the owner" in n}', 'switched_off = set()', 'a provider the owner switched off is not listed'),
    ('u49-cmd-only-ignored', 'contentintel/ai/commands.py', '    if only and provider.id not in only:\n        return "SKIPPED"', '    if False:\n        return "SKIPPED"', 'a provider outside the run is listed as tried'),
    ('u49-cmd-count-wrong', 'contentintel/ai/commands.py', 'lines.append("    " + ", ".join(f"{counts.get(k, 0)} {k}"', 'lines.append("    " + ", ".join(f"{counts.get(k, 0) + 1} {k}"', 'the status counts are wrong'),
    ('u49-sheetrows-retired-status', 'contentintel/ai/sheetrows.py', '"status": "retired" if p.retired else ("resting" if resting else ("ready" if p.enabled else "off")),', '"status": ("resting" if resting else ("ready" if p.enabled else "off")),', 'the AI Gateway Health tab shows the retired provider as off'),
    ('u49-yaml-github-enabled', 'ai_providers.yaml', '    label: GitHub Models\n    enabled: false', '    label: GitHub Models\n    enabled: true', 'GitHub Models is switched back on in the provider file'),
    ('u49-yaml-github-route', 'ai_lanes.yaml', '      - {provider: groq, model: openai/gpt-oss-20b}\n', '      - {provider: groq, model: openai/gpt-oss-20b}\n      - {provider: github, model: openai/gpt-4.1-mini}\n', 'a lane routes to GitHub Models again'),
    ('u49-wf-observatory-models-permission', '.github/workflows/observatory.yml', 'permissions:\n  contents: write\n', 'permissions:\n  contents: write\n  models: read\n', 'the Observatory job asks for the Models permission again'),
    ('u49-wf-ai-models-permission', '.github/workflows/ai-gateway.yml', 'permissions:\n  contents: write\n', 'permissions:\n  contents: write\n  models: read\n', 'the AI job asks for the Models permission again'),
    ('u49-readme-secret-back', 'README.md', '| `REDDIT_CLIENT_ID` |', '| `GH_MODELS_TOKEN` | a token for GitHub Models | optional | nowhere |\n| `REDDIT_CLIENT_ID` |', 'the retired Models secret is documented again'),
]
MUTATIONS.extend(U54)

U55 = [   # update 49: a line for every brand even without the tabs
    ('u49-sheets-no-competitors-tab-silent', 'contentintel/sheets.py', '            report.notes.append("Competitor decisions read from the sheet: 0 (no Competitors tab', '            _ = ("Competitor decisions read from the sheet: 0 (no Competitors tab', 'a brand with no Competitors tab prints no line for the read'),
    ('u49-sheets-no-controls-tab-silent', 'contentintel/sheets.py', '            report.notes.append("Controls read from the sheet: 0 change(s) (no Controls tab', '            _ = ("Controls read from the sheet: 0 change(s) (no Controls tab', 'a brand with no Controls tab prints no line for the read'),
]
MUTATIONS.extend(U55)

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_mutations import U51 as _U51                                  # the stored-session code's own checks (Update 48), wired in now that the code is
MUTATIONS.extend(_U51)

U56 = [   # update 50: stored sessions in use
    ('u50-live-proof-days', 'contentintel/session_live.py', 'PROVED_DAYS = 14 ', 'PROVED_DAYS = 4000 ', 'a check stays live proof for ever'),
    ('u50-live-proof-from-the-future', 'contentintel/session_live.py', 'timedelta(0) <= here - then <= timedelta(days=PROVED_DAYS)', 'here - then <= timedelta(days=PROVED_DAYS)', 'a check that has not happened yet counts as proof'),
    ('u50-live-rejected-keeps-proof', 'contentintel/session_live.py', 'CLEARS_PROOF = (REJECTED, EXPIRED, BAD_CAPTURE, NOT_SET_UP)', 'CLEARS_PROOF = (EXPIRED, BAD_CAPTURE, NOT_SET_UP)', 'a refusal does not void the earlier proof'),
    ('u50-live-cannot-check-voids-proof', 'contentintel/session_live.py', 'CLEARS_PROOF = (REJECTED, EXPIRED, BAD_CAPTURE, NOT_SET_UP)', 'CLEARS_PROOF = (REJECTED, EXPIRED, BAD_CAPTURE, NOT_SET_UP, CANNOT_CHECK)', 'a check that could not be made voids the proof'),
    ('u50-live-bad-capture-not-red', 'contentintel/session_live.py', 'OWNER_ACTION = (REJECTED, EXPIRED, BAD_CAPTURE)', 'OWNER_ACTION = (REJECTED, EXPIRED)', 'an unusable secret does not turn the run red'),
    ('u50-live-not-set-up-red', 'contentintel/session_live.py', 'OWNER_ACTION = (REJECTED, EXPIRED, BAD_CAPTURE)', 'OWNER_ACTION = (REJECTED, EXPIRED, BAD_CAPTURE, NOT_SET_UP)', 'a missing secret turns the run red'),
    ('u50-live-off-still-checked', 'contentintel/session_live.py', '    if not on:\n        return done(OFF, why, record=False)', '    if False:\n        return done(OFF, why, record=False)', 'a platform the owner switched off is still loaded and checked'),
    ('u50-live-off-recorded', 'contentintel/session_live.py', 'return done(OFF, why, record=False)', 'return done(OFF, why, record=True)', 'a platform that is switched off is recorded as a result'),
    ('u50-live-master-switch-ignored', 'contentintel/session_live.py', '    if not sec.get("enabled", True):', '    if False:', 'the master switch does nothing'),
    ('u50-live-expired-as-bad-capture', 'contentintel/session_live.py', 'return done(EXPIRED if exc.code == K.NEED_LOGIN else BAD_CAPTURE, text)', 'return done(BAD_CAPTURE, text)', 'an expired session is reported as a bad capture'),
    ('u50-live-not-set-up-undetected', 'contentintel/session_live.py', '        if "is not set" in text:', '        if False:', 'a missing secret is reported as a bad capture'),
    ('u50-live-offline-requests', 'contentintel/session_live.py', '    if offline:\n        proof = f', '    if False:\n        proof = f', 'the offline status makes a live request'),
    ('u50-live-plain-http-is-refusal', 'contentintel/session_live.py', '    if plain:\n        return done(UNCONFIRMED,', '    if False:\n        return done(UNCONFIRMED,', 'a page read over plain HTTP is called a refusal'),
    ('u50-live-403-not-refused', 'contentintel/session_live.py', '        if status in (401, 403):\n            return done(REJECTED,', '        if False:\n            return done(REJECTED,', 'a 403 is not reported as a refusal'),
    ('u50-live-no-raw-page', 'contentintel/session_live.py', 'job["return_html"] = True', 'job["return_html"] = False', 'the raw page is not asked for'),
    ('u50-live-wrong-secret-name', 'contentintel/session_live.py', 'adv["cookies_env"] = name', 'adv["cookies_env"] = "SESSION_COOKIES"', 'the worker is told a secret name that holds nothing'),
    ('u50-live-primary-uses-session', 'contentintel/session_live.py', 'with_session=False, reading=True)\n        if not res.get("ok"):', 'with_session=True, reading=True)\n        if not res.get("ok"):', 'the plain reader already sends the session'),
    ('u50-live-session-always-on', 'contentintel/session_live.py', 'enabled=on and proved, off_reason=off', 'enabled=True, off_reason=off', 'the session is used whatever the owner switch and the proof say'),
    ('u50-live-session-unproved', 'contentintel/session_live.py', 'enabled=on and proved, off_reason=off', 'enabled=on, off_reason=off', 'the session is used without a live proof'),
    ('u50-live-session-ignores-switch', 'contentintel/session_live.py', 'enabled=on and proved, off_reason=off', 'enabled=proved, off_reason=off', 'the session is used although the owner switched it off'),
    ('u50-live-other-hosts', 'contentintel/session_live.py', '    if not _host_ok(platform, url):\n        raise K.Failure(K.BAD_INPUT, f"{url} is not on {\', \'.join(S.PLATFORMS[platform][\'hosts\'])}: a stored session is never sent to another site")\n    on, why = switched_on(settings, platform)\n\n    def primary', '    if False:\n        raise K.Failure(K.BAD_INPUT, f"{url} is not on {\', \'.join(S.PLATFORMS[platform][\'hosts\'])}: a stored session is never sent to another site")\n    on, why = switched_on(settings, platform)\n\n    def primary', 'a session can be sent to another site'),
    ('u50-live-host-suffix', 'contentintel/session_live.py', 'return any(host == h or host.endswith("." + h) for h in S.PLATFORMS[platform]["hosts"])', 'return any(host.endswith(h) for h in S.PLATFORMS[platform]["hosts"])', 'a site whose name merely ends the same way receives the session'),
    ('u50-live-wall-not-login', 'contentintel/session_live.py', '    if WALL.search(message):\n        return K.Failure(K.NEED_LOGIN, message)', '    if False:\n        return K.Failure(K.NEED_LOGIN, message)', 'a login wall is reported as an ordinary failure'),
    ('u50-live-unknown-platform', 'contentintel/session_live.py', '    if platform not in S.PLATFORMS:\n        raise K.Failure(K.BAD_INPUT, f"no stored-session platform called {platform!r}; the platforms are {\', \'.join(S.PLATFORMS)}")\n    if not _host_ok(platform, url):\n        raise K.Failure(K.BAD_INPUT, f"{url} is not on {\', \'.join(S.PLATFORMS[platform][\'hosts\'])}: a stored session is never sent to another site")\n    on, why = switched_on(settings, platform)\n\n    def primary', '    if False:\n        raise K.Failure(K.BAD_INPUT, f"no stored-session platform called {platform!r}; the platforms are {\', \'.join(S.PLATFORMS)}")\n    if not _host_ok(platform, url):\n        raise K.Failure(K.BAD_INPUT, f"{url} is not on {\', \'.join(S.PLATFORMS[platform][\'hosts\'])}: a stored session is never sent to another site")\n    on, why = switched_on(settings, platform)\n\n    def primary', 'an unknown platform is not refused with a reason'),
    ('u50-live-count-order', 'contentintel/session_live.py', 'order = [s for s in ("PRESENT", *ORDER) if counts.get(s)]', 'order = [s for s in (*ORDER, "PRESENT") if counts.get(s)]', 'the count line is in the wrong order'),
    ('u50-live-ledger-keeps-junk', 'contentintel/session_live.py', 'if k in S.PLATFORMS and isinstance(v, dict)', 'if isinstance(v, dict)', 'a damaged ledger keeps entries for platforms that do not exist'),
    ('u50-live-report-no-caveat', 'contentintel/session_live.py', '    if not offline:\n        lines.append("  ACCEPTED means', '    if False:\n        lines.append("  ACCEPTED means', 'the report does not say what ACCEPTED does not mean'),
    ('u50-sess-jar-is-a-list', 'contentintel/sessions.py', 'return {c["name"]: c["value"] for c in self._cookies()}', 'return [dict(c) for c in self._cookies()]', 'the worker is handed a list it cannot read'),
    ('u50-worker-html-always', 'contentintel/observatory/scrapling_worker.py', '        if self.job.get("return_html"):                                                    # only a stored-session check asks for the raw page', '        if True:                                                    # only a stored-session check asks for the raw page', 'every page read hands back its raw page'),
    ('u50-worker-html-unbounded', 'contentintel/observatory/scrapling_worker.py', '            _keep_raw(out, html)', '            out["html"] = html', 'the raw page is not bounded'),
    ('u50-worker-status-late', 'contentintel/observatory/scrapling_worker.py', '        out["status"] = int(resp.status)\n        if self.job.get("return_html"):', '        if self.job.get("return_html"):', 'a refused page loses its status'),
    ('u50-cfg-sessions-unregistered', 'contentintel/observatory/config.py', 'register_service("stored_sessions", label=', 'register_service("stored_sessions_off", label=', 'the owner has no switches for the stored sessions'),
    ('u50-cfg-sessions-default-off', 'contentintel/services.py', '"bool", True, f"the stored {c} session may be loaded', '"bool", False, f"the stored {c} session may be loaded', 'every stored session starts switched off'),
    ('u50-cmd-check-never-red', 'contentintel/observatory/commands.py', 'return SL.exit_code(rows) if what == "check" else 0', 'return 0', 'an expired or refused session never turns the run red'),
    ('u50-cmd-key-unchecked', 'contentintel/observatory/commands.py', '    if args.key and args.key not in S.PLATFORMS:', '    if False:', 'an unknown platform is not refused'),
    ('u50-cmd-read-unchecked', 'contentintel/observatory/commands.py', '    if what == "read":\n        if len(platforms) != 1 or not args.url:', '    if what == "read":\n        if False:', 'a read without a platform or an address is attempted'),
    ('u50-wf-sessions-secret-dropped', '.github/workflows/sessions.yml', '          X_SESSION_B64: ${{ secrets.X_SESSION_B64 }}\n          TIKTOK_SESSION_B64', '          X_SESSION_B64_UNUSED: ${{ secrets.X_SESSION_B64 }}\n          TIKTOK_SESSION_B64', 'the sessions workflow is not given the X session'),
    ('u50-wf-sessions-input-inline', '.github/workflows/sessions.yml', 'args+=(--url "$READ_URL")', 'args+=(--url "${{ inputs.url }}")', 'the typed address is pasted into the script'),
    ('u50-wf-sessions-no-browser', '.github/workflows/sessions.yml', "if: ${{ inputs.what != 'list' }}", "if: ${{ inputs.what == 'read' }}", 'a live check runs without the browser'),
    ('u50-wf-sessions-never-red', '.github/workflows/sessions.yml', "        if: steps.run.outcome == 'failure'\n        run: exit 1", '        if: false\n        run: exit 1', 'a session that needs capturing never turns the run red'),
    ('u50-wf-acc-sessions-never-red', '.github/workflows/acceptance.yml', " || steps.sessions.outcome == 'failure'", '', 'Acceptance ignores a session that needs capturing'),
    ('u50-wf-acc-secret-dropped', '.github/workflows/acceptance.yml', '          BOSS_SESSION_B64: ${{ secrets.BOSS_SESSION_B64 }}', '          BOSS_SESSION_B64_UNUSED: ${{ secrets.BOSS_SESSION_B64 }}', 'Acceptance is not given the Boss session'),
    ('u50-wf-observatory-gets-a-session', '.github/workflows/observatory.yml', '          REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}\n', '          INSTAGRAM_SESSION_B64: ${{ secrets.INSTAGRAM_SESSION_B64 }}\n          REDDIT_CLIENT_ID: ${{ secrets.REDDIT_CLIENT_ID }}\n', 'the Observatory workflow is handed an Instagram login'),
    ('u50-readme-session-undocumented', 'README.md', '| `BOSS_SESSION_B64` |', '| `BOSS_SESSION_B64_X` |', 'a stored-session secret is not documented'),
]
MUTATIONS.extend(U56)

U57 = [   # update 50: a stale Controls row is removed
    ('u50-ctl-tidy-off', 'contentintel/sheets.py', '        if tab.get("drop_unknown") and ws is not None and not dry_run:', '        if False:', 'a stale row for a removed control stays in the Controls tab'),
    ('u50-ctl-tidy-on-dry-run', 'contentintel/sheets.py', '        if tab.get("drop_unknown") and ws is not None and not dry_run:', '        if tab.get("drop_unknown") and ws is not None:', 'a dry run deletes rows'),
    ('u50-ctl-tidy-every-tab', 'contentintel/sheets.py', '        if tab.get("drop_unknown") and ws is not None and not dry_run:', '        if True and ws is not None and not dry_run:', 'every tab loses rows the tool does not know, including ones a person wrote'),
    ('u50-ctl-tidy-keyless-rows', 'contentintel/sheets.py', '        if key and key not in known_keys:', '        if key not in known_keys:', 'a row with no key is deleted'),
    ('u50-ctl-tidy-empty-known', 'contentintel/sheets.py', '    if not known_keys:\n        return []', '    if False:\n        return []', 'a tab is emptied when the tool knows no keys'),
    ('u50-ctl-tidy-missing-column', 'contentintel/sheets.py', '    if key_col not in header:\n        return []\n    key_at = header.index(key_col)\n    doomed, dropped = [], []', '    if False:\n        return []\n    key_at = header.index(key_col)\n    doomed, dropped = [], []', 'a tab without the key column breaks the sheet update'),
    ('u50-ctl-tidy-silent', 'contentintel/sheets.py', 'report.notes.append(f"{tab[\'title\']}: removed {len(dropped)} row(s)', '_ = (f"{tab[\'title\']}: removed {len(dropped)} row(s)', 'the rows that were removed are not mentioned'),
    ('u50-ctl-tidy-failure-stops', 'contentintel/sheets.py', '            except Exception as exc:                                       # a tidy that fails never stops the sheet', '            except KeyError as exc:                                       # a tidy that fails never stops the sheet', 'a failed tidy stops the whole sheet update'),
    ('u50-ctl-flag-dropped', 'contentintel/services.py', '"starter_manual": [], "drop_unknown": True}', '"starter_manual": []}', 'the Controls tab is not tidied'),
]
MUTATIONS.extend(U57)

U58 = [   # update 51: the browser, BLOCKED, one attempt
    ('u51-live-retries', 'contentintel/session_live.py', 'job["retry"] = {"attempts": 1, "backoff_seconds": 0}', 'job["retry"] = {"attempts": 3, "backoff_seconds": 0}', 'a challenge page is asked for again with the same session'),
    ('u51-live-blocked-is-cannot-check', 'contentintel/session_live.py', '    if err.get("code") == "PLATFORM_BLOCKED" or refused or looks_like_challenge(status, html, row["evidence"]["title"]):\n        blocked = (', '    if False:\n        blocked = (', 'a bot-check page is reported as a check that could not be made'),
    ('u51-live-blocked-voids-proof', 'contentintel/session_live.py', 'CLEARS_PROOF = (REJECTED, EXPIRED, BAD_CAPTURE, NOT_SET_UP)', 'CLEARS_PROOF = (REJECTED, EXPIRED, BAD_CAPTURE, NOT_SET_UP, BLOCKED)', 'a block voids the earlier proof of the session'),
    ('u51-live-blocked-red', 'contentintel/session_live.py', 'OWNER_ACTION = (REJECTED, EXPIRED, BAD_CAPTURE)', 'OWNER_ACTION = (REJECTED, EXPIRED, BAD_CAPTURE, BLOCKED)', 'a platform blocking the runner turns the run red'),
    ('u51-live-reason-dropped', 'contentintel/session_live.py', 'why_plain = f"the browser could not be used here ({res.get(\'browser_error\') or \'no reason was given\'})"', 'why_plain = "the browser could not be used here (no reason was given)"', "the browser library's own reason is not shown"),
    ('u51-live-box-advice', 'contentintel/session_live.py', 'and this site may only show its logged-in state in a browser")', 'and this site may only show its logged-in state in a browser: run the check with the browser box ticked")', 'the check points at an input that does not exist'),
    ('u51-live-days-zero', 'contentintel/session_live.py', '(" (less than a day left)" if r["days_left"] == 0 else f" ({r[\'days_left\']} day(s) left)")', 'f" ({r[\'days_left\']} day(s) left)"', 'an expiry within a day is shown as 0 days'),
    ('u51-worker-reason-hidden', 'contentintel/observatory/scrapling_worker.py', 'out["browser_error"] = _short_error(exc, [str(c["value"]) for c in kw.get("cookies", [])])', 'out["browser_error"] = ""', "the browser library's reason is not passed on"),
    ('u51-worker-reason-unredacted', 'contentintel/observatory/scrapling_worker.py', '[str(c["value"]) for c in kw.get("cookies", [])])', '[])', 'a cookie value can travel in the browser error'),
    ('u51-worker-reason-unbounded', 'contentintel/observatory/scrapling_worker.py', '    return text[:300]', '    return text', 'the browser error is not bounded'),
    ('u51-worker-reason-short-values', 'contentintel/observatory/scrapling_worker.py', '        if len(value) >= 6:', '        if len(value) >= 600:', 'a short cookie value is not hidden'),
    ('u51-wf-sessions-playwright', '.github/workflows/sessions.yml', '-m patchright install --with-deps chromium', '-m playwright install --with-deps chromium', 'the sessions workflow installs the browser the stealth fetcher does not use'),
    ('u51-wf-acceptance-playwright', '.github/workflows/acceptance.yml', 'scrapling-env/bin/python" -m patchright install --with-deps chromium', 'scrapling-env/bin/python" -m playwright install --with-deps chromium', 'Acceptance installs the browser the stealth fetcher does not use'),
    ('u51-wf-observatory-playwright', '.github/workflows/observatory.yml', 'scrapling-env/bin/python" -m patchright install --with-deps chromium', 'scrapling-env/bin/python" -m playwright install --with-deps chromium', 'the Observatory browsers option installs the browser the stealth fetcher does not use'),
    ('u51-requirements-unpinned', 'requirements-scrapling.txt', 'scrapling[fetchers]==0.5', 'scrapling[fetchers]>=0.5', 'a new Scrapling version is adopted without checking'),
]
MUTATIONS.extend(U58)

U59 = [   # update 52: page evidence, visible-text rule, twscrape last resort for X, discovery
    ('u52-sess-text-ignored', 'contentintel/sessions.py', 'visible = " ".join(str(text).split()).lower() if text is not None else low', 'visible = low', "the logged-out wording is looked for in the page's code, so a logged-in page's translations reject it"),
    ('u52-sess-evidence-query', 'contentintel/sessions.py', 'return text.split("?")[0].split("#")[0][:200]', 'return text[:200]', 'the printed address keeps what follows a question mark'),
    ('u52-sess-evidence-userinfo', 'contentintel/sessions.py', 'text = re.sub(r"^(https?://)[^/@]*@", r"\\1", text)', 'text = text', 'a name and password in an address are printed'),
    ('u52-sess-evidence-password', 'contentintel/sessions.py', '"password_field": \'type="password"\' in low or "type=\'password\'" in low,', '"password_field": False,', 'a password field on the page is never reported'),
    ('u52-sess-evidence-code-only', 'contentintel/sessions.py', '"logged_out_only_in_code": [m for m in spec["logged_out"] if m in low and m not in vis],', '"logged_out_only_in_code": [],', "wording found only in the page's code is not reported"),
    ('u52-sess-excerpt-unbounded', 'contentintel/sessions.py', '"text_starts": " ".join(str(text or "").split())[:240]}', '"text_starts": " ".join(str(text or "").split())}', 'the visible text excerpt is not bounded'),
    ('u52-live-evidence-dropped', 'contentintel/session_live.py', '    row["evidence"] = _evidence(platform, res, session, plain)', '    row["evidence"] = None', 'a platform that was checked shows nothing about its page'),
    ('u52-live-accepted-full-evidence', 'contentintel/session_live.py', '    if r["state"] == ACCEPTED:\n        return [f"      page:', '    if False:\n        return [f"      page:', 'an ACCEPTED platform gets the full evidence too'),
    ('u52-live-cookie-values', 'contentintel/session_live.py', 'cookies_sent=sorted(session.cookie_jar())', 'cookies_sent=sorted(f"{k}={v}" for k, v in session.cookie_jar().items())', 'the cookie values are printed, not only their names'),
    ('u52-live-twscrape-any-platform', 'contentintel/session_live.py', '        if platform == "x":                                                              # the approved last resort', '        if True:                                                              # the approved last resort', 'every blocked platform is sent to twscrape'),
    ('u52-live-twscrape-accepted-as-browser', 'contentintel/session_live.py', '({via}; so the browser read of x.com is not proved)", via="twscrape")', '({via}; so the browser read of x.com is not proved)", via="browser")', 'a twscrape answer is recorded as proof of the browser read'),
    ('u52-live-twscrape-rejected-ignored', 'contentintel/session_live.py', '            if layered["state"] == "rejected":\n                return done(REJECTED, f"{layered[\'detail\']} ({via})")', '            if False:\n                return done(REJECTED, f"{layered[\'detail\']} ({via})")', "X's rejection is ignored and the platform stays blocked"),
    ('u52-live-twscrape-env-everything', 'contentintel/session_live.py', '"PATH": environ.get("PATH", "/usr/bin:/bin"), "HOME": environ.get("HOME", "/tmp")}', '"PATH": environ.get("PATH", "/usr/bin:/bin"), "HOME": environ.get("HOME", "/tmp"), **dict(environ)}', "twscrape's program is handed every secret of the run"),
    ('u52-live-twscrape-telemetry-on', 'contentintel/session_live.py', '"TWS_TELEMETRY": "0", "DO_NOT_TRACK": "1",', '"TWS_TELEMETRY": "1", "DO_NOT_TRACK": "0",', "twscrape's usage reporting is left on"),
    ('u52-live-twscrape-timeout', 'contentintel/session_live.py', 'def twscrape_layer(session, environ, runner=subprocess.run, timeout=150):', 'def twscrape_layer(session, environ, runner=subprocess.run, timeout=15000):', 'the last-resort program may run for hours'),
    ('u52-live-proof-via-ignored', 'contentintel/session_live.py', '<= timedelta(days=PROVED_DAYS) and self.proved_via(platform) == via)', '<= timedelta(days=PROVED_DAYS))', 'a twscrape proof enables the browser read'),
    ('u52-live-proof-via-lost', 'contentintel/session_live.py', 'entry["proved_on"], entry["proved_via"] = now, via', 'entry["proved_on"], entry["proved_via"] = now, "browser"', 'the way a session was proved is forgotten'),
    ('u52-live-report-via', 'contentintel/session_live.py', '(f" (through {r[\'proved_via\']}, not the browser)" if r.get("proved_via") == "twscrape" else "")', '""', 'the report does not say a proof came through twscrape'),
    ('u52-live-discovery-off', 'contentintel/session_live.py', 'SC.Step("discovery", discovery)', 'SC.Step("discovery", discovery, enabled=False, off_reason="x")', 'the discovery fallback never runs'),
    ('u52-live-discovery-empty-results', 'contentintel/session_live.py', 'for r in results if r.snippet or r.title]', 'for r in results]', 'empty search results are returned as rows'),
    ('u52-live-discovery-num', 'contentintel/session_live.py', 'provider.search(_query_for(url), kind="web", num=5)', 'provider.search(_query_for(url), kind="web", num=50)', 'discovery asks for fifty results'),
    ('u52-live-discovery-order', 'contentintel/session_live.py', 'SEARCH_ORDER = ("serper_dev", "tavily", "serpapi", "exa", "exa_mcp")', 'SEARCH_ORDER = ("exa", "serper_dev", "tavily", "serpapi", "exa_mcp")', 'the providers that return no snippets are asked first'),
    ('u52-live-discovery-auth', 'contentintel/session_live.py', 'raise K.Failure(K.NEED_SETUP, str(exc)) from None\n    except P.QuotaError', 'raise K.Failure(K.UPSTREAM_BROKEN, str(exc)) from None\n    except P.QuotaError', 'a rejected search key is reported as an upstream failure'),
    ('u52-live-query-www', 'contentintel/session_live.py', 'text = re.sub(r"^https?://(www\\.)?", "", str(url).split("?")[0]', 'text = re.sub(r"^https?://", "", str(url).split("?")[0]', 'the search query keeps www.'),
    ('u52-cmd-discovery-note', 'contentintel/observatory/commands.py', 'The page itself could not be read, so this is discovery', 'This is the page', 'discovery snippets are presented as the page'),
    ('u52-cmd-discovery-line', 'contentintel/observatory/commands.py', 'discovery=SL.discovery_status(os.environ)))', 'discovery=""))', 'the report does not say whether discovery can run'),
    ('u52-worker-refused-page-lost', 'contentintel/observatory/scrapling_worker.py', '            if self.job.get("return_html"):                                                  # a stored-session check judges the page, so it keeps the last one it saw, refused or not', '            if False:                                                  # a stored-session check judges the page, so it keeps the last one it saw, refused or not', 'a refused page leaves no evidence'),
    ('u52-worker-cookie-values', 'contentintel/observatory/scrapling_worker.py', 'out["cookie_names_back"] = _cookie_names(getattr(resp, "cookies", None))', 'out["cookie_names_back"] = [f"{c.get(\'name\')}={c.get(\'value\')}" for c in (getattr(resp, "cookies", None) or ()) if isinstance(c, dict)]', 'the cookies a platform sent back are printed with their values'),
    ('u52-worker-fetched-with', 'contentintel/observatory/scrapling_worker.py', '                    resp = self.f.stealth_get(current, **kw)\n                    out["fetched_with"] = "browser"', '                    resp = self.f.stealth_get(current, **kw)', 'the check does not say it used the browser'),
    ('u52-xt-telemetry-value', 'contentintel/x_twscrape_check.py', 'os.environ["TWS_TELEMETRY"] = "0"', 'os.environ["TWS_TELEMETRY"] = "1"', 'twscrape reports usage to an outside service'),
    ('u52-xt-user-id', 'contentintel/x_twscrape_check.py', 'found = re.search(r"(?:u|%3D|=)\\D{0,3}(\\d{5,})", raw) or re.search(r"(\\d{5,})", raw)', 'found = None', 'the profile to read is never found'),
    ('u52-xt-required', 'contentintel/x_twscrape_check.py', 'REQUIRED = ("auth_token", "ct0")', 'REQUIRED = ("auth_token",)', 'a session without ct0 is asked about'),
    ('u52-xt-all-cookies', 'contentintel/x_twscrape_check.py', 'cookie_header = "; ".join(f"{n}={jar[n]}" for n in (*REQUIRED, "twid") if jar.get(n))', 'cookie_header = "; ".join(f"{n}={v}" for n, v in jar.items())', 'every cookie of the session is handed to twscrape'),
    ('u52-xt-explain-32', 'contentintel/x_twscrape_check.py', 'if "(32)" in msg or "could not authenticate" in low:', 'if False:', 'cookies X did not accept are not reported as rejected'),
    ('u52-xt-explain-326', 'contentintel/x_twscrape_check.py', 'if "(326)" in msg or "locked" in low or "denied by access control" in low:', 'if False:', 'a locked account is not reported as rejected'),
    ('u52-xt-explain-403', 'contentintel/x_twscrape_check.py', '    if active is False and not msg:', '    if False:', 'an ended session with no message is not reported'),
    ('u52-xt-accepted-wrong', 'contentintel/x_twscrape_check.py', '        if user is not None:', '        if user is None:', 'a missing profile is called accepted'),
    ('u52-xt-timeout-ignored', 'contentintel/x_twscrape_check.py', 'user = await asyncio.wait_for(api.user_by_id(int(uid)), timeout)', 'user = await api.user_by_id(int(uid))', 'the profile read can wait for ever'),
    ('u52-xt-scrub-off', 'contentintel/x_twscrape_check.py', '        if len(str(value)) >= 6:', '        if len(str(value)) >= 600:', 'a cookie value can appear in an error'),
    ('u52-xt-main-bad-json', 'contentintel/x_twscrape_check.py', '        if not isinstance(jar, dict):\n            raise ValueError("not an object")', '        if False:\n            raise ValueError("not an object")', 'cookies that are not an object crash the program'),
    ('u52-req-twscrape-unpinned', 'requirements-twscrape.txt', 'twscrape==0.20.1', 'twscrape>=0.20.1', 'a new twscrape version is adopted without checking'),
    ('u52-wf-sessions-twscrape-always', '.github/workflows/sessions.yml', 'if [ -n "$X_SESSION_B64" ]; then', 'if true; then', 'twscrape is installed even when no X session is stored'),
    ('u52-wf-sessions-exa-dropped', '.github/workflows/sessions.yml', '          EXA_API_KEY: ${{ secrets.EXA_API_KEY }}', '          EXA_API_KEY_UNUSED: ${{ secrets.EXA_API_KEY }}', 'the discovery fallback is not given the Exa key'),
    ('u52-wf-sessions-twscrape-env-lost', '.github/workflows/sessions.yml', 'echo "TWSCRAPE_PYTHON=$RUNNER_TEMP/twscrape-env/bin/python" >> "$GITHUB_ENV"', 'echo "TWSCRAPE_PYTHON_X=$RUNNER_TEMP/twscrape-env/bin/python" >> "$GITHUB_ENV"', 'the check never finds the twscrape environment'),
    ('u52-wf-acc-twscrape-env-lost', '.github/workflows/acceptance.yml', 'export TWSCRAPE_PYTHON=', 'export TWSCRAPE_PYTHON_X=', 'Acceptance never finds the twscrape environment'),
]
MUTATIONS.extend(U59)

U60 = [   # update 53: cookie shapes, unclean reads, bot-check pages, honest evidence labels
    ('u53-worker-cookie-dict-only', 'contentintel/observatory/scrapling_worker.py', '                if isinstance(item, dict):\n                    name = item.get("name")', '                if False:\n                    name = item.get("name")', "a browser's cookie objects give no names"),
    ('u53-worker-cookie-raises', 'contentintel/observatory/scrapling_worker.py', '    except Exception:\n        return []\n    return sorted(set(names))[:40]', '    except KeyError:\n        return []\n    return sorted(set(names))[:40]', 'an unknown cookie shape crashes the read'),
    ('u53-worker-cookie-values', 'contentintel/observatory/scrapling_worker.py', '                    names.append(str(name))', '                    names.append(str(name) + "=" + str(item.get("value") if isinstance(item, dict) else ""))', 'a cookie value travels with its name'),
    ('u53-worker-cookie-repeats', 'contentintel/observatory/scrapling_worker.py', '    return sorted(set(names))[:40]', '    return sorted(names)[:40]', 'a cookie name is listed as many times as it was set'),
    ('u53-worker-cookie-cap', 'contentintel/observatory/scrapling_worker.py', '    return sorted(set(names))[:40]', '    return sorted(set(names))[:4000]', 'the list of cookie names is not bounded'),
    ('u53-worker-text-dropped', 'contentintel/observatory/scrapling_worker.py', 'out["title"], out["text"] = seen["title"], seen["text"][:3000]', 'out["title"] = seen["title"]', 'a refused page keeps no visible text'),
    ('u53-worker-text-unbounded', 'contentintel/observatory/scrapling_worker.py', 'seen["text"][:3000]', 'seen["text"]', 'the visible text kept for a check is not bounded'),
    ('u53-live-unclean-judged', 'contentintel/session_live.py', '    if err and err.get("code") != "PLATFORM_BLOCKED" and not refused and not WALL.search(', '    if False and err and err.get("code") != "PLATFORM_BLOCKED" and not refused and not WALL.search(', "a read the worker did not complete is judged as if it were the platform's answer"),
    ('u53-live-wall-unclean', 'contentintel/session_live.py', 'and not WALL.search(str(err.get("message", ""))) and int(status or 0) not in (401, 403)', 'and int(status or 0) not in (401, 403)', 'a login wall is treated as a read that could not be judged'),
    ('u53-live-403-unclean', 'contentintel/session_live.py', 'and int(status or 0) not in (401, 403) and not looks_like_challenge', 'and not looks_like_challenge', 'a plain 403 is treated as a read that could not be judged'),
    ('u53-live-challenge-unrecognised', 'contentintel/session_live.py', '    if err.get("code") == "PLATFORM_BLOCKED" or refused or looks_like_challenge(status, html, row["evidence"]["title"]):', '    if err.get("code") == "PLATFORM_BLOCKED" or refused:', 'a bot-check page the worker mislabelled is not a block'),
    ('u53-live-challenge-any-status', 'contentintel/session_live.py', '    if int(status or 0) not in (401, 403, 429, 503):\n        return False', '    if False:\n        return False', "a normal page with the words 'just a moment' is called a block"),
    ('u53-live-challenge-words', 'contentintel/session_live.py', 'CHALLENGE_WORDS = ("just a moment", "attention required", "cf-chl",', 'CHALLENGE_WORDS = ("just a moment", "cf-chl",', "a Cloudflare 'Attention Required' page is not recognised"),
    ('u53-live-no-text-note', 'contentintel/session_live.py', 'out_words = "no visible text was available, so the logged-out wording was not judged"', 'out_words = "logged-out wording in the visible text: none"', 'a page with no visible text is reported as having no logged-out wording'),
    ('u53-sess-text-label', 'contentintel/sessions.py', '"visible_text_available": bool(vis), "logged_out_in_text": [m for m in spec["logged_out"] if m in vis]', '"visible_text_available": bool(vis), "logged_out_in_text": [m for m in spec["logged_out"] if m in (vis if vis else low)]', "wording in the page's code is called visible text"),
    ('u53-sess-tiktok-upload', 'contentintel/sessions.py', '"logged_in": (\'"islogin":true\', "data-e2e=\\"profile-icon\\""), "logged_out": ("log in to tiktok",),', '"logged_in": (\'"islogin":true\', "data-e2e=\\"profile-icon\\"", "upload"), "logged_out": ("log in to tiktok",),', 'an Upload button alone counts as logged in on TikTok'),
    ('u53-sess-code-only-needs-text', 'contentintel/sessions.py', '"logged_out_only_in_code": [m for m in spec["logged_out"] if m in low and m not in vis],', '"logged_out_only_in_code": [m for m in spec["logged_out"] if vis and m in low and m not in vis],', "wording in the page's code is not reported when there is no visible text"),
]
MUTATIONS.extend(U60)

U61 = [   # update 54: Reddit's words, the either-or expiry, compare, twscrape's own log
    ('u54-sess-reddit-text-ignored', 'contentintel/sessions.py', 'if not (any(marker in low for marker in spec["logged_in"]) or any(marker in visible for marker in spec.get("logged_in_text", ()))):', 'if not any(marker in low for marker in spec["logged_in"]):', "Reddit's logged-in menu in the visible text is not evidence"),
    ('u54-sess-reddit-markers-dropped', 'contentintel/sessions.py', '"logged_in_text": ("expand user menu", "open profile menu"),', '"logged_in_text": (),', 'Reddit has no logged-in words to look for'),
    ('u54-sess-expiry-earliest-either', 'contentintel/sessions.py', '            dates.append(max(stamped))', '            dates.append(min(stamped))', 'a short renewed token sets the date of a session that a long cookie keeps alive'),
    ('u54-sess-expiry-undated-either', 'contentintel/sessions.py', '        if either and len(stamped) == len(either):', '        if either:', 'an either-or cookie with no expiry crashes or sets a date'),
    ('u54-sess-expiry-required-ignored', 'contentintel/sessions.py', '        dates = self._expiry_of(set(self.spec["required"]))\n        either', '        dates = []\n        either', 'the cookies that are all needed do not set the date'),
    ('u54-sess-instagram-wait-dropped', 'contentintel/sessions.py', '"adv": {"network_idle": True, "wait": 8000}, "read_adv": {"network_idle": True, "wait": 8000},', '"read_adv": {"network_idle": True, "wait": 8000},', "Instagram's page is not waited for until it has loaded"),
    ('u54-sess-shape-noscript', 'contentintel/sessions.py', '"noscript": " ".join(re.sub(r"<[^>]+>", " ", noscript.group(1)).split())[:200] if noscript else "",', '"noscript": "",', 'the no-script note of a script-only page is not reported'),
    ('u54-sess-text-words-missing', 'contentintel/sessions.py', '"logged_in_text_missing": [m for m in spec.get("logged_in_text", ()) if m not in vis]', '"logged_in_text_missing": []', 'the logged-in words that were not found are not listed'),
    ('u54-live-adv-spec-ignored', 'contentintel/session_live.py', 'adv = {"stealth": True, **(S.PLATFORMS[platform].get("adv") or {}), **((S.PLATFORMS[platform].get("read_adv") or {}) if reading else {})}', 'adv = {"stealth": True}', "a platform's browser wait is never passed to the worker"),
    ('u54-live-compare-any-host', 'contentintel/session_live.py', '    if not _host_ok(platform, url):\n        raise K.Failure(K.BAD_INPUT, f"{url} is not on {\', \'.join(S.PLATFORMS[platform][\'hosts\'])}: a stored session is never sent to another site")\n    public =', '    if False:\n        raise K.Failure(K.BAD_INPUT, f"{url} is not on {\', \'.join(S.PLATFORMS[platform][\'hosts\'])}: a stored session is never sent to another site")\n    public =', 'a compare sends a session to another site'),
    ('u54-live-compare-unproved', 'contentintel/session_live.py', '    elif ledger is None or not ledger.proved(platform, now):', '    elif False:', 'a compare uses a session that was never proved'),
    ('u54-live-compare-off-ignored', 'contentintel/session_live.py', '    on, why = switched_on(settings, platform)\n    if not on:\n        session = {"used": False, "reason": why}', '    on, why = switched_on(settings, platform)\n    if False:\n        session = {"used": False, "reason": why}', 'a compare uses a session the owner switched off'),
    ('u54-live-compare-always-adds', 'contentintel/session_live.py', '        if session["chars"] > public["chars"] * 1.2 and new_words >= 20:', '        if True:', 'the session is always said to add content'),
    ('u54-live-compare-words', 'contentintel/session_live.py', 'new_words = len(session["words"] - public["words"])', 'new_words = len(session["words"])', 'the new words are counted against nothing'),
    ('u54-live-compare-session-only', 'contentintel/session_live.py', '        verdict = "only the session read works: the public read failed (" + public["error"] + ")"', '        verdict = "the session adds content"', 'a failed public read is not reported'),
    ('u54-live-compare-raises', 'contentintel/session_live.py', '    except K.Failure as exc:\n        return {"ok": False, "chars": 0, "words": set(),', '    except KeyError as exc:\n        return {"ok": False, "chars": 0, "words": set(),', 'a missing worker crashes the compare'),
    ('u54-live-shape-line', 'contentintel/session_live.py', '    else:\n        lines.append(f"      page code:', '    elif False:\n        lines.append(f"      page code:', 'a page with no visible text is not described'),
    ('u54-live-text-markers-line', 'contentintel/session_live.py', '    if ev["logged_in_text_found"] or ev["logged_in_text_missing"]:', '    if False:', 'the logged-in words are never reported'),
    ('u54-live-detail-bound', 'contentintel/session_live.py', 'str(got.get("detail", ""))[:600]', 'str(got.get("detail", ""))[:30]', "twscrape's answer is cut to a few words"),
    ('u54-xt-logs-ignored', 'contentintel/x_twscrape_check.py', 'heard = _scrub(" | ".join(logs[-3:]), secrets, bound=300)', 'heard = ""', "twscrape's own warnings are not in the answer"),
    ('u54-xt-logs-explain-ignored', 'contentintel/x_twscrape_check.py', 'from_logs = explain("\\n".join(logs), None) if logs else None', 'from_logs = None', "a rejection that only twscrape's log states is not recognised"),
    ('u54-xt-logs-sink-level', 'contentintel/x_twscrape_check.py', 'level="WARNING", format="{message}")', 'level="ERROR", format="{message}")', "twscrape's warnings are not kept"),
    ('u54-xt-detail-bound', 'contentintel/x_twscrape_check.py', 'secrets, bound=600)}', 'secrets, bound=60)}', "twscrape's answer is cut off before its own words"),
    ('u54-wf-compare-option', '.github/workflows/sessions.yml', 'options: [check, list, read, compare]', 'options: [check, list, read]', 'the sessions workflow cannot run a compare'),
    ('u54-cmd-compare-off', 'contentintel/observatory/commands.py', 'what = args.what if args.what in ("check", "read", "compare") else "status"', 'what = args.what if args.what in ("check", "read") else "status"', 'the command ignores compare'),
    ('u54-cmd-compare-unchecked', 'contentintel/observatory/commands.py', '    if what == "compare":\n        if len(platforms) != 1 or not args.url:', '    if what == "compare":\n        if False:', 'a compare without a platform or address is attempted'),
]
MUTATIONS.extend(U61)

U62 = [   # update 55: junk is not content, page waits, refusal as a block, plain-words errors
    ('u55-worker-wait-uncapped', 'contentintel/observatory/scrapling_worker.py', 'kw["wait"] = max(0, min(int(adv["wait"]), 15000))', 'kw["wait"] = int(adv["wait"])', 'the page wait is not capped'),
    ('u55-worker-wait-dropped', 'contentintel/observatory/scrapling_worker.py', '    if adv.get("wait"):\n        kw["wait"]', '    if False:\n        kw["wait"]', 'the page wait never reaches the browser'),
    ('u55-worker-cap-small', 'contentintel/observatory/scrapling_worker.py', 'RAW_PAGE_CAP = 2_000_000', 'RAW_PAGE_CAP = 300_000', "a big page's code is cut at 300,000 characters"),
    ('u55-worker-truncated-flag', 'contentintel/observatory/scrapling_worker.py', 'out["html_truncated"] = len(html) > RAW_PAGE_CAP', 'out["html_truncated"] = False', 'a cut page does not say it was cut'),
    ('u55-worker-cap-off-by-one', 'contentintel/observatory/scrapling_worker.py', 'out["html"] = html[:RAW_PAGE_CAP]', 'out["html"] = html[:RAW_PAGE_CAP - 1]', 'the page code is one character short'),
    ('u55-sess-ig-check-wait', 'contentintel/sessions.py', '"adv": {"network_idle": True, "wait": 8000}, "read_adv"', '"adv": {"network_idle": True}, "read_adv"', "Instagram's check does not wait for the page to draw itself"),
    ('u55-sess-fb-read-wait', 'contentintel/sessions.py', '"read_adv": {"wait": 6000},', '"read_adv": {},', 'a Facebook read does not wait for the page to draw itself'),
    ('u55-sess-tiktok-read-wait', 'contentintel/sessions.py', '"logged_out": ("log in to tiktok",), "read_adv": {"wait": 3000},', '"logged_out": ("log in to tiktok",),', 'a TikTok read does not wait for the page to draw itself'),
    ('u55-live-image-data-kept', 'contentintel/session_live.py', '{20,}")\nLONG_TOKEN', '{20000,}")\nLONG_TOKEN', 'image data is printed as if it were text'),
    ('u55-live-long-token-kept', 'contentintel/session_live.py', 'LONG_TOKEN = re.compile(r"\\S{200,}")', 'LONG_TOKEN = re.compile(r"\\S{200000,}")', 'a long unbroken token is printed as if it were text'),
    ('u55-live-readable-chars', 'contentintel/session_live.py', 'MIN_READABLE_CHARS, MIN_READABLE_WORDS = 80, 12', 'MIN_READABLE_CHARS, MIN_READABLE_WORDS = 0, 12', 'a page of a few short words counts as readable'),
    ('u55-live-readable-words', 'contentintel/session_live.py', 'MIN_READABLE_CHARS, MIN_READABLE_WORDS = 80, 12', 'MIN_READABLE_CHARS, MIN_READABLE_WORDS = 80, 1', 'a page of one long word counts as readable'),
    ('u55-live-gate-off', 'contentintel/session_live.py', '    if text and not readable(text):\n        raise K.Failure(', '    if False:\n        raise K.Failure(', 'a script shell is returned as a successful read'),
    ('u55-live-compare-gate-off', 'contentintel/session_live.py', '    if worked and not readable(text):', '    if False:', 'a compare counts a script shell as a read'),
    ('u55-live-humanise-refusal', 'contentintel/session_live.py', '    if "err_http_response_code_failure" in low:', '    if False:', "a platform's error-status refusal is shown in the browser's own words"),
    ('u55-live-humanise-timeout', 'contentintel/session_live.py', '    elif "timeout" in low and ("goto" in low or "exceeded" in low):', '    elif False:', "a timeout is shown in the browser's own words"),
    ('u55-live-refused-not-blocked', 'contentintel/session_live.py', '    if err.get("code") == "PLATFORM_BLOCKED" or refused or looks_like_challenge(', '    if err.get("code") == "PLATFORM_BLOCKED" or looks_like_challenge(', 'an error-status refusal is not reported as a block'),
    ('u55-live-refused-unclean', 'contentintel/session_live.py', 'err.get("code") != "PLATFORM_BLOCKED" and not refused and not WALL', 'err.get("code") != "PLATFORM_BLOCKED" and not WALL', 'an error-status refusal is reported as a check that could not be made'),
    ('u55-live-x403-note', 'contentintel/session_live.py', '            if "403" in layered["detail"]:', '            if False:', 'the report does not say X refuses the runner on both routes'),
    ('u55-live-cut-note', 'contentintel/session_live.py', "{' (cut at the limit: the rest was not searched)' if ev.get('html_truncated') else ''}", "{''}", 'a cut page is not said to be cut'),
    ('u55-live-check-waits-like-read', 'contentintel/session_live.py', '**((S.PLATFORMS[platform].get("read_adv") or {}) if reading else {})}', '**(S.PLATFORMS[platform].get("read_adv") or {})}', 'a check waits as long as a read'),
    ('u55-live-read-primary-no-wait', 'contentintel/session_live.py', 'with_session=False, reading=True)\n        if not res.get("ok"):', 'with_session=False, reading=False)\n        if not res.get("ok"):', 'the plain read does not wait for the page'),
    ('u55-live-failure-raw', 'contentintel/session_live.py', '    return K.Failure(code, humanise_error(err.get("code") or "no answer", message))', '    return K.Failure(code, message)', "a failed read says it in the browser's own words"),
    ('u55-live-wall-reworded', 'contentintel/session_live.py', '        return K.Failure(K.NEED_LOGIN, message)', "        return K.Failure(K.NEED_LOGIN, humanise_error('x', message))", "a login wall's own wording is changed"),
]
MUTATIONS.extend(U62)

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
