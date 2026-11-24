"""The Apify money, split per feature. The allowance is 5 dollars per working key a month: 4 working keys = 20 dollars (key 3 was checked and replaced on 2026-11-23), shared by EVERY feature that uses Apify. This file is where
the split is written down and where every cap in the tool comes from, so no number is a guess and no feature can eat the month. Each feature gets a share of the dollars, items = dollars / what one item costs, and a DAILY cap of
four days' worth of its month, so even a feature that runs every hour cannot use its whole month in two or three days. The per-run dollar cap (1 dollar for Collect: research.yaml max_cost_per_run_usd; 3 dollars for the voice lane) and the per-call cost ceilings stay as guardrails.

WHAT AN ITEM COSTS. Read from Apify's published pay-per-result prices on 2026-11-19, at the FREE plan's list price (the highest one):
  TikTok scraper (clockworks): 5.00 dollars per 1,000 results = 0.005 an item;   TikTok comments scraper (clockworks): 1.00 dollars per 1,000 = 0.001 an item.
ASSUMPTIONS, NOT LIST PRICES (no price was found for them): Instagram and the sessions page reader 0.005 an item (TikTok's price, assumed) and Reddit 0.004 an item (assumed). If they cost more, the per-call ceiling
(0.25 dollars a call) and the 3 dollar per-run cap limit the damage, and the next price check corrects the number here.

                       share   dollars (20)   items a month   items a day (at most)
  collect (Collect)     30%     6.00           1200            160
  voice                 25%     5.00           1000            134
  sessions_read         15%     3.00           600             80
  reddit_backup          7%     1.40           349             47
  analyze_comments       5%     1.00           1000            134
  reserve (future)      18%     3.60           -               -
  (items = dollars / cost; the daily number = items / 30 x 4, rounded up; the shares add to 100%, the dollars to 20.00)

With fewer working keys everything scales down with the keys (5 dollars each). The numbers below are computed, never typed in twice."""
import math

MONTH_USD_PER_KEY = 5.0
WORKING_KEYS = 4                       # the keys that work today (key 3 was checked and replaced on 2026-11-23): the built-in Collect default and research.yaml follow it
SHARES = {"collect": 0.30, "voice": 0.25, "sessions_read": 0.15, "reddit_backup": 0.07, "analyze_comments": 0.05, "reserve": 0.18}
COST_PER_ITEM = {"collect": 0.005, "voice": 0.005, "sessions_read": 0.005, "reddit_backup": 0.004, "analyze_comments": 0.001}
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
