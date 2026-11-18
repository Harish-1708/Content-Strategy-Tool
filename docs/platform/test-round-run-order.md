# The test round: the order to run things on GitHub (updated 2026-11-18)

Everything here is run from Actions on GitHub; nothing needs a command line. A provider without a key is skipped with a printed note. Keyless providers (OVH, AI Horde) need no secret.

1. **Upload this update.** Paste the rows from `README-video-trials-rows.md` into the README's secrets table (an addition only: the README is yours and is NOT replaced by this update).
2. **Skills and registries > verify.** Must print `skills: OK: 120 skills at 7932adf517`.
3. **Provider test** (retry). It never uses the repository's Claude key. It tries every key it finds, even for providers that are switched off, so SambaNova, Ollama, NaraRouter and Mistral will still show as failed: switching them off means the tool no longer USES them, not that the test skips them. OVH and AI Horde are tried with no key. Send the summary: how many connected, which, which failed and why. BluesMinds' three new models are candidates only: this run is the check.
4. **Skills and registries > foundation**, brand swoveralls, `max_steps` 3, `max_calls` 6, repeated until all nine sections are proposed. The save step now works with a changed tracked file and prints what it set aside; the proposals are committed to main this time. Look for "model route: N call(s) answered by the free pool, M by Claude".
5. **reclassify-conflicts** (swoveralls: one call for all 12 conflicts), then **refresh-story-bank** and **foundation** for duderobe and defi_snacks. The refresh has not yet been proven live on the pool (its run #8 was all Claude).
6. **Sync to Google Sheets.** Adds the foundation rows to the Controls tab. In the sheet, restrict who may edit the Decision columns of the Canon and Claims tabs to Harish only (Data > Protect sheets and ranges): the tool cannot tell who typed a Decision (M149).
7. **Pool retest** (apply on). This is the measurement: each job's real prompts against every candidate, FreeLLMAPI and OmniRoute providers together. M240 closes only by this run.
8. **Tests > sabotage**, then **sabotage_full** (reaches the 52 missed first-script checks). The updated set alone does not.
9. **Collect** with ignore_cooldown ticked (send the summary and `read-log.jsonl`); the first **Apify** run and a real voice run (M103's $3 per-run cap): $19.34 was left of $20 across 4 keys on 2026-11-16: check the allowance before dispatching.
10. **Video trials** (one clip per site: PixVerse, Google Flow, Dreamina Seedance 2.0 or Mini only, Kling; Luma stays out, its driver is a stub and no credit information was shown) and the **photo trial** (Pollinations, free, no key; a 503 is a no-go verdict, not a failure). Both take a brand input.
11. Still pending: the brief workflow's refresh step, the retry and re-check live, M196 (a real browser), and the GEMINI_COOKIES refresh (blocks only the Acceptance retest).

If any step fails, send the exact lines it printed; do not skip ahead.
