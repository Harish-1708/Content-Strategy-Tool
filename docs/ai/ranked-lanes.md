# The pool is never random: one ranked list per job

**The rule (owner's, 2026-11-17).** The free pool (FreeLLMAPI's providers and, when it is wired in, OmniRoute's) is used extensively and the load is spread across it, but never at random. Every piece of work that uses AI providers has its own **ranked list**: the provider best for that exact job first, then backups in order, switching automatically when one fails, with Claude as the final fallback and the overseer, not the workhorse. The lists come from what actually works live in the pool retest, not from guessing. Every future build or feature that uses AI providers gets its own list, built the same way.

## How it works in the tool

- A **lane** in `ai_lanes.yaml` is the ranked list for one kind of work (a `chain` of provider and model, in order). A route is used only if its provider is enabled, approved for the data class, has a credential and is not resting; each provider is paced to its own limits, and when one has no free slot the next is tried first, which is how load is spread. After the lane's own chain comes the shared pool (every approved model), then Claude.
- A **job** is one thing a module asks the pool to do. `contentintel/services.py` (`JOB_LANES`) maps each job to its lane, and `RoutedClaude.for_job(job)` asks that lane. One gateway serves every job. A job with no lane of its own, or an install whose lanes file lacks it, uses `general_json`.
- Jobs and their lanes today:

| Job | Lane | Ranking status |
|---|---|---|
| The foundation chain (one section of positioning and messaging) | `foundation_json` | NOT YET MEASURED: seeded with `general_json`'s chain |
| The story-bank refresh: extract facts from a page | `story_extract_json` | NOT YET MEASURED |
| The story-bank refresh: review each fact | `story_review_json` | NOT YET MEASURED |
| Conflict check (refresh and `reclassify-conflicts`, many pairs per call) | `conflict_check_json` | NOT YET MEASURED |
| Every older module (ideas, planning, briefs, deep checks, collectors) | the general lanes and the ten task lanes | designed order, not measured live |

The honest state: until the pool retest has run, the order inside each of these lists is the order the project already used (Groq, Zhipu, Mistral, OpenRouter, Cloudflare), copied, not measured. Each lane's description says "RANKING NOT YET MEASURED" until someone replaces it.

## How a list is built from live results (the pool retest)

1. For each job, run the real prompts of that job against each candidate provider and model (extraction needs exact quotations and valid JSON; review needs a strict yes/no; conflict check needs the three-way label; the foundation chain needs long careful writing with the required fields).
2. Score each on what the job needs: valid output, the gate and quotation checks passing, speed, and how often the provider is limited or down. Take each provider down in turn and show the next one answers (failover), and show Claude answers when none can.
3. Order the chain best first in `ai_lanes.yaml`, with the evidence (date, runs, results) in the lane's description, and remove the "NOT YET MEASURED" words. A provider that fails the job is left out of that lane's list, not just ranked last.
4. A new build adds its job to `JOB_LANES` and its lane to `ai_lanes.yaml` in the same change; a test fails if the code asks for a job that has no lane, or a lane is unused.

## The pool retest

`python -m contentintel ai rank --jobs all --runs 2 --apply` (Actions > Pool retest) runs each job's real prompts, with inputs whose right answers are known, against every provider and model that can serve it (FreeLLMAPI's and OmniRoute's together). Each answer is scored by a check the code can make (a quotation really in the page, a right verdict, a matching label, a text that avoids a blocked claim). With `--apply` the measured order is written to `data/_shared/ai_rankings.json`; every later run reorders that job's list from it: measured providers best first, a provider that passed nothing left out of that job's list, providers not measured after them in their seeded order. A job that measured nothing keeps its earlier measured order.

## Every workflow uses the pool

Every workflow job that runs `python -m contentintel` sets up the pool with `.github/actions/ai-pool` and carries the provider secrets in its env, except jobs that never call a model (listed with the reason in `tests/test_ranking.py`). A test fails for any new workflow that has neither. A pool that cannot be set up never fails the job; the run prints why Claude answered.

## Not done yet

No provider has been measured per job yet: run the pool retest once the keys are in.
