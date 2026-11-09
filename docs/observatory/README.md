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

Tested with the real library: robots.txt checked under the honest identity, robots.txt handling, pauses and rests, Markdown without hidden text, adaptive selectors, impersonation, cookies, proxy use and rotation, and the refusals when an owner switch is off or a host has refused us.
Mock-tested only: the stealth browser and challenge handling (a browser cannot be installed in the build environment; the call is checked against a stand-in), Jina Reader, Exa, the YouTube Data API, GitHub Models, the Reddit API route, the Agent-Reach routes (Instagram, Reddit), the AI competitor judge, multi-source account discovery, every sheet tab.

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

## Shared services for the whole platform

Scrapling, Agent-Reach, web search, the AI Gateway and the page readers are **platform services**, not features of one module. Anything that needs one asks for it with a *purpose*, and you control them in one place: the `services:` block of `observatory.yaml`, the **Controls** tab on every brand sheet (edit a value and the next sheet update applies it), or `observatory controls` (`--what set --key K --value V`, `--what reset`). The workflow's **controls** action does the same from the GitHub page. `docs/platform/integration-matrix.md` lists, for every part of the project, which service is connected, which tests cover it, and exactly why nothing is live-tested; `docs/platform/adding-a-service.md` is the pattern for the next service or repository.

**Order of precedence** for a request option: the owner's central defaults, then any matching override (`match:` a site such as `*.example.com`, or `purpose:discovery`), then the target's own settings (a competitor's `advanced:` block). A service that is switched off is refused with a message that names the switch, and an owner's switch is never recorded as a failure of the route.

**Initial defaults** (everything supported is on; the owner can turn any single service, purpose or channel off, and back on, at any time):

| Service | Master | Purposes it may be used for | Per-channel switches | Routes |
|---|---|---|---|---|
| Scrapling | on | all 12 | none | no central request option set; no override |
| Agent-Reach | on | all 12 | instagram, reddit, youtube, twitter, facebook, linkedin, github, web, xiaohongshu, boss: on | none |
| Web search | on | all 12 | serper_dev, tavily, serpapi, exa, exa_mcp, agent_reach_reddit, agent_reach_youtube: on | none |
| AI Gateway | on | all 12 | none | every AI purpose: **gateway_first** (free providers first, Claude as fallback) |
| Page readers | on | all 12 | none | every purpose: **auto** (Scrapling first when installed, then Jina Reader, then the built-in reader) |

The 12 purposes are competitors, discovery, social discovery, content research, YouTube research, general research, idea bank, planning, brief, deep check, autoseeds and collectors. "On" means the owner's permission is on, and it is on everywhere. The defaults also **use** what is available: the AI Gateway is tried first for every language-model purpose (Claude answers when no free provider has a credential on that machine, or none gives a usable answer), and pages are read with `auto` (Scrapling first when its worker is installed, then Jina Reader, then the built-in reader). Any purpose can be sent back to Claude only, or to one reader, with a single control, and an explicit choice (or `--engine`) always wins over a default.

**How each interface behaves.**

- *Page readers.* `jina`, `core` and `scrapling` read one page each; `auto` tries Scrapling, then Jina Reader, then the built-in reader. An engine that is unavailable or fails for an ordinary reason passes the page on. **A refusal (blocked, rate limited, not found) ends the chain**: no other engine is ever tried after one. Used by `corpus --what read`, `deepread` (the pages behind your own search results), `channelpages` (a YouTube channel's About page and the accounts it links to) and social-account discovery.
- *Search.* One call returns the providers a purpose may use, filtered by your switches: Google-backed (Serper, SerpApi), Tavily and Exa, plus Reddit and YouTube search through Agent-Reach (on in every workflow through `AGENT_REACH_SEARCH=on`, and only answering where they can run: the Reddit one needs OpenCLI and a logged-in browser on a machine you control). research.yaml's web routing lists the Agent-Reach providers last, so they answer only when the others cannot.
- *AI Gateway.* A Claude-compatible adapter: each module keeps calling `complete`, `json_task` and the others. By default every purpose asks the free providers first (lanes `general_text` and `general_json`, Internal data at most), and if none has a credential here or none gives a usable answer, Claude answers exactly as before. Set a purpose's route to `claude` to keep it on Claude only. Picture analysis always goes to Claude: the free providers here have no vision. Each external free provider (Cloudflare, GitHub Models, Google, Groq, Mistral, OpenRouter) has its own switch, `services.ai_gateway.channels.<provider>`, on by default; Claude is always the fallback. What each provider may receive is a separate setting (its approved data classes in `ai_providers.yaml`) that this switch does not change.
- *Agent-Reach.* Ten channels, each with its own switch, per purpose, in `observatory instagram`, `reddit`, `agentreach` and as search providers: Instagram, Reddit, X (Twitter), Facebook, LinkedIn jobs, Xiaohongshu and Boss go through OpenCLI in a logged-in browser; **YouTube goes through yt-dlp, exactly as Agent-Reach itself does**, with no login; **GitHub** (search, repository README, issues) uses GitHub's public API with no login; **web** is the shared page readers. Four more Agent-Reach channels (Bilibili, V2EX, Xueqiu, Xiaoyuzhou) are not connected, for reasons given in `docs/platform/capability-matrix.md`, which also lists every action of every channel. `agentreach --ar-channel C --what A --query Q` (or `--url`, and `--param name=value`) runs any connected action. YouTube channel discovery (`corpus --what youtube --find-channels`) also searches through it, spending none of the Data API's units. The 40 actions Agent-Reach declares across its 14 channels, 25 of them connected, are listed one by one, with what each needs and why any is not connected, in `docs/platform/capability-matrix.md`.
- *YouTube research as a whole.* Video collection stays on the official Data API (Agent-Reach's YouTube channel has no channel-uploads listing). Channel discovery uses the API search plus Agent-Reach; channel About pages (`corpus --what channelpages`) go through the page readers and the accounts they link to are recorded on the competitor whose channel it is; search, read and subtitles go through `agentreach`; and About pages, search results and videos are all searchable with `corpus --what search`. `docs/platform/integration-matrix.md` lists each process and its evidence.
- *General research.* The `research` and `request` commands only search. The Research workflow has an optional **read_pages** step that reads the pages behind the new results through the page readers (`corpus --what deepread`); the workflow never chooses the reader, so your Controls decide.
- *Scrapling.* Request options and the browser, resolved per host and purpose (next section). It can also **crawl a site**: `corpus --what crawl --url U` (pages at most `--max-pages`, links deep at most `--depth`; the owner's `crawl:` settings are the defaults, 25 pages and 2 deep, with hard limits of 200 and 6). `--sitemap` also starts from the site's sitemap, `--resume` carries on a crawl that stopped early (page limit or a refusal) without reading anything twice, and `--extract name=kind:value` (kind: css, xpath, text, regex or similar) pulls named values out of every page with Scrapling's parser tools; the values are searchable with the page. `corpus --what feeds --url U` finds a site's RSS and Atom feeds. Every page is read through the same guarded fetch as any single page, stays on the start site, applies robots.txt under the crawl policy, and the pace slows down by itself for a site that answers slowly; a block, challenge or rate limit stops the crawl. Any module or repository can use these through `SV.scrapling_crawl` and `SV.discover_feeds`. What Scrapling offers and what is connected is listed in `docs/platform/capability-matrix.md`.

**Genuine limits and dependencies** (not restrictions we chose):

| Capability | Needs | Why |
|---|---|---|
| Agent-Reach, Instagram, Reddit, X, Facebook, LinkedIn, Xiaohongshu and Boss channels (and the Reddit search provider) | OpenCLI and a browser logged in to the site, on a machine you control | The login must stay on that machine and is never given to GitHub's hosted runners, which therefore refuse. With GitHub's hosted runners only, these cannot run live |
| Agent-Reach, GitHub channel | Nothing (public API) | 60 requests an hour per address without a token; the workflow passes the repository's own token, which raises that and is sent only to api.github.com |
| Agent-Reach, YouTube channel (and the YouTube search provider) | yt-dlp (already a dependency) | It needs no login or browser, so it runs on a hosted runner too. YouTube may show a bot check to data-centre addresses; that is reported as a refusal and not worked around, so it may not work from GitHub's machines |
| AI Gateway in a workflow | The gateway build step and its secrets in that workflow | Today only the AI Gateway and Tests workflows have them; elsewhere the default route finds no provider and Claude answers |
| Scrapling's browser path | The workflow's browsers box | A browser is a large download |
| Reddit's official API | Reddit's approval on record | Free access is non-commercial only |
| Anything live | A real account, key or authorised run | None has been exercised; see the matrix |

## Request controls (Scrapling): every capability is an owner setting

**All of these options are ON by default** (browser, session, proxy, TLS impersonation, canvas and WebRTC changes, cursor simulation, challenge solving, skipping images and fonts), as central defaults under `services.scrapling.defaults` that apply to every competitor, discovery and research read. Only you turn one off: set it to `false` there, in the Controls tab, or with `observatory controls`, and a competitor's own `advanced:` block or a matching override still wins for that target. Two degrade gracefully instead of failing: with no proxy listed the proxy default does nothing, and with no browser installed the page is read over plain HTTP (the result says so). If you switch a permission in `access_controls` off, the defaults that need it are skipped quietly, while an explicit request for it is refused with the reason. What this means in practice: pages read in the browser no longer announce our honest identity, so a site cannot tell who is reading it. robots.txt is still checked under our honest name, a refusal still rests the host with no retry, and private addresses are never read. Using these options can breach a site's terms, and that is your responsibility.

**There is no list of allowed hosts and no approval step.** Every control below is a plain setting you choose, for any website, on that competitor's own entry in `brands/<brand>/observatory.yaml` under `advanced:`. Nothing existing was removed. In `observatory.yaml`, `access_controls:` holds one on/off switch per kind of control (all on by default); switch one off and a competitor that asks for it is refused with a message, never quietly changed. To see what is switched on right now, run `observatory controls` (every competitor, every setting, secrets by name only) or look at the **request_controls** column of the Competitors tab.

| Control | Setting (`advanced:`) | What it does | Switch |
|---|---|---|---|
| Request identity | `identity: "Kelson-Research/2.0 (+https://...)"` | the User-Agent sent, instead of the default `KelsonResearchBot/1.0 (research; contact: ...)` | `custom_identity` |
| Headers | `headers: {X-Campaign: q4}`, `accept_language: en-GB` | added to every request, in the browser too. Not allowed: Cookie, Authorization, Via, X-Forwarded-For, Host and similar (use `cookies_env` for cookies; the others falsify where a request comes from) | `custom_identity` |
| Proxy and rotation | `proxy: true`, `rotate: sequential` or `random` | uses the proxies named under `advanced.proxies` in `observatory.yaml` (addresses are secrets named `OBSERVATORY_PROXY_<NAME>`), for the browser as well | `proxy` |
| Session | `session: true` | keeps the cookies a site sets between this competitor's requests, for the length of the run | `session` |
| Account cookies | `cookies_env: OBSERVATORY_COOKIES_SHOP` | sends an account's cookies from a named secret (JSON `{"name": "value"}`); never in a file; never in results or logs | `account_cookies` |
| Stealth browser | `browser: true` (`stealth: true` is the same) | reads the page in Scrapling's browser, for sites that need JavaScript | `browser` |
| Browser profile | `locale: en-GB`, `timezone: Europe/London`, `disable_resources: true`, `headless: false`, `network_idle: true`, `wait_selector: "#app"` | how the browser presents itself and when it counts a page as loaded | `browser` |
| Fingerprint | `impersonate: true` (TLS and browser identity), `hide_canvas: true`, `block_webrtc: true` | changes how the request or browser looks to fingerprinting. The older name `fingerprint: true` means canvas and WebRTC together | `evasion` |
| Cursor simulation | `humanize: true` | moves the cursor like a person in the browser | `evasion` |
| Challenge handling | `solve_challenges: true` (older name `solve_cloudflare`), `rest_hours: 12` | asks the browser to solve Cloudflare checks; and how long a host rests after a challenge, refusal or rate limit (longer than the standing rest, never shorter) | `evasion` |
| Pace | `delay: 20` | slower than the standing pause; never faster than the site's own Crawl-delay or the tool's minimum | none |
| Adaptive selectors | `selectors: {announcement: ".promo"}` | finds the element again after a redesign | none |

A `From:` header naming us is always sent, whatever the identity. robots.txt is read for the identity actually used, so a declared identity cannot dodge rules written for it.

```yaml
competitors:
  - name: Example Store
    domain: example-store.com
    advanced:
      browser: true
      locale: en-GB
      timezone: Europe/London
      identity: "Kelson-Research/2.0 (+https://kelson.example/research)"
      session: true
      proxy: true
      impersonate: true
      humanize: true
      solve_challenges: true
      rest_hours: 12
```

### Dependencies

Scrapling 0.5, pinned in `requirements-scrapling.txt`, in its own environment (the workflow's **scrapling** box creates it and **browsers** installs the browser); a proxy needs `advanced.proxies` and its secret; cookies need their secret; every one of `custom_identity`, `proxy`, `session`, `browser`, `account_cookies` and `evasion` must be on in `access_controls`.

### Limitations and risks, stated plainly

- **Not live-verified.** The browser path (browser profile, challenge solving, canvas and WebRTC changes, cursor simulation, cookies and proxy in the browser) is checked against a stand-in, not a real browser, because a browser cannot be installed in the build environment. HTTP behaviour (identity, headers, TLS impersonation, session, cookies, proxy rotation, pace) is tested with the real library against a local test server.
- **Nothing can retry past a refusal.** A challenge page, a 401/403 or a 429 rests that host for the run whatever options are set, and the rest is remembered between runs (`rest_hours` can lengthen it). Changing identity, rotating proxies or switching on any option above does not lift it. This is a technical safeguard in the worker, not an approval step.
- **Challenge solving and fingerprint changes can fail.** They depend on the site and on Scrapling; a site can change its checks at any time. A failed solve ends in the same rest as any refusal.
- **Your responsibility.** Getting past a site's bot detection, using another party's account, or ignoring robots.txt can breach that site's terms of use and, depending on the place and the data, the law. The tool does not check any of it and no setting here records anyone's approval. Decide per competitor, and keep a note of why (a comment in the file is enough).
- **Scrapling's browser** is itself built with anti-detection patches that cannot be switched off; the tool adds the further changes above only when you set them.
- **Cookies and proxy addresses are secrets**: a secret passed to a workflow is visible to that workflow's code. Use cookies only for accounts you are entitled to use, and prefer a machine you control for anything logged in.
- **Session and cookies are per run**; nothing is stored between runs except each host's rest.

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

## Finding competitors' websites and accounts from several sources

`discover` (unless `--no-enrich`, or `discovery.enrich: false`) looks for each strong candidate's Instagram account, and its website when it has none, in sources that are independent of each other:

| Source | What it gives | Weight |
|---|---|---|
| the existing Instagram collector | an account whose posts were already collected | 0.85 |
| the competitor's own home page (read politely, or through the Scrapling worker when `engine: scrapling`) | the site links to its own account (links and JSON-LD `sameAs`) | 0.90 |
| the account's profile bio (Agent-Reach) | the profile links back to the competitor's website | 0.95 |
| a web search for "<name> instagram" (Serper, Tavily, SerpApi or Exa, whichever the search settings allow) | an Instagram result whose handle resembles the name | 0.60, 0.50, 0.40 by rank |
| Agent-Reach Instagram account search | an account whose name resembles the competitor | 0.50 |

Independent evidence combines (one minus what each source leaves in doubt), so two weak agreeing sources can pass where one cannot. A handle is written to the competitor record only at `discovery.min_confidence` (0.7) or above, and only when it is clearly ahead of any other candidate account; otherwise it is kept as a **possible**, with its confidence and evidence, and shown in the sheet's note. An account that disagrees with one the existing collector already found changes nothing and is reported. A short single-word name needs a near-identical handle, so "Defi" does not match `defi.finance`. The Competitors tab shows `instagram_confidence` and `match_sources`. `--no-lookup` and `--no-probe` make no outside request at all; Agent-Reach is asked only on a machine you control, never a hosted runner. Discovery can never add a competitor by itself; it only fills in who a candidate is.

## The AI competitor judge

`discover` also asks the AI (lane `competitor_intelligence`, Internal data, the approved free providers, Claude as a capped fallback) for a second opinion on each strong candidate: is it a real competitor, how sure is it, what is the evidence, is it direct, adjacent or not a competitor. It sits beside the scoring rubric, which is unchanged.

- **It cannot add a competitor.** Only the rubric and a checked website can. It **can stop** an automatic addition (a confident "not a competitor", confidence 0.6 or more) and it adds its reasoning, confidence and evidence to the record and to the sheet's note.
- **A person reviews every verdict.** One candidate per call, so each verdict has its own review item. `observatory judge --what list` shows them; `--what approve --competitor <key>` or `--what reject --competitor <key>` records your decision (the key or the review id works). A rejected verdict is ignored from then on and not asked for again. An approved verdict is not replaced by a newer unreviewed one. The sheet's **Decision** column always overrides everything.
- Only the company's name, website, signals and short public snippets are sent; the snippets travel as fenced untrusted data, never inside the instructions. No credential, key or password is ever in a prompt.
- If no provider answers, the candidate keeps its rubric score and has no verdict; nothing is guessed. `judge --what run` judges the current candidates now; `discovery.judge: false` turns it off.
- Not live-verified: it has run only against a scripted stand-in for the AI layer.

## Instagram: two routes

The existing collector (browser and Apify) is unchanged. **Agent-Reach** (OpenCLI in a browser a person has logged in to) is a second route beside it, never a replacement: `observatory instagram --what collect | compare | check`. Both give the same post id (`instagram:<code>`), so a post found by both is stored once: the established route's values stand, Agent-Reach fills gaps, and a count that differs by more than 10% is noted. `compare` writes an advisory report; nothing is switched off by it. Settings: `instagram: {max_handles: 15, posts_per_handle: 12, timeout_seconds: 90}`; `OPENCLI_BIN` names the program. It refuses to run on a GitHub-hosted runner: the Instagram login must stay on a machine you control, and no cookie or password is ever passed to a hosted runner. OpenCLI's field names for Instagram are read through generic mappings and were not verified live.

## Reddit: two routes

`observatory reddit --what collect | compare | check`, with `--route all | api | agent_reach`, targets from `reddit: {subreddits: [...], queries: [...]}` in the brand's `observatory.yaml` or `--subreddit` / `--query`. Both routes give the same post id (`reddit:<id>`); the API's values stand and Agent-Reach fills gaps; each route has its own health record, so a failing one rests while the other carries on.

**Prerequisite for live API requests: Reddit's approval.** Reddit's Data API is free only for approved non-commercial use (about 100 requests a minute per client); commercial use needs a contract with Reddit (reported at $0.24 per 1,000 calls with a $12,000-a-year minimum), and new access requests were reported to have closed on 31 October 2026. These come from secondary reports; Reddit's own pages could not be opened when this was checked, so confirm them with Reddit. An agency's use is commercial, so the API route **makes no request at all** until you record Reddit's approval in `observatory.yaml`:

```yaml
reddit:
  api_approval: {basis: commercial_contract, approved_by: Harish, date: "2026-11-10", reference: "Reddit contract no. ..."}   # basis: non_commercial_approved or commercial_contract
```

It also needs the secrets `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (an app made at reddit.com/prefs/apps once Reddit has approved your access) and `OBSERVATORY_CONTACT` (Reddit requires a user agent that says who to contact). The Agent-Reach route reads through a logged-in browser, which may also be against Reddit's terms for automated use; that is your decision, and it runs only on a machine you control. Posts only: comment threads are not read. Neither route is live-verified.

## TikTok

The existing TikTok collectors (browser and Apify) are unchanged. **Agent-Reach currently has no TikTok route** (its channels are Bilibili, Boss, Facebook, GitHub, Instagram, LinkedIn, Reddit, Twitter, V2EX, web, Xiaohongshu, Xiaoyuzhou, Xueqiu and YouTube), so there is nothing to integrate for TikTok. `observatory reliability --platform tiktok` still compares the existing routes.

## Route reliability

`observatory reliability --platform instagram | tiktok | reddit` writes `<platform>-reliability.md`: for each route, the posts it collected, how many are usable, how complete the records are, what only that route found, and its success rate over 7 days. It says which route has been most reliable and which needs attention. It is advice only; no route is switched on or off.

## Social links, trend speed and housekeeping

- **Social links.** Each competitor's home page is read for the Instagram, TikTok and YouTube accounts it links to (links and JSON-LD `sameAs`; posts, share links and legacy URLs are ignored). A value you wrote in a file always wins over one found. One `social_found` event is recorded when a new account is found. The Competitors tab has `instagram`, `tiktok` and `youtube` columns.
- **Trend speed.** `corpus --what trending` ranks videos by views per day between readings (an age-based estimate for a recent video with one reading) against the channel's own median of at least three videos, and exports `youtube-trending.md`. Settings: `youtube: {trend_window_days: 7, trend_min_hours: 12, trend_min_multiple: 2.0}`. The Public Corpus tab has `views_per_day`, `trend_multiple` and `trend_basis`. `corpus --what search` accepts `--source` and `--since`.
- **Pruning.** `retention.auto_prune` (on by default) prunes old snapshots after each monitor run, using the retention settings.

## Security-sensitive restrictions

| Restriction | Where it is enforced |
|---|---|
| Every request control is the owner's setting; `access_controls` switches (identity, proxy, session, browser, account_cookies, evasion) turn each kind off, and a switched-off control is refused with a message | job builder and worker |
| A challenge, 401/403 or 429 rests the host; no change of identity, proxy or option retries it | worker (this run) and the pause ledger (between runs) |
| robots.txt is read for the identity actually used; `crawl_policy` can set it to advisory only with a recorded basis | worker and polite client |
| A pace can only be slower than the site's Crawl-delay or the tool's minimum | job builder and worker |
| Headers that carry credentials or falsify origin (Cookie, Authorization, Via, X-Forwarded-For, Host and similar) cannot be set | settings reader |
| Proxy addresses and cookies come only from named secrets, never a file, and never appear in results or logs | job builder and worker |
| A `From:` header naming us is always sent | worker |
| Agent-Reach routes (Instagram, Reddit) refuse to run on a hosted runner; Instagram and TikTok credentials are never passed to one | the OpenCLI wrapper |
| The Reddit API route makes no request without a recorded Reddit approval | the API client |
| The AI judge can stop an addition but never cause one; every verdict is reviewable; no credential reaches a prompt | merge and judge |
| Restricted data, credentials, keys, passwords and tokens are never sent to an AI provider | the AI router |

## Not built yet

Reading Reddit pages directly without either route (`reddit_public` stays planned), comment threads from Reddit, connecting Agent-Reach or Scrapling to the Meta ad collector (needs checking against the live Ad Library first) and to TikTok (Agent-Reach has no TikTok channel), and checking the terms of the remaining free AI providers.

## When something fails

Every message names the route and the reason. `OBSERVATORY_CONTACT` missing: add the secret. `PLATFORM_BLOCKED` for a site: it refused or showed a bot check; the site rests and is tried again next run. `RATE_LIMITED`: a free limit was reached; it rests. `NEED_SETUP`: a key or environment is missing; the message says which. The **Collector Health** tab shows every route's last result.
