# Creative intelligence (Stage 4) and brief checks (Stage 5)

Everything here is plain code over data the tool already holds: no model is called, nothing is published, no social site is visited.

## Stage 4: what the report shows (Actions > Creative intelligence, every Monday 07:00 UTC, or by hand)

For each brand, from the kept items of its Idea Bank:

| Part | What it is | How it is computed |
|---|---|---|
| Formula library | 26 Reel hook formulas and 21 YouTube hook formulas | Two MIT-licensed repositories. The copy in `contentintel/data/formulas` is **refreshed from them before every weekly report** (and by hand with Creative intelligence > refresh-library): the newest commit is read, checked, used only if the validator bank's golden set still passes with it, and `VERSION.json` records which commit each file came from; a report calls the copy stale after 14 days. A newer checkout in the external folder is used when present. A Reel hook is named by the first formula (most specific first) whose pattern it matches; a YouTube hook by the formula whose patterns cover most of it. |
| Winners | Items whose signal is at least 3 times the median of their own platform in the same brand | The signal is likes + comments + shares + saves for a social post and days running for an ad. A platform needs at least 4 items before an outlier multiple is given. |
| Formula lift | How much more often a formula appears among winners than among all tagged items | Only given with at least 8 tagged items and one winner; otherwise the report says why not. |
| Opportunity score | 0 to 100 | Fit 30%, momentum 20%, originality 15%, feasibility 15%, strategic value 10%, evidence 10%. An input that cannot be known counts as 0.5, is named in the report, and lowers the confidence. |
| Cross-brand | Formulas that win in several brands, and formulas that win in one brand while another brand never uses them | Needs at least two brands. |

Every report shows its **sample size and a confidence label** (low under 8 items, medium under 25, high from 25). The classifier was measured against the library's own 47 worked examples: its own patterns alone matched 32 with first-match and 38 with the best-match rule used here; 9 were pattern gaps (6 Reel, 3 YouTube: counts written as words, apostrophes, longer callouts, time pressure) and are closed by `contentintel/data/formulas/overrides.json` (each entry says why); none was a bad example. All 47 now match, and plain copy matches nothing. The additions were written against those 47 examples, so they are not proof of accuracy on real hooks: that needs the real briefs and winners. Momentum uses recency and days running today; trend velocity from TimesFM joins when real weekly series exist.

Saved: `data/<brand>/extras/creative_intel.json` and `data/_shared/creative_intel.json` (the cross-brand view).

## Stage 5: what checks a brief

When a brief is made, after the rules check and the claims gate, the **validator bank** looks at every concept:

* the hook: empty or over about 45 words (error), longer than a spoken hook of about two seconds (warning), none of the 47 known formulas (warning);
* AI-writing slop from the lexicon (invisible characters are an error; stock words, phrases and sentence shapes and typographic marks are warnings);
* the parts each format needs (name, format, hook, script and call to action are errors when missing; carousel slides, a short script for a static image and a visual direction for a motion piece are warnings).

Then an **independent reviewer** (its own ranked lane, `brief_review_json`; a model that did not write the brief) gets the brief, the rules and the findings and answers pass or revise. A reviewer that is down never blocks a brief (it is recorded as unavailable). The brief's status is "needs review" when a rule or claim is broken, the bank found an error, or the reviewer says revise. The findings, the review and the **rules version** are saved on the brief record (`validator_findings`, `review`, `rules_version`); the brief rules get a new version number each time their digest changes (`data/<brand>/extras/rules_versions.json`).

## The golden set and the brand gate

`intel --action golden` runs 51 cases through the bank: texts that must be flagged (so a validator that goes blind is caught), and the worked example of every formula (none may be refused). A brand can add its own approved and must-flag texts in `brands/<brand>/golden.yaml` (`cases: [{text, platform, expect: pass|flag, rule}]`). `intel --action gate` runs the same bank, the same sample brief and the same golden set for every brand with no brand-specific code: a brand passes with no code change.

## Stage 7 layer 1: the storyboard and the styleframes (Actions > Storyboard)

Built by code from a brief that already passed the rules, the claims gate, the validator bank and the reviewer; **no model is called and no video is rendered** (layers 2 and 3 wait for the Remotion licence answer and a timed test render). For one concept (or all):

* the hook becomes shot 1 (three seconds at most), every sentence of the script a shot, the call to action the last shot;
* each shot has its seconds (words at 2.5 a second, fitted to the length the format names, 30 seconds when it names none; a storyboard more than 20% off says so), its spoken line, an on-screen line of six words at most, its visual (the brief's visual direction, in order), the product and a picture query;
* every text is checked again (AI-writing slop, the brand's text rules, the claims gate) because a storyboard is a copy a person may have edited;
* each shot gets a **styleframe**: a still the brand may use (its own saved pictures, its own site, Openverse, Wikimedia Commons, Pixabay) with its licence and credit, or "no usable picture found" (never a guess);
* saved as `data/<brand>/extras/storyboard.json` and `storyboard.html` (a contact sheet to open). The older Stage 4 "storyboard" proposal (Skills > stage storyboard) stays as a model-written alternative.

A brief record now keeps its content (`content`), so any brief can be storyboarded later.

## Discovery seeds: two tiers (voice discover)

The trending lane of `voice --mode discover` feeds the seed suggestions. Items that are about the brand are saved as audience voice as before. The rest go through two tiers:

1. **Priority**: the item shares a word (four letters or more) with the brand's own terms (name, research keywords, topics, product terms, competitors), all read from the brand's own files at run time. Kept at once, no model asked.
2. **Judged**: for every other item a pool model decides whether the topic could help the brand. Yes joins the list (marked `judged`), no is dropped. The judge is told only the brand's own terms; there is no word list in the code.

The judge has its own ranked lane (`seed_judge_json`: the first choice with backups behind it, measured by the next pool retest) and is built to share the pool: at most 2 calls a run (12 topics a call, a few hundred tokens), every answer remembered 30 days (`data/<brand>/extras/seed_judgments.json`) so a topic is never judged twice, at most 60 calls a month, the router's own pacing, rests and shared usage ledger apply, and there is **no fallback to Claude**: when the pool has no answer the topics stay unjudged (dropped this run, never assumed useful, never remembered, asked again next time). The run says how many items fell in each tier.

## What is not proven live

The storyboard has run only on test briefs and fake pictures. The first real brief has not been written yet (the brief test is held until briefs are prepared), so the bank, the reviewer lane and the rules version have only been proven with test doubles. The reviewer lane's ranking is not measured until the next pool retest.
