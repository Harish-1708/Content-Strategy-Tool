# The daily provider-model health run

**Why.** Pinned model ids keep dying: AnyAPI's qwen3.8, xKiro's qwen3.6-plus, llm7 (402), NavyAI (a model that never existed), NVIDIA (timeouts). Nobody can hand-edit the code every time a provider retires a model, so this run keeps the ranked lists of the free pool alive by itself.

**When.** Actions > **Provider health**, every day at 05:30 UTC (and on demand). It is `contentintel ai health --live`.

**What it does, in order**
1. **Discovers** the live free models of every provider that publishes a list: AnyAPI and OpenRouter (only ids ending `:free`) and OVH (every model of its keyless free endpoint). A provider without a list has its pinned models probed directly.
2. **Probes** every enabled provider's candidate models with ONE tiny call (`Reply with the single word OK.`, a few tokens) and records: did it answer, how fast, how many tokens, and why not (404 dead, 402 out of credits, 429 rate-limited, timeout).
3. **Scores** every probe-passed model per job with the seven weights the router already uses: quality 0.35 (from the last measured order), headroom 0.20 (from the usage ledger), reliability 0.15, speed 0.10, cost 0.08, capability 0.07, terms 0.05.
4. **Guards.** Free providers only, so a paid model is never added. **Cost guard:** a model is left out of a job when one call of that job (its largest answer plus about 1,500 tokens of prompt) would be more than `cost_guard_share` of the provider's daily token allowance (or one request more than that share of its daily requests). The default is 10 percent: 1 percent would leave every token-limited provider out of the 8,000-token jobs. **Capability guard:** the model must fit the job (text for text, reasoning for reasoning, vision where needed).
5. **Claude judges only what the rules cannot decide**: a model never seen before in a job's top 3, a tie at the top (scores within 0.01), or a job's previous top pick that just died. Then Claude gets ONE small call with a summarized table (a few hundred tokens), at most once a day, and its answer is recorded with its reasoning in `provider_health.json`. Its pick must be among the candidates that already passed the guards, or it is ignored. Claude never runs the probe and never touches live traffic. If the rules decide everything, there is no Claude call that day.
6. **Writes** the new ranked lists to the generated files, within the swap cap.

**Safety rules**
- Only a model that PASSED the probe is added; a dead or out-of-credit model drops out automatically.
- Never a paid model, never a card.
- The LAST working model of a job is never removed: if everything for a job fails, its last known-good list is kept and the job is flagged.
- At most `max_swaps` (default 6) DIFFERENT routes are dropped or added in one run, so one bad probe cannot churn the setup.
- It touches **only** `data/_shared/provider_health.json` and `data/_shared/ai_rankings.json`: no code, no other data file, no workflow, no setting. Each change is a git commit with a readable message (`Provider health 2026-11-25: dropped dead ...`), pushed directly with no human gate; one `git revert` undoes it.
- **Owner switch:** `auto_update` in `ai_health.yaml` (default **true**). With `false` the run only reports what it would change and writes nothing.
- The router uses the generated lists only while they are fresh (`stale_days`, default 3); missing or older, it falls back to the static lists in `ai_lanes.yaml` and the last pool retest. The router's own failover at run time stays as the mid-day safety net.

**Proof.** The run's own summary in the Actions page (which models died, which replaced them, what Claude decided and why) and `data/_shared/provider_health.json`; the next day's real runs then use the new lists.

**Cost.** About 15 providers x a few models x a few tokens. Claude costs at most one small call a day, and only when needed, inside the existing $0.50 a day and $10 a month caps.
