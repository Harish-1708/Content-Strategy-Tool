# Batch 2, stage 1: the external skills connection and the two registries (Update 70)

What this stage does, in plain terms. Later stages (the foundation chain, the brief pipeline and the rest) are built on it and are not in this update yet.

## 1. The Aaron marketing skills, connected externally

- **The whole repository is checked out at a pinned commit**, scripts and references included, because the auditors return NOT_SCORED without their runtime files. Nothing is copied or rebuilt: a copy goes stale the moment the repository changes.
- **The pin** is `repo.pinned_commit` in `external_skills.yaml` (now `7932adf517`, the commit of 2026-11-16). Change it by hand, only after reading the update report.
- **Skill text is data.** It is read, hashed, size-capped (60,000 characters; the largest skill is 26,179) and wrapped so the model treats it as a published method to apply to the brand's own inputs, never as an instruction to the tool. No script of the repository is run by this stage. A skill path can never reach outside the checkout (symlinks included).
- **All 120 are reachable.** `external_skills.yaml` records each skill's verdict from the research report (64 useful, 35 conditional, 21 not useful) and two things the tool acts on:
  - `state: waiting_for_access` (10 skills: audience-segment-builder, campaign-architect, search-term-miner, attribution-reconciler, fatigue-frequency-manager, offsite-signal-analyzer, performance-monitor, rank-tracker, dark-social-attributor, social-measurement-loop). They are built ready, paused until the stores' own numbers exist (GA4, Search Console, customer or platform exports), and say `NEEDS_INPUT`. Asking for one by name does not unlock it. They are not removed.
  - `auto_run: false` (the 21 not useful). Reachable when asked for by name, never started by a schedule or a chain. cold-outbound-sequencer belongs to the separate email outreach tool.
- **Actions > Skills and registries** (`skills.yml`): `status`, `verify` (fails when the connection cannot be trusted: not the pinned commit, a skill or runtime file missing), `update-report` (what changed upstream since the pin; nothing is adopted), `catalogue` (writes `data/_shared/skills_catalogue.json`, each skill's hash; git keeps the history).

## 2. The two registries (approved design: Sheets tabs + the git log)

- **Per brand, an append-only event log** `data/<brand>/registry/events.ndjson`, committed with the rest of `data/`. Nothing rewrites or deletes a line; a damaged line is counted and skipped.
- **The narrative canon.** A skill's output is only ever *proposed*. Nothing is canonical until the OWNER approves it (the Decision column of the Canon tab); a newly approved text for a section supersedes the earlier one. The tool cannot approve.
- **The offer-claims ledger.** Each claim is verified, unverified, blocked or conflicted. Only the owner can mark a claim verified. `gate` checks a piece of text against the ledger: BLOCKED (it repeats something the ledger says not to claim), NEEDS_REVIEW (a sensitive statement nothing verifies: an award, press, ranking, health claim, figure, customer count) or OK.
- **Seeded from the story bank's own gaps.** `brands/<slug>/claims_seed.yaml` holds 13 (Swoveralls), 10 (DudeRobe) and 13 (DeFi Snacks) claims copied from each brand's "Gaps / not found" section as blocked, unverified or conflicted, never verified. Each brand also has its part of the story bank (`story_bank_material.md`, with the two corrections: DudeRobe is a separate company from DUDE Products / Dude Wipes; Swoveralls' founder is Kyle Bergman) ready for the foundation chain. Seeding twice adds nothing.
- **Sheets.** Once a brand has registry events, its sheet gets a Canon tab and a Claims tab (the tool owns the other columns; Decision and Notes are the owner's and are never overwritten). The sync reads the Decision column before it rewrites the tabs.

## What is not proven

- The workflow's checkout and the real Canon and Claims tabs have not run live (the connection itself was run against the real repository in the sandbox).
- The gate is lexical (word overlap and a list of sensitive patterns). It is a safety net, not a fact-checker: a claim phrased very differently from the ledger's wording can pass as NEEDS_REVIEW instead of BLOCKED, never as OK if it contains a sensitive pattern.
