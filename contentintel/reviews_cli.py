"""`reviews`: fill and read the brands' customer-review stores (see reviews.py). import = the Judge.me export file (the complete route), collect = the Judge.me widget on each product page of the brand's own shop (the first page of
each product's reviews), status = what is stored. Nothing here writes to a brand's shop or Judge.me; no email address or IP address is read or kept."""
import time
from pathlib import Path

from . import reviews as R
from .brands import list_brand_slugs, load_brand


def add_parsers(sub, common):
    p = sub.add_parser("reviews", help="customer reviews kept word for word, so a brief can quote them and be checked: import the Judge.me export, collect from the product pages, or show what is stored")
    common(p)
    p.add_argument("--action", choices=["status", "import", "collect"], default="status")
    p.add_argument("--brand", default="all")
    p.add_argument("--file", default="", help="import: the Judge.me export (CSV) in the repository; default imports/<brand>-judgeme-reviews.csv")
    p.add_argument("--max-products", type=int, default=0, help="collect: the most products to read per brand (0 = all of them)")
    p.add_argument("--pause", type=float, default=1.0, help="collect: seconds between two requests")
    p.add_argument("--per-page", type=int, default=10, help="collect: reviews asked of Judge.me per page")
    p.add_argument("--time-budget", type=int, default=150, help="collect: minutes after which no new product is started (the next run continues)")
    p.add_argument("--topup-days", type=int, default=28, help="collect: a product read in full is topped up again after this many days")
    p.add_argument("--force", action="store_true", help="collect: top up every product now, whatever the date")


def _slugs(args):
    return list_brand_slugs(args.brands_dir) if args.brand == "all" else [args.brand]


MAX_PAGES = 600                      # per product: the most pages of reviews one run reads (a product with this many has more than 5,000 reviews)
STOP_AFTER_REFUSALS = 3              # Judge.me's page address refusing this many products in a row ends the run: the rest would be refused too


def _days_between(old, new):
    from datetime import date
    try:
        return (date.fromisoformat(str(new)[:10]) - date.fromisoformat(str(old)[:10])).days
    except ValueError:
        return 10 ** 6


def collect_brand(slug, args, store, now, get, sleep, clock):
    """Read the brand's reviews from the public Judge.me widget, ALL of them: each product page gives the shop and the product number and the first reviews, then Judge.me's own page address is read page by page until an empty page (the first
    reading of a product). A product already read in full is only topped up (newest pages until one holds nothing new) and only when the last top-up is older than --topup-days. Progress is saved after every product, so a run that ends at its
    time limit is simply continued by the next one. Returns 1 when nothing at all could be read."""
    brand = load_brand(slug, args.brands_dir)
    latest = store.load_latest(slug) or {}
    handles = sorted({p["handle"] for p in (latest.get("products") or {}).values() if isinstance(p, dict) and p.get("handle")})
    if args.max_products > 0:
        handles = handles[:args.max_products]
    if not handles or not brand.domain:
        print(f"{slug}: no product pages to read (no catalogue yet, or the brand has no shop address)")
        return 0
    data = R.load(store, slug)
    state = dict(data.get("collect") or {})

    def needs(handle):
        entry = state.get(handle) or {}
        return not entry.get("complete") or args.force or _days_between(entry.get("topup_at", ""), now) >= args.topup_days
    todo = sorted((h for h in handles if needs(h)), key=lambda h: bool((state.get(h) or {}).get("complete")))                     # products never read in full come first
    if not todo:
        newest = max((e.get("topup_at", "") for e in state.values()), default="")
        print(f"{slug}: up to date: all {len(handles)} product(s) were read in full and topped up on {newest[:10] or 'an earlier run'}; the next top-up is due {args.topup_days} days after that")
        return 0
    deadline = clock() + max(1, args.time_budget) * 60
    stats = {"products": 0, "pages": 0, "found": 0, "new": 0, "complete": 0, "notes": []}
    refused_in_a_row = 0
    for handle in todo:
        if clock() > deadline:
            stats["notes"].append(f"stopped at the time limit ({args.time_budget} minutes) with {len(todo) - stats['products']} product(s) still to read: the next run continues from there")
            break
        url = f"https://{brand.domain}/products/{handle}"
        status, html = get(url)
        sleep(max(0.0, args.pause))
        if status != 200 or not html:
            stats["notes"].append(f"{handle}: the product page answered {status or 'nothing'}")
            continue
        known = {r["id"] for r in R.load(store, slug).get("reviews", [])}
        first_reading = not (state.get(handle) or {}).get("complete")
        found = R.parse_widget(html, handle=handle, url=url)
        info = R.shop_info(html)
        pages, complete, note = 0, False, ""
        if info["shop"] and info["product_id"]:
            page = 1
            while page <= MAX_PAGES:
                status, text = get(R.page_url(info["shop"], info["product_id"], page, args.per_page))
                sleep(max(0.0, args.pause))
                if status != 200:
                    note = f"Judge.me's page address answered {status or 'nothing'} on page {page}"
                    break
                got = R.parse_page(text, handle, url)
                if not got:
                    complete = True                                                                  # an empty page: that was the last one
                    break
                for r in got:
                    r["handle"] = handle
                found += got
                pages += 1
                if not first_reading and all(r["id"] in known for r in got):
                    complete = True                                                                  # a top-up has caught up with what is stored
                    break
                page += 1
            else:
                note = f"stopped at the page limit ({MAX_PAGES} pages)"
        else:
            complete = True
            note = "no Judge.me widget (shop and product number) was found on this product page: it has no reviews drawn, or the widget is not Judge.me's"
        refused_in_a_row = refused_in_a_row + 1 if note.startswith("Judge.me's page address answered") else 0
        added, total = R.merge(store, slug, found, "judge.me widget (all review pages)", now,
                               progress={handle: {"complete": complete, "pages": pages, "found": len({r["id"] for r in found}), "note": note, "read_at": now, "topup_at": now if complete else (state.get(handle) or {}).get("topup_at", "")}})
        state[handle] = R.load(store, slug)["collect"][handle]
        stats["products"] += 1
        stats["pages"] += pages
        stats["found"] += len({r["id"] for r in found})
        stats["new"] += added
        stats["complete"] += 1 if complete else 0
        if note and not complete:
            stats["notes"].append(f"{handle}: {note}")
        if refused_in_a_row >= STOP_AFTER_REFUSALS:
            stats["notes"].append(f"stopped: Judge.me's page address refused {STOP_AFTER_REFUSALS} products in a row, so the rest would be refused too (the first page of each product's reviews was still kept)")
            break
    total_now = len(R.load(store, slug).get("reviews", []))
    print(f"{slug}: read {stats['products']} of {len(todo)} product(s) ({stats['pages']} page(s) of reviews), found {stats['found']} review(s), {stats['new']} new; the store holds {total_now}; "
          f"{stats['complete']} of {stats['products']} product(s) read in full" + ("".join(f"\n{slug}:   {n}" for n in stats["notes"][:8])))
    return 1 if stats["products"] == 0 and stats["notes"] else 0


def run_reviews(args, store, now, get=None, sleep=time.sleep, clock=time.monotonic):
    if get is None:
        from .research_tools import default_get
        get = lambda url: default_get(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=30)          # noqa: E731
    worst = 0
    for slug in _slugs(args):
        if args.action == "status":
            data = R.load(store, slug)
            reviews = data.get("reviews", [])
            five = [r for r in reviews if r.get("rating", 0) >= 5]
            by_source = {}
            for r in reviews:
                by_source[r.get("source") or "unknown"] = by_source.get(r.get("source") or "unknown", 0) + 1
            progress = data.get("collect") or {}
            print(f"{slug}: " + (f"{sum(1 for e in progress.values() if e.get('complete'))} of {len(progress)} product(s) read in full; " if progress else "") + f"{len(reviews)} review(s) stored, {len(five)} of them five star, newest {max((r.get('date', '') for r in reviews), default='none') or 'none'}; by source: "
                  + (", ".join(f"{k} {v}" for k, v in sorted(by_source.items())) or "none") + ("" if reviews else "; a brief cannot quote a customer for this brand until reviews are imported or collected"))
        elif args.action == "import":
            path = Path(args.file or f"imports/{slug}-judgeme-reviews.csv")
            if not path.is_file():
                print(f"{slug}: no export file at {path}: upload the Judge.me export there (Judge.me > Reviews > Export) or name another file")
                worst = 1
                continue
            reviews, skipped = R.parse_export(path.read_text(encoding="utf-8", errors="replace"))
            added, total = R.merge(store, slug, reviews, "judge.me export", now)
            print(f"{slug}: read {len(reviews)} review(s) from {path} ({skipped} row(s) had no review text), {added} new; the store holds {total}")
            if not reviews:
                worst = 1
        else:
            worst = max(worst, collect_brand(slug, args, store, now, get, sleep, clock))
    return worst
