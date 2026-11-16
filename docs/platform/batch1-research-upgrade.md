# Update 64: the Batch 1 research upgrade

What was added, where it plugs in, and how to switch it off. Every external repository is a connection with the owner's four controls (ON/OFF, route, version, secrets) in the Controls tab; the platform's own version stays as the backup.

| Tool | Connection | Used where | Backup if it fails or is switched off |
|---|---|---|---|
| Audience voice: what people say, with real engagement | `last30days` (last30days-skill) | the `voice` command and the Research tools workflow; kept per brand; sent to the scoring prompts, briefs, research-topic suggestions and the competitor view | the platform's own Hacker News and Reddit reader |
| Video facts: views, likes, sound, captions, who posted, outlier against the poster's followers, top comments | `yt_dlp` | read for each video item before it is scored; shown in the Idea Bank columns outlier and sound; sent to scoring and briefs | no video facts (cover pictures and page text only) |
| Video pace: shot length, cuts in the first seconds, loudness, first sound | ffmpeg (the platform's own) | measured on each video as it is saved to Drive; column pace; sent to scoring; briefs state the common pace of the approved videos | not measured |
| Free AI capacity candidates | `omniroute_catalog` (data only) | the `pool` command lists providers worth adding; nothing is changed | the snapshot in ai/omniroute-free-catalog.json |
| Motion formats from 703 prompts | `opus_video_prompts` (data only) | briefs get up to three close formats, with authors credited | the snapshot in reference/motion-prompts.json |
| Licence-safe pictures and sounds | Openverse service | the `assets` command; kept per brand and offered to briefs with their credit line | Wikimedia Commons (pictures) |
| Second reader of Meta Ad Library pages | `fb_ads_library_mcp` | after the first reader, on the same page: fills the headline strip and landing address when missing, and supplies ads only when the first reader found none | the first reader alone |
| Better matching of a brand to its Facebook page | (method from last30days-skill, re-written) | the main-page finder ranks pages whose name carries the brand's name above lookalikes and leaves a lookalike choice to a person | the earlier ranking |

## What was not proven

Nothing here has been run against the live internet: no live last30days run, no real yt-dlp output from TikTok, Instagram or Facebook, no real Openverse call, and the second Ad Library reader has not read a real page. Each row in docs/platform/integration-matrix.md says "Not live-tested" for the same reason. The first Research tools run, a Collect run and an Idea Bank run on GitHub will show what they do for real.

## The first runs to make

1. Actions > Research tools > tool `install`, then `voice` for one brand, then `analyze` with one public TikTok address.
2. Actions > Collect > the usual run: the Idea Bank now reads video facts before scoring and measures each saved video's pace.
3. Actions > Research tools > `pool` to see the free-capacity candidates.

## Update 65: what the live round of Update 64 showed, and what changed

| Finding in the live runs | What changed |
|---|---|
| Every voice item came from the platform's own reader: the repository's engine never answered, and the run did not say why | Each failure now carries the engine's own words, the run prints the reason, and it prints which keys the engine was given (names only) and whether SCRAPECREATORS_API_KEY is set |
| The topics asked about were 'core', 'merch' and 'kids' (the shop's own category labels) | Topics are the brand's own search phrases first (research.yaml keywords, auto seeds), then real category names; generic labels are never used |
| Competitors mode showed one competitor with one item and did not say whom it asked | It asks about the followed benchmark brands (never the skipped ones, never the brand itself), lists them, and reports every one, including those with nothing said |
| Audience voice was generic Hacker News news | The comments viewers wrote under videos already read (the top comments yt-dlp returns) are added as voice, with their likes: real audience words from TikTok, Instagram and YouTube, with no key |
| Pace was never measured on GitHub: the runner has no ffmpeg | The Collect, Research tools (analyze) and Tests workflows install ffmpeg; `analyze --pace` downloads the video (720p at most, 60 MB at most) and measures how it moves |
| The ScrapeCreators allowance was stated as 10,000 free calls | It is 100 free credits once, then paid from about 10 dollars a month; stated so everywhere |
| Page ids could not be read from the page, its mobile address or its basic address | Facebook's public Page Plugin (the embed any website may show) is tried as a fourth place |
| The competitor cap (6 added automatically, 10 in all) | Removed: the defaults and observatory.yaml are the settings' own maximums, 50 and 100; lower numbers still work if set |
| A lane could not say what its job needs, so a model that cannot do it could be tried | A lane may declare `needs` (text, vision, tools); a model that cannot do it is never tried for that job, and if none can the run says so plainly |

Found afterwards, in the health file of the live round (data/_shared/connections_health.json): the engine had never started at all. The run started it from inside its own folder with a script path that was relative to the repository, so Python looked for the file inside the folder a second time (".../external/last30days/external/last30days/skills/..."). The platform now uses an absolute path, and a test runs the real subprocess from a relative folder (the old tests mocked the runner and could not see this). Whatever the engine does when it really starts, Reddit refusing and YouTube showing a bot check included, is reported in words by the run.

| Also added | What it does |
|---|---|
| Instagram's login page recognised | browser.yaml: Instagram's `Log into Instagram` page and its `email` field now count as signed out even when the header looks signed in; in Collect #59 that page was in the debug capture while the read looked normal (12 items against the usual 30). A login wall now stops the read and is reported as NEEDS YOU, as for any other account problem |
| Apify as a voice lane | `voice --apify` (the Research tools box 'apify'), research mode only as shipped in Update 65: TikTok and Instagram scrapers asked about a topic; off unless asked; needs APIFY_TOKEN; its own allowance of 40 items a month and 15 a run in Update 65's first form, never beyond the Collect step's monthly Apify allowance; each call capped at 0.25 dollars. The live round showed TikTok refusing that cap and the lane not running in competitors mode: both are corrected in Updates 66 and 67 (see below) |
| Reddit posts into voice | Reddit posts the Observatory already collected (Actions > Observatory > reddit: the official API once Reddit approves the app, or the stored dummy-account session through Agent-Reach) are added to the voice with their score and comments; the run says plainly when none are collected yet |
| A page id check | Actions > Research tools > `pageid` with a page address (for example https://www.facebook.com/Chobani) tries the four places a page id can be read and prints what each answered, so the Page Plugin can be proven or disproved in one run |

Page ids: the page, its mobile address and its basic address often give a login page; the page itself, asked with the tool's own honest identity, gives the id, and the Page Plugin is tried as well. When none gives an id the finder searches for it and the AI reviewer confirms it; nothing is ever entered by hand.


## Update 66: what the live round of Update 65 showed

| Finding in the live runs | What changed |
|---|---|
| The engine started: the path fix is proven live. Pace was measured live (slow pace, 8.8 s average shot on the Swoveralls video). The page id for Chobani was read live from the page and from the Page Plugin (the same id, 100064549155148) | Nothing to change; these are now proven |
| TikTok through Apify failed: its actor refuses a cost ceiling below 0.50 dollars; Instagram read 27 usable items for 0.25 | Per-actor ceilings: 0.50 for TikTok, 0.25 for Instagram. An override from the environment is clamped between the actor's own minimum and one dollar, so it can neither fail the actor nor spend freely |
| The Apify lane did nothing in competitors mode, and said nothing | The lane runs in competitors mode too: it asks about each competitor (never the brand itself), splitting the run's allowance between them, and tags each item with its competitor |
| The summary showed 'used so far this month: 0' after 27 items were read | That line is printed at the start and shows the earlier runs (the run before had read nothing, since its key was rejected). The run now also prints an end line with this run's reading and the month's totals (voice and all Apify use) |
| The regional hint missed lower-case 'chobaniau' and flagged the plain name 'MadeIn' | With the brand's name (the Research tools topic box for `pageid`) the hint is exact in any letter case; without it only unambiguous signs are used (a region after a separator, an upper-case tail, a Capitalised tail that is not an ordinary word). A lower-case address cannot be told from a plain name such as snuggie without the brand |
| A failed ffmpeg install was swallowed | The install is retried three times and the step, and so the run, fails if ffmpeg and ffprobe are not there |
| Stale ApifyVoice text and defaults (off by default, 15 and 40) | The class describes and defaults to what it does (on, 40 and 60) |

Finding a page id without a person (Hu Kitchen and Dude Wipes, where none of the four places gives an id): when the page's own HTML has no id, the finder searches for the page's numeric id (Serper: people addresses, profile.php addresses, Ad Library `view_all_page_id` links), and the AI reviewer must confirm that one of the ids found is the SAME main page (not a regional page, a fan page or another brand). The id has to be one of the candidates the search found, so an invented id is refused; if nothing is found or confirmed the brand stays unresolved and the reason says what was tried. Nothing is added by hand. `pageid` (Research tools) also lists the numeric ids a search finds, so this can be seen on a live brand before Collect uses it. Not proven live: whether Serper's results for these two brands carry such addresses.


## Update 67: what the live round of Update 66 showed

| Finding | What changed |
|---|---|
| Hu Kitchen and Dude Wipes: `pageid` read the id straight from the page (100064252197325 and 100064866812951), while the Collect finder never did in any run since Update 62 | The finder fetched the page with a pretend-Chrome identity and got a login-gated page without the id; `pageid` uses the tool's own honest identity (`Mozilla/5.0 (compatible; content-strategy-tool; read-only research)`) and gets the public page (1.2 million characters). The finder now uses the same honest identity, and its reason now says how many characters each address returned |
| The keyed ScrapeCreators lanes never showed in voice results | The engine runs TikTok and Instagram as bonus lanes outside the plan's own source list (it rewrites a plan's sources), so a lane that returns nothing never appeared in the source status; Meta Ads also needs its own opt-in (`INCLUDE_SOURCES=meta_ads`), which the platform now sets when the key is handed over. The run now prints the engine's own account (research quality, bonus sources unavailable or returning nothing) |
| Several Apify keys | `APIFY_TOKEN`, `APIFY_TOKEN_2`, `_3`, `_4` (the same key twice counts once): a key that is rejected (401/403) or out of credit is dropped for the run and the next one answers. Failover only: the allowances count Apify as a whole |
| The run's allowance could be stranded when one competitor read less, the lane was dropped when the engine failed, the usage line printed once per brand | What is left is split between the competitors still to ask; the lane runs even when the engine and the backup fail; the usage is saved after every ask; the end line prints once per run |
| Smaller points from the review | The brand's own address is never flagged regional however it is punctuated (made-in for the brand Made In); the id search always runs all three queries, shows the reviewer exactly the candidates it is checked against, and survives a reviewer that fails; no-reviewer and no-brand-name situations are said in words; the ffmpeg step now checks explicitly and fails the run; the pageid name is validated; the lane clamps its own limits |

Counters: the Apify and voice counters are in `data/_shared/research_usage.json`, which the Research tools workflow commits after voice and assets runs. TikTok asks for half of what is left to read and Instagram for the rest (Instagram gets the whole ask if TikTok is refused); limits are 1 to 300 items per run and 1 to 600 per month; the end line reads "Apify usage after this run: voice X/60 items this month, all Apify use Y/300 (this run read N)". The live analyze run measured -21.8 LUFS on the Swoveralls video.

### The Apify budget: three keys, 15 dollars a month, every month its own
- Each key carries Apify's free 5 dollars a month; three keys (APIFY_TOKEN, APIFY_TOKEN_2, APIFY_TOKEN_3) make 15 dollars. The monthly item allowance of the Collect step (research.yaml `collectors.apify.monthly_items`) is 300 (it was 100 for one key), the voice lane's own is 60 items a month per key (180 for three), and one workflow run reads at most 40 items per key (120 for three).
- Before any Apify run the run prints the money Apify itself says is left, per key and in total (its account limits endpoint: `Apify budget this cycle (...): $X left of $15.00 across 3 key(s)`). A key with less left than a call's cost ceiling is not asked and the next key answers; the lane does not ask at all when the total left is below the ceiling. If Apify does not report the money, the run says so and the item counters alone guard the spending. The endpoint's reply was written from Apify's documentation and has not been read live yet.
- Every month is its own budget, nothing is carried forward: the counters in `data/_shared/research_usage.json` are kept per calendar month and start at zero, and what Apify does not spend in its own monthly cycle lapses. Nothing here borrows from the next month.
- Collect #61 (real summary): the finder followed Defi Snacks, Magic Spoon, Chobani, DudeRobe and Snuggie by ids read from the page with the earlier browser identity, while Hu Kitchen, Lily's Sweets and Dude Wipes gave none; `pageid`, asking with the tool's honest identity, read ids for the last two. The finder now asks the page with the honest identity first and with the earlier browser identity second, so what worked keeps working and what only the honest identity reads is added.
