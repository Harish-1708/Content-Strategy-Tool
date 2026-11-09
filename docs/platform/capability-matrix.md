# Scrapling and Agent-Reach: capability matrix and owner-control defaults

What each library can do in the versions this platform uses (**Scrapling 0.5**, and **Agent-Reach's 14 channels** as read from its own channel files), what the platform has connected, what is on by default, and why anything is not connected. Nothing here was live-tested: every connection is checked by automated tests with the real Scrapling library or with stand-ins for the outside world, never against a real third-party site or account. The wider picture (which module uses which service) is in `integration-matrix.md`.

How to read the **Default** column. *On* means the capability is on by default: the permission switch is on **and**, for Scrapling's per-request options (browser, session, proxy, impersonation, fingerprint changes, cursor simulation, challenge solving, skipping images and fonts), the central request default is on too. The owner turns anything off, or back on, in the Controls tab, with `observatory controls`, or in `observatory.yaml`; nothing is off unless the owner turns it off. Two defaults degrade instead of failing: with no proxy listed the proxy default does nothing, and with no browser installed a page is read over plain HTTP (the result says so). A permission the owner switches off quietly skips the defaults that need it, while an explicit request for it (an override or a competitor's own setting) is refused with the reason, as before.

## Scrapling 0.5

| Capability | What it is | Implemented | Connected to the shared interface | Default | Needs or limit |
|---|---|---|---|---|---|
| HTTP page fetch | `Fetcher.get`, polite, honest identity | Yes (`contentintel/observatory/scrapling_worker.py::Worker.one`) | `contentintel/services.py::scrapling_page`, `read_page` | On | The isolated worker environment (`SCRAPLING_PYTHON`, which the workflow's Scrapling box creates) |
| Per-run cookie session | `FetcherSession` | Yes | via request options | On | none |
| TLS and header impersonation | `impersonate`, `stealthy_headers` | Yes, under the `evasion` permission | request options | On | none |
| Custom identity and headers | `headers`, declared identity | Yes (`custom_identity` permission) | request options | On (the permission); the identity text is blank, which means the honest default | none |
| Proxies and rotation | `proxy`, `proxy_rotator` | Yes (our own rotation, `proxy` permission) | request options | On | Your own proxy addresses, held as secrets |
| Stealth browser fetch | `StealthyFetcher.fetch` | Yes (`contentintel/observatory/scrapling_worker.py::stealth_kwargs`) | request options | On | A browser installed in the worker environment (the workflow's browsers box); no hosted default installs it |
| Browser profile and speed options | `locale`, `timezone_id`, `disable_resources`, `network_idle`, `wait_selector`, `headless` | Yes | request options | On | Browser |
| Browser fingerprint and behaviour options | `hide_canvas`, `block_webrtc`, `humanize` | Yes, under `evasion` | request options | On | Browser |
| Challenge solving | `solve_cloudflare` | Present as an owner option under `evasion` | request options | On, by the owner's decision | Browser. The worker never retries past a refusal, but solving an access challenge is the one option that goes toward bypassing a site's protection; **kept ON by the owner's decision**; turn it off with `services.scrapling.defaults.solve_challenges`. It is a request to the browser to solve a site's challenge page, and the site's terms may forbid that, which is the owner's responsibility |
| Account cookies | `cookies` | Yes (`account_cookies` permission, named secrets only) | request options | On | Cookies you supply |
| Markdown conversion | `response.markdown()` | Yes | every page result | On | none |
| Adaptive selectors | `adaptive`, `auto_save`, relocate after a redesign | Yes (competitor `selectors`) | competitor monitoring | On where a competitor lists selectors | none |
| Same-site link discovery | `LinkExtractor` | Yes (new, used by the crawl) | `scrapling_crawl` | On | none |
| Site crawl to Markdown | breadth-first crawl of one site, optionally started from its sitemap | **Yes**: `contentintel/observatory/scrapling_worker.py::Worker.crawl`, built on the guarded fetch | `contentintel/services.py::scrapling_crawl`; command `corpus --what crawl` | On; `crawl.max_pages` 25 (1 to 200), `crawl.depth` 2 (0 to 6) | It stays on the start site, applies robots.txt under the crawl policy, paces itself, never repeats a page, and stops at the first block, challenge or rate limit |
| Spider engine and templates | `Spider`, `CrawlSpider`, `ContentSpider`, `SitemapSpider`, `ShopifySpider`, feed spiders, auto-throttle, pause and resume, development cache | **Not used, deliberately; the useful parts are rebuilt on the guarded fetch** (crawl, sitemap seeding, auto-throttle, pause and resume, below) | none | not applicable | The engine follows redirects and links internally, so it would bypass the guarded fetch (public-address check on every redirect, robots policy, host rest after a refusal). The platform's own Shopify, sitemap and feed collectors already cover the Shopify and feed templates, and the development cache has no use here. Revisit only if the engine gains per-request hooks |
| Parser extraction | `css`, `xpath`, `find_by_text`, `find_by_regex`, `find_similar` | **Yes, new**: `contentintel/observatory/scrapling_worker.py::Worker._extracted` | `scrapling_crawl(extract=...)`; command `corpus --what crawl --extract name=kind:value` | On; none unless asked | A bad selector is reported for that value and never loses the page |
| Other parser tools | `below_elements`, `find_ancestor`, `siblings`, `generate_css_selector` and the like | Available, unused | none | not applicable | Low relevance now: pages are understood through JSON-LD, the built-in parser and the extraction above |
| Sitemap discovery | the pages a site lists in its sitemaps | **Yes, new**: `contentintel/observatory/scrapling_worker.py::Worker._sitemap_urls` | `scrapling_crawl(sitemap=True)`; `corpus --what crawl --sitemap` | On | robots.txt's Sitemap lines and `/sitemap.xml`; index files followed to 5 children (8 files, 2000 addresses); every file read through the guarded fetch; compressed (.gz) files skipped. Competitor monitoring keeps its own sitemap collector |
| Feed discovery | RSS and Atom feeds of a site | **Yes**: `contentintel/observatory/worker_client.py::discover_feeds` | `contentintel/services.py::discover_feeds`; command `corpus --what feeds` | On | Feeds the page advertises plus feeds at the usual addresses that really answer with a feed. Competitor monitoring keeps its own feed discovery |
| Auto-throttle | pace follows how fast the site answers | **Yes**: `contentintel/observatory/scrapling_worker.py::Worker._latency_floor` | every worker read | On (`autothrottle` job option, default on) | A site that answers slowly is asked less often (twice its average answer time, within the maximum delay); never faster than the configured pace |
| Pause and resume | continue a crawl that stopped early | **Yes**: state kept in `data/_shared/crawl_state/` by `contentintel/services.py::scrapling_crawl` | `scrapling_crawl(resume=True)`; `corpus --what crawl --resume` | Kept after an early stop (page limit or a refusal); removed when the crawl finishes | Nothing already read is read again; a plain crawl always starts from the beginning |
| HTTP POST, PUT, DELETE | other methods | Not used | none | not applicable | The platform only reads public pages; it submits no forms and logs in nowhere |
| Async fetchers and sessions | `AsyncFetcher`, async sessions | Not used | none | not applicable | The worker is sequential on purpose: one request at a time per run keeps the load on a site gentle, and the platform reads few pages |
| Browser automation callbacks | `page_action`, `page_setup`, `init_script` | Not exposed | none | not applicable | They run arbitrary code in the browser; a job file passes data only, never code |
| Saved browser state and real browsers | `save_state`, `load_state`, `user_data_dir`, `real_chrome`, `cdp_url`, `extra_flags`, `allow_webgl`, `additional_args`, `blocked_domains` | Available, unused | none | not applicable | They serve logged-in sessions or a local Chrome; no relevance to polite public reads |
| Command line and AI/MCP extras | `scrapling extract`, shell, `scrapling[ai]` | Not applicable | none | not applicable | They are for interactive and agent use; the platform calls the library directly (these were read from the package's extras list, not examined in depth) |

## Agent-Reach (14 channels)

"Connected" means the platform runs the channel's actions through the one shared client (`SV.agent_reach_client`), the `agentreach` command and a per-channel owner switch, all **on by default**. The catalogue that drives this is `contentintel/agent_reach_catalog.py`, and a test checks it against Agent-Reach's own channel files.

| Channel | Actions Agent-Reach offers | Connected actions | How it runs | Default | Needs or limit |
|---|---|---|---|---|---|
| instagram | search, profile, posts | search, profile, posts | OpenCLI | On | OpenCLI, Node, Chrome and a logged-in browser on a machine you control; never a hosted runner |
| reddit | search, read, subreddit | all three | OpenCLI | On | same |
| youtube | search, read, subtitle, transcribe | search, read, subtitle | yt-dlp (no login) | On | YouTube may bot-check data-centre addresses (reported, not worked around). Transcribe needs a speech model and ffmpeg and is not offered |
| twitter (X) | search, profile, tweets, thread | all four | OpenCLI | On | OpenCLI and a logged-in browser |
| facebook | search, profile | both | OpenCLI | On | same |
| linkedin | jobs, read | both (job postings only) | OpenCLI | On | same; the channel reads jobs, not company pages or posts |
| xiaohongshu | search, read, comments | all three | OpenCLI | On | same |
| boss | search, read | both | OpenCLI | On | same. Low relevance (a Chinese job site) |
| github | search, read | both | GitHub public REST API (`contentintel/github_reach.py::GitHubPublic`) | On | No login; 60 requests an hour per address without a token, more with `GITHUB_TOKEN` (the workflow passes the repository's own token; it is sent only to api.github.com) |
| web | search, read | read | the platform's shared page readers (`contentintel/observatory/commands.py::_agentreach_web`) | On | Agent-Reach's web channel is Jina Reader and Exa, which are already shared services; search is the shared search providers |
| bilibili | search, read, hot, subtitle | none | not connected | not applicable | Needs the separate bili-cli program; a Chinese video platform with no use for the brands served |
| v2ex | hot, node, read, user | none | not connected | not applicable | A Chinese-language developer forum; no use for the brands served (its public API needs no login, so it can be added from `services_extra.yaml`) |
| xueqiu | quote, search, hot_posts, hot_stocks | none | not connected | not applicable | Chinese stock quotes and chatter; most actions need a site session token |
| xiaoyuzhou | transcribe | none | not connected | not applicable | Chinese podcasts transcribed with Groq's Whisper (a Groq key and ffmpeg); no use for the brands served |

### Every action of every channel (generated from the catalogue and checked against Agent-Reach's own files)

| Channel | Action | Inputs | Connected | Runs as | Needs | Why it is not connected |
|---|---|---|---|---|---|---|
| bilibili | search | query, limit | No | - | the bili-cli program (or the bilibili-api package) | it needs the separate bili-cli program that this platform does not install, and the platform's brands and languages have no use for it; add it as a service from services_extra.yaml if one ever does |
| bilibili | read | target | No | - | the bili-cli program (or the bilibili-api package) | it needs the separate bili-cli program that this platform does not install, and the platform's brands and languages have no use for it; add it as a service from services_extra.yaml if one ever does |
| bilibili | hot | limit | No | - | the bili-cli program (or the bilibili-api package) | it needs the separate bili-cli program that this platform does not install, and the platform's brands and languages have no use for it; add it as a service from services_extra.yaml if one ever does |
| bilibili | subtitle | target | No | - | the bili-cli program (or the bilibili-api package) | it needs the separate bili-cli program that this platform does not install, and the platform's brands and languages have no use for it; add it as a service from services_extra.yaml if one ever does |
| boss | search | query, city, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| boss | read | target | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| facebook | search | query, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| facebook | profile | username | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| github | search | query, limit | Yes | GitHub public API | nothing (public REST API: 60 requests an hour without a token; GITHUB_TOKEN raises it) | - |
| github | read | target | Yes | GitHub public API | nothing (public REST API: 60 requests an hour without a token; GITHUB_TOKEN raises it) | - |
| instagram | search | query, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| instagram | profile | username | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| instagram | posts | username, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| linkedin | jobs | query, location, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| linkedin | read | target | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| reddit | search | query, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| reddit | read | target | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| reddit | subreddit | name, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| twitter | search | query, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| twitter | profile | username | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| twitter | tweets | username, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| twitter | thread | target | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| v2ex | hot | limit | No | - | nothing (public API) | not connected because it has no use for the brands served; its public API needs no login, so it can be added later through services_extra.yaml |
| v2ex | node | node_name, limit | No | - | nothing (public API) | not connected because it has no use for the brands served; its public API needs no login, so it can be added later through services_extra.yaml |
| v2ex | read | target | No | - | nothing (public API) | not connected because it has no use for the brands served; its public API needs no login, so it can be added later through services_extra.yaml |
| v2ex | user | username | No | - | nothing (public API) | not connected because it has no use for the brands served; its public API needs no login, so it can be added later through services_extra.yaml |
| web | search | query, limit | No | - | nothing extra: served by the platform's shared page readers and search providers | use the shared search providers (the research command); Agent-Reach's web search is Exa, which is one of them |
| web | read | target | Yes | shared page readers | nothing extra: served by the platform's shared page readers and search providers | - |
| xiaohongshu | search | query, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| xiaohongshu | read | target | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| xiaohongshu | comments | target, limit | Yes | OpenCLI | OpenCLI with Node and Chrome, and a browser logged in to the site, on a machine you control; never a GitHub-hosted runner | - |
| xiaoyuzhou | transcribe | target | No | - | ffmpeg and a Groq key for Whisper transcription | it transcribes Chinese-podcast audio with Groq's Whisper, which needs a Groq key and ffmpeg and has no use for the brands served |
| xueqiu | quote | symbol | No | - | a Xueqiu web session token for most actions | stock quotes and investor chatter on a Chinese exchange have no use for the brands served, and most actions need a site session token |
| xueqiu | search | query, limit | No | - | a Xueqiu web session token for most actions | stock quotes and investor chatter on a Chinese exchange have no use for the brands served, and most actions need a site session token |
| xueqiu | hot_posts | limit | No | - | a Xueqiu web session token for most actions | stock quotes and investor chatter on a Chinese exchange have no use for the brands served, and most actions need a site session token |
| xueqiu | hot_stocks | limit | No | - | a Xueqiu web session token for most actions | stock quotes and investor chatter on a Chinese exchange have no use for the brands served, and most actions need a site session token |
| youtube | read | target | Yes | yt-dlp | yt-dlp (installed by the workflow); no login; YouTube may bot-check data-centre addresses | - |
| youtube | search | query, limit | Yes | yt-dlp | yt-dlp (installed by the workflow); no login; YouTube may bot-check data-centre addresses | - |
| youtube | subtitle | target, lang | Yes | yt-dlp | yt-dlp (installed by the workflow); no login; YouTube may bot-check data-centre addresses | - |
| youtube | transcribe | target | No | - | yt-dlp (installed by the workflow); no login; YouTube may bot-check data-centre addresses | needs a Whisper speech model and ffmpeg (compute a hosted runner should not spend); subtitle already returns the video's own captions |

## Owner-control defaults (verified from the registry, not assumed)

`tests/test_owner_defaults.py` checks every line below against the code and the shipped `observatory.yaml`, and checks that turning each of the 81 permission switches off blocks exactly that capability and turning it back on restores it.

| Control | Default |
|---|---|
| Every service master switch (Scrapling, Agent-Reach, search including Google (Serper), AI Gateway, page readers) | On |
| Every purpose, for every service (competitors, discovery, social discovery, content research, YouTube research, general research, idea bank, planning, brief, deep check, autoseeds, collectors) | On |
| Every channel: all connected Agent-Reach channels, and every search provider | On |
| Scrapling permissions (`access_controls`: custom identity, proxy, session, browser, account cookies, evasion) | On |
| AI Gateway route, every AI purpose | `gateway_first`: the free providers first, Claude when none can answer. With no free-provider credential on the machine nothing is tried and Claude answers as before |
| AI Gateway providers (Cloudflare, GitHub Models, Google, Groq, Mistral, OpenRouter: every external provider enabled in `ai_providers.yaml`) | Each has its own owner switch (`services.ai_gateway.channels.<provider>`), on by default. Claude is the fallback and has no switch here. Availability is not permission to send data: what each provider may receive is set by its approved data classes in `ai_providers.yaml` (Internal for these six, by the owner's decision; secret data never), and no setting in this update widens that |
| Page reader route, every purpose | `auto`: Scrapling first when its worker is installed, then Jina Reader, then the built-in reader. An explicit owner route (or `--engine`) always wins, including over the workflow's Scrapling box |
| Per-request Scrapling options (`services.scrapling.defaults.*`: browser, session, proxy, impersonate, hide_canvas, block_webrtc, humanize, solve_challenges, disable_resources) | **On.** The numbers and texts (delay, rest hours, locale, timezone, identity, accept-language) are blank, which means the standing pace and the honest identity |
| Agent-Reach search providers in research | Last in the web search order; on in every workflow (`AGENT_REACH_SEARCH=on`); they only answer where they can run |
| Site crawl | `crawl.max_pages` 25, `crawl.depth` 2 |
| Reddit's official API route | Refuses until Reddit's approval is recorded in `observatory.yaml` (`reddit.api_approval`); that is Reddit's term, not a default we chose |

Every module reads these through the shared interfaces, so a setting changed once applies to competitor monitoring, discovery, general and content research, YouTube, the social channels and any other repository that installs the package. A module-specific override (a competitor's own `advanced:` block, or an `overrides:` entry matched by purpose or host) is optional and, when given, wins for that target only.

## Owner decision recorded

The owner decided that Scrapling's `solve_cloudflare` option (the `solve_challenges` control) stays, and that every feature is on by default, with only the owner turning anything off. With browser identity options on, a browser-read page no longer announces our honest identity, so a site cannot see who is reading it; robots.txt is still checked under our honest name, a refusal still rests the host with no retry, and the tool still refuses private addresses. Using these options against a site can breach its terms, and that is the owner's responsibility.
