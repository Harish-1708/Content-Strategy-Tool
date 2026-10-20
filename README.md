# Content strategy tool: Phase 1 (catalogue foundation)

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

A quiet run writes only `last_checked.json` (about 100 bytes). Files are rewritten only when
their content changes.

## Good to know

- Because the repository is private, runs use your monthly GitHub Actions minutes. Syncing every
  6 hours takes about 4 minutes a day. To run less often, change the `cron` line in
  `.github/workflows/sync.yml` (for example `17 5 * * *` for once a day).
- If the Google Sheets step fails, the run turns red and the run page says what to fix
  (usually sharing, or an API not enabled). The catalogue data is still saved.
- Optional sabotage check: Actions > Tests > Run workflow > tick sabotage. It breaks the code on
  purpose in about 100 ways and confirms the tests notice each one (several minutes).

## Not verified from the build environment

Live fetches from the stores, the sitemap cross-check against a real site, real GitHub runs,
and every call to Google. The sheet logic was tested against a strict stand-in that rejects
anything the real library would reject, and the "no Drive storage" error was reproduced from
a real run's log; the first real run with your own spreadsheets is the real test. The Shared
Drive option is untested.
