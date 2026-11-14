# The shared pool, pacing and the token caps (Update 57)

## The shared pool

Every approved model is listed once, in one place: the `pool:` at the end of `ai_lanes.yaml` (42 models today: Groq, Mistral, OpenRouter, Cloudflare, Gemini, NVIDIA, Ollama, Cohere, Zhipu and one Kilo model). A model belongs to the pool, not to a lane. Every lane tries its own preferred routes first, then the shared pool in order, then Claude as the fallback. A new lane, a future stage (4 to 7), Stage B, the Observatory or the website needs no wiring to use any model: a lane with no routes of its own works from the pool alone. The three lanes that stay with Claude (strategic reasoning, brief generation, validation) say `use_pool: false`.

A pool route is used only when its provider is switched on, approved for the data class of the task, has a key (or needs none), is not resting and is within its own limits. To take a model out, delete its line from the pool; to add one, approve it in `ai_providers.yaml` and add a line.

## Pacing: the provider's own limit, never ours

Pacing is built once, in `contentintel/ai/pacing.py`, and used by the router for every call. It keeps each provider's own stated limits and nothing tighter: `requests_per_minute`, `requests_per_hour` and `concurrency` (Ollama allows one request at a time), plus a provider's own monthly call cap (`requests_per_month`, Cohere's trial key). The calls go at once up to the allowance and the tool never sends a call the provider would reject.

- A call that would have to wait more than `pacing.fast_wait_seconds` (2) is passed over: the next provider answers first, so load is spread. Only when no provider can answer sooner does it wait, for exactly as long as the provider's own window needs and never more than `pacing.max_wait_seconds` (120).
- **Retry-After:** when a provider answers 429 and says how long to wait, nothing more is sent to it until then, and it rests for exactly that long (not for the standard hour). With no Retry-After the standard rest applies.
- **NVIDIA's limit is a setting**, not a fact: sources say 30 to 40 requests a minute and it varies by model and load. `ai_providers.yaml` starts at `requests_per_minute: 30`; the account dashboard is the source of truth, so put its number there.
- The limits for Cohere, Zhipu, Kilo and Ollama are the providers' own stated figures, unverified until you sign up and run the check. None of them is a limit of ours.

## The token caps

Every lane's `max_tokens` is 8,000: a ceiling, not a target (a model stops when it is done, so a short answer costs what it costs). If a provider refuses a cap that large (a message about max tokens or a maximum), the router halves it (down to 1,024), remembers what that model accepts for the rest of the run and asks again, taking a new pacing slot each time. So no working route breaks because the caps are high.

One technical effect, stated plainly: for Claude, the daily and monthly spending cap ($0.50 a day, $10 a month, untouched) is checked before each call using the worst case of that call (the whole 8,000 tokens). With a higher cap the worst case is larger, so a Claude call can be held back slightly earlier as the day's spending nears $0.50. The cap is never crossed and actual spending is only for the tokens used.

## Internal data

Internal data goes to every enabled provider under your recorded decision of 2026-11-03 (and 2026-11-14), through experimental routes too; nothing in the code forbids it. Restricted data, keys and passwords are never sent (Claude only). Your switches, yours to use:

- **A provider:** Controls, `services.ai_internal_data.channels.<provider>` (ON by default for all ten). OFF: that provider receives Public data only and stays on.
- **One model:** in `ai_providers.yaml` add `internal: false` to the model's entry.
- **A whole provider off:** `services.ai_gateway.channels.<provider>`.

## Update 60: stale models, Kilo, and a withdrawn model resting alone

**What the live check of 14 Nov 2026 (AI gateway #21) showed.** 16 models answered. 17 were gone: NVIDIA 12 (ten answered HTTP 410 "end of life", two 404 "not found for this account"), Ollama 4 (410, retired between June and 9 November), Google 1 (`gemini-2.5-flash-lite`, 404 "no longer available to new users"). Those 17 are switched off in `ai_providers.yaml` (`approved: false`, with the date and the exact answer in each model's note) and taken out of every lane and the pool. The pool is now 25 models.

**The lane order is what worked before.** The lead routes of Update 57 (NVIDIA DeepSeek, NVIDIA Kimi, Ollama Kimi) were among the dead ones, so those lanes had been falling through to Groq. They now lead with the routes that worked in Update 56 again, and the Cohere reasoning model that answered live sits behind Groq in `formula_analysis` and `trend_opportunity` (Mistral, which has no key, is skipped).

**Why no replacements were added.** The gateway is pinned (v0.16.17) and only routes models in its own catalogue; every model that catalogue holds for NVIDIA and Ollama was already approved, and Google's replacement `gemini-3.5-flash-lite` is not in it. Newer models need a newer gateway pin: a separate decision, not taken here. To add a model later: it must be in `docs/ai/provider_inventory.json`, be approved in `ai_providers.yaml`, and answer in a live check.

**How to refresh next time.** Run Actions, AI gateway, live. A line that says model_not_found, 410, 404 "not found for this account" or "retired" is a withdrawn model: set its `approved: false`, add a dated note, delete its lines from `ai_lanes.yaml`. Models that failed only with "rate-limited" or "exhausted" are not shown to be gone.

**A withdrawn model now rests alone.** When a provider says a model is gone, that one model is not asked again for a day and its provider is not rested, so the provider's other models keep working. Any other failure still rests the provider as before.

**Kilo.** The gateway routes a platform only if it holds a stored key row; for a provider that needs no key it stores a `no-key` row when it is given a key entry without a key. Our gateway configuration gave Kilo no such entry, so it answered "no key configured for their platform". It now does, and the gateway smoke test has a ninth check ("a key-less provider has the gateway's stored no-key row"). The next live check shows whether Kilo answers; Kilo's own limit (200 an hour per address) still applies.

**Ollama.** One model answered with HTTP 402 ("add usage credits") and the gateway then put every other Ollama model on cooldown, so only gpt-oss:120b is known to need credits; the rest are not shown to be gone or to work. Run the live check again after the cooldown.

## Update 61: the model lists, Kilo, Ollama and the catalog tool

**Kilo (live check #22).** The key fix worked: the call now reaches Kilo. Kilo then answered 404 `model_not_found` for `nvidia/nemotron-3-super-120b-a12b:free`, so that model is switched off. The other three models of Kilo that the pinned gateway's catalogue holds (`poolside/laguna-m.1:free`, `stepfun/step-3.7-flash:free`, `poolside/laguna-xs.2:free`) are approved as **candidates**: they are free, need no key and have **not answered live yet**. The next live check decides; a model that is gone rests alone and Kilo keeps working. The pool is 27 models.

**Ollama: wait or replace?** Neither, by hand, and Ollama stays on in Controls. Live check #22 got HTTP 402 ("add usage credits") for `gpt-oss:120b` and the other six models were never tried (a long wait reported for that one call had blocked the whole provider). A 402 on one model does not prove the others are out of credits; only trying them tells us. So now: a model that answers **out of credits** rests for a day **on its own**, and a long wait reported for one model rests **that model only**; the provider's other models are still tried. A short wait (up to the longest wait, 120 seconds), whether the provider's Retry-After or the gateway's own "Soonest cooldown reset ~13s", is **waited for** before the next model is tried, so that call is a real try and not the gateway's cooldown answering for the provider. A model that fails is marked with the provider's exact words and the run moves on to the next one.

**A live check tries every model.** The AI gateway live check now ignores any rest an earlier run left (a model or a whole provider), because finding out which models work is its purpose; ordinary runs still leave a resting model alone. So the next live check tries all of Kilo's three candidates and all seven of Ollama's models, and prints each failure with its exact error. If every Ollama model fails with no quota, switch Ollama off in Controls (`services.ai_gateway.channels.ollama`) then; nothing is switched off for you.

**The catalog tool (refilling the pool).** The sandbox cannot reach the providers, so the list of what each provider really offers is read on GitHub: **Actions > AI gateway > Run workflow, tick "catalog"** (the command is `ai catalog`; live need not be ticked; no model is called; nothing is changed). It reads each provider's own list (Kilo, OpenRouter, NVIDIA, Groq, Mistral, Cohere, Ollama, Zhipu, Google; Cloudflare has no list address the tool knows) and prints, per provider: our approved models it does not list (probably withdrawn: confirm with the live check before switching one off), models it lists that the **pinned gateway can route** and we have not approved (candidates: approve one in `ai_providers.yaml` and add its line to the pool), and models it lists that the pinned gateway does **not** know. That last line is the answer to "do we need a newer gateway pin": it shows exactly what a newer pin would add. A pin change is a separate decision (it changes the gateway the whole AI layer runs on) and is not made here.

**Other provider limits seen in #22.** Cohere `command-r-08-2024` timed out once (15 s); a timeout is not a withdrawal and is not treated as one. Zhipu `glm-4.7-flash` answered this time.
