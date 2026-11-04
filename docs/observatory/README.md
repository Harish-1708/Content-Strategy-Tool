# The research observatory (Stage 3)

It watches competitors politely, keeps dated evidence, tells you what changed, and builds a searchable corpus of public content (YouTube and any public page you point it at).
Everything runs from **Actions > Observatory** in GitHub. Nothing needs to be installed.

## What is verified, and how

| Word | Meaning |
|---|---|
| **tested with the real library** | run in the tests against the real Scrapling, in its own environment, talking to a local test server |
| **mock-tested** | run in the tests against scripted stand-ins for the outside service |
| **live-verified** | proved against the real service by the weekly acceptance run |

**Nothing in Stage 3 is live-verified yet.** The Acceptance workflow now has a check for every route; each route becomes trusted only when its check passes there, and the Collector Health tab shows which have. Until then treat every route as built but unproven.

Tested with the real library: honest identity by default, robots.txt handling, pauses and rests, Markdown without hidden text, adaptive selectors, impersonation, cookies, proxy use and rotation, and the authorised-access refusals.
Mock-tested only: the stealth browser and challenge handling (a browser cannot be installed in the build environment; the call is checked against a stand-in), Jina Reader, Exa, the YouTube Data API, GitHub Models, every sheet tab.

## First run

1. Add the secret **`OBSERVATORY_CONTACT`**: your business email address or the address of your contact page. The tool puts it in every request so a site owner can reach you, and refuses to read without it.
2. Optional secrets: `YOUTUBE_API_KEY`, `EXA_API_KEY`, `JINA_API_KEY` (all free).
3. Run **Observatory** with action `plan` (no request is made), then `discover`, then `monitor`. It also runs by itself every Monday.

## What it reads

For each competitor: the Shopify catalogue and collections (public product feeds), the sitemap, any RSS/Atom/product feed, and a few pages (home page, blog). Each read is saved as a dated snapshot. Differences between snapshots become events: new and removed products (a removal is confirmed on a second run first), price changes, sales starting, sold-out and back-in-stock, collection moves, new pages, changed headlines and announcements, changed offers. Unchanged days are not stored again. Old snapshots are thinned (daily, weekly, monthly), never the latest three.

`report` prints the alerts. The sheet gets four tabs: **Competitors**, **Observatory Events**, **Competitor Products**, **Public Corpus**, plus **AI Gateway Health** (the same on every sheet). Columns you add by hand (Decision, Notes) are never overwritten.

## Choosing competitors: 10 monitored, up to 6 added automatically

- At most **10** competitors are monitored per brand in total; at most **6** of those are added by discovery (`discovery.max_monitored`, `discovery.auto_monitor_max`).
- Discovery looks at what has already been collected (ad libraries, Instagram, TikTok, searches, benchmarks), scores each candidate on a rubric (how often it appears, how long its ads run, how many variants, how relevant its words are), finds its website (a search per name, within your search credit caps), and only then checks the site is a store we may read. A candidate scoring at least `auto_monitor_score` (65) is added automatically; everything is listed with its reasons in the sheet. Candidates you have not decided on are listed on the **Competitors** tab with their score and reasons. Type `monitor` or `stop` in its **Decision** column (or run the `decide` action), and the next sheet update reads it; `reset` hands the choice back to the rubric. A person's choice is permanent: automatic runs never change it. Competitors you list in a brand file are controlled by that file, not by the sheet.
- Add competitors yourself in `brands/<brand>/observatory.yaml`:

```yaml
competitors:
  - name: Elev8 Foods
    domain: elev8foods.com
    pages: [https://elev8foods.com/pages/about]
    blog: https://elev8foods.com/blogs/news
    youtube: "@elev8foods"
youtube:
  channels: ["@elev8foods"]
  queries: ["protein snacks"]        # used only with --find-channels; each search costs 100 of the free 10,000 daily units
```

## Politeness, and what "robots.txt" means here

By default the tool's own crawler **obeys robots.txt** (and treats an unreadable robots.txt as "closed"), names itself `KelsonResearchBot/1.0 (research; contact: ...)`, pauses at least 5 seconds per site (more if the site asks, doubling after any block), and stops a site for the run when it is rate-limited or shows a bot check.

robots.txt is a crawler preference, not an access control, so the policy is yours to set in `observatory.yaml` (`crawl_policy`):

- `robots: obey` (default) or `advisory` for the tool's own crawler. `advisory` needs a written `basis`; the page is read and the report says robots.txt asked otherwise.
- `reader_robots` for pages read through **Jina Reader**: `advisory` by default (read, and say so); `obey` skips them.
- `overrides` per site, each a recorded decision (host, basis, approved_by, date).

**A real restriction is never worked around under any policy:** a login page, a bot check or challenge, a refusal (401/403), a rate limit (429). The run stops for that site and says why. Jina Reader and Exa are third-party services that see the address you give them, so only public pages are sent, never an address that carries a credential.

## Advanced access (Scrapling and Agent-Reach capabilities)

Scrapling can impersonate a browser, run a stealth browser, handle Cloudflare challenges, change its fingerprint, use proxies and rotate them, and send a logged-in account's cookies. All of it is built, behind one gate, and available only for hosts you list under `authorised_access` in `observatory.yaml`: **sites and accounts we own or are explicitly authorised to access.** Used on someone else's site these methods would defeat their security controls, so for any host not listed the code refuses them, in the core and again inside the worker.

```yaml
authorised_access:
  hosts:
    - {host: ourstore.com, basis: owner, approved_by: Harish, date: "2026-11-03", methods: [fingerprint, proxy, challenge], note: "our store behind a bot shield"}
```

`basis` is `owner`, `client_authorised` or `written_permission`. Listing a host is a statement that this is true; the tool cannot check it. Methods: `stealth` (browser), `challenge` (solve Cloudflare checks), `fingerprint` (browser identity and canvas/WebRTC changes), `proxy`, `logged_in_browser` (cookies of an account we own).

Turn them on per competitor, in that brand's file:

```yaml
competitors:
  - name: Our Store
    domain: ourstore.com
    advanced: {impersonate: true, proxy: true, rotate: sequential}
    # stealth: true, solve_cloudflare: true, cookies_env: OBSERVATORY_COOKIES_OURSTORE
```

The tool refuses to run when a competitor asks for a method its host is not listed for. Proxy addresses and cookies are **secrets, never in a file**: list proxy names under `advanced.proxies` (`eu1`), add the secret `OBSERVATORY_PROXY_EU1`, and add one line passing it in the `env:` block of the Observatory workflow (`OBSERVATORY_PROXY_EU1: ${{ secrets.OBSERVATORY_PROXY_EU1 }}`). The worker receives only the secrets a job names, never your API keys.

Limits, stated plainly:
- **Stealth browser and challenge solving need browsers**: tick **browsers** when running the workflow. Their code path is mock-tested, not tested with a real browser.
- **Logged-in Instagram**: the existing Instagram collector, which uses an account we own, is unchanged and remains the supported route. Agent-Reach's own Instagram route needs OpenCLI and a shared browser with a person logged in to it, which a hosted runner does not have, so it is not built.
- Impersonation changes the client's identity: use it only where listed.

## The isolated Scrapling worker

`engine: {pages: scrapling}` (or the workflow's **scrapling** box) reads pages through Scrapling 0.5, installed unmodified into its own environment (`requirements-scrapling.txt`, pinned exactly; a different installed version is refused). You get cleaner Markdown, and per-competitor `selectors` (`announcement`, `headline`) that Scrapling can find again after a redesign. Do not switch engines casually: the page text changes shape once, which shows as one page-change event.

## The public corpus

- **YouTube** (`corpus --what youtube`, needs `YOUTUBE_API_KEY`): reads each channel's recent uploads through the official Data API, keeps view counts over time, ranks **outliers** as a multiple of the channel's own median (needs at least four videos), and matches titles to the 21 hook formulas. The tool keeps its own daily ceiling (3,000 units) under YouTube's free 10,000. Limit: the API gives no captions of other people's videos, so there is no spoken hook, only the title; every record says so. Shorts are a guess from the length.
- **Search** (`corpus --what search --query "..."`): one keyword search across collected ads and posts, search results, videos, competitor pages and blog posts.
- **Read** (`corpus --what read --url ...`): reads one public page through Jina Reader and adds it to the corpus.

## Free AI providers

Enabled, with your recorded Internal-data approval (not secret, may be used by providers' free plans for training; Restricted data, credentials, keys, passwords and tokens are never sent): Google, Groq, Mistral, OpenRouter, and **GitHub Models**.

- **GitHub Models** is free with the workflow's own token (the AI workflow now has `models: read`). Checked 2026-11-03 against GitHub's blog and secondary summaries: no card; small models 15 requests a minute and 150 a day, large models 10 and 50; 8,000 tokens in and 4,000 out per request. Only the two small-tier models are approved. **Paid inference must stay off in GitHub settings.** It is called directly, not through the gateway.
- **Exa** (free hosted search, and an API key with free credits) and **Jina Reader** (free, 20 a minute without a key) are on for research.
- **Not enabled, with reasons found by checking:** **Brave Search** (its free tier was retired in 2026; a card is required) and **Cerebras** (its no-card free tier ended 2026-07-21; the new credit needs a card). Other providers the gateway knows are still unverified and stay off until checked; the registry refuses to enable an unchecked one.
- Claude stays capped at **$0.50 a day and $10 a month**, enforced before each call.

## Not built yet

The AI judge for discovery (scoring is by rubric only; the setting is refused if turned on), Reddit, TikTok and Instagram competitor discovery beyond what the collectors already gather, trend velocity from the stored view histories, and checking the terms of the remaining free providers.

## When something fails

Every message names the route and the reason. `OBSERVATORY_CONTACT` missing: add the secret. `PLATFORM_BLOCKED` for a site: it refused or showed a bot check; the site rests and is tried again next run. `RATE_LIMITED`: a free limit was reached; it rests. `NEED_SETUP`: a key or environment is missing; the message says which. The **Collector Health** tab shows every route's last result.
