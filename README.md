# Content strategy tool: Phase 1 (catalogue foundation)

Everything runs on GitHub. Nothing is installed or run on a computer of your own.

## What it does

- Every day, reads each brand's Shopify catalogue (`/products.json`, page by page until
  an empty page), checks that it is complete, and saves the result in `data/`.
- Detects new, changed, sold-out, restocked, suspected-removed and returned products.
- Records what each product description states (fabric, pockets, ingredients,
  allergens) and what is missing. Nothing is invented.
- Keeps clean, reusable files per brand: `products.json`, `products.csv`, `variants.csv`.
- Blocks generation (later phases) whenever a catalogue cannot be trusted.

## One-time set-up (all in the GitHub website)

1. Upload the project files to the repository, keeping the folder paths.
2. Upload the five saved catalogues to `tests/fixtures/`. They are supplied already named,
   so no renaming is needed: `swoveralls_full_72.json`, `swoveralls_truncated_30.json`,
   `duderobe_fetch_a.json`, `duderobe_fetch_b.json`, `defi_snacks.json`. Without them the
   Tests workflow stops with one message listing what is missing.
3. Check `domain` in each `brands/<brand>/brand.yaml`.
4. If a run ever fails with a permission error while saving: Settings > Actions >
   General > Workflow permissions > Read and write.
5. Optional alerts: add a repository secret `SLACK_WEBHOOK_URL`.

The **Tests** workflow runs by itself on every push. Green means the project is sound,
and it also validates any edit you make to a `brand.yaml` or `rules.yaml` (a typo in a
setting name fails the run and names the setting).

## First run for a brand

1. Actions > **Catalogue sync** > Run workflow (brand `all`, or one brand).
   The first run ends red on purpose: "needs baseline". Open the run page for the report.
2. Check the product count on the store.
3. Actions > **Catalogue admin** > Run workflow: action `confirm-baseline`, the brand,
   and the count you checked. Your GitHub username is recorded as the confirmer.
4. From then on the sync runs daily (05:17 UTC) and you only look at red runs.

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
| `catalogue/products.csv` | one row per product: status, flags, price range, stock, fabric, pockets, ingredients, missing facts |
| `catalogue/variants.csv` | one row per size/colour: options, SKU, price, sale price, available |
| `catalogue/products.json` | the same plus description, facts with their sources, variants and images |
| `changes.jsonl` | every detected change, appended over time |
| `last_checked.json` | when the last good sync ran |
| `latest.json`, `snapshots/` | the full records the later phases read; a snapshot is kept only when real content changed |
| `registry.json` | every product ever seen, including suspected and confirmed removals |
| `failed_runs.jsonl` | every rejected run and why |

A quiet day writes only `last_checked.json` (about 100 bytes). Files are rewritten only
when their content changes.

To use the data in Google Sheets: open the file in GitHub (for example
`data/swoveralls/catalogue/products.csv`) and choose Download raw file, then File > Import
in the sheet. In a private repository the raw-URL `IMPORTDATA` trick does not work. An
automatic Google Sheet sync can be added; it needs a Google service account key stored as a
repository secret.

## Good to know

- In a private repository, workflow runs use your monthly GitHub Actions minutes. The daily
  sync takes about a minute; the Tests workflow runs only when code or settings change.
- Public repositories pause scheduled workflows after 60 days with no repository activity
  (GitHub's rule), and a public repository makes everything in it public, including
  `brands/` and `briefs/`. Keep this repository private.
- Optional sabotage check: Actions > Tests > Run workflow > tick sabotage. It breaks the
  code on purpose in about 70 ways and confirms the tests notice each one.

## Not verified from the build environment

Live fetches from the stores, the sitemap cross-check against a real site, and real
GitHub runs (including Slack alerts). The workflow steps were run against a simulated
remote repository and linted with actionlint; everything else is tested against saved
catalogues.
