"""Completeness gate: decide whether a fetched catalogue can be trusted.

Statuses
  pass         safe to use
  warn         usable, but a person should look (reasons explain why)
  fail         do not use; the previous good snapshot stays in place
  unbaselined  looks fine but no person has confirmed the product count yet
Generation is allowed only for pass and warn.
"""
from dataclasses import dataclass, field

# Common storefront page sizes. A count that equals one of them, without an empty
# final page to prove the end, may simply be the first page.
DEFAULT_PAGE_SIZES = (30, 50, 250)


@dataclass
class GateResult:
    status: str
    count: int
    reasons: list = field(default_factory=list)
    reference_count: int = None
    baseline_count: int = None

    @property
    def can_generate(self):
        return self.status in ("pass", "warn")

    def codes(self, level=None):
        return [r["code"] for r in self.reasons if level is None or r["level"] == level]


def _reason(level, code, message):
    return {"level": level, "code": code, "message": message}


def check_completeness(count, meta, *, cfg, reference_count=None, baseline_count=None,
                       feed_handles=None, sitemap_handles=None, problems=None):
    meta = meta or {}
    reasons = []

    if count == 0:
        reasons.append(_reason("fail", "empty_catalogue", "The fetch returned no products."))
    if problems:
        sample = ", ".join(p["product_id"] for p in problems[:5])
        reasons.append(_reason("fail", "malformed_products",
                               f"{len(problems)} product(s) could not be read (ids: {sample})."))
    if meta.get("duplicate_ids"):
        reasons.append(_reason("fail", "duplicate_ids",
                               "The same product id came back on more than one page."))
    if meta.get("loop_detected"):
        reasons.append(_reason("fail", "paging_loop",
                               "Paging returned only products already seen, so the end was never confirmed."))
    if meta.get("max_pages_hit"):
        reasons.append(_reason("fail", "no_final_page",
                               "The page limit was reached before an empty page confirmed the end."))

    min_abs = cfg["min_abs_change"]
    if reference_count:
        if count < reference_count:
            drop = (reference_count - count) / reference_count
            gap = reference_count - count
            if drop > cfg["fail_drop"] and gap >= min_abs:
                reasons.append(_reason(
                    "fail", "drop_vs_last_run",
                    f"Product count fell from {reference_count} to {count} ({drop:.0%} drop)."))
            elif drop > cfg["warn_change"] and gap >= min_abs:
                reasons.append(_reason(
                    "warn", "drop_vs_last_run",
                    f"Product count fell from {reference_count} to {count} ({drop:.0%} drop)."))
        elif count > reference_count:
            growth = (count - reference_count) / reference_count
            if growth > cfg["warn_growth"] and count - reference_count >= min_abs:
                reasons.append(_reason(
                    "warn", "growth_vs_last_run",
                    f"Product count grew from {reference_count} to {count} ({growth:.0%})."))

    if baseline_count and count < cfg["baseline_floor"] * baseline_count:
        reasons.append(_reason(
            "fail", "below_baseline_floor",
            f"Product count {count} is below {cfg['baseline_floor']:.0%} of the confirmed "
            f"baseline of {baseline_count}."))

    confirmed_end = meta.get("terminal_empty_page") is True
    if not confirmed_end and count in DEFAULT_PAGE_SIZES:
        reasons.append(_reason(
            "warn", "default_page_size",
            f"The count ({count}) equals a common default page size and no empty final page "
            f"confirms the end. Check this is the whole catalogue."))

    if sitemap_handles is not None:
        if not sitemap_handles:
            reasons.append(_reason("warn", "sitemap_empty",
                                   "The sitemap listed no products, so it could not cross-check the feed."))
        else:
            missing = sorted(set(sitemap_handles) - set(feed_handles or []))
            ratio = len(missing) / len(set(sitemap_handles))
            if missing and ratio > cfg["sitemap_fail_missing"] and len(missing) >= min_abs:
                level = "fail"
            elif missing and ratio > cfg["sitemap_warn_missing"] and len(missing) >= min_abs:
                level = "warn"
            else:
                level = None
            if level:
                reasons.append(_reason(
                    level, "sitemap_mismatch",
                    f"{len(missing)} product(s) in the site sitemap are missing from the feed "
                    f"(for example: {', '.join(missing[:5])})."))

    levels = {r["level"] for r in reasons}
    if "fail" in levels:
        status = "fail"
    elif not baseline_count:
        status = "unbaselined"
        reasons.append(_reason("info", "needs_baseline",
                               f"No confirmed baseline yet. A person must confirm the product count "
                               f"({count}) before generation is allowed."))
    elif "warn" in levels:
        status = "warn"
    else:
        status = "pass"
    return GateResult(status=status, count=count, reasons=reasons,
                      reference_count=reference_count, baseline_count=baseline_count)
