"""Deliberate-bug checks for Update 59 (the gateway smoke test expects exactly the approved models of enabled providers to be on). Same format as run_observatory_sabotage.MUTATIONS; added to that runner."""
U59 = [
    ('u59-keyed-provider-expected-without-key', 'contentintel/ai/smoke.py', 'if p.credential_env and gw._credential(p, environ) is None:', 'if False:', 'a provider with no key is expected to be on'),
    ('u59-direct-provider-expected', 'contentintel/ai/smoke.py', 'if p.id == "claude" or p.direct or not p.enabled:', 'if p.id == "claude" or not p.enabled:', 'OpenRouter, called directly, is expected in the gateway'),
    ('u59-unapproved-expected-on', 'contentintel/ai/smoke.py', '{(p.gateway_platform, m.id) for m in p.models if m.approved}', '{(p.gateway_platform, m.id) for m in p.models}', 'an unapproved model is expected to be on'),
    ('u59-wrongly-on-unchecked', 'contentintel/ai/smoke.py', 'on = sorted(k for k, v in seen.items() if v and k not in want)', 'on = []', 'a model that is on but not approved passes'),
    ('u59-approved-off-unchecked', 'contentintel/ai/smoke.py', 'off = sorted(k for k in want if k in seen and not seen[k])', 'off = []', 'an approved model that is off passes'),
    ('u59-empty-rows-pass', 'contentintel/ai/smoke.py', 'return bool(seen) and not on and not off,', 'return not on and not off,', 'a gateway that lists no models passes'),
    ('u59-custom-model-counted', 'contentintel/ai/smoke.py', 'for r in rows if r.get("platform") != "custom"}', 'for r in rows}', "the smoke's own fake model counts as wrongly enabled"),
    ('u59-model-id-spelling', 'contentintel/ai/smoke.py', 'r.get("modelId") or r.get("model_id")', 'r.get("modelId")', 'a row that spells its id model_id is not seen'),
    ('u59-check-not-enforced', 'contentintel/ai/smoke.py', 'ok, detail = switch_check(rows, registry, environ)', 'ok, detail = True, ""', 'the real smoke test no longer checks the switches')
]
