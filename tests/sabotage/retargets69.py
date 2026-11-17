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
