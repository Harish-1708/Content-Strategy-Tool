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
}

PARKED = {
    'login-wall-mid-collection-ignored': 'the code it targets moved: needs a retarget',
    'challenge-mid-collection-ignored': 'the code it targets moved: needs a retarget',
    'collected-twice-inside-the-window': 'the code it targets moved: needs a retarget',
    'apify-token-without-bearer': 'the code it targets moved: needs a retarget',
    'apify-returns-more-than-the-cap': 'the code it targets moved: needs a retarget',
    'apify-gives-up-too-early': 'the code it targets moved: needs a retarget',
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
    'u19-stats-pages-uncounted-searches': 'the code it targets moved: needs a retarget',
    'u23-wf-tidy-option': 'the code it targets moved: needs a retarget',
    'u33-cli': 'the code it targets moved: needs a retarget',
    's1-final-login-retried': 'the code it targets moved: needs a retarget',
    's1-final-block-retried': 'the code it targets moved: needs a retarget',
    's1-final-timeout-final': 'the code it targets moved: needs a retarget',
    's1-health-no-pause': 'the code it targets moved: needs a retarget',
    's1-health-rest-never-ends': 'the code it targets moved: needs a retarget',
    's1-acc-no-key-search': 'the code it targets moved: needs a retarget',
    's1-cli-collect-registry-dropped': 'the code it targets moved: needs a retarget',
    's1-cli-collect-health-dropped': 'the code it targets moved: needs a retarget',
    's1-cli-ideabank-health-dropped': 'the code it targets moved: needs a retarget',
    's1-col-login-unrecorded': 'the code it targets moved: needs a retarget',
    's2-reg-experimental-nonpublic': 'the code it targets moved: needs a retarget',
    's2-reg-elig-experimental': 'the code it targets moved: needs a retarget',
    's2-lane-experimental-nonpublic': 'the code it targets moved: needs a retarget',
    's2-rt-experimental': 'the code it targets moved: needs a retarget',
    's2-rt-model-allowlist': 'the code it targets moved: needs a retarget',
    's2-rt-rest': 'the code it targets moved: needs a retarget',
    's2-rt-fail-health': 'the code it targets moved: needs a retarget',
    's2-gw-login': 'the code it targets moved: needs a retarget',
    's2-cmd-plan-clients': 'the code it targets moved: needs a retarget',
    's2b-apify-201': 'the code it targets moved: needs a retarget',
    's2b-apify-any-2xx': 'the code it targets moved: needs a retarget',
    's2b-apify-408-retried': 'the code it targets moved: needs a retarget',
    's2c-gw-build-shared-mock': 'the code it targets moved: needs a retarget',
    's2c-gw-build-shared-live': 'the code it targets moved: needs a retarget',
}


def apply(mutations):
    """(the mutations with the retargets applied and the parked ones left out, the parked ids)."""
    out, parked = [], []
    for m in mutations:
        if m[0] in PARKED:
            parked.append(m[0])
            continue
        if m[0] in RETARGET:
            m = (m[0], m[1], RETARGET[m[0]][0], RETARGET[m[0]][1], *m[4:])
        out.append(m)
    return out, parked
