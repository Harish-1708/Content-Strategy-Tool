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
4. The tool also enforces its own cap in `ai_providers.yaml`: **$0.50 a day and $3 a month**. It refuses a call whose worst case would cross it. You can raise these two numbers later.

## 2. Groq (free)

1. Go to **console.groq.com**, sign up with a company email (no card is asked for the free plan).
2. **API Keys > Create API Key**. Name it `kelson-actions`. Copy it once.
3. Open **Settings > Data Controls** and turn on **Zero Data Retention**.
4. Add the key as the GitHub Secret **`GROQ_API_KEY`**.
5. Do **not** upgrade to the Developer plan (it asks for a payment method).

## 3. Mistral (free "Experiment" plan)

1. Go to **console.mistral.ai**, sign up with a company email.
2. **Admin > Subscriptions > Experiment for free** (phone verification is required; one phone number per plan). Read and accept the terms.
3. **Important:** Mistral says Experiment-plan requests **may be used to train its models**. Keep Mistral on **Public data only**.
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
