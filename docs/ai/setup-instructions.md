# Setting up the AI providers (GitHub only, no software to install)

Nothing here is done by the tool. Each account belongs to you or an organisation you control, and each step needs a person (a phone number, an email, accepting terms). Do them one provider at a time. **Do not add a payment card anywhere.** If a sign-up page asks for one, stop and tell me.

The order that makes sense: **1. spending limit on Anthropic, 2. Groq, 3. Mistral, 4. OpenRouter, 5. Gemini.** Cerebras and Cloudflare are optional.

## 0. Two secrets we generate ourselves

You need a 64-character key and a password for the gateway. You can make them in any web browser without installing anything:

1. Open any browser tab, press **F12**, choose **Console**.
2. Paste this line and press Enter. The result is your **encryption key** (64 characters). It is made inside your browser and sent nowhere.

   `[...crypto.getRandomValues(new Uint8Array(32))].map(b=>b.toString(16).padStart(2,'0')).join('')`
3. Paste this line for the **admin password** (48 characters):

   `[...crypto.getRandomValues(new Uint8Array(24))].map(b=>b.toString(16).padStart(2,'0')).join('')`
4. Store both straight away as GitHub Secrets (section 6). Do not save them in a document or message.

## 1. Anthropic (Claude): keep the balance safe

Your `ANTHROPIC_API_KEY` secret already exists. A Claude.ai subscription does **not** include API use; the API has its own balance in the Anthropic Console.

1. Go to **console.anthropic.com**, sign in, open **Settings > Limits** (or Billing).
2. Set a **monthly spend limit** of **$5** (half your balance) so that even a bug cannot spend more.
3. Make sure **auto-reload is off** (Billing).
4. The tool also enforces its own cap in `ai_providers.yaml`: **$0.50 a day and $10 a month**. It refuses a call whose worst case would cross it. You can raise these two numbers later.

## 2. Groq (free)

1. Go to **console.groq.com**, sign up with a company email (no card is asked for the free plan).
2. **API Keys > Create API Key**. Name it `kelson-actions`. Copy it once.
3. Open **Settings > Data Controls** and turn on **Zero Data Retention**.
4. Add the key as the GitHub Secret **`GROQ_API_KEY`**.
5. Do **not** upgrade to the Developer plan (it asks for a payment method).

## 3. Mistral (free "Experiment" plan)

1. Go to **console.mistral.ai**, sign up with a company email.
2. **Admin > Subscriptions > Experiment for free** (phone verification is required; one phone number per plan). Read and accept the terms.
3. **Read the terms and write down three answers (the same three for every provider; they go in that provider's `terms:` record in `ai_providers.yaml`):** **Does it train on prompts?** Mistral says Experiment-plan requests **may be used to train its models**. **Commercial use allowed?** Say it for your own use: this tool is a personal project (not a product, not sold, not offered as a service, no production deployment). **Is a card asked for?** If it is, stop and do not add the provider. Internal data goes to every enabled provider under your recorded decision; you can take it away from one in Controls (`services.ai_internal_data.channels.<provider>`) or from one model (`internal: false`).
4. Create a workspace, then **API keys > Create**. Add it as **`MISTRAL_API_KEY`**.

## 4. OpenRouter (free `:free` models)

1. Go to **openrouter.ai**, sign up.
2. **Keys > Create Key**. Add it as **`OPENROUTER_API_KEY`**.
3. Free models allow 20 requests a minute and **50 requests a day** unless you ever buy $10 of credits. Do not buy credits without approving it first.
4. Some free models are hosted by providers that log or train on prompts: **Public data only**.

## 5. Gemini (free tier)

1. Go to **aistudio.google.com** with a company Google account, **Get API key**. Add it as **`GEMINI_API_KEY`**.
2. Google says free-tier content may be used to improve its products and human reviewers may read it. **Public data only.** Clients in the EEA, Switzerland or the UK must use paid services, so do not use Gemini for them.

Optional: **Cerebras** (`CEREBRAS_API_KEY`, from cloud.cerebras.ai) and **Cloudflare Workers AI** (`CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN`). I have not verified their free terms; I will before they are switched on.

## 5b. NVIDIA, Zhipu, Ollama, Cohere (keys) and Kilo (no key)

Where each is, as of 2026-11-13 (screens change). For each one, step 3 is the same: **Does it train on prompts?** **Commercial use allowed?** **Is a card asked for?** (write the answers in its `terms:` record; if a card is asked for, stop and tell me).

**NVIDIA** (`NVIDIA_API_KEY`). 1. build.nvidia.com, sign in with an email and verify a phone number. 2. Open any model and choose **Get API Key**. 3. Terms: the free API is for prototyping, development and research (production needs an NVIDIA AI Enterprise licence). 4. Add the key as **`NVIDIA_API_KEY`**. The free limit varies by model and load (sources say 30 to 40 requests a minute): read the number in your account dashboard and put it in `limits: requests_per_minute` for nvidia (it starts at 30).

**Zhipu / Z.ai** (`ZHIPU_API_KEY`). 1. Use the international platform, z.ai (not bigmodel.cn), and sign up. 2. Create an API key. 3. Terms: **data is processed in China**; the record says so. The Flash models are free. 4. Add the key as **`ZHIPU_API_KEY`**. It publishes no rate limit: the 60 a minute in the file is a setting.

**Ollama Cloud** (`OLLAMA_API_KEY`). 1. ollama.com, create an account. 2. Settings, **Keys**, create a key. 3. Terms: Ollama says it does not log or train on prompts; a free account has a monthly starter amount on starter models only (which of the approved models that covers is shown by the check). One request at a time (`concurrency: 1`). 4. Add the key as **`OLLAMA_API_KEY`**.

**Cohere** (`COHERE_API_KEY`). 1. dashboard.cohere.com, sign up. 2. **API Keys**: the **trial key** is free. 3. Terms: a trial key is **not permitted for production or commercial use**: it is right for your personal evaluation and development, and a production key would be a later decision. Limits: 20 requests a minute, 1,000 calls a month (kept by the tool). 4. Add the key as **`COHERE_API_KEY`**.

**Kilo** (no key, no signup). Already switched on. Its free anonymous models are limited to 200 requests an hour per address and its own documentation says prompts may be logged for training; your standing decision covers it, and you can take Internal data away from it in Controls.

After adding a key, run **Actions > AI gateway > Run workflow** (check, live): each provider is reported on its own line as LIVE, FAILED or SKIPPED. A model a free tier does not serve fails and says so: delete its line from the `pool:` in `ai_lanes.yaml`.

## 6. Adding a GitHub Secret

Repository **Settings > Secrets and variables > Actions > New repository secret**. Name exactly as written, paste the value, save.

| Secret | What it is |
|---|---|
| `FREEAPI_ENCRYPTION_KEY` | the 64-character key from section 0 |
| `FREEAPI_ADMIN_PASSWORD` | the password from section 0 |
| `ANTHROPIC_API_KEY` | already exists |
| `GROQ_API_KEY` | section 2 |
| `MISTRAL_API_KEY` | section 3 |
| `OPENROUTER_API_KEY` | section 4 |
| `NVIDIA_API_KEY`, `ZHIPU_API_KEY`, `OLLAMA_API_KEY`, `COHERE_API_KEY` | section 5b (Kilo needs none) |
| `GEMINI_API_KEY` | section 5 |
| `CEREBRAS_API_KEY`, `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN` | optional |

## 7. Switching a provider on

A provider does nothing until you switch it on. In the repository open **`ai_providers.yaml`**, click the pencil, find the provider (for example `groq:`) and change:

```
enabled: false        ->   enabled: true
approved_classes: []  ->   approved_classes: [public]
```

Commit. The tool refuses to enable a provider whose terms are only declared and unchecked.

**To let a provider receive Internal data** (briefs, campaign results, audience insight), add a recorded decision under that provider, for example for Groq after you have read its agreement and turned on Zero Data Retention:

```
owner_override: {ceiling: internal, approved_by: "Your name", date: "2026-11-03", reason: "Zero Data Retention on; agreement read"}
approved_classes: [public, internal]
```

Free tiers that train on prompts (Mistral, Gemini) should stay Public unless you decide otherwise knowingly; the tool will do what you record, and the record stays in the repository's history.

## 8. Checking it works

**Actions > AI gateway > Run workflow.**

- Leave **live** unticked: this runs the **mock** checks (the real pinned gateway with a fake model). It proves our plumbing. It does not prove any provider works.
- Tick **live** only after the secrets exist and you have switched a provider on: it makes tiny real calls and reports **problems on our side** separately from **problems on the provider's side** (rate limits, outages, unusable answers). Tick **evaluate** to also run the sample tasks on the enabled free providers and compare them.

Costs: the free providers cost nothing; the live check uses a few Claude calls worth well under a cent each. `python -m contentintel ai usage` (or the run summary) shows today's spend against the cap.


## 9. Checking the upload

After uploading update 36, open the repository on GitHub and confirm:

- the folder `contentintel/ai/` exists and holds **14 files**: `__init__.py`, `clients.py`, `commands.py`, `coordinator.py`, `data_class.py`, `evaluate.py`, `gateway.py`, `lanes.py`, `registry.py`, `review.py`, `router.py`, `smoke.py`, `usage.py`, `validate.py`;
- `ai/gateway.lock.json` (a folder named `ai` at the top level, next to `contentintel`), the three files `ai_providers.yaml`, `ai_lanes.yaml`, `ai_eval_tasks.yaml` at the top level, and the folder `docs/ai/` with four files;
- `.github/workflows/ai-gateway.yml`.

If `contentintel/ai/` is missing, nothing else is affected: ordinary commands keep working and only the `ai` command says its files are missing. To add the folder through the GitHub website: open `contentintel`, choose **Add file > Create new file**, type `ai/__init__.py` as the name (typing the slash creates the folder), paste the file's text, commit; then open `contentintel/ai/` and use **Add file > Upload files** for the other 13.
