# Batch 2: where it stands and a suggested way through the rest (suggestions, for the owner to choose)

## Where it stands
Stage 1 (the external skills connection and the two registries) and Stage 2 (the story-bank refresh and the foundation chain) are built and pushed. This round added the free pool as the primary route (FreeLLMAPI and OmniRoute), one ranked list per job, the pool retest, and the sheet and safety fixes. What is still waiting on live runs: the pool answering on GitHub, a real provider answering through OmniRoute, the first measurement of each job's ranked list, the foundation chain's remaining sections, and the refresh and chain for DudeRobe and DeFi Snacks.

## The remaining stages, in a suggested order (one bundled update, built stage by stage inside it)
1. **Stage 3, the brief pipeline with its gates.** Builds on the approved canon and the claims gate: a brief is written only from approved canon, every claim in it passes the gate, the story bank is refreshed first (already wired), and anything unverified is flagged for the owner. Needs its own ranked lane (`brief_json`) measured in the retest. Why first: it is the first thing that turns Stage 1 and 2 into something a client sees.
2. **Stage 4, competitor-to-ideas.** Turns what the observatory already collects into ideas, each checked against the claims ledger. Own lane. Low risk: it reads data the tool already holds.
3. **Stage 7, listen-measure-learn.** The skills that need the stores' own numbers (GA4, Search Console) stay NEEDS_INPUT until access is granted; the stage reports what it cannot know instead of inventing it. Build the shell now so access can be plugged in later.
4. **Stage 5, transcription.** Groq Whisper first (free), Faster-Whisper as the backup, VibeVoice last and only if its licence allows. Own lane for the cleanup step.
5. **Stage 6, the creator chain** and **Stage 8, voice-to-creative.** Depend on Stage 5's transcripts; build after it.
6. **Stage 9, claude-seo** and **Stage 10, the browser bake-off.** The bake-off compares Obscura and Lightpanda on public pages, and agent-browser on public pages first, with the logged-in trial kept as a trial under the existing caps, rests and read log. Run after the pool work is proven, because it needs the same live runs.
7. **Stage 11, RustXNG, last.**

## Batch 3 (after Batch 2), each behind a go/no-go gate
Concat headless export (1080x1920, under 5 minutes, no GUI or GPU, 2 of 3 runs), TimesFM (beats a naive baseline on at least 60 percent of held-out series, under 10 minutes of CPU per brand), MoneyPrinterTurbo drafts only with publishing off, the ArcReel billing ledger (reconciles within 5 percent), OpenMontage's scored routing, and OmniRoute as a primary pool member with FreeLLMAPI (already built here; the retest decides the order), ArcReel (billing ledger pattern) and OpenMontage (scored routing), both still in Batch 3. agent-browser belongs to Stage 10, not Batch 3.

## Two suggestions that cut across all stages
- Every new stage that calls a model gets its own job lane and probe in the pool retest in the same change; a test already fails if a job has no lane.
- Stages 3 to 11 come in ONE single update (the owner's decision, 2026-11-18). Their live proof happens in the test round after that package lands, one brand first, not between stages. Each stage still gets its own job lane and retest probe in the same change.


Order question (2026-11-18): the order 3, 4, 7, 5, 6 with 8, 9 with 10, 11 stands as a recommendation; the owner has not ruled on it, so it is built in that order unless he says otherwise.
