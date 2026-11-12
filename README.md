# Content strategy tool: catalogue foundation and research

Everything runs on GitHub. Nothing is installed or run on a computer of your own.

## What it does

- Every 6 hours, reads each brand's Shopify catalogue (`/products.json`, page by page until
  an empty page), checks that it is complete, and saves the result in `data/`.
- Detects new, changed, sold-out, restocked, suspected-removed and returned products.
- Records what each product description states (fabric, pockets, ingredients, allergens)
  and what is missing. Nothing is invented.
- Keeps each brand's own Google Sheet up to date automatically (see below), and clean files
  in `data/<brand>/catalogue/` for other tools.
- Blocks generation (later phases) whenever a catalogue cannot be trusted.
- Every Monday, searches the web for each brand through three search services (Serper.dev,
  Tavily and SerpApi), inside credit caps you set, and shows the findings in a Research tab in
  each brand's sheet (see "Research" below).

## One-time set-up (GitHub website, plus the Google Cloud console for the sheets)

1. Upload the project files to the repository, keeping the folder paths.
2. Upload the five saved catalogues to `tests/fixtures/` (they are supplied already named):
   `swoveralls_full_72.json`, `swoveralls_truncated_30.json`, `duderobe_fetch_a.json`,
   `duderobe_fetch_b.json`, `defi_snacks.json`.
3. Check `domain` in each `brands/<brand>/brand.yaml`.
4. If a run fails with a permission error while saving: Settings > Actions > General >
   Workflow permissions > Read and write.
5. Optional alerts: add a repository secret `SLACK_WEBHOOK_URL`.

The **Tests** workflow runs by itself on every push. Green means the project is sound, and
it also validates any edit you make to a `brand.yaml` or `rules.yaml`.

## First run for a brand

1. Actions > **Catalogue sync** > Run workflow (brand `all`, or one brand). The first run
   ends red on purpose: "needs baseline". Open the run page for the report.
2. Check the product count on the store.
3. Actions > **Catalogue admin** > Run workflow: action `confirm-baseline`, the brand, and the
   count you checked. Your GitHub username is recorded as the confirmer.
4. From then on the sync runs every 6 hours and you only look at red runs.

## Google Sheets (one-time set-up)

One Google service account serves every brand. Each brand gets its own spreadsheet; tabs and
columns are created automatically, and any that go missing are recreated on the next run.

Create the three spreadsheets yourself. A service account usually cannot create files:
Google gives it no Drive storage, and the error is "The user's Drive storage quota has been
exceeded". Spreadsheets in your own Drive have no such limit, and the service account only
edits them.

1. In the Google Cloud console, create **one new service account** for this tool (do not reuse
   the outreach one), enable the **Google Sheets API** for its project, and create a JSON key.
2. In GitHub, Settings > Secrets and variables > Actions, add a repository secret
   `GOOGLE_SERVICE_ACCOUNT_JSON` containing the whole key file. Never paste it anywhere else.
3. In Google Sheets, create three blank spreadsheets, for example "Swoveralls Catalogue",
   "DudeRobe Catalogue" and "Defi Snacks Catalogue". Share each one with the service account's
   address (the `client_email` inside the key) as **Editor**.
4. In GitHub, open `sheets.yaml`, paste each spreadsheet's link after its brand name, and commit.
5. Actions > **Google Sheets** > Run workflow with **dry run** ticked. The run page lists what it
   would create. Then run it again with dry run unticked.

After that, every Catalogue sync updates the sheets by itself.

Optional, to skip steps 3 and 4: if your company has Google Workspace Shared Drives, make a
folder in a Shared Drive, add the service account to that Shared Drive as a member who can add
files, and set two more secrets: `SHEET_FOLDER_ID` (the folder's link or id) and
`SHEET_SHARE_EMAIL` (your Google address). The tool then creates each sheet in that folder and
shares it with you. This route has not been tried against a live account.

### What is in each sheet

| Tab | Contents |
|---|---|
| Products | one row per product; added columns on the right start with **Notes** |
| Variants | one row per size/colour: options, SKU, price, sale price, available |
| Changes | every detected change, appended once |
| Benchmarks | appears once benchmark brands have been looked up: which Meta advertiser page each one means, how it was decided, and the candidate pages for any brand that needs your decision |
| Status | whether the catalogue can be trusted now, when it last synced, what needs facts |

Two columns are the tool's own: **`last_updated`** (the UTC time the tool last changed that
row; on a sheet that existed before the column appeared, every row shows the time of the first
run after it did) and **`missing_expected`** (the facts a product is still missing, for example
`nutrition_facts`). Change entries name the variants that sold out or came back.

Rules that protect your work:
- The tool owns only the columns it created. **Any column you add is never read, overwritten
  or moved**, wherever you put it. Do not rename the tool's own headers (it would add them
  again).
- Rows are matched by `product_id` / `variant_id`, never by position, so sorting and
  filtering are safe.
- New products are added at the bottom. A product that leaves the catalogue keeps its row and
  its status changes to `suspected_removed` or `confirmed_removed`; its variants show
  `removed`.
- Only cells whose value changed are written. A run with nothing new writes only the Status tab.
- The Changes tab is a log: rows you delete there are added back on the next run. Hide or
  filter them instead, or add your own column to mark them as dealt with.
- If the tool finds a tab with content but no `product_id` column, it writes nothing there
  and says so.

## Decisions as buttons (Actions > Catalogue admin)

| Action | When |
|---|---|
| `confirm-baseline` | after the first sync, with the count you checked |
| `confirm-removed` | a product was flagged "suspected removed" and really is gone |
| `reset-baseline` | retire the baseline on purpose (the next clean sync must be confirmed again) |
| `status` | can generation use this brand's catalogue right now? |

## Research (Actions > Research)

Three search services, one rule: never spend more credits than you allow.

**Set-up, once.** In GitHub, Settings > Secrets and variables > Actions, add a secret for each
service you have a key for. Any one is enough; each extra gives the tool more room.

| Secret name | Service | Get a key at |
|---|---|---|
| `SERPER_API_KEY` | Serper.dev (Google results) | serper.dev |
| `TAVILY_API_KEY` | Tavily (rich results, news with dates) | app.tavily.com |
| `SERPAPI_API_KEY` | SerpApi (Google results, and its own balance check) | serpapi.com |

Mind the names: `SERPER_API_KEY` is Serper.dev. SerpApi is spelt differently and has its own secret.

**What each free plan gives** (checked October 2026; plans change, so re-check): Serper.dev gives
2,500 free searches once, when you sign up. Tavily gives 1,000 credits every month. SerpApi gives a
small monthly free plan (its changelog says 250; some pages still say 100).

**How credits are protected.** All of this is set in `research.yaml`, which you can edit in GitHub.
- **Caps:** a monthly cap per service (and a lifetime cap for Serper.dev's one-time pool). Defaults
  keep Serper.dev lasting about ten months and leave most of Tavily and SerpApi untouched.
- **A shared count:** every search is counted (`data/_shared/research_usage.json`), across all
  brands. For Tavily and SerpApi the tool also asks the service how many credits are really left.
- **Routing:** web searches go to Serper.dev first, news searches to Tavily first. When one
  service is at its cap or out of credits, the next one takes the search. A search that nobody can
  afford is skipped, never forced.
- **Reuse:** a search made in the last 7 days is reused, not paid for again.
- **A safety limit** on searches per run, and a **dry run** that shows the plan and which service
  would take each search without spending anything.

**What it searches.** Out of the box, only the brand's own name (three searches a week: reviews,
competitors, TikTok). Add competitors, topics and exact searches in `brands/<brand>/research.yaml`.
Each competitor or topic becomes a few searches (a web search and a recent-news search), so a short
list goes a long way.

**Running it.** It runs by itself every Monday. To run it now: Actions > Research > Run workflow.
"Dry run" is ticked by default when you run it by hand; untick it to search for real. You can
untick a service to leave it out of that run.

**Proving each service works.** In Actions > Research > Run workflow, tick **Provider check**
(and leave the services you want ticked). It runs one web search and one news search on each
ticked service, shows whether each worked, and counts the credits (about two per service; Dry run
is ignored for this). Day to day, web searches go to Serper.dev and news searches to Tavily, so
SerpApi only steps in as a fallback or when you tick it alone; that is by design, not a fault.
If a check says "answered but returned no results", the bracket after it shows what the service
actually sent back, which tells a genuinely empty answer from a misread one. The news check uses
the broad word "overalls" over the last year, so an empty answer there deserves a closer look.

**Comparing the three services.** Normally each search goes to one service. With **Compare** ticked
(it is ticked by default when you run Research by hand), each of the first few searches (5 by
default, `compare_queries` in `research.yaml`) is run on every ticked service, so you can see how
they differ. The report shows, per search, how many results each service gave and how many only it
found, then an overall summary of how much each pair of services agrees. That costs one credit per
service per compared search (about 15 credits for the default), still inside every cap. Answers
from the last 7 days are reused, so repeating a comparison is free. The weekly automatic run never
compares.

**Reading the results.** Each brand's sheet gets a **Research** tab: one row per page found, with
when it was first and last seen, how often, which service and which search found it, and a
**Review** column for your team (for example useful, irrelevant, saturated). Review marks are never
overwritten. Addresses are shown without tracking codes (Google adds a new one to every result),
so the same page found next week is the same row, with "times seen" going up. The raw findings are in `data/<brand>/research/searches.jsonl`.

**If a run goes red:** a service rejected its key (the log names it; check the secret), or searches
failed on every service. Running out of credits under your caps is a yellow warning, not a failure.
Findings are facts about what the web returned: nothing is judged or summarised yet.

## Secrets

Add each one in GitHub: the repository's **Settings > Secrets and variables > Actions > New repository
secret**. Type the name exactly as shown, paste the value, save. Every Collect run starts with a list of these
names marked set or not set, so you can see what the run can see (the values are never shown).

**Google Sheets and alerts** (used by Sync, Sheets, Research and Collect)

| Secret | What it is for | Needed? | What to paste |
|---|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | writing to the brands' Google Sheets | yes, for the sheets | the whole key file of the Google service account |
| `SHEET_SHARE_EMAIL` | who the sheets are shared with when the tool creates them | optional | an email address |
| `SHEET_FOLDER_ID` | a Shared Drive folder for new sheets | optional | the folder's ID |
| `SLACK_WEBHOOK_URL` | a Slack message when a run needs attention | optional | a Slack incoming-webhook address |

**Searching the web** (Actions > Research)

| Secret | What it is for | Needed? | What to paste |
|---|---|---|---|
| `SERPER_API_KEY` | Serper.dev searches | at least one of the three; all three for Compare mode | the key from serper.dev |
| `TAVILY_API_KEY` | Tavily searches | as above | the key from tavily.com |
| `SERPAPI_API_KEY` | SerpApi searches | as above | the key from serpapi.com |

**Ads, social posts and deep research** (Actions > Collect)

| Secret | What it is for | Needed? | What to paste |
|---|---|---|---|
| `INSTAGRAM_COOKIES` | Instagram posts, logged in as the dummy account | for Instagram | the dummy account's cookies, exported as a JSON list |
| `INSTAGRAM_USERNAME` and `INSTAGRAM_PASSWORD` | a backup way to log in: used when there are no cookies and, for Instagram and TikTok (`password_fallback: when_cookies_fail` in `browser.yaml`), when the site refuses the cookies | optional; both or neither | the dummy account's login |
| `TIKTOK_COOKIES` | TikTok posts, logged in as the dummy account | for TikTok | cookies exported as a JSON list |
| `TIKTOK_USERNAME` and `TIKTOK_PASSWORD` | a backup way to log in | optional; both or neither | the dummy account's login |
| `GEMINI_COOKIES` | Gemini Deep Research, logged in as the dummy Google account | for Deep Research | cookies exported as a JSON list (Google blocks automated sign-in, so there is no password option) |
| `ANTHROPIC_API_KEY` | Claude writing each brand's research seeds, the Deep Research prompt, and answering Gemini's questions | optional, recommended | the key from console.anthropic.com |
| `APIFY_TOKEN` | the optional Apify extra | only if you switch Apify on | the API token from your Apify account |
| `IDEA_BANK_FOLDER_ID` | the Shared Drive folder where each Idea Bank item's pictures and videos are saved | optional (without it the files are skipped; everything else works) | the id of a folder inside a Shared Drive (the part after `/folders/` in its address) |
| `DEEP_RESEARCH_DOC_ID` | the Google Doc where every Deep Research report is added as a new tab | optional (reports are always saved in the repository too) | the Doc's address, or just the long id between `/d/` and `/edit` |
| `FREEAPI_ENCRYPTION_KEY` | the AI gateway's own storage key (64 hex characters) | only for the AI layer's live checks | you generate it in any browser (docs/ai/setup-instructions.md) |
| `FREEAPI_ADMIN_PASSWORD` | the AI gateway's admin password | only for the AI layer's live checks | you generate it the same way |
| `GROQ_API_KEY` | Groq models through the gateway | only if you switch Groq on | console.groq.com > API Keys |
| `MISTRAL_API_KEY` | Mistral models through the gateway (free plan may train on prompts: Public data only) | only if you switch Mistral on | console.mistral.ai > API keys |
| `OPENROUTER_API_KEY` | OpenRouter's free models, called directly: the tool asks for the models that are free today (they change every few days), never a paid one | only if you switch OpenRouter on | openrouter.ai > Keys |
| `GEMINI_API_KEY` | Google Gemini through the gateway (free tier may train on prompts: Public data only) | only if you switch Gemini on | aistudio.google.com > Get API key |
| `CEREBRAS_API_KEY` | Cerebras through the gateway | optional | cloud.cerebras.ai |
| `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` | Cloudflare Workers AI through the gateway | optional; both or neither | the Cloudflare dashboard > Workers AI |
| `YOUTUBE_API_KEY` | public YouTube video details through YouTube's official Data API (free quota, no card) | optional | Google Cloud console > APIs and services > Credentials (docs/acceptance-fixes.md) |
| `OBSERVATORY_CONTACT` | the contact address the competitor observatory puts in its requests so a site owner can reach you | **required** for the observatory | your business email address, or the address of your contact page (the tool names itself honestly and will not read without it) |
| `EXA_API_KEY` | Exa search (free credits that stop at zero; add no card) | optional | dashboard.exa.ai > API keys. Exa's free hosted search needs no key and is switched on by the workflows |
| `GITHUB_TOKEN` | raises GitHub's public-API limit for Agent-Reach's GitHub channel (60 requests an hour without it); the Observatory workflow passes the repository's own token automatically, and it is sent only to api.github.com | optional, nothing to set up | provided by GitHub Actions; for another repository a personal token with no extra permissions |
| `INSTAGRAM_SESSION_B64` | the stored login session of a DUMMY Instagram account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `X_SESSION_B64` | the stored login session of a DUMMY X (Twitter) account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `TIKTOK_SESSION_B64` | the stored login session of a DUMMY TikTok account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `REDDIT_SESSION_B64` | the stored login session of a DUMMY Reddit account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `FACEBOOK_SESSION_B64` | the stored login session of a DUMMY Facebook account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `LINKEDIN_SESSION_B64` | the stored login session of a DUMMY LinkedIn account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `XIAOHONGSHU_SESSION_B64` | the stored login session of a DUMMY Xiaohongshu account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `BOSS_SESSION_B64` | the stored login session of a DUMMY Boss (Zhipin) account, captured by hand with the browser-extension cookie export (docs/platform/session-capture-guide.md); used only by `observatory sessions` and the Acceptance session step | optional | github.com > Settings > Secrets and variables > Actions; never a main account |
| `REDDIT_CLIENT_ID` | the id of a Reddit app, for the official Reddit API route (it runs only once Reddit has approved your use and you have recorded that in `observatory.yaml`) | optional | reddit.com/prefs/apps, after Reddit approves your access |
| `REDDIT_CLIENT_SECRET` | the secret of that Reddit app | optional, with the id | the same page |
| `JINA_API_KEY` | raises Jina Reader's free limit from 20 to 500 requests a minute | optional | jina.ai > API key (the reader works without one) |

**Needs no secret:** Meta Ad Library (public pages), the catalogue sync (public store data), and
`GITHUB_TOKEN` (GitHub provides it automatically).

**Adding them in a sensible order.** Meta Ad Library needs nothing, so start there: run Collect with mode
`check` and only Meta ads ticked. Then add `INSTAGRAM_COOKIES` and `TIKTOK_COOKIES` and check again. Last, add
`GEMINI_COOKIES` and `ANTHROPIC_API_KEY`, run `check` with Deep Research ticked, and finally `test_deep_research`.

## Ads, social posts and deep research (Actions > Collect)

Four more sources, each optional, each capped. They drive a real browser, the way a person would:

| Source | How | Login |
|---|---|---|
| Competitor ads (Meta Ad Library) | the public Ad Library pages | none |
| Instagram posts | a dummy Instagram account | `INSTAGRAM_COOKIES` (or `INSTAGRAM_USERNAME` + `INSTAGRAM_PASSWORD`) |
| TikTok posts | a dummy TikTok account | `TIKTOK_COOKIES` (or `TIKTOK_USERNAME` + `TIKTOK_PASSWORD`) |
| Gemini Deep Research | the Gemini web app, as a dummy Google account; it waits for the report (10+ minutes, up to 25) | `GEMINI_COOKIES` only (Google blocks automated sign-in) |

**Setting up a dummy account's login (once per account).** Log in as the dummy account in an
ordinary browser. With a cookie extension (for example Cookie-Editor) choose Export, then JSON. In the
repository go to Settings > Secrets and variables > Actions, create the secret named in the table, and
paste the JSON. Cookies, not passwords, are the reliable way: logins from GitHub's servers are often
stopped with a code or a puzzle, and the username/password route is only a best-effort fallback. Cookies
expire after days or weeks; when they do, **Check** says "not logged in" and names the secret to refresh.

**Google (Gemini) cookies need extra care.** The check used to report a signed-out Gemini as logged in, because
Gemini shows its prompt box to anyone; it now treats a visible Sign in link as "not logged in", and says so with
facts about the cookies it was given (how many, for which sites, how many already expired, and which Google
sign-in cookies are missing; names and counts only, never values). The commonly reported cause of Google cookies
dying within minutes is that Google rotates them while the original browser stays open, so export them like this:
open a private (incognito) window, log in as the dummy Google account, open gemini.google.com/app, export all
cookies as JSON including hidden (HttpOnly) ones, then close the window without clicking Sign out or browsing
further. Paste the result into `GEMINI_COOKIES` and run Check straight away. This is the commonly reported
behaviour, not something the tool can confirm. A password cannot replace cookies for Google: Google blocks
automated password sign-ins from servers, so the Gemini username and password are never used.

**TikTok's puzzle and "Something went wrong".** In the automated browser TikTok's hashtag page can fail while a
search by hand works: it showed "Something went wrong" in one Check and a slider puzzle in a Recon, and in both the
list of posts behind the page came back empty. The tool never solves a puzzle. For each hashtag it now does what a
person does: if the tag page shows an error or a puzzle, it presses the puzzle's own X (`challenge_dismiss` in
`browser.yaml`; once per page, never again on the same page), and if the page still will not load it **clicks the
search box, types `#tag` and presses Enter** (`search_box`), falling back to the search address if the box cannot
be found. It also looks again after the first read, because the error can appear after the first wait. The run and
Check say which tags were read by search and how many pages still failed; Recon saves what the search page sends.
If TikTok blocks the search page as well, TikTok is blocking the automated browser, and Collect with **backend**
`fallback` reads TikTok through Apify instead (it uses your Apify credit).

**What a Deep Research report file tells you.** The test file (and the report saved for each run) says how the run
went, not just what came back: whether Deep Research really switched on, whether the Start research button was
pressed, whether a finished-report button appeared, how long it waited, and the sources found. If neither Start
research nor a finished-report button was seen, it warns that the answer may be an ordinary chat answer and not a
Deep Research report. The prompt Claude writes is never cut off: it has its own limit (`prompt_max_tokens`, 2000),
a reply that runs out of room is asked again with twice the room, and if it still does not fit the question is sent
as written and the report says so. Gemini's page text sometimes holds the report twice (a cut-off copy, then the
complete one); the complete copy is kept and the raw text is saved beside it as `...-raw.md`.

**Running it.** It runs by itself every Monday. To run it now: Actions > Research > Run workflow.
"Dry run" is ticked by default when you run it by hand; untick it to search for real. You can
untick a service to leave it out of that run.

**Proving each service works.** In Actions > Research > Run workflow, tick **Provider check**
(and leave the services you want ticked). It runs one web search and one news search on each
ticked service, shows whether each worked, and counts the credits (about two per service; Dry run
is ignored for this). Day to day, web searches go to Serper.dev and news searches to Tavily, so
SerpApi only steps in as a fallback or when you tick it alone; that is by design, not a fault.
If a check says "answered but returned no results", the bracket after it shows what the service
actually sent back, which tells a genuinely empty answer from a misread one. The news check uses
the broad word "overalls" over the last year, so an empty answer there deserves a closer look.

**Comparing the three services.** Normally each search goes to one service. With **Compare** ticked
(it is ticked by default when you run Research by hand), each of the first few searches (5 by
default, `compare_queries` in `research.yaml`) is run on every ticked service, so you can see how
they differ. The report shows, per search, how many results each service gave and how many only it
found, then an overall summary of how much each pair of services agrees. That costs one credit per
service per compared search (about 15 credits for the default), still inside every cap. Answers
from the last 7 days are reused, so repeating a comparison is free. The weekly automatic run never
compares.

**Reading the results.** Each brand's sheet gets a **Research** tab: one row per page found, with
when it was first and last seen, how often, which service and which search found it, and a
**Review** column for your team (for example useful, irrelevant, saturated). Review marks are never
overwritten. Addresses are shown without tracking codes (Google adds a new one to every result),
so the same page found next week is the same row, with "times seen" going up. The raw findings are in `data/<brand>/research/searches.jsonl`.

**If a run goes red:** a service rejected its key (the log names it; check the secret), or searches
failed on every service. Running out of credits under your caps is a yellow warning, not a failure.
Findings are facts about what the web returned: nothing is judged or summarised yet.

## Secrets

Add each one in GitHub: the repository's **Settings > Secrets and variables > Actions > New repository
secret**. Type the name exactly as shown, paste the value, save. Every Collect run starts with a list of these
names marked set or not set, so you can see what the run can see (the values are never shown).

**Google Sheets and alerts** (used by Sync, Sheets, Research and Collect)

| Secret | What it is for | Needed? | What to paste |
|---|---|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | writing to the brands' Google Sheets | yes, for the sheets | the whole key file of the Google service account |
| `SHEET_SHARE_EMAIL` | who the sheets are shared with when the tool creates them | optional | an email address |
| `SHEET_FOLDER_ID` | a Shared Drive folder for new sheets | optional | the folder's ID |
| `SLACK_WEBHOOK_URL` | a Slack message when a run needs attention | optional | a Slack incoming-webhook address |

**Searching the web** (Actions > Research)

| Secret | What it is for | Needed? | What to paste |
|---|---|---|---|
| `SERPER_API_KEY` | Serper.dev searches | at least one of the three; all three for Compare mode | the key from serper.dev |
| `TAVILY_API_KEY` | Tavily searches | as above | the key from tavily.com |
| `SERPAPI_API_KEY` | SerpApi searches | as above | the key from serpapi.com |

**Ads, social posts and deep research** (Actions > Collect)

| Secret | What it is for | Needed? | What to paste |
|---|---|---|---|
| `INSTAGRAM_COOKIES` | Instagram posts, logged in as the dummy account | for Instagram | the dummy account's cookies, exported as a JSON list |
| `INSTAGRAM_USERNAME` and `INSTAGRAM_PASSWORD` | a backup way to log in: used when there are no cookies and, for Instagram and TikTok (`password_fallback: when_cookies_fail` in `browser.yaml`), when the site refuses the cookies | optional; both or neither | the dummy account's login |
| `TIKTOK_COOKIES` | TikTok posts, logged in as the dummy account | for TikTok | cookies exported as a JSON list |
| `TIKTOK_USERNAME` and `TIKTOK_PASSWORD` | a backup way to log in | optional; both or neither | the dummy account's login |
| `GEMINI_COOKIES` | Gemini Deep Research, logged in as the dummy Google account | for Deep Research | cookies exported as a JSON list (Google blocks automated sign-in, so there is no password option) |
| `ANTHROPIC_API_KEY` | Claude writing each brand's research seeds, the Deep Research prompt, and answering Gemini's questions | optional, recommended | the key from console.anthropic.com |
| `APIFY_TOKEN` | the optional Apify extra | only if you switch Apify on | the API token from your Apify account |
| `IDEA_BANK_FOLDER_ID` | the Shared Drive folder where each Idea Bank item's pictures and videos are saved | optional (without it the files are skipped; everything else works) | the id of a folder inside a Shared Drive (the part after `/folders/` in its address) |
| `DEEP_RESEARCH_DOC_ID` | the Google Doc where every Deep Research report is added as a new tab | optional (reports are always saved in the repository too) | the Doc's address, or just the long id between `/d/` and `/edit` |

**Needs no secret:** Meta Ad Library (public pages), the catalogue sync (public store data), and
`GITHUB_TOKEN` (GitHub provides it automatically).

**Adding them in a sensible order.** Meta Ad Library needs nothing, so start there: run Collect with mode
`check` and only Meta ads ticked. Then add `INSTAGRAM_COOKIES` and `TIKTOK_COOKIES` and check again. Last, add
`GEMINI_COOKIES` and `ANTHROPIC_API_KEY`, run `check` with Deep Research ticked, and finally `test_deep_research`.

## Ads, social posts and deep research (Actions > Collect)

Four more sources, each optional, each capped. They drive a real browser, the way a person would:

| Source | How | Login |
|---|---|---|
| Competitor ads (Meta Ad Library) | the public Ad Library pages | none |
| Instagram posts | a dummy Instagram account | `INSTAGRAM_COOKIES` (or `INSTAGRAM_USERNAME` + `INSTAGRAM_PASSWORD`) |
| TikTok posts | a dummy TikTok account | `TIKTOK_COOKIES` (or `TIKTOK_USERNAME` + `TIKTOK_PASSWORD`) |
| Gemini Deep Research | the Gemini web app, as a dummy Google account; it waits for the report (10+ minutes, up to 25) | `GEMINI_COOKIES` only (Google blocks automated sign-in) |

**Setting up a dummy account's login (once per account).** Log in as the dummy account in an
ordinary browser. With a cookie extension (for example Cookie-Editor) choose Export, then JSON. In the
repository go to Settings > Secrets and variables > Actions, create the secret named in the table, and
paste the JSON. Cookies, not passwords, are the reliable way: logins from GitHub's servers are often
stopped with a code or a puzzle, and the username/password route is only a best-effort fallback. Cookies
expire after days or weeks; when they do, **Check** says "not logged in" and names the secret to refresh.

**Google (Gemini) cookies need extra care.** The check used to report a signed-out Gemini as logged in, because
Gemini shows its prompt box to anyone; it now treats a visible Sign in link as "not logged in", and says so with
facts about the cookies it was given (how many, for which sites, how many already expired, and which Google
sign-in cookies are missing; names and counts only, never values). The commonly reported cause of Google cookies
dying within minutes is that Google rotates them while the original browser stays open, so export them like this:
open a private (incognito) window, log in as the dummy Google account, open gemini.google.com/app, export all
cookies as JSON including hidden (HttpOnly) ones, then close the window without clicking Sign out or browsing
further. Paste the result into `GEMINI_COOKIES` and run Check straight away. This is the commonly reported
behaviour, not something the tool can confirm. A password cannot replace cookies for Google: Google blocks
automated password sign-ins from servers, so the Gemini username and password are never used.

**TikTok's security puzzle.** When TikTok shows its slider puzzle, no posts are loaded behind it (the page is empty
placeholders). The tool never solves a puzzle. It presses the puzzle's own X button once (`challenge_dismiss` in
`browser.yaml`) and, if the puzzle is still there, stops and says so. If TikTok keeps blocking the browser, run
Collect with **backend** set to `fallback`: TikTok is then read through Apify (it uses your Apify credit) whenever
the browser is blocked.

**Where Deep Research reports are kept.** Every report is saved in the repository
(`data/<brand>/research/deep/`), in the brand's **Collected** tab, and, when `DEEP_RESEARCH_DOC_ID` is set, as a
**new first tab in one Google Doc** (newest on top), named like `2026-10-22 swoveralls - how comfortable overalls are marketed`.
Each tab holds the question, the details (brand, date, how long it waited, whether Deep Research really switched on),
the exact prompt sent to Gemini, any questions Gemini asked and Claude's answers, the report, and the source links.
This runs as its own step after the run (like the Sheets step), so a Google problem can never lose or fail a report:
whatever could not be added is tried again next time, and the file `deep_archive.json` remembers what already has a tab,
so a report is never added twice. To set it up once: (1) create a Google Doc; (2) press **Share** and add the service
account's address (the `client_email` inside `GOOGLE_SERVICE_ACCOUNT_JSON`) as an **Editor**; (3) in Google Cloud, for
the same project, switch on the **Google Docs API** (the Check names the exact link if it is off); (4) add the Doc's
address as the `DEEP_RESEARCH_DOC_ID` secret; (5) run Collect with mode `check`: it adds a test tab and removes it
again, and says what is wrong if it cannot. Gemini's page gives the tool plain text, not formatted text, so headings
are recognised by their shape and tables arrive as lines with `|` between the columns.

**The Idea Bank (everything sourced, waiting for a human decision).** Every ad, social post and web result a run finds can
become a row in the brand sheet's **Idea Bank** tab with an id (`IB-0001`, `IB-0002`, ...). The tool fills in what it owns:
what it is, its numbers and performance signal, a **score** (0 to 100: how useful it is for this brand and this request),
a **summary**, **how the brand could use it**, any **risk** (a claim, copyright or brand-fit concern), a topic, the region
and the request it came from. **You** fill in the two columns the tool never touches: **Decision** (a Use / Maybe / Skip list
on a new tab) and **Notes**. Nothing is ever deleted by the tool, and a source item never gets two rows. Claude scores items in
batches (`ideabank.score_batch`), most interesting first, within `ideabank.max_scoring_calls` per run and
`ideabank.claude_monthly_calls` a month; what is left over is scored on the next run. Without a Claude key the rows are still
added, unscored.

**Asking for the kinds of content a brand makes.** A request can list the **formats** it wants, in plain words, in the Request workflow's
formats box: for example "UGC, AI content and posts". The formats are `ugc` (real customers and creators on camera or in photos: reviews,
hauls, try-ons, unboxings, testimonials), `ai` (AI-made or AI-assisted content: AI images, AI video ads, AI avatars), `influencer`
(creator partnerships), `static` (single-image posts and ads; also typed as posts or images), `carousel`, `video` (Reels, TikToks, video ads)
and `meme`. Words such as creators, reels, hauls and "AI generated ads" are understood; an unknown word is refused with this list. The plan
then goes looking for the formats: Claude shapes the keywords, hashtags and creator angles to surface them, and one web search per wanted
format is added. Creator searches follow the platforms the plan keeps, so dropping TikTok drops the TikTok page searches too. Every item
found is given a **format label** (the Format column of the Idea Bank tab: `ugc`, `ai`, `influencer`, `static`, `carousel`, `video`, `meme`,
`article` or `other`), and an item in a wanted format scores 8 points higher (never above 100, and a generic or unrelated item still cannot
reach the minimum). The Requests tab shows the formats asked for and the tools actually in the plan.

**Ideas, not generic content.** An item is judged on two things, and the code (not the model) combines them. **Fit** is how well its product
category, audience and occasion match the brand. **Idea strength** is how distinctive and reusable the concept itself is: a specific hook,
format or story that can be re-made for the brand's own product. The score is 40% fit and 60% idea strength. An item Claude marks **generic**
(a discount or promo code, a price, a restock notice, free shipping, a plain product shot, a bare list of reviews or perks, or just "so
comfy") can never score above 45, and an unrelated product category (shoes, baby goods, food, outdoor gear) can never score above 45, so
neither reaches the usual minimum of 50. Evidence counts: an ad that has run 60 or more days, or in several variants, scores higher on
idea strength when a real concept is there. Each kept row also shows the **mechanism** (the technique, such as "problem-first hook"), the
**concept** (the reusable idea with the product taken out, which is what to copy), and a **preview** column: "saved in Drive" or "original
link only" (the file could not be saved, usually because Meta's picture link had expired). Rows scored under the earlier rubric are **scored
again once** with this one, and items from **brands you skip** (`benchmark_pages: {Name: skip}` in the brand's research.yaml) are removed
too, even ones collected before you chose to skip them. The new columns are added to the right of an existing Idea Bank tab, and your
Decision and Notes columns are never touched.

**A clean bank: one original, nothing of your own, nothing irrelevant.** The bank keeps only ideas that can genuinely help the brand.
(1) **One original of every idea.** Two items count as the same idea when their text is the same (an ad is compared by its words whoever runs
it; a post by its account plus its words; a web result by its address), or when their picture or video file is byte for byte the same. Only
one original is kept (the longest-running ad, else the oldest post) and the copies are never added, including a copy of something that was
removed earlier. (2) **The brand's own content is left out**: an item from the brand's own account or its own Meta page (found through the
benchmark pages), a link to its own site, or a post that names the brand (`ideabank.skip_own_brand`). (3) **Only relevant items stay.** An item
is scored first and kept only if it scores at least `ideabank.min_relevance` (50 by default); nothing is saved in Drive or shown in the sheet
for an item that is waiting to be scored or was removed. A removed item has its Drive folder moved to the **Drive trash** (recoverable for 30
days; this needs the service account to be a **Content manager** in the Shared Drive, and if it is not, the folders are named in the log to
delete by hand and tried again next run) and its sheet row deleted. **A row you wrote a Decision or Notes on is never deleted.** Every run tidies
what is already in the bank the same way, and the Collect workflow's mode **tidy** does only that (it collects nothing), so raising
`min_relevance` and running `tidy` cleans the sheet and the Drive.

**Saved files are checked, and described honestly.** A picture or video is judged from its own bytes before it is saved: the real type
(a PNG that a server called "image/jpeg" is saved as a `.png`), a picture that decodes fully, is not blank and is at least 200 pixels on
its shorter side, and a video whose structure is whole (a header with a length and a picture track, data that adds up to the file's size).
A download that arrives short of the size announced, or whose connection drops half way, is tried once more and otherwise refused with
the reason. After the upload Drive is asked for the file's size and checksum, and a copy that does not match is trashed and sent again.
The sheet's **preview** column says what is really in Drive, worked out from the files that are there and never from the last download
attempt, so a reviewer knows at a glance whether they can judge the full creative, a cover only, or nothing: "video saved and checked (28s,
720x1280)"; "image saved (1122x1402)"; "cover image only: the video could not be downloaded (the platform refused the download)" (or "the post
is no longer available", or the reason); "cover image only: the video has not been fetched yet"; "image post saved (1080x1920), it has no
video" and "carousel: first slide saved, the other slides were not" (a post that simply has no video is never shown as a failed download);
"no saved media: the original link expired" (Meta's links last hours or days) or "...is unavailable", or "...original link only"; with
"low resolution" for a picture under 600 pixels and "not yet checked" for a file saved before the checks existed. Each video outcome is kept
on the item (`video_status`: saved, none, gone, blocked, failed). If TikTok or Instagram refuses three downloads in a row in one run, the rest
of that platform waits for the next run instead of hammering it, and a refusal costs no try. The real video is fetched by `yt-dlp` first and,
if that is refused, through the logged-in browser (the post's own page data, with the browser's cookies); this is best effort, and the log
says which route failed and why. The Collect workflow's mode **tidy** now also runs an
**audit**: every saved file is downloaded back from Drive and checked again as a person opening it would find it; a broken, cut short or
missing file is removed from the item (and saved again if its link still works), a wrongly named file is renamed, and the log counts files
complete, renamed, broken, missing and low resolution, and the items that have only a cover. **Real videos:** for each kept TikTok video
page or Instagram reel and post, `yt-dlp` is asked for the actual video (using your TikTok and Instagram login cookies), at most
`ideabank.video_fetches_per_run` (12) a run and twice per item; TikTok and Instagram may refuse a data-centre address, and then the log says
why and the cover stays. Switch it off with `ideabank.fetch_videos: false`.

**The claim check: a brief never states what the brand cannot back up.** A brief goes to creators who will say its words on camera, so every factual claim in
what they will say or show (the title, the key message and each concept's name, hook, script, visual direction and call to action) is checked by code, and each
must be supported by the catalogue, the approved-claims register or the brief's own allowed list; instructions to the team (dos, don'ts, deliverables, notes) are
not checked. Rules (each named in the brief's violation list and, for an error, handed to the repair pass): `price_not_in_catalogue` (only real prices),
`offer_not_provided` (no discount, code, free shipping or guarantee unless allowed), `material_claim_unsupported` and `count_claim_unsupported` (fibre
percentages and pocket counts), `nutrition_claim_unsupported` (figures such as 20g of protein or 150 calories), `dietary_claim_unsupported` (vegan, gluten
free, organic, keto and so on), `health_claim_prohibited` (cures, detox, clinically proven), `social_proof_unverified` (10,000 customers, rated 4.8),
`result_claim_unproven` (results in 7 days), `unregistered_claim` (as seen on, featured in, recommended by), `claim_status` (a register claim that is
prohibited, or licensed with its guidelines still pending: a warning) and `superlative_unproven` (best-selling, #1: a warning). Support comes from the
product's title, description, tags and extracted facts (a claim about no particular product is supported if any allowed product supports it), the brand's
`claims.yaml` register, and the brief's `claim_rules` in its rules file: `allowed_offers` (for example "30% off"), `allowed_claims` (wording that may be
used) and `skip` (rules to switch off for this brief; not `off`, which YAML reads as False). The model is told these facts, offers and approved claims in
its prompt, so the check is a safety net behind a prompt that already says what may be said. Prices, offers, figures and endorsements are never invented.
The DudeRobe rules file allows "30% off" because the brief itself states it.

**For a creative team: variety, a way to check the tool's judgement, and what to do next.**
- *One technique, at most twice.* After scoring, one Claude call groups the kept ideas by technique (a gift guide sorted by type of person,
  a reaction clip, a blind taste test), whatever the product. Each technique keeps its best `ideabank.max_per_technique` (2; 0 switches this
  off); a long-running ad that is proven outranks a higher score. The rest are removed with the reason and the better ideas named.
- *Rejected tab.* Everything the tool removed (a copy, the brand's own, not relevant enough, the same technique) is listed with the reason.
  Mark **Use** or **Maybe** in the **Rescue** column and run the Idea Bank step (Collect, mode tidy): the item comes back, stays kept whatever
  any later tidy-up says, and its Drive folder is taken out of the trash if it is still there (30 days). A rescued row leaves the tab unless
  you wrote on it.
- *The tool looks at the saved pictures.* Claude looks at the saved picture of each kept item (one frame of a video, when ffmpeg is there) and
  writes `visual_hook` (what a viewer sees in the first second), `look` (ugc, polished, ai-generated, screenshot, graphic, photo) and a quality
  note. A picture that plainly looks AI-made labels the item `ai`, which the text alone never could. At most `ideabank.vision_calls_per_run`
  (3) calls of `vision_batch` (4) pictures a run; `ideabank.vision: false` switches it off.
- *Hook line, first test, audience, channel.* Every scored idea comes with the first line or visual to open our version with, the cheapest first
  test, who it is for, and a channel (paid social, organic social, creator, email, site, mixed): columns `hook_line`, `first_test`, `audience`,
  `channel`, next to `how_to_use`. Ideas scored before these existed are filled in without their scores changing (`enrich_calls_per_run`, 4).
- *A picture for a TikTok found by search.* A TikTok video page found by web search has no picture from the collectors; TikTok's own oEmbed
  gives its thumbnail (up to `cover_lookups_per_run`, 20 a run), saved as a cover and labelled so.

**What you approved is never removed by the tool.** Every Idea Bank run reads your **Decision** column: a row marked Use or Maybe is protected from
every automatic removal (not relevant enough, a copy, the same technique, the same file). A removed item you rescue on the **Rejected** tab is protected the
same way. The run needs the sheet for this (`--sheets-config`, default `sheets.yaml`); if the sheet cannot be read the log says so and the run carries
on, but a mistake in the program itself (a missing option, a wrong call) is never hidden as a note. The sheet report also says **which tab** added rows
went to, for example "added 427 rows (Rejected 427)", so a large number is never a mystery; change entries are counted on their own.

**Approval follows the sheet; a rescue is permanent.** A row marked Use or Maybe is protected for as long as it is marked: change it to Skip and the
tool may remove the item again at the next run. If the sheet cannot be read, nothing is lifted. An item you rescue on the Rejected tab stays kept whatever
the Decision column says. Request mode (`request run`) protects exactly as the `ideabank` command does, and reading the sheet never creates one.

**An expired Meta link is refreshed, not given up on.** Meta's picture and video links last hours or days, but the ad itself is often still
shown in the Ad Library. For a kept Meta ad with no saved file and an expired link, the browser opens the ad's own Ad Library page, reads the
fresh link from its data, and the file is saved in the same run (`ideabank.refresh_links`, at most `link_refreshes_per_run` (10) a run and
twice per ad). An ad that is no longer shown says so ("no saved media: the original link expired"). It needs the browser, which tidy mode installs.

**Google's limits, and what the tool does about them.** Google allows 60 sheet writes a minute per user. The tool paces its writes to 45 a
minute (a brand-new spreadsheet takes about 27, so a normal update never waits), deletes many rows in one request instead of one request per
row, and if Google refuses a write anyway it waits out a whole minute before it tries again (a refused read waits 20 seconds, then 65). A run
that still fails can simply be run again: every step carries on from what the sheet already holds.

**Picture links that expire.** Meta's picture and video links work for only hours or days. The Idea Bank step runs straight after a
collection, while the links work. When a file is wanted later (a `tidy` of older items) and the link has expired, nothing is downloaded and the
run says so; the row keeps its link to the original ad, and the file is saved if that ad is collected again (the fresh link replaces the old
one for any item that has no file yet).

**The minimum score works both ways.** Each run says how far below `min_relevance` the items it removed scored (for example "3 scored 50-59").
If that looks too strict, lower the minimum and run `tidy`: items removed only for a low score come back when they reach the new minimum, and
get a new Drive folder if their link still works. A copy or the brand's own content never comes back. A run scores up to 12 batches of 8 items
(`max_scoring_calls`), enough for the 60 new items it can take in.

**Pictures and videos in the Shared Drive.** At the moment an item is added, its picture or video is downloaded (these links
expire within about a day, so it cannot wait) and saved in the Shared Drive folder named by `IDEA_BANK_FOLDER_ID`, laid out as
`<root> / <Brand name> / IB-0001 - topic / the files`, with each brand's finished briefs in `<Brand name> / Briefs`. Only the
files live in Drive; links, text, scores and summaries stay in the sheet, and the row's **drive_folder** column holds the link to
its folder. An item with no downloadable file gets no folder. Pictures are limited to `ideabank.image_max_mb` and videos to
`ideabank.video_max_mb` (switch videos off with `ideabank.save_videos: false`). Meta video ads are saved as videos; for Instagram
and TikTok the collectors read only the cover picture, so those items get a picture, not the video. A service account has no
storage of its own in a normal My Drive, which is why the folder must be inside a **Shared Drive** that the service account has been
added to as a Content manager (or have the folder shared with it as an Editor); Collect with mode `check` proves it.

**Request mode: type an idea.** The **Request** workflow turns your own words into a researched Idea Bank in two steps. Run it with
**action `plan`**, write what you want ("Father's Day gifting ideas for Swoveralls"), and either leave **tools** on `auto` (Claude
chooses) or set it to `manual` and tick the tools you allow: Meta ads, Instagram, TikTok, web search, Deep Research. Claude
proposes a plan (ad keywords, hashtags, web searches, Deep Research questions, and the **creator and influencer angles**, which become
searches such as `what I bought my dad haul site:tiktok.com`, so the results are not only competitors) and what it will use. The web
searches (Serper.dev, Tavily and SerpApi) always run as an extra beside the collectors. **Nothing is searched at this point.** The plan is
saved as `RQ-0001` and shown in the **Requests** tab. Then run it with **action `run`** and the request id. To change the plan, write the
changes in the **changes** box (for example "drop TikTok, add a gift guide angle") and Claude revises it before anything runs. The region is
blank (worldwide) or a country code, and **apify_extra** runs Apify beside the browser for this request. Claude only plans: code trims the
plan to the allowed tools and to the limits in the `planner` section of `research.yaml`, and the tools themselves are the ones used everywhere
else, so the Instagram careful mode and warning rests apply here too. A step that fails is recorded and the others still run.

**Deep Research check.** After a Deep Research report arrives, Claude reads it against the question and says whether it answers every part,
and whether its examples are concrete (named, dated, linked). If something important is missing it sends ONE follow-up to Gemini in the same chat,
asking for exactly that, and adds the answer under its own heading (`collectors.claude.review_rounds`: 1 by default, 0 switches the check off;
each round is one Claude call, and a follow-up may use part of Gemini's own allowance). Claude cannot confirm that the facts are true, and the
report says so. Whether Gemini keeps Deep Research mode on for a follow-up has not been seen on the live site.

**Briefs from approved items only.** Run the Sheets update, mark the Idea Bank rows you want as **Use** (Maybe rows are used only if you ask),
then run the **Brief** workflow with the brief's rules folder (`briefs/<name>/rules.yaml`) and what the brief is for. The catalogue gate must
pass; only Use rows are used, and the saved pictures are read from Drive, so nothing is searched again. Claude writes the brief as concepts (hook,
script, visuals, call to action, product, which ideas inspired it); the exact rules (banned characters and words, word order, product
eligibility) are then **checked by code** on every hook and line, problems go back to Claude for one repair pass, and whatever is still wrong is
listed under **Needs review** in the file, never hidden. Tone and visual rules are given to Claude as guidance and listed for a person, because code
cannot enforce them. With more than one model in `brief.models` each writes the brief, a judge scores them, and the best is kept
(rule problems count against a version). The result is a Word file (with the reference pictures and links) in `data/<brand>/briefs/`, in the brand's Briefs folder in Drive,
and a row in the **Briefs** tab.

**Gemini's Deep Research menu.** Gemini moves its buttons from time to time. The path the tool presses is in
`deep_research_clicks` in `browser.yaml`: the **+** at the left of the prompt box (labelled "Upload & tools"),
then **More tools**, then **Deep research**. Check presses it for real, so a change shows up as a failed Check
with the step number and a picture (`gemini-no-deep-research-button`), not as a failed report. After the last
press the tool looks for the prompt box asking "What do you want to research?" (`deep_research_active_any`); if it
does not see it, Check fails and a real run carries on, saves a picture and says so.

**TikTok hashtag pages that say "Something went wrong".** In the automated browser a tag page can show this error
while the same search works by hand. When that happens the tool searches TikTok for `#tag` instead (the way a
person would; `hashtag_via_search_on_error` in `browser.yaml`) and the run and Check say which tags were read that
way. If the search page shows the error too, the report says how many pages still failed; that means TikTok is
blocking the automated browser, and Collect with **backend** `fallback` reads TikTok through Apify instead.

**Running it.** Every Wednesday by itself (ads and social only; Deep Research never runs on the schedule).
By hand: Actions > Collect > Run workflow, then pick a **mode**:

| Mode | What it does |
|---|---|
| `plan` (the default) | shows what would be collected; opens no browser and spends nothing |
| `seeds` | has Claude write (or refresh) each brand's research seeds from its catalogue, and shows them; no browser |
| `check` | opens each ticked site, confirms the login, and collects a handful of items. With Deep Research ticked it also opens Gemini, presses the buttons that switch the chat to Deep Research (the + menu, More tools, Deep research; nothing is typed or sent), and tests Claude's key, but starts no report |
| `recon` | saves pictures, each page's HTML and samples of the data each site sends; download them from the run's **debug** artifact |
| `test_deep_research` | runs ONE real, short Deep Research report, including Claude's prompt and answers; the report is saved in the debug artifact, not as collected data |
| `collect` | the real run: saves the data and updates the Google Sheets |

Tick which of Meta ads, TikTok, Instagram and Deep Research to include (Deep Research is off by default).
Every run starts with a list of the secrets it can see, marked set or not set (never the values).

**Claude and Deep Research.** With `ANTHROPIC_API_KEY` set, Claude does two jobs. Before the report is
requested it turns your question, plus what it knows of the brand, into one complete prompt, so Gemini has
little reason to ask anything. If Gemini asks a clarifying question anyway, Claude answers it from the
brand context and Gemini carries on. It answers at most `max_rounds` (3) questions per report and makes at
most `monthly_calls` (60) calls a month; the conversation is saved with the report's record. You can tell
Claude about the brand with an optional `context:` line under `deep_research:` in the brand's `research.yaml`.
Without the key the question is sent as written, and a Gemini question ends the report with Gemini's words in the message.

**Apify (an optional extra, off).** Apify runs ready-made scrapers on its own servers and has a free monthly
credit. It is not used unless you switch it on, either for one run (the Collect workflow's **backend** box) or for good
(`collectors.apify.mode` in `research.yaml`). The choices:

- `apify`: Apify only. `fallback`: the browser first, and Apify only if the browser fails.
- **`extra`: both run on every task, and what they find is merged.** An ad or post both find is kept once, each filling
  the other's blanks (a picture link, a reach figure), and the report says how many came from each and how many both found.
  If one route fails the other's results are still kept and the failure is reported. It costs Apify credit on every task.

It needs `APIFY_TOKEN` and has its own caps (100 items a month, 30 a run, and a dollar ceiling per run). When Apify
rescues a failed browser run, the run still shows red, so you remember the cookies need refreshing. Use mode `check`
with backend `apify` to test it.

**Where to look: worldwide, or one country.** The default is worldwide (`defaults.country` in `research.yaml`, and a
brand's `ads: country:` for ads). A run can override it: the Collect and Research workflows have a **region** box
(blank, `worldwide`, or a two-letter code such as `gb`). Worldwide asks the Meta Ad Library for ALL countries and sends
no country to Serper.dev or SerpApi. Be aware: Google has no real "worldwide" market, so without a country its results
lean towards its default market; a truly wide search names several countries, one search (one credit) each. Tavily is
sent no country by this tool at all. Instagram and TikTok have no country setting here (what they show depends on the
account and where the run is). The Ad Library with ALL countries is not yet checked against the live site.

**When a site warns an account.** If Instagram or TikTok shows an account warning (suspected automated behaviour, an
action blocked, a lock), the tool stops using that account immediately, saves a picture (`warning-instagram`), and rests
the account: its browser tasks are skipped (not failed) for `collectors.safety.cooldown_days` (TikTok 3 days by default; Instagram 0, which
means only the rest of that run, so Instagram is never switched off automatically; set a number of days to turn the automatic rest on). The report lists resting accounts, and Apify still runs if it is switched on. Instagram stays
switched on and in the schedule; the rest is there because using a warned account again straight away is what turns a
warning into a ban. Once you have dealt with a warning by hand, tick **ignore_cooldown** in the Collect workflow to use
the account again early. The phrases that count as a warning are in `browser.yaml` (`warning_any`, `warning_url_any`):
they are strict on purpose, so an ordinary caption cannot match. The first two are Instagram's own words from the real
warning the dummy account received on 2026-10-26 ("We've detected automated behavior on your account ... ensure that no
other users or tools have access to your account"); the rest are wording these sites are known to use. If a warning
appears on the site and you have not run the tool since, **rest the account by hand**: Collect with mode `rest` (box
`rest_account`: `instagram`, `tiktok` or both; box `rest_days`: blank uses research.yaml) writes the rest without
opening the account at all.

**Careful mode (slower, smaller, spread out) is OFF by default**, because it limits what a run can collect. Instagram is read as fast and
as deep as TikTok. To switch it on for a warned account, remove the `#` from the four commented lines under `sites.instagram` in
`browser.yaml` (any site can have them):
`pacing` (20 to 60 seconds before each page, 8 seconds after each scroll, against 3 to 8 and 3 for the other sites),
`max_scrolls_per_page: 3`, `max_pages_per_run: 2` and `max_pages_per_day: 4`. A run that has more hashtags than it
may open takes its turn: it opens the next ones after where the last run stopped and goes back to the first after the
last, so every hashtag is covered over several runs (`data/_shared/page_log.json` remembers where it is and what each
day has used). A run that covered only some of the pages is not counted as finished, so the next run carries on instead
of saying "already collected". The report says how many pages were opened out of how many. This makes the tool gentler
and smaller; it cannot promise an account will not be warned again. Logging in (one visit to the home page) is not
counted in the page caps. Raise the numbers only if the account stays quiet.

**What the browser does not do.** It does not hide that it is automated. An earlier build started Chromium with an
option that hides the automation marker; that option has been removed. It does use an ordinary window size, language,
time zone and browser name (settings of the browser, not a disguise), random pauses between pages, and it stops when
a site warns the account.

**Set up by trial, the first time.** The page addresses, button names and login checks in `browser.yaml`
are first guesses, written without being able to open the live sites. Do **Recon** and **Check** first;
expect a round or two of small edits to `browser.yaml` (it is plain text, no code) before everything reads.
When something cannot be found, the run says what, and saves a picture.

**What is read.** Ads and posts are read from the data each site sends to its own page (found by shape, so
a renamed wrapper does not break it), with the page's links as a fallback. The report says how many
items came from each.

**Keeping it gentle.** Random pauses of a few seconds before each page and after each scroll, small runs
(`items_per_run` 30), `monthly_items` 0 (no monthly limit while the tool is being tested: set a number in `research.yaml` to bring a ceiling back), Deep Research `monthly_tasks` 2 and
`tasks_per_run` 1, and the same seeds are not collected again for 3 days (14 for Deep Research). Dummy
accounts can still be limited or banned by the sites; small, paced runs reduce that risk but cannot remove it.

**Seeds write themselves.** You do not need to type what to search for, and nothing is hard-coded per brand.
Every run builds a factual profile of the brand from its catalogue (products, price bands, materials, claims),
so it can never go stale. Claude then turns that profile, plus the advertisers already seen, into Meta Ad
Library keywords for three kinds of study: **direct** rivals, **adjacent** categories, and **cross-industry**
ad angles. It also suggests **benchmark brands**: rivals and brands from any industry chosen for the craft of
their advertising rather than their product. It also writes the
hashtags and the Deep Research questions, including one that asks what hooks and angles work best across any
industry and how this brand could adapt them. The seeds are saved in `data/<brand>/research/auto_seeds.json` and
refreshed only when the catalogue changes or after 30 days. Run Collect with mode `seeds` to see them (it
also refreshes them). Whatever you write by hand in a brand's `research.yaml` is used first, and the automatic
ones are added after it. For ads, the best hand-written seed is an Ad Library link for an advertiser you want to
follow; a plain Facebook page address is searched by the page's name. Add `auto_seeds: false` to a brand to
turn the automatic seeds off for it. The brand's own name is always searched (Meta ads, TikTok and Instagram
hashtags) and its own advertiser page is followed, whatever Claude suggests; `track_own_brand: false` turns that off. Without `ANTHROPIC_API_KEY` the tool still works: it uses the brand's own
name and the fresh profile, and keeps any seeds written earlier.

**Benchmark brands are followed by page, never by name.** Searching the Ad Library for a brand's name returns
every ad that merely mentions it (a gym opening that hashtags Gymshark), so the tool looks up each suggested
brand's own advertiser page once, using the page id, name and follower count that every ad carries, and then
follows that page. It never guesses: exactly one page with that exact name is accepted; several pages with the
name are accepted only when one has at least five times the followers of the next; anything else (several
similar pages, only similar names, nothing) is listed for you. A run report and the **Benchmarks** tab show
three kinds of line: *following* (decided), *NEEDS YOU* (ambiguous, with the candidate pages and their ids) and
*not found* (tried again after 30 days). To decide, add to the brand's `research.yaml` under `ads:`

    benchmark_pages:
      Gymshark: 123456789012345            # a page id, or an Ad Library link containing view_all_page_id=...
      Some Brand: skip                      # never follow it

Your choice always wins and is never overwritten. A page chosen this way is followed even if Claude stops
suggesting the brand. Pages in `ads: pages:` are followed too. Limits (`collectors.benchmarks` in `research.yaml`):
6 benchmark pages followed per brand, 3 brands looked up per run, 8 ads read per followed page.

**Reading the results.** A **Collected** tab in each brand's sheet: one row per ad, post or report, with
handle, text, link, date, the performance numbers below, a cautious **signal**, first and last seen, **queries** (which search or
hashtag found it), and a **Review** column for your team. Numbers update in place as posts grow; Review marks are
never overwritten. Deep Research reports are saved in full in `data/<brand>/research/deep/`; the sheet shows an excerpt.

**What performance data exists, honestly.** Competitor ad performance is not published, so each source gives what
it can:

| Number | Meta Ad Library (ads) | TikTok posts | Instagram posts |
|---|---|---|---|
| views | not published | yes | yes (video plays) |
| likes, comments | not published | yes | yes |
| shares, saves | not published | yes | not public |
| reach | not published for US ads (EU ads may show a range) | not public | not public |
| **days_running** | yes: first day to last day the ad was seen | n/a | n/a |
| **variants** | yes: copies of the same creative | n/a | n/a |
| **engagement_pct** | n/a | likes + comments + shares + saves as a % of views | same, where views show |

For ads, **days_running is the strongest public signal**, but it is a pointer and not proof: an ad that ran
for months is a long-running creative worth studying, not a confirmed money-maker. The **signal** column says
"worth studying" for an ad that ran 60 days or more, or exists in 3 or more copies, and is blank otherwise;
a blank number means Meta did not show it, never zero, and a later blank never erases a number already known.
Blank cells on older rows fill in when the ad is seen again. The ad picture and video
links Meta provides stop working after about a day, so open them soon, or ask for the images to be kept.

**Catalogue ads with a template.** Meta's dynamic product ads can carry a template token such as
`{{product.brand}}` in place of text. It is filled in only when the ad's own landing site confirms the
advertiser is that brand (a page named Rhoback selling on rhoback.com); otherwise it is dropped, so the text
is left blank rather than showing a token or a wrong brand.

## Source health and live acceptance (Actions > Acceptance)

Unit tests prove the code handles an answer correctly. They cannot prove that TikTok, Instagram, YouTube or a search service still talks to a GitHub server. So the tool keeps a record of what each way of reaching each source has actually done, and trusts a route only after it has been proved from GitHub.

**`sources.yaml`** declares every source and route: how it is reached (an official API, a public page read politely, a dummy account's login, a paid service), what comes back (a site's own data, only page links, a search snippet, a report, a media file), what it can give and what it cannot. It declares; it proves nothing. The Instagram route through Agent-Reach is listed but switched off: its only Instagram route drives a logged-in stealth browser, which is not wired in.

**The result every route gives.** Every action reports in one shape and every failure has one of eight codes: `NEED_SETUP`, `NEED_LOGIN`, `UPSTREAM_BROKEN`, `TIMEOUT`, `RATE_LIMITED`, `PLATFORM_BLOCKED`, `NOT_FOUND`, `BAD_INPUT`. A login wall, a block, a rate limit, a wrong request or a missing page is *final*: another route cannot fix it, so that source stops for the run instead of trying the next. The published shape is `contracts/envelope.v1.schema.json`.

**The health ledger** (`data/_shared/collector_health.json`) records every real attempt and rests a route after a failure that deserves a rest: a rate limit an hour, a login problem 12 hours, a block a day, a timeout a minute. The rest survives to the next run, so a refused platform is not asked again at once. A route is called *degraded* only after 3 failures in a row and *healthy* again only after 2 successes in a row, so one flicker never changes it (`sources.yaml` holds the numbers). A request problem (a missing page, a wrong address) says nothing about the source and is not counted. **Nothing is healthy because ordinary runs worked.** A route is healthy only when a live acceptance run has passed within 14 days, for the route as it is declared and set up now; change a selector in `browser.yaml` and the route has to pass again. Everything else is shown as `unverified`, and nothing that scores or ranks may depend on it.

**Actions > Acceptance** (also every Thursday) makes one small real call per route and records the result, failures included, in `data/_shared/acceptance.jsonl`: time, version, route, items, error code. Tick `plan` to see what would be checked and what is missing, making no call. A route whose secret is missing is reported as *skipped*, never *failed*. A route that is resting is not asked again. The run turns red when a route fails. Besides the collectors, search services, Claude and your catalogue feed, it checks one public YouTube video through YouTube's official Data API (it needs the free `YOUTUBE_API_KEY`). The first live run showed that YouTube refuses `yt-dlp` from GitHub's servers ("Sign in to confirm you're not a bot"), so that route is switched off. A route that fails rests for a while and the run skips it; the **clear** box lifts a rest after you have fixed the cause. Fixes for the first run's failures are in `docs/acceptance-fixes.md`.

**`python -m contentintel health`** prints every route with its status (`--matrix` compares what each source gives and cannot give, `--json` for other tools, `--clear SOURCE` or `SOURCE|ROUTE` when you have dealt with a problem and want its rest removed). The same table is the **Collector Health** tab of each brand's sheet, updated whenever the sheets step runs.

**The evidence record** (`python -m contentintel evidence --brand NAME`, with `--check` and `--export FILE`). Every ad, post, search result and report can be seen as one record with the same fields: where it came from and how it was reached, its identity and duplicates, its content and media, how complete it is, how old it is, its metrics with what each one measures, and its handling. Records are built on demand from what is already saved (each saved batch now also carries a small `provenance` note: the route, how it was reached, and the route's health when it was collected); nothing stored is changed. The honesty rules are checked, not just stated: a search snippet is labelled `snippet_only` and never as the page; an item known only from links is `links_only`; a cover picture is never a video; days running and variant counts are labelled as proxies, never as measurements; old data is labelled stale with its age. The published shape is `contracts/evidence.v1.schema.json`.

**Safety.** Every address read from the internet before a file is fetched must be a clearly public web address (not a private or internal one, nor one with a name and password in it). Secrets are hidden from every message the health ledger keeps. Invisible and control characters (the way instructions can be hidden in a caption) are removed from the title, text and details of everything collected. `contentintel/safety.py` also holds the rule for what a model is told about collected text: it is fenced and described as data, never instructions. A saved file that is not plain JSON, such as a pickle, is refused and never opened.

**What this does not do.** It does not make any source work: the acceptance run says whether one does. It does not change how collecting, the Idea Bank or the briefs behave, except that a route that is resting is skipped and a media address that is not public is not opened. The address check applies to the address given; it does not follow or check redirects. Moving the model-facing text fence into every prompt is a later step.

## The AI layer (Claude coordinating, a gateway for specialist models)

Claude is the coordinator. A pinned, local copy of the open-source FreeLLMAPI gateway (v0.16.17, commit in `ai/gateway.lock.json`) can reach many free providers, but **nothing is switched on until a person approves it**. Comparison and setup: `docs/ai/` (`freellmapi-version-comparison.md`, `provider-comparison.md`, `provider_inventory.json`, `setup-instructions.md`).

**Files you edit.** `ai_providers.yaml` lists all 84 platforms of the pinned gateway, OpenRouter (called directly, not through the gateway), GitHub Models (retired by GitHub on 30 July 2026: kept as a record, never called) and Claude, 86 in all: whether each is on, the highest data class its terms allow, which classes you approved, and the **approved-model allowlist** (a model is used only if it is listed there; the gateway's remote catalogue is switched off, and even if it were on it could not make a model usable). `ai_lanes.yaml` defines ten kinds of work and, for each, a fixed chain of providers and models, how the output is checked and whether a person must review it. `ai_eval_tasks.yaml` holds the sample tasks used to compare models.

**Data classes.** Public, Internal, Restricted. A provider receives data only if its ceiling (set by its checked terms) and your approval both allow that class; unknown means Restricted. Restricted data can go to Claude only. You can raise a ceiling for a provider with a dated `owner_override` naming who decided and why; the tool does what you record.

**What happens to a task.** The lane must be allowed to carry the data class; each route is checked (enabled, approved model, approved class, has a credential, not resting after a failure, inside its free limits or spending cap); the first usable one answers; the answer must pass the lane's checks; a failure or an unusable answer moves to the next route, and Claude answers only if the lane allows it and the spending cap has room; otherwise the task is **deferred**. A free quota running out never switches to a paid route silently. Claude's spending is capped (default $0.50 a day, $10 a month in `ai_providers.yaml`, the same limit the owner set in the Anthropic Console) and a call is refused *before* it is made if its worst case would cross the cap.

**Commands** (`python -m contentintel ai ACTION`; mock unless `--live`): `plan` (what would run and why not, no call), `providers`, `usage`, `review` (list, or `--approve ID --by NAME`), `ask --lane L --text T`, `check`, `eval`, `smoke`. **Actions > AI gateway** runs two separate jobs: *mock* (always; the real pinned gateway with a fake model, proving the plumbing) and *live* (only if you tick it; real calls, with our problems reported separately from provider problems). A mock pass never proves a live provider works.

**What this does not do.** It creates no accounts and holds no keys; each provider needs a person to sign up (instructions in `docs/ai/setup-instructions.md`). Nothing in the platform calls the router yet: the lanes are ready for the next stages to use.

## Telling the tool what it cannot know (`brands/<brand>/manual_facts.yaml`)

Facts you state outrank the website. Edit the file in GitHub; keys are product handles
(the last part of a product's web address). You can add `audience`, `description`, `family`, and a
lifecycle **`status`** (`active`, `inactive`, `coming_soon` or `pre_order`).

Status example: a product with no `Active` tag counts as inactive, which keeps it out of concept
drafts. The seven Sidekick Crew sweatshirts currently show as inactive for that reason. If they
are live, add each handle with `status: active` (the template is `brands/swoveralls/manual_facts.yaml`):
`sidekick-crew-dancing-skeletons-fog-grey`, `sidekick-crew-dancing-skeletons-salt-navy`,
`sidekick-crew-free-the-waistband-fog-grey`, `sidekick-crew-swoveralls-cloud-cream`,
`sidekick-crew-swoveralls-olive-branch`, `sidekick-crew-trash-panda-fog-grey`,
`sidekick-crew-trash-panda-salt-navy`.

## Where the data lives (`data/<brand>/`)

| File | What it is |
|---|---|
| `catalogue/products.csv`, `variants.csv`, `products.json` | clean exports for other tools |
| `changes.jsonl` | every detected change, appended over time |
| `last_checked.json` | when the last good sync ran |
| `sheet.json` | which Google spreadsheet belongs to this brand |
| `latest.json`, `snapshots/` | the full records later phases read; a snapshot is kept only when real content changed |
| `registry.json` | every product ever seen, including suspected and confirmed removals |
| `failed_runs.jsonl` | every rejected run and why |
| `research/searches.jsonl` | every search made for this brand, with the results as returned |
| `research/runs.jsonl` | one line per research run: what was planned, reused, searched, skipped |
| `research/collected.jsonl` | every ad, post and report collected for this brand, as flattened rows |
| `research/auto_seeds.json` | the brand's automatic seeds (rewritten when the catalogue changes, after 30 days, or when the seed format changes) |
| `research/benchmarks.json` | which advertiser page each benchmark brand was decided to mean, and the evidence |
| `research/deep/*.md` | the full text of each Deep Research report |

`data/_shared/research_usage.json` counts the credits spent on each search service, across all
brands. `data/_shared/collector_health.json` is the health ledger and `data/_shared/acceptance.jsonl` every live
acceptance result (see Source health and live acceptance).

A quiet run writes only `last_checked.json` (about 100 bytes). Files are rewritten only when
their content changes.

## Good to know

- Because the repository is private, runs use your monthly GitHub Actions minutes. Syncing every
  6 hours takes about 4 minutes a day. To run less often, change the `cron` line in
  `.github/workflows/sync.yml` (for example `17 5 * * *` for once a day).
- If the Google Sheets step fails, the run turns red and the run page says what to fix
  (usually sharing, or an API not enabled). The catalogue data is still saved.
- Optional sabotage check: Actions > Tests > Run workflow > tick sabotage. It breaks the code on
  purpose in over 500 ways and confirms the tests notice each one (several minutes).

## Not verified from the build environment

The whole benchmark look-up on real Meta results (the Ad Library link format for one advertiser's page, and whether a brand's name search returns that brand's own ads often enough to find its page; a brand with few active ads can come back "not found"), and everything else that drives a real website (Meta Ad Library, Instagram, TikTok and the Gemini web app: the page names, buttons and login checks in `browser.yaml` are first guesses, tested only against a stand-in browser), every call to Tavily, Serper.dev and SerpApi (the tests use a scripted stand-in built from each
service's documentation, so the first real run with your keys is the real test; start with a dry
run, then a real run on one brand), live fetches from the stores, the sitemap cross-check against a real site, real GitHub runs,
and every call to Google. The sheet logic was tested against a strict stand-in that rejects
anything the real library would reject, and the "no Drive storage" error was reproduced from
a real run's log; the first real run with your own spreadsheets is the real test. The Shared
Drive option is untested.
