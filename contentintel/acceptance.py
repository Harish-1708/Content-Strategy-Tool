"""Live acceptance: prove, from the machine that does the work, that each declared route really answers.

Unit tests prove the code handles an answer correctly; they cannot prove a platform still talks to GitHub's
servers. An acceptance run does: it makes one small, real call per route, records exactly what happened
(time, version, route, how many items, the error code), and feeds the result to the health ledger. A route is
trusted ("healthy") only after one has passed recently, for the route as it is declared now.

Each result is kept, failures included, in data/_shared/acceptance.jsonl. The idea of a ledger of real
per-action results, with failures kept, follows Agent-Reach's release-acceptance record (MIT; see
THIRD_PARTY_NOTICES.md).

A route that cannot run because a secret is missing is reported as skipped, not failed, and stays
unverified. A route that is resting after a block is not asked again.
"""
import os
import time
from dataclasses import dataclass, field

from . import contract
from .contract import Failure
from .safety import scrub

KINDS = ("web", "news")
_NOT_RUN = ("skipped", "no login", "no key")          # the words check rows use when they could not even try


class Skipped(Exception):
    """A probe that could not run (a missing key, an exhausted cap): reported as skipped, never as failed."""


@dataclass
class Probe:
    source: str
    action: str
    route: str
    run: object = None          # () -> (items, detail); raises on failure
    skip: str = ""              # why it cannot run now (a missing secret), in plain words


@dataclass
class Context:
    """What the probes need. Each runner is a function; a missing one means the route cannot be checked on this machine."""
    registry: object
    collect_check: object = None      # (source, backend) -> a check_collectors row
    provider_check: object = None     # (provider name, kind) -> a check_providers row
    claude_check: object = None       # () -> detail
    catalogue_check: object = None    # () -> (items, detail)
    youtube_check: object = None      # () -> (items, detail), the yt-dlp route
    youtube_api_check: object = None  # () -> (items, detail), the official Data API route
    observatory_checks: dict = field(default_factory=dict)   # (source, route) -> () -> (items, detail), the competitor-site routes
    providers: tuple = ()             # provider names that have a key
    missing: dict = field(default_factory=dict)     # (source, route) -> reason


_OBSERVATORY_ACTIONS = {("competitor_stores", "public_json"): "catalogue_walk", ("competitor_stores", "sitemap"): "read_sitemap", ("competitor_pages", "static_fetch"): "read_page",
                        ("competitor_pages", "feed"): "read_feed", ("competitor_pages", "reader"): "read_via_reader"}


def row_result(row):
    """(items, detail) from a check row, or raise the failure it describes."""
    detail = str(row.get("detail") or row.get("problem") or "")
    if row.get("ok"):
        return int(row["items"] if "items" in row else row.get("count", 1)), detail
    if detail.startswith(_NOT_RUN):
        raise Skipped(detail)
    raise Failure(row.get("code") or contract.classify_message(detail) or contract.UPSTREAM_BROKEN, detail or "the check failed")


def build_probes(ctx, wanted=None):
    """One probe per enabled route that has something to check, in the order of sources.yaml."""
    probes = []
    for src in ctx.registry.sources.values():
        if src.status == "planned" or (wanted and src.id not in wanted):
            continue
        for rt in src.enabled_routes():
            key = (src.id, rt.name)
            skip = ctx.missing.get(key, "")
            if src.id in ("meta_ads", "tiktok", "instagram", "deep_research") and rt.name in ("browser", "apify"):
                backend = "apify" if rt.name == "apify" else "browser"
                if ctx.collect_check is None:
                    skip = skip or "no collection check on this machine"
                probes.append(Probe(src.id, "check", rt.name, None if skip else (lambda s=src.id, b=backend: row_result(ctx.collect_check(s, b))), skip))
            elif src.id == "web_search":
                for kind in KINDS:
                    reason = skip or ("" if rt.name in ctx.providers else "no API key for this service")
                    if ctx.provider_check is None:
                        reason = reason or "no search check on this machine"
                    probes.append(Probe(src.id, f"search_{kind}", rt.name, None if reason else
                                        (lambda n=rt.name, k=kind: row_result(ctx.provider_check(n, k))), reason))
            elif src.id == "shopify_catalogue":
                skip = skip or ("" if ctx.catalogue_check else "no brand catalogue to try")
                probes.append(Probe(src.id, "fetch_page", rt.name, None if skip else ctx.catalogue_check, skip))
            elif src.id == "youtube_public" and rt.name == "data_api":
                skip = skip or ("" if ctx.youtube_api_check else "no key: set YOUTUBE_API_KEY")
                probes.append(Probe(src.id, "video_details", rt.name, None if skip else ctx.youtube_api_check, skip))
            elif src.id == "youtube_public":
                skip = skip or ("" if ctx.youtube_check else "no yt-dlp on this machine")
                probes.append(Probe(src.id, "metadata", rt.name, None if skip else ctx.youtube_check, skip))
            elif src.id in ("competitor_stores", "competitor_pages"):
                action = _OBSERVATORY_ACTIONS.get(key, "check")
                fn = ctx.observatory_checks.get(key)
                skip = skip or ("" if fn else "no contact address or no brand website: set OBSERVATORY_CONTACT")
                probes.append(Probe(src.id, action, rt.name, None if skip else fn, skip))
            elif src.id == "claude":
                skip = skip or ("" if ctx.claude_check else "no ANTHROPIC_API_KEY")
                probes.append(Probe(src.id, "check", rt.name, None if skip else (lambda: (1, str(ctx.claude_check()))), skip))
    return probes


def run_acceptance(probes, *, health, store, now, version, run_id, clock=time.monotonic, platform=None):
    """Run each probe once. Returns one result per probe; passed and failed ones are written to the ledger and the health ledger."""
    platform = platform or ("github-actions" if os.environ.get("GITHUB_ACTIONS") else "other")
    results = []
    for p in probes:
        base = {"source": p.source, "action": p.action, "route": p.route, "items": 0, "code": "", "detail": "", "elapsed_ms": 0}
        rest = health.is_paused(p.source, p.route, now)
        if p.skip or rest:
            results.append({**base, "status": "skipped", "detail": p.skip or rest["message"]})
            continue
        t0 = clock()
        try:
            items, detail = p.run()
            if items < 1:
                raise Failure(contract.UPSTREAM_BROKEN, "answered, but nothing usable came back")
            passed, code, message = True, "", detail
        except Skipped as why:
            results.append({**base, "status": "skipped", "detail": str(why)})
            continue
        except Exception as exc:        # a probe may fail in any way; the failure is the finding
            err = contract.classify_exception(exc)
            items, passed, code, message = 0, False, err["code"], err["message"]
        ms = int((clock() - t0) * 1000)
        record = {"at": now, "run": run_id, "source": p.source, "action": p.action, "route": p.route, "items": items, "passed": passed,
                  "code": code, "message": scrub(message, 300), "elapsed_ms": ms, "version": version,
                  "route_hash": health.route_hash(p.source, p.route), "platform": platform}
        health.record_acceptance(record)
        store.append_acceptance(record)
        results.append({**base, "status": "passed" if passed else "failed", "items": items, "code": code, "detail": record["message"],
                        "elapsed_ms": ms})
    return results


def format_results(results):
    lines = ["Acceptance: one small live call per route (passed routes become healthy; skipped ones stay unverified)"]
    for r in results:
        label = f"{r['source']} / {r['route']} / {r['action']}"
        tail = (f"{r['items']} item(s) in {r['elapsed_ms']} ms" if r["status"] == "passed"
                else f"{r['code']}: {r['detail']}" if r["status"] == "failed" else r["detail"])
        lines.append(f"  {label:<52} {r['status'].upper():<8} {tail}"[:230])
    passed = sum(r["status"] == "passed" for r in results)
    failed = sum(r["status"] == "failed" for r in results)
    lines.append(f"  {passed} passed, {failed} failed, {len(results) - passed - failed} skipped")
    return "\n".join(lines)
