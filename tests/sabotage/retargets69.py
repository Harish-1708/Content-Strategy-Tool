"""Retargets and parked entries for the FIRST sabotage script (run_sabotage.py, 1,884 checks).

The live sabotage run never reached the later scripts, and the first script's own check found entries whose target text no longer exists exactly once
(the code moved in Updates 67 and 68 and the captcha layer). RETARGET gives the entries that were fixed a new target; PARKED are the ones not yet retargeted:
they are NOT run, they are counted and listed in the run summary so nothing is hidden, and each still has to be retargeted by reading its code.
"""
RETARGET = {
    'u18-merge-page-facts-lost': ('        if key in first:', '        if False:'),
    's1-health-trust-ignores-hash': ('                 and acc.get("route_hash") == self._hash_for(source, route))', '                 )'),
    'u19-yaml-pacing-fast': ('    pacing: {min_delay_seconds: 20,', '    pacing: {min_delay_seconds: 2,'),
    'u19-yaml-run-cap-loose': ('    max_pages_per_run: 2             # hashtag pages opened in one run', '    max_pages_per_run: 20            # hashtag pages opened in one run'),
    'u31-cfg-careful': ('    max_pages_per_run: 2             # hashtag pages opened in one run', '    # max_pages_per_run: 2             # hashtag pages opened in one run'),
    'u31-cfg-rest': ('      instagram: 7          # after a warning', '      instagram: 0          # after a warning'),
    'u31-cfg-default': ('"safety": {"cooldown_days": {"instagram": 7, "tiktok": 7}},', '"safety": {"cooldown_days": {"instagram": 0, "tiktok": 3}},'),
    's2d-tt-fallback-off': ('    password_fallback: when_cookies_fail\n    max_pages_per_run: 2             # careful mode, as for Instagram', '    max_pages_per_run: 2             # careful mode, as for Instagram'),
    'u22-pw-not-released': ('            closer = lambda browser=browser: (_close_browser(browser), _SHARED.release())', '            closer = lambda browser=browser: (_close_browser(browser), None)'),
    'login-wall-mid-collection-ignored': ('        if after == "login_wall":', '        if False:'),
    'challenge-mid-collection-ignored': ('        if after == "challenge":', '        if False:'),
    'apify-gives-up-too-early': ('                    timeout=timeout + 30)', '                    timeout=30)'),
    'u19-stats-pages-uncounted-searches': ('                  "pages": len(urls) + len(how) + len(tag_fallback), "pages_planned": len(urls), "pages_total": pages_total,', '                  "pages": len(urls) + len(tag_fallback), "pages_planned": len(urls), "pages_total": pages_total,'),
    'u23-wf-tidy-option': ('        options: [plan, seeds, benchmarks, check, recon, test_deep_research, collect, rest, tidy]', '        options: [plan, seeds, benchmarks, check, recon, test_deep_research, collect, rest]'),
    's1-final-login-retried': ('FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, NEED_LOGIN, PROVIDER_RETIRED})', 'FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, PROVIDER_RETIRED})'),
    's1-final-block-retried': ('FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, NEED_LOGIN, PROVIDER_RETIRED})', 'FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, RATE_LIMITED, NEED_LOGIN, PROVIDER_RETIRED})'),
    's2-rt-model-allowlist': ('        ok, why = self.registry.eligibility(pid, data_class, model=route["model"])', '        ok, why = self.registry.eligibility(pid, data_class)'),
    's2-rt-rest': ('        rest = None if ignore_rests else self.health.is_paused(f"ai:{pid}", "gateway", now)', '        rest = None'),
    's2-rt-fail-health': ('                        self.health.observe(source, "gateway", ok=False, code=err["code"], message=err["message"], now=now, retry_after=short or err.get("retry_after_seconds"))', '                        pass'),
    's2-cmd-plan-clients': ('        clients = {pid: None for pid, p in registry.providers.items() if p.credential_env and (any if p.direct else all)(os.environ.get(n, "").strip() for n in p.credential_env)}', '        clients = {}'),
}

RETIRED = {
    's2-rt-experimental': 'the experimental-routes feature no longer exists: registry.eligibility, the router and the lane loader have no experimental gating (checked 2026-11-17), so there is no behaviour to break',
    's2-reg-elig-experimental': 'the experimental-routes feature no longer exists: registry.eligibility, the router and the lane loader have no experimental gating (checked 2026-11-17), so there is no behaviour to break',
    's2-reg-experimental-nonpublic': 'the experimental-routes feature no longer exists: registry.eligibility, the router and the lane loader have no experimental gating (checked 2026-11-17), so there is no behaviour to break',
    's2-lane-experimental-nonpublic': 'the experimental-routes feature no longer exists: registry.eligibility, the router and the lane loader have no experimental gating (checked 2026-11-17), so there is no behaviour to break',
}

PARKED = {
    'collected-twice-inside-the-window': 'the code it targets moved: needs a retarget',
    'apify-token-without-bearer': 'the code it targets moved: needs a retarget',
    'apify-returns-more-than-the-cap': 'the code it targets moved: needs a retarget',
    'prompt-not-prepared-by-claude': 'the code it targets moved: needs a retarget',
    'login-failure-not-switching-site-off': 'the code it targets moved: needs a retarget',
    'browser-download-not-retried': 'the code it targets moved: needs a retarget',
    'u14-decisions-unsaved': 'the code it targets moved: needs a retarget',
    'u16-tt-fallback-off': 'the code it targets moved: needs a retarget',
    'u16-tt-flag-ignored': 'the code it targets moved: needs a retarget',
    'u16-tt-any-page': 'the code it targets moved: needs a retarget',
    'u16-tt-no-decode': 'the code it targets moved: needs a retarget',
    'u17-tt-note-no-how': 'the code it targets moved: needs a retarget',
    'u18-cli-region-collect-dropped': 'the code it targets moved: needs a retarget',
    'u18-rest-zero-still-writes': 'the code it targets moved: needs a retarget',
    'u33-cli': 'the code it targets moved: needs a retarget',
    's1-final-timeout-final': 'the code it targets moved: needs a retarget',
    's1-health-no-pause': 'the code it targets moved: needs a retarget',
    's1-health-rest-never-ends': 'the code it targets moved: needs a retarget',
    's1-acc-no-key-search': 'the code it targets moved: needs a retarget',
    's1-cli-collect-registry-dropped': 'the code it targets moved: needs a retarget',
    's1-cli-collect-health-dropped': 'the code it targets moved: needs a retarget',
    's1-cli-ideabank-health-dropped': 'the code it targets moved: needs a retarget',
    's1-col-login-unrecorded': 'the code it targets moved: needs a retarget',
    's2-gw-login': 'the code it targets moved: needs a retarget',
    's2b-apify-201': 'the code it targets moved: needs a retarget',
    's2b-apify-any-2xx': 'the code it targets moved: needs a retarget',
    's2b-apify-408-retried': 'the code it targets moved: needs a retarget',
    's2c-gw-build-shared-mock': 'the code it targets moved: needs a retarget',
    's2c-gw-build-shared-live': 'the code it targets moved: needs a retarget',
}


RETIRED_IDS = set(RETIRED)


def apply(mutations):
    """(the mutations with the retargets applied and the parked ones left out, the parked ids)."""
    out, parked = [], []
    for m in mutations:
        if m[0] in RETIRED_IDS:
            continue                                                                       # retired: the behaviour no longer exists (see RETIRED), nothing to break
        if m[0] in PARKED:
            parked.append(m[0])
            continue
        if m[0] in RETARGET:
            m = (m[0], m[1], RETARGET[m[0]][0], RETARGET[m[0]][1], *m[4:])
        out.append(m)
    return out, parked


# The checks below were retargeted BY HAND on 2026-11-18: each old text is the code as it is now (matched exactly once), each new text breaks the same behaviour the check always meant to break.
HAND = {
    'collected-twice-inside-the-window': ('if not fresh and _fresh(records, task.source, task.key, now, days):', 'if False:'),
    'apify-token-without-bearer': ('return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}', 'return {"Authorization": self.api_key, "Content-Type": "application/json"}'),
    'apify-returns-more-than-the-cap': ('        body = self._call("GET", url, params={"limit": max_items, "offset": 0, "format": "json"},', '        body = self._call("GET", url, params={"limit": 100000, "offset": 0, "format": "json"},'),
    'prompt-not-prepared-by-claude': ('prompt = _for_purpose(claude, "collectors").prepare_prompt(question, context)', 'prompt = question'),
    'login-failure-not-switching-site-off': ('+ rest_after(exc, site)\n                        disabled[site] = message\n                        result.problems_for.append(site)\n                        result.notes.append(message)\n                        report.status, report.detail = "failed", message\n                        continue\n                    except BrowserError', '+ rest_after(exc, site)\n                        result.problems_for.append(site)\n                        result.notes.append(message)\n                        report.status, report.detail = "failed", message\n                        continue\n                    except BrowserError'),
    'browser-download-not-retried': ('          pip install -r requirements-browser.txt\n          for attempt in 1 2 3; do', '          pip install -r requirements-browser.txt\n          for attempt in 1; do'),
    'u14-decisions-unsaved': ('                if healed:\n                    store.write_benchmarks(brand.slug, state)', '                if healed:\n                    pass'),
    'u16-tt-fallback-off': ('if tag is not None and not searched and (browser.exists(errors) or browser.exists(challenges)):', 'if False:'),
    'u16-tt-flag-ignored': ('tag = _tag_in_url(url) if site_cfg.get("hashtag_via_search_on_error") or site_cfg.get("hashtag_via_search_first") else None', 'tag = _tag_in_url(url) if True else None'),
    'u16-tt-any-page': ('    for part in ("/tag/", "/tags/"):\n        if part in url:\n            return unquote(url.split(part, 1)[1].split("?")[0].split("/")[0])\n    return None', '    for part in ("/tag/", "/tags/"):\n        if part in url:\n            return unquote(url.split(part, 1)[1].split("?")[0].split("/")[0])\n    return "anything"'),
    'u16-tt-no-decode': ('return unquote(url.split(part, 1)[1].split("?")[0].split("/")[0])', 'return url.split(part, 1)[1].split("?")[0].split("/")[0]'),
    'u18-cli-region-collect-dropped': ('region=args.region.strip() or None, health=health_ledger, registry=registry, fresh=bool(args.fresh)', 'region=None, health=health_ledger, registry=registry, fresh=bool(args.fresh)'),
    'u18-rest-zero-still-writes': ('        if days <= 0 or dry_run:', '        if dry_run:'),
    'u33-cli': ('browser_factory=_ad_browser_factory(args), health=ledger, video_facts=video_facts', 'browser_factory=None, health=ledger, video_facts=video_facts'),
    's1-final-timeout-final': ('FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, NEED_LOGIN, PROVIDER_RETIRED})', 'FINAL_CODES = frozenset({NOT_FOUND, BAD_INPUT, PLATFORM_BLOCKED, RATE_LIMITED, NEED_LOGIN, PROVIDER_RETIRED, TIMEOUT})'),
    's1-health-no-pause': ('        if seconds > 0 and guards.on("rest_after_refusal"):\n            until = add_seconds(now, seconds)', '        if False:\n            until = add_seconds(now, seconds)'),
    's1-health-rest-never-ends': ('or not pause.get("until") or pause["until"] <= now:', 'or not pause.get("until"):'),
    's1-acc-no-key-search': ('reason = skip or ("" if rt.name in ctx.providers else missing)', 'reason = skip'),
    's1-cli-collect-registry-dropped': ('health=health_ledger, registry=registry, fresh=bool(args.fresh)', 'health=health_ledger, registry=None, fresh=bool(args.fresh)'),
    's1-cli-collect-health-dropped': ('health=health_ledger, registry=registry, fresh=bool(args.fresh)', 'health=None, registry=registry, fresh=bool(args.fresh)'),
    's1-cli-ideabank-health-dropped': ('browser_factory=_ad_browser_factory(args), health=ledger, video_facts=video_facts', 'browser_factory=_ad_browser_factory(args), video_facts=video_facts'),
    's1-col-login-unrecorded': ('                    except (LoginWall, Challenge) as exc:\n                        seen(task.source, "browser", exc=exc)\n                        result.skipped_sources.setdefault(site, skip_details(site, exc, opened))', '                    except (LoginWall, Challenge) as exc:\n                        result.skipped_sources.setdefault(site, skip_details(site, exc, opened))'),
    's2-gw-login': ('        if status != 200 or not token:\n            raise GatewayError(f"the gateway admin login failed ({status})")', '        if False:\n            raise GatewayError(f"the gateway admin login failed ({status})")'),
    's2b-apify-201': ('        if status in (200, 201):\n            run = body.get("data", body) if isinstance(body, dict) else {}', '        if status in (200,):\n            run = body.get("data", body) if isinstance(body, dict) else {}'),
    's2b-apify-any-2xx': ('        if status in (200, 201):\n            run = body.get("data", body) if isinstance(body, dict) else {}', '        if 200 <= status < 300:\n            run = body.get("data", body) if isinstance(body, dict) else {}'),
    's2b-apify-408-retried': ('            if status == 408:\n                raise _Sync408(body)', '            if False:\n                raise _Sync408(body)'),
    's2c-gw-build-shared-mock': ('            npm run build -w server\n          fi\n\n      - name: Real gateway, fake model', '            npm run build -w shared -w server\n          fi\n\n      - name: Real gateway, fake model'),
    's2c-gw-build-shared-live': ('            npm run build -w server\n          fi\n\n      - name: Set up the free provider pool', '            npm run build -w shared -w server\n          fi\n\n      - name: Set up the free provider pool'),
    'u17-tt-note-no-how': ('way = " and ".join(sorted(set(stats.get("fallback_how") or []))).replace("typed", "typed into the search box, as a person would")', 'way = " and ".join(sorted(set(stats.get("fallback_how") or []))).replace("typed", "typed")'),
}
RETARGET.update(HAND)
for _id in HAND:
    PARKED.pop(_id, None)
