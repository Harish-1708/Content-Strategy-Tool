# Provider comparison and access assessment

Prepared 3 November 2026 for FreeLLMAPI v0.16.17. The machine-readable version is `provider_inventory.json`.

**How to read the status words.** *Verified*: read from the provider's own page on 3 November 2026. *Verified (secondary)*: checked the same day against the provider's documentation as quoted by several sites; a person must confirm the numbers in our own account before activation. *Upstream-documented*: stated in the gateway project's own documentation. *Declared only*: nothing but the gateway's claim. **No provider is approved, enabled or signed up by this document.**

## 1. Coverage

- 84 platform ids in the pinned version; 43 are chat providers per the upstream catalogue document; the rest are search, decision-extraction, media and other services. 17 ids are not described in that document and are recorded as unclassified.
- 108 model rows ship with the gateway across 15 platforms; every other platform's models arrive through a remote catalogue we have switched off.
- 4 providers were checked by me. **Mistral** was read from its own help page; **Groq's data-retention page** was read from Groq's own docs (its free limits and training clause are still secondary); **Google Gemini** and **OpenRouter** are secondary sources. Anthropic's API billing was verified from Anthropic's own support article. Everything else is declared only.

## 2. Priority providers

| Provider | Credential | Payment needed | Free allowance | Training on prompts | Business use |
|---|---|---|---|---|---|
| **Mistral** (`mistral`) | API key (Mistral Studio); phone verification for the free plan | no | recurring (standing free plan): about 1 request/second, about 500K tokens/minute, about 1B tokens/month per model; Mistral no longer publishes exact numbers: read them in Admin > Limits | yes: Mistral states Experiment-plan requests may be used to train its models (newer pages mention an opt-out toggle; treat as training until confirmed in our account) | free Experiment plan is for evaluation and prototyping, not production |
| **Groq** (`groq`) | API key (GroqCloud console) | not stated for the free tier; a payment method is needed to upgrade to the Developer tier | recurring free plan, limits per organisation: sample free-plan limits: openai/gpt-oss-120b 30 requests/minute, 1,000/day, 8K tokens/minute, 200K tokens/day; groq/compound 250 requests/day. The console limits page is authoritative | no: the Services Agreement says Groq may not use inputs or outputs for training unless the customer permits it | free plan is rate-limited for prototyping |
| **OpenRouter** (`openrouter`) | API key (OpenRouter account) | no for 50 requests/day; buying at least $10 of credits once raises the free-model cap to 1,000/day | recurring, request-capped: models whose id ends in :free: 20 requests/minute, 50 requests/day (1,000/day after at least $10 of lifetime credit purchases), per account across all keys; no token cap; a negative balance returns 402 even on free models | depends on the hosting provider: some free endpoints log or train on prompts. OpenRouter lets a request refuse such providers; not tested by us | free models are described as unsuitable for production |
| **Google Gemini** (`google`) | Google account + AI Studio API key | no | recurring free tier: per-model limits are no longer published; they are shown only per project in the AI Studio rate-limit console | yes on the free tier: content may be used to improve Google products and human reviewers may read it; the paid tier does not (terms effective 2026-03-23) | free tier is for prototyping; clients serving the EEA, Switzerland or the UK must use paid services |

Not verified by me: **Cerebras** and **Cloudflare Workers AI** (optional candidates). The gateway ships 4 Cerebras and 10 Cloudflare model rows with its own declared limits; confirm the current free entitlement before use. Cloudflare needs an account id plus an API token (stored as `account_id:token`).

**Consequence for data classes.** Every verified free tier either trains on prompts (Mistral, Gemini), may log or train depending on the hosting provider (OpenRouter), or has retention documentation that contradicts itself (Groq). So all of them are **Public only**. Nothing Internal or Restricted may use a free route. Internal data needs a paid tier with a written no-training term, which we would approve provider by provider.

## 3. No-key routes (experimental, isolated)

| Route | Facts (upstream documentation) | Rule |
|---|---|---|
| Kilo | anonymous `:free` routes, 200 requests/hour per IP, prompts and outputs logged for training | Public data only, experimental, never in a business lane |
| OVHcloud AI Endpoints | anonymous tier about 2 requests/minute per IP per model; the authenticated tier needs a project with a payment method | experimental |
| AI Horde | community volunteer workers behind a queue; anonymous key | assume everything sent is public; experimental |

## 4. Claude (the coordinator) and billing

Anthropic's own support article states that a paid Claude subscription "doesn't include API usage through the Console". The API is a separate Console account with its own payment method and credit balance. **The coordinator interface is therefore built against a mock and nothing requires funded API use until you approve it.** (The existing `ANTHROPIC_API_KEY` secret is already how Claude is called today; its billing is separate from any claude.ai plan.)

## 5. Every chat provider in the pinned version

Status is "declared only" unless stated above. Notes are the gateway project's own words, shortened.

| Id | Auth (upstream) | Upstream note |
|---|---|---|
| `google` | Keyed | Gemini wire format; 60s default timeout because Gemma reasoning variants take 20–60s on cold start. |
| `groq` | Keyed | Standard compatible endpoint at `api.groq.com/openai/v1`. |
| `cerebras` | Keyed | `api.cerebras.ai/v1`. |
| `sail` | Keyed (payment method required for recurring credit) | Stable `/v1/responses` integration submits background jobs and polls them to completion; flex-only models use the `flex` completion window. Sail grant |
| `electronhub` | Keyed | Shared weekly credit pool; validates against `/v1/user/me`, rejects HTTP-200 proxy-error banners. Funding and integration notes. |
| `experiential` | Keyed | Shared monthly credit pool; authenticated `/v1/models`, model-specific Claude sampling compatibility. Funding and account-verification notes. |
| `bai` | Keyed | Gateway added in #918; only catalog row is a limited-time 0-credit promo, kept in the hosted catalog. |
| `anyapi` | Keyed | Free tier $0/no card/recurring, capped at 100K tokens/day over "free and basic" models; no RPM/RPD published (#772). Model ids arrive via catalog-sync |
| `radeon` | Keyed | Shared Model API at `developer.amd.com.cn/radeon/api/v1`; public models are free without consuming instance credits and use a rotating experimental ro |
| `nvidia` | Keyed | `parallel_tool_calls` pinned false (#255); 180s timeout doubles as streaming first-byte grace (#584). |
| `mistral` | Keyed | `api.mistral.ai/v1`. |
| `sambanova` | Retired (V23) | Free tier permanently gone; kept in the type union for historical key rows. |
| `openrouter` | Keyed | Sends `HTTP-Referer`/`X-Title` headers; `:free` model suffix marks zero-cost routes. |
| `cohere` | Keyed | Uses Cohere's OpenAI-compatible endpoint. |
| `cloudflare` | Keyed (`account_id:token`) | Compound credential format parsed by the adapter. |
| `zhipu` | Keyed | Console autodetect: a rejected domestic key is re-probed against the global `api.z.ai` host; 60s timeout covers glm-4.7-flash hidden reasoning. |
| `ollama` | Keyed | 120s timeout for frontier reasoning models; reasoning returned in `message.reasoning`; catalog filtered to confirmed-free rows. |
| `kilo` | Keyless | Anonymous `:free` routes rate-limited 200 req/hr per IP; prompts/outputs logged for training; validation probes `/api/gateway/models` (#181). |
| `pollinations` | Keyed | `GET /v1/models` answers 200 even for revoked keys, so validation probes the authenticated `/account/key` instead (#608). |
| `llm7` | Keyed (anonymous works for basic models) | 100 req/hr free tier. |
| `huggingface` | Keyed | `router.huggingface.co` meta-router (re-added V13); recurring $0.10/mo router credit on the free tier. |
| `opencode` | Keyed | Free roster is locked to the OpenCode client since 2026-09 (403 FreeTierError, #1249) and disabled in the catalog; paid models only. |
| `ovh` | Keyless | Anonymous tier: 2 req/min per IP per model (observed stricter); authenticated tier requires a Public Cloud project with payment method on file (`migra |
| `agnes` | Keyed | Proprietary models served at $0/token promotionally; ~30 concurrent requests before 429s; 60s timeout for reasoning TTFB. |
| `reka` | Keyed | No free tier for new accounts since 2026-09: prepaid credits required (#1202). Accounts that still hold credit keep working; balance dashboard-only. |
| `siliconflow` | Keyed | Registered mainly for FREE generative-media models (FLUX.1-schnell image, CosyVoice2 TTS) routed via `services/media.ts`. |
| `routeway` | Keyed | Requires browser-style User-Agent (Cloudflare rejects others with error 1010); free pool observed stricter (~5 rpm) than the documented 20 rpm / 200 r |
| `bazaarlink` | Keyed | Only the `auto:free` route is cataloged — direct model ids are paid (#385). |
| `ainative` | Keyed | Advertises recurring ~10M tokens/month free allocation; quota treated as unverified until confirmed by a real account. |
| `aion` | Keyed | No-card free key; availability catalog-managed behind the 30-day age gate. |
| `requesty` | Keyed | Router endpoint at `router.requesty.ai/v1`; free rows age into the monthly catalog; the free tier is 200 requests/day shared across all free models. |
| `navy` | Keyed | Free plan: 150K tokens/day and 20 RPM; live smoke tests require an explicit User-Agent header. |
| `nara` | Keyed | Free plan additionally requires Telegram channel/link verification; live-probed 2026-07-09. |
| `sealion` | Keyed | First-party API; Google sign-in, no card, no region wall; 10 RPM recurring free tier. |
| `orcarouter` | Keyed | Recurring `$0` free aliases with intentionally unpublished limits (#896): 429 is a clean quota signal because free routes never fall back to paid mode |
| `unorouter` | Keyed | Free models carry `:free` suffix; per-minute rate limit (429 on cap). `GET /v1/models` public (200 no key) but 401 on wrong key; default key validatio |
| `xkiro` | Keyed | Free plan: 5M tokens/day on free models (Mistral, MiniMax, DeepSeek families); paid models 403. `GET /v1/models` public (200 no key), so `validateUrl` |
| `modelscope` | Keyed | Calls require binding to an Alibaba Cloud CHINA-site account with real-name verification; `GET /v1/models` accepts garbage tokens so validation uses a |
| `qianfan` | Keyed | ERNIE-Speed/Lite/Tiny free indefinitely via pay-as-you-go billing bounded by rate limits; Chinese real-name auth required (#936). |
| `volcengine` | Keyed | Recurring daily per-model reward quota of 2M tokens/day/model for individual developers, on top of a one-time 500K new-user grant; real-name auth requ |
| `longcat` | Keyed | Daily free quota; exception among the Chinese providers — email signup works from outside mainland China. Also speaks Anthropic wire at `/anthropic` ( |
| `xfyun` | Keyed | Auth is the console APIPassword as a Bearer token; the Lite model is the documented free one; no published token/QPS ceiling (#936). |
| `aihorde` | Keyless (anonymous sentinel `0000000000`; registered key raises queue priority) | Community volunteer workers via queue-based proxy: max_tokens >= 16, stop must be array, no tools, usage reported as kudos, 120s timeout, no upstream  |

Providers worth flagging from those notes: Cohere, NVIDIA (the project's own terms review marked them avoid or evaluation-only); Chinese-platform providers (ModelScope, Qianfan, Volcengine, xFyun) need real-name verification; Reka and OpenCode Zen no longer have a usable free tier; SambaNova's free tier was retired.

## 6. What the free tiers can carry (assumptions, not quotas)

Assumed monthly volume for three brands, public data only: **classification** 2,400 calls (about 900 tokens in, 120 out each); **summarisation** 600 calls (2,500 in, 400 out); **image review** 300 calls; **creative briefs** stay on Claude (about 12 briefs, 20,000 tokens in and 4,000 out each).

| Route | Fits? | Reasoning |
|---|---|---|
| Groq free | classification and summarisation fit | 3,000 calls and about 4M tokens a month against 1,000 requests/day and 200K tokens/day per model: roughly 6M tokens/month at the cap, so it fits with little headroom |
| Mistral free | fits on volume | about 1B tokens/month, but trains on prompts: public data only |
| OpenRouter `:free` | does not fit unfunded | 50 requests/day is about 1,500/month, below the 3,000 calls; a one-time $10 purchase lifts it to 1,000/day |
| Gemini free | unknown | limits are visible only in our AI Studio console |
| Claude briefs | cost depends on current pricing | not yet verified; run a measured month before estimating |

These are planning figures, not guaranteed allowances.

## 7. Secret names and where each comes from

Create nothing until approved.

| GitHub Secret | Comes from |
|---|---|
| `FREEAPI_ENCRYPTION_KEY` | we generate it: 64 hex characters (`node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"`) |
| `FREEAPI_ADMIN_PASSWORD` | we generate it: a random password of at least 8 characters |
| `MISTRAL_API_KEY` | console.mistral.ai > Admin > Subscriptions > "Experiment for free" > create a workspace > API keys |
| `GROQ_API_KEY` | console.groq.com > API Keys > Create |
| `OPENROUTER_API_KEY` | openrouter.ai > Keys > Create |
| `GEMINI_API_KEY` | aistudio.google.com > Get API key |
| `CEREBRAS_API_KEY` (optional) | cloud.cerebras.ai |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` (optional) | Cloudflare dashboard > Workers AI |
| `ANTHROPIC_API_KEY` (exists) | console.anthropic.com; separate from claude.ai billing |

## 8. Recommended initial allowlist, and what is switched on

**Switched on at present (read from `ai_providers.yaml`): cloudflare, github, google, groq, mistral, openrouter, and claude.** Everything else is off. The six free providers receive Public and Internal data under the owner's recorded decision of 2026-11-03; Restricted data goes to Claude only. The list below was the original recommendation and is kept for the reasoning; where it says Public only, the owner's decision since then allows Internal.

1. **Groq**: first candidate for bulk classification and summarisation of public text.
2. **Mistral**: second; Public only because it trains on prompts.
3. **OpenRouter `:free`**: third; Public only; useful as a fallback.
4. **Gemini**: Public only; learn the real limits from our console first. Not for any client in the EEA, Switzerland or the UK.
5. Optional: Cerebras, Cloudflare, once verified.
6. **Kilo, OVH, AI Horde**: experimental, isolated, never in a business lane.
