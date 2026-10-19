# Content strategy tool: Phase 1 (catalogue foundation)

Reads each brand's Shopify catalogue, checks it is complete, keeps a history of
every change, and records what is known (and not known) about each product.
Nothing is generated yet: generation is blocked until a catalogue is trusted.

## What Phase 1 does

- Fetches `/products.json` page by page until an empty page, so a 30-product first
  page is never mistaken for the catalogue.
- **Completeness gate**: fails on a truncated feed, warns on suspicious changes,
  cross-checks against the site sitemap, and requires a person to confirm the first
  product count (the baseline).
- Keeps every accepted snapshot; a rejected sync never replaces the last good one.
- Detects new, changed, sold-out/restocked, suspected-removed and returned products.
  `updated_at` is ignored (Shopify changes it on every product between fetches).
- Classifies products from each brand's own config (excluded, active, inactive,
  coming soon, pre-order; flags for new, limited edition, licensed, sale).
- Extracts facts only when the description states them, records their source, and
  marks products "needs facts" when required facts are missing.
- Loads per-brief rules (banned characters and words, word order, approved products).

## Layout

    brands/<slug>/brand.yaml      brand profile (domain, lifecycle tags, required facts, gate limits)
    brands/<slug>/manual_facts.yaml   facts a person states (outrank the website)
    brands/<slug>/claims.yaml     approved-claim register (stored now, enforced in a later phase)
    briefs/<brief>/rules.yaml     rules for one brief only
    contentintel/                 the code
    tests/                        tests; tests/fixtures/ holds saved catalogues
    tests/sabotage/run_sabotage.py   breaks the code on purpose to prove the tests notice
    data/                         created at run time (snapshots, baseline, registry, run log)

## First run for a brand

    pip install -r requirements.txt
    python -m contentintel sync --brand swoveralls          # fetches live
    python -m contentintel confirm-baseline --brand swoveralls --count 72 --by Harish

The first clean sync stops at "needs baseline". Check the product count against the
store, then confirm it. Later syncs compare against it automatically.
Before the first live run, check `domain` in each `brand.yaml`
(`duderobe` and `defi_snacks` are marked as unconfirmed).

## Other commands

    python -m contentintel sync --all                        # every brand
    python -m contentintel sync --brand X --from-file f.json # use a saved products.json
    python -m contentintel sync --brand X --sitemap          # add the sitemap cross-check
    python -m contentintel status --brand X                  # can generation use this catalogue now?
    python -m contentintel confirm-removed --brand X --product-id ID --by NAME
    python -m contentintel reset-baseline --brand X --by NAME

Exit codes: 0 ok or warn, 1 error, 2 sync blocked, 3 baseline needs confirming.

## Tests

    pip install -r requirements-dev.txt
    python -m pytest
    python -m pyflakes contentintel tests
    python tests/sabotage/run_sabotage.py

## Not verified from the build environment

Live fetches from the stores, the sitemap cross-check against a real site, a real
GitHub Actions run (`.github/workflows/sync.yml`), and the Slack alert. Everything
else is tested against saved catalogues.
