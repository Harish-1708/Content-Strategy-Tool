"""The Apify money, split per feature. The allowance is 5 dollars per working key a month: 4 working keys = 20 dollars, shared by EVERY feature that uses Apify. This file is where the split is written down and where every
cap in the tool comes from, so no number is a guess and no feature can eat the month. Each feature gets a share of the dollars, items = dollars / what one item costs, and a DAILY cap of four days' worth of its month, so even a
feature that runs every hour cannot use its whole month in two or three days. The per-run dollar caps (1 dollar for Collect: research.yaml max_cost_per_run_usd; 3 dollars for the voice lane) and the per-call cost ceilings
(0.25 dollars a call for page reads) stay as guardrails.

WHAT AN ITEM COSTS, read from the actor's own Apify page (2026-11-24; the highest listed price is used where a page says "from"):
  TikTok scraper (clockworks) 5.00 dollars per 1,000 = 0.005; TikTok comments (clockworks) 1.00 per 1,000 = 0.001;
  Reddit Scraper Lite (trudax/reddit-scraper-lite) 3.40 per 1,000 = 0.0034;
  X tweet scraper (khadinakbar/x-tweet-scraper) 0.002 a tweet (its page: 1.84 to 2.00 per 1,000; the free plan returns up to 50 tweets a run);
  LinkedIn company posts (harvestapi/linkedin-company-posts) 2.00 per 1,000 = 0.002 (its pricing page; "from 1.50").
STILL AN ASSUMPTION, NOT READ: Instagram and the sessions page reader (0.005 an item).

                          share   dollars (20)   items a month   items a day (at most)
  collect (Collect)        30%     6.00           1200            160
  voice                    25%     5.00           1000            134
  sessions_read            10%     2.00            400             54     (was 15%, 3.00, 600: 1.00 moved to X and LinkedIn)
  reddit_backup             7%     1.40            411             55     (cost read: 0.0034, was assumed 0.004)
  analyze_comments          5%     1.00           1000            134
  x_read                  2.5%     0.50            250             34     (new)
  linkedin_read           2.5%     0.50            250             34     (new)
  reserve (future)         18%     3.60            -               -
  (items = dollars / cost; the daily number = items / 30 x 4, rounded up; the shares add to 100%, the dollars to 20.00)

With fewer working keys everything scales down with the keys (5 dollars each). The numbers below are computed, never typed in twice."""
import math

MONTH_USD_PER_KEY = 5.0
WORKING_KEYS = 4                       # the keys that work today (key 3 was checked and replaced on 2026-11-23): the built-in Collect default and research.yaml follow it
SHARES = {"collect": 0.30, "voice": 0.25, "sessions_read": 0.10, "reddit_backup": 0.07, "analyze_comments": 0.05, "x_read": 0.025, "linkedin_read": 0.025, "reserve": 0.18}
COST_PER_ITEM = {"collect": 0.005, "voice": 0.005, "sessions_read": 0.005, "reddit_backup": 0.0034, "analyze_comments": 0.001, "x_read": 0.002, "linkedin_read": 0.002}
DAY_FACTOR = 4                                                                       # a feature may use at most this many days' worth of its month in one day


def plan(keys=4):
    """{feature: {usd, items, per_day}} for the number of working keys; the reserve has dollars only."""
    total = MONTH_USD_PER_KEY * max(1, int(keys))
    out = {}
    for feature, share in SHARES.items():
        usd = round(total * share, 2)
        if feature in COST_PER_ITEM:
            items = int(usd / COST_PER_ITEM[feature])
            out[feature] = {"usd": usd, "items": items, "per_day": max(10, math.ceil(items / 30 * DAY_FACTOR))}
        else:
            out[feature] = {"usd": usd, "items": 0, "per_day": 0}
    return out


def items(feature, keys=4):
    return plan(keys)[feature]["items"]


def counter(feature):
    return f"apify_{feature}_items"


def day_used(ledger, name, day):
    return ledger.usage.get("days", {}).get(day, {}).get(name, 0)


def charge_day(ledger, name, day, n):
    days = ledger.usage.setdefault("days", {})
    today = days.setdefault(day, {})
    today[name] = round(today.get(name, 0) + n, 4)
    for old in sorted(days)[:-3]:                                                    # only the last three days are kept
        del days[old]


def room(feature, ledger, day, keys=4):
    """Items this feature may still read now: what is left of its month and of its day."""
    p = plan(keys)[feature]
    return max(0, min(p["items"] - ledger.month_used(counter(feature)), p["per_day"] - day_used(ledger, counter(feature), day)))


def charge(feature, ledger, day, n):
    """Count n items against the feature's month and day, and against the shared Apify counter."""
    ledger.charge(counter(feature), n)
    ledger.charge("apify_items", n)
    charge_day(ledger, counter(feature), day, n)


def report(keys=4):
    p = plan(keys)
    lines = [f"Apify money plan for {keys} key(s): {MONTH_USD_PER_KEY * keys:.2f} dollars a month, the 3 dollar per-run cap stays"]
    for f, v in p.items():
        lines.append(f"  {f}: {v['usd']:.2f} dollars" + (f" = {v['items']} items a month at most {COST_PER_ITEM[f]} each, {v['per_day']} a day at most" if v["items"] else " kept in reserve"))
    return lines
