# The Apify money plan (the math)

**Budget used: 20 dollars a month = 4 working keys at 5 dollars each.** The plan lives in `contentintel/apify_budget.py` (`WORKING_KEYS = 4`); every cap in the tool comes from it. The 20 dollars are shared by every feature that uses Apify. Items a month = dollars divided by what one item costs. A feature may use at most four days' worth of its month in one day (daily number = items / 30 x 4, rounded up), so none can use its month in a week. The guardrails stay: the per-run dollar caps (1 dollar for Collect, 3 for the voice lane) and the 0.25 dollar ceiling Apify enforces on every page read.

## What one item costs: read from the actor's own Apify page (2026-11-24)
| Feature | Actor | Price on its page | Used here |
|---|---|---|---|
| Collect (TikTok) | clockworks/tiktok-scraper | 5.00 dollars per 1,000 results | 0.005 |
| Analyze comments | clockworks/tiktok-comments-scraper | 1.00 dollar per 1,000 | 0.001 |
| Reddit | trudax/reddit-scraper-lite | "from 3.40 dollars per 1,000 results" | 0.0034 |
| X | khadinakbar/x-tweet-scraper | 0.002 dollars a tweet (1.84 to 2.00 per 1,000); the free plan returns up to 50 tweets a run | 0.002 |
| LinkedIn | harvestapi/linkedin-company-posts | 2.00 dollars per 1,000 posts ("from 1.50") | 0.002 |
Still an ASSUMPTION, not read from a page: Instagram and the generic page reader (0.005 an item). Where a page says "from", the highest listed price is used.

## The split (20 dollars): what changed from the first plan
Before: Collect 6.00, voice 5.00, sessions read 3.00, Reddit backup 1.40, analyze comments 1.00, reserve 3.60 = 20.00. Now: **sessions read goes from 3.00 to 2.00 (minus 1.00)**, and that 1.00 is split into **X read 0.50 and LinkedIn read 0.50** (new). Reddit's cost was an assumed 0.004 and is now the read 0.0034, so its 1.40 buys 411 items instead of 349. Everything else is unchanged and the reserve stays 3.60.

| Feature | Share | Dollars | Cost per item | Items a month | Items a day (at most) |
|---|---|---|---|---|---|
| Collect | 30% | 6.00 | 0.005 | 1200 | 160 |
| voice | 25% | 5.00 | 0.005 | 1000 | 134 |
| sessions read (page reads of Instagram, TikTok and other sites) | 10% | 2.00 | 0.005 | 400 | 54 |
| Reddit (voice backup and Reddit page reads) | 7% | 1.40 | 0.0034 | 411 | 55 |
| analyze comments | 5% | 1.00 | 0.001 | 1000 | 134 |
| x read (an X account's recent tweets) | 2.5% | 0.50 | 0.002 | 250 | 34 |
| linkedin read (a company page's posts) | 2.5% | 0.50 | 0.002 | 250 | 34 |
| reserve (future) | 18% | 3.60 | - | - | - |
| **Total** | 100% | **20.00** | | | |

## Which cap applies (the per-run dollar caps)
There are three kinds of cap and they do not contradict each other: the **monthly item share** of a feature (the table above), a **per-call cost ceiling** (0.25 dollars for every page read: Instagram, TikTok, Reddit, X, LinkedIn; 0.50 for a TikTok collection), and a **per-run dollar cap** that depends on the lane:
- **Collect** (`collectors.apify` in `research.yaml`): `max_cost_per_run_usd: 1.0`. Collect now runs Apify as a FALLBACK (`mode: fallback`): when the browser route fails, including while an account rests after a warning, Apify runs inside these caps.
- **The voice lane** (audience voice and its Reddit backup): `CONTENTINTEL_VOICE_APIFY_RUN_USD`, default **3.0** dollars a run.
- **Page reads** (Reddit, X, LinkedIn, Instagram, TikTok, others): one item at most, 0.25 dollars, counted against the platform's own share (Reddit: Reddit; X: X read; LinkedIn: LinkedIn read; everything else: sessions read).

## If only 3 keys work (15 dollars)
Everything scales with the keys:

| Feature | Dollars | Items a month | Items a day |
|---|---|---|---|
| Collect | 4.50 | 900 | 120 |
| voice | 3.75 | 750 | 100 |
| sessions read (page reads of Instagram, TikTok and other sites) | 1.50 | 300 | 40 |
| Reddit (voice backup and Reddit page reads) | 1.05 | 308 | 42 |
| analyze comments | 0.75 | 750 | 100 |
| x read (an X account's recent tweets) | 0.38 | 190 | 26 |
| linkedin read (a company page's posts) | 0.38 | 190 | 26 |
| reserve (future) | 2.70 | - | - |

## Reddit, X and LinkedIn: which actors work, and which do not
- **Reddit: wired.** `trudax/reddit-scraper-lite` (21K users, no login or key).
- **X: wired.** `khadinakbar/x-tweet-scraper`: no login, no cookies, an account's recent public tweets. Cheaper alternatives exist (0.00025 a tweet) but have a dozen users; atomus/twitter-scraper (4.00 per 1,000 on the free plan) was the second choice.
- **LinkedIn: wired for company pages only.** `harvestapi/linkedin-company-posts` (5.5K users, no cookies). A person's profile or posts search needs a login we do not give Apify, so it is NOT read: the code refuses it plainly.
- The input names of the X and LinkedIn actors come from the actors' pages, not from a live call: Acceptance proves them (the three new routes are probes).
