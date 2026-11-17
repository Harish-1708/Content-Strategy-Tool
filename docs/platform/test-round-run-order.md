# The test round: the order to run things on GitHub

Everything here is run from Actions on GitHub; nothing needs a command line. Add the provider keys as repository secrets first (Settings > Secrets and variables > Actions); a provider without a key is skipped with a printed note.

1. **Skills and registries > verify.** Must print `skills: OK: 120 skills at 7932adf517`. Proves the upload landed.
2. **Provider test** (new). Tries every provider and every approved route once, FreeLLMAPI and OmniRoute, and prints how many connected, which ones, and which failed and why. It never uses the repository's Claude key. Read the summary and tell me which providers to keep.
3. **Skills and registries > foundation**, brand swoveralls, `max_steps` 3, `max_calls` 6. Repeat until all nine sections are proposed. The free pool is asked first; the run prints "model route: N calls answered by the free pool, M by Claude" and any reason the pool was not used. Check the Canon tab's gate-note column.
4. **Skills and registries > reclassify-conflicts**, brand swoveralls (one call for all 12 conflicts). Then **refresh-story-bank** and **foundation** for duderobe and defi_snacks.
5. **Sync to Google Sheets.** Adds the new foundation rows to the Controls tab, the Decision lists on Canon and Claims, and the gate-note column.
6. **Pool retest** (new; needs the keys). Runs each job's real prompts against every provider and writes the measured order of each job's ranked list. Run it with `apply` on. Until it has run, every job's list is the seeded order.
7. **Tests > sabotage** (the updated set first). Then **Collect** with ignore_cooldown ticked, and send the summary and `read-log.jsonl`.
8. First Apify run and a real voice run (M103's $3 cap), and the Claude key as fallback: send the "model route" lines.

If any step fails, send the exact lines it printed; do not skip ahead.
