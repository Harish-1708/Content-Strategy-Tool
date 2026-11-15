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
| Apify as a voice lane | ON by default in voice research and competitors modes (`--no-apify`, or untick the Research tools box, switches it off): TikTok and Instagram scrapers asked about a topic, or about each competitor (the brand itself is not asked in competitors mode; research mode covers it). Needs APIFY_TOKEN; its own allowance of 60 items a month and 40 in a whole workflow run (shared by every brand and, in competitors mode, split between the competitors), never beyond the monthly Apify allowance in research.yaml (collectors.apify.monthly_items). Each call has a cost ceiling Apify enforces by stopping the call: 0.50 dollars for TikTok (its actor refuses less) and 0.25 for Instagram (27 usable items measured live); the spending is saved |
| Reddit posts into voice | Reddit posts the Observatory already collected (Actions > Observatory > reddit: the official API once Reddit approves the app, or the stored dummy-account session through Agent-Reach) are added to the voice with their score and comments; the run says plainly when none are collected yet |
| A page id check | Actions > Research tools > `pageid` with a page address (for example https://www.facebook.com/Chobani) tries the four places a page id can be read and prints what each answered, so the Page Plugin can be proven or disproved in one run |

What is still not done, and why: the page id and the Chobani decision. Reading ids from the page, the mobile address and the basic address gives a login page; the Page Plugin is untried on a live page until `pageid` has run once. If it gives no id either, the only remaining sources are a person choosing the page (research.yaml: `benchmark_pages: Chobani: <id>`) or an ad found for that page (the id is in its Ad Library address).


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
