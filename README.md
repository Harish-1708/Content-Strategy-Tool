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

**Running it.** Every Wednesday by itself (ads and social only; Deep Research never runs on the schedule).
By hand: Actions > Collect > Run workflow, with three useful boxes:
- **Dry run** (ticked by default): shows the plan and opens no browser.
- **Check**: opens each ticked site, confirms the login, and collects a handful of items. It only *opens*
  Gemini; it never starts a report.
- **Recon**: saves pictures, each page's HTML and samples of the data each site sends. Download them from
  the run's **debug** artifact. They show exactly what to change in `browser.yaml`.

**Set up by trial, the first time.** The page addresses, button names and login checks in `browser.yaml`
are first guesses, written without being able to open the live sites. Do **Recon** and **Check** first;
expect a round or two of small edits to `browser.yaml` (it is plain text, no code) before everything reads.
When something cannot be found, the run says what, and saves a picture.

**What is read.** Ads and posts are read from the data each site sends to its own page (found by shape, so
a renamed wrapper does not break it), with the page's links as a fallback. The report says how many
items came from each.

**Keeping it gentle.** Random pauses of a few seconds before each page and after each scroll, small runs
(`items_per_run` 30), `monthly_items` 150 across all brands, Deep Research `monthly_tasks` 2 and
`tasks_per_run` 1, and the same seeds are not collected again for 3 days (14 for Deep Research). Dummy
accounts can still be limited or banned by the sites; small, paced runs reduce that risk but cannot remove it.

**Seeds.** Nothing is collected until you add seeds to `brands/<brand>/research.yaml` (the `ads`, `social`
and `deep_research` examples are there, switched off). For ads, the best seed is an Ad Library link for
the advertiser (copy it from your browser); a plain Facebook page address is searched by the page's name.

**Reading the results.** A **Collected** tab in each brand's sheet: one row per ad, post or report, with
handle, text, link, date, likes, comments, shares, views, first and last seen, and a **Review** column for
your team. Numbers update in place as posts grow; Review marks are never overwritten. Deep Research reports
are saved in full in `data/<brand>/research/deep/`; the sheet shows an excerpt.

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
| `research/deep/*.md` | the full text of each Deep Research report |

`data/_shared/research_usage.json` counts the credits spent on each search service, across all
brands.

A quiet run writes only `last_checked.json` (about 100 bytes). Files are rewritten only when
their content changes.

## Good to know

- Because the repository is private, runs use your monthly GitHub Actions minutes. Syncing every
  6 hours takes about 4 minutes a day. To run less often, change the `cron` line in
  `.github/workflows/sync.yml` (for example `17 5 * * *` for once a day).
- If the Google Sheets step fails, the run turns red and the run page says what to fix
  (usually sharing, or an API not enabled). The catalogue data is still saved.
- Optional sabotage check: Actions > Tests > Run workflow > tick sabotage. It breaks the code on
  purpose in about 240 ways and confirms the tests notice each one (several minutes).

## Not verified from the build environment

Everything that drives a real website (Meta Ad Library, Instagram, TikTok and the Gemini web app: the page names, buttons and login checks in `browser.yaml` are first guesses, tested only against a stand-in browser), every call to Tavily, Serper.dev and SerpApi (the tests use a scripted stand-in built from each
service's documentation, so the first real run with your keys is the real test; start with a dry
run, then a real run on one brand), live fetches from the stores, the sitemap cross-check against a real site, real GitHub runs,
and every call to Google. The sheet logic was tested against a strict stand-in that rejects
anything the real library would reject, and the "no Drive storage" error was reproduced from
a real run's log; the first real run with your own spreadsheets is the real test. The Shared
Drive option is untested.
