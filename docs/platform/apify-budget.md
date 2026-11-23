# The Apify money plan (the math)

**Budget used: 20 dollars a month = 4 working keys at 5 dollars each** (key 3 was checked and replaced on 2026-11-23). The plan lives in `contentintel/apify_budget.py` (`WORKING_KEYS = 4`); every cap in the tool comes from it. The 20 dollars are shared by every feature that uses Apify. Items a month = dollars divided by what one item costs. A feature may use at most four days' worth of its month in one day, so none can use its month in two or three days; the daily numbers are in the table. The 3 dollar per-run cap and the per-call cost ceilings (0.25 dollars a call for the new routes) stay as the guardrails.

## What one item costs
- **Read from Apify's published prices (2026-11-19, the FREE plan's list price, the highest):** TikTok scraper (clockworks) 5.00 dollars per 1,000 results = 0.005 an item; TikTok comments scraper (clockworks) 1.00 dollars per 1,000 = 0.001 an item.
- **ASSUMPTIONS, NOT LIST PRICES (no price was found):** Instagram and the sessions page reader 0.005 an item (TikTok's price, assumed); Reddit 0.004 an item (assumed). If they cost more, the per-call ceiling and the per-run cap limit the damage, and the next price check corrects the number.

## The split (20 dollars)
| Feature | Share | Dollars | Most an item costs | Items a month | Items a day (at most) |
|---|---|---|---|---|---|
| collect | 30% | 6.00 | 0.005 | 1200 | 160 |
| voice | 25% | 5.00 | 0.005 | 1000 | 134 |
| sessions read | 15% | 3.00 | 0.005 | 600 | 80 |
| reddit backup | 7% | 1.40 | 0.004 | 349 | 47 |
| analyze comments | 5% | 1.00 | 0.001 | 1000 | 134 |
| reserve | 18% | 3.60 | - | - | - |
| **Total** | 100% | **20.00** | | | |

If every item cost the number in the table, the five features would spend 16.40 dollars and 3.60 dollars stay in reserve for future features: exactly the 20. **Collect's cap is 1,200 items a month** (6.00 dollars / 0.005), carried by `collectors.apify.monthly_items` in `research.yaml`; that is the only line of that file that changed.

## If only 3 keys work (15 dollars)
Everything scales with the keys: 

| Feature | Dollars | Items a month | Items a day |
|---|---|---|---|
| collect | 4.50 | 900 | 120 |
| voice | 3.75 | 750 | 100 |
| sessions read | 2.25 | 450 | 60 |
| reddit backup | 1.05 | 262 | 35 |
| analyze comments | 0.75 | 750 | 100 |
| reserve | 2.70 | - | - |

The voice lane reads the keys it really has, so it scales by itself; Collect's number follows `WORKING_KEYS` and `research.yaml`. The owner's overrides are `CONTENTINTEL_VOICE_APIFY_MONTHLY` and `CONTENTINTEL_VOICE_APIFY_DAILY` (0 = no daily limit).
