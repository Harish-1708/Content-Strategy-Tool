# FreeLLMAPI: version reconciliation and pin recommendation

Prepared 3 November 2026. Everything here was measured by fetching the upstream tags and building the gateway, not taken from the README.

## 1. A correction to the earlier blueprint

The blueprint called the reviewed code "v0.13". It was not. The snapshot we reviewed is **byte-for-byte identical to tag v0.16.15** (0 differing files). It is 17 files away from v0.16.17 and 438 files away from v0.13.10. "v0.13" is also not one version: upstream has eleven 0.13.x tags (0.13.0 to 0.13.10). Every capability described in the blueprint (search chain, typed decision calls, fusion, compression, declarative config) belongs to the 0.16 line.

The README headline ("34 providers, 635 endpoints") is the same text in every version and is stale. The code registers **84 platform ids** in v0.16.17 (43 chat providers per the project's own catalogue document, plus search, decision-extraction, media and other services).

## 2. The versions side by side

| | v0.13.0 | v0.13.10 | v0.16.15 | v0.16.17 |
|---|---|---|---|---|
| Platform ids | 58 | 66 | 83 | 84 |
| Provider source files | 26 | 34 | 52 | 53 |
| Database migrations | 38 | 39 | 47 | 47 |
| Route files | 28 | 29 | 31 | 31 |
| Service files | 42 | 44 | 50 | 50 |
| Commit | n/a | bae85413b9c9 | eae1a4038d59 | 741d03094398 |

## 3. v0.13.10 to v0.16.17 (the large jump)

- **Added platforms (19):** aisense, apinex, beatapi, brave, exa, firecrawl, halx, iteracompute, jamai, keenable, kenari, kiosapi, linkup, nabh, novai, onomeo, parallel, serpsearch, tavily. Most are search or decision-extraction services, not chat models.
- **Removed platform:** `github` (GitHub Models). Migration `20261017_000002_remove_github_provider` deletes its keys. Another migration removes `auray`.
- **New migrations (8):** model probe status, search engines, decision requests, llmtr key re-enable, GitHub removal, request-failure attribution, Auray removal, key last-healthy time.
- **New routes:** `/v1/search` and `/v1/decisions`.
- **New environment variables:** ALLOWED_HOSTS, AUTO_BACKUP_DUMP_RETENTION, BUDGET_ALERT_WEBHOOK_URL, HEALTH_CHECK_INTERVAL_MS, HEALTH_TRANSPORT_PARK_AFTER, MODEL_PROBE_ENABLED, MODEL_PROBE_MAX_PER_PASS, ROUTER_PROVIDER_MOMENT_ATTRIBUTION.
- **Upgrade risk from 0.13:** moderate. State from a 0.13 database would pass through eight migrations, two of which delete provider keys. We have no 0.13 state, so this does not affect us.

## 4. v0.16.15 to v0.16.17 (the small jump)

Seventeen files. All non-test changes were read:
- adds the `novai` platform (daily free pool) and its key prefix;
- Free.ai wording: speech and transcription only, not chat;
- catalogue-sync fix: a legacy disabled audio row no longer disables a still-enabled transcription route;
- quota pool entry for novai; a client-side routing file; test files.

No breaking change, no new environment variable, no migration.

## 5. Facts that shape our integration (all observed by running v0.16.17)

1. **It builds and runs here:** `npm ci` for the server takes about 10 seconds, `tsc` about 18 seconds; native SQLite works.
2. **`HOST` defaults to `::`, all interfaces.** Our sidecar must set `HOST=127.0.0.1`. With it set, the server listened on 127.0.0.1 only.
3. **Authentication is required on `/v1`** (401 without the unified key).
4. **A bootstrap that works:** an `admin` entry in `FREEAPI_CONFIG_JSON` creates the first account and closes the open first-run setup window. Then `POST /api/auth/login` returns a token; `GET /api/settings/api-key` with that token returns the unified key. We ran this end to end against a fake upstream model: the call came back routed, with usage numbers.
5. **Without an `admin` entry the server prints a first-run setup code to its log.** Mask it, and always provision `admin`.
6. **The model list is not in the repository.** It comes from a signed remote catalogue (ed25519) at `api.freellmapi.co`. With `CATALOG_SYNC_DISABLED=1`, 108 model rows still exist after boot (from the baseline migrations), across 15 platforms. Pinning the version therefore pins that 108-row baseline, not "the free models of the day".
7. **Declared limits in those rows are unreliable.** The gateway row for an OpenRouter free model says 200 requests/day; OpenRouter documents 50 (1,000 after a $10 purchase). We must treat embedded numbers as hints and set our own limits.
8. **Off by default, keep off:** MODEL_PROBE_ENABLED (sends probe requests with our keys), BUDGET_ALERT_WEBHOOK_URL, the premium `license` field, ALLOWED_HOSTS.
9. **Declarative config detail:** custom providers take `apiKey` (not `key`).

## 6. Recommendation

**Pin v0.16.17, commit `741d03094398facceb1b6019a838f806e4d9f7f9`, built from source in the Actions job, run on Node 22 (tested), with `CATALOG_SYNC_DISABLED=1`.**

Why:
- The reviewed code is v0.16.15, and the whole difference to v0.16.17 is small, was read in full, and contains a fix with no breaking change. Pinning the reviewed v0.16.15 would be equally defensible; we prefer .17 for the fix.
- v0.13.x is older, lacks the search and decision features, and was never the code we reviewed.
- Building from the commit (not "latest") is reproducible. We did not test the upstream Docker image (it uses Node 24.22.1; the server allows Node 20.18 to 24).
- Disabling catalogue sync means no phone-home and a frozen model list. The price: new free models do not appear until we deliberately refresh the pin. We add models ourselves through `models` or `customProviders` in our own config.

Not done: a live test against any real provider, the Docker image, and the signed-catalogue flow (deliberately disabled).
