"""The provider connection test: every provider in the pool (FreeLLMAPI's and OmniRoute's), every approved route, one tiny call each, and a plain report of who connected, who failed and why, and who was not tried and why.
Claude is never used: the command removes the repository's Claude key from the environment before anything starts, and a route to Claude is left out."""
import statistics


def targets(router, lanes):
    """Every distinct (provider, model) route in the lanes' own chains and the shared pool, except Claude's, in a stable order."""
    seen, out = set(), []
    for lane in lanes.values():
        if lane.name == "canary":
            continue
        for r in list(lane.chain) + list(router.pool):
            key = (r["provider"], r["model"])
            if r["provider"] != "claude" and key not in seen:
                seen.add(key)
                out.append(r)
    return out


def run_connect(router, registry, lanes, notes, now):
    per = {}
    for r in targets(router, lanes):
        res = router.run_route("canary", r, instructions="Reply with the single word OK.", now=now, ignore_rests=True)
        slot = per.setdefault(r["provider"], {"ok": [], "failed": [], "skipped": []})
        if res["status"] == "ok":
            slot["ok"].append((r["model"], getattr(res.get("completion"), "latency_ms", 0) or 0))
        elif res["status"] == "failed":
            slot["failed"].append((r["model"], str(res.get("reason", "failed"))[:200]))
        else:
            slot["skipped"].append((r["model"], str(res.get("reason", "not tried"))[:200]))
    rows = []
    for pid, p in registry.providers.items():
        if pid == "claude":
            continue
        slot = per.get(pid)
        if slot is None:
            rows.append({"provider": pid, "label": p.label, "state": "not tried", "why": "no approved model in any lane or the shared pool", "tried": 0})
        elif slot["ok"]:
            rows.append({"provider": pid, "label": p.label, "state": "connected", "ok": len(slot["ok"]), "tried": len(slot["ok"]) + len(slot["failed"]), "median_ms": int(statistics.median(ms for _m, ms in slot["ok"])),
                         "failed": slot["failed"]})
        elif slot["failed"]:
            rows.append({"provider": pid, "label": p.label, "state": "failed", "why": slot["failed"][0][1], "tried": len(slot["failed"]), "failed": slot["failed"]})
        else:
            rows.append({"provider": pid, "label": p.label, "state": "not tried", "why": slot["skipped"][0][1], "tried": 0})
    return {"rows": rows, "notes": list(notes)}


def format_report(report):
    rows = report["rows"]
    connected = [r for r in rows if r["state"] == "connected"]
    failed = [r for r in rows if r["state"] == "failed"]
    skipped = [r for r in rows if r["state"] == "not tried"]
    lines = [f"Provider connection test (the repository's Claude key is not used): {len(connected)} of {len(rows)} providers connected, {len(failed)} failed, {len(skipped)} not tried"]
    for note in report["notes"]:
        lines.append("  " + note)
    for r in connected:
        lines.append(f"  CONNECTED  {r['provider']}: {r['ok']} of {r['tried']} route(s) answered, median {r['median_ms']} ms" + (f"; failed: {r['failed'][0][0]}: {r['failed'][0][1]}" if r["failed"] else ""))
    for r in failed:
        lines.append(f"  FAILED     {r['provider']}: {r['why']} ({r['tried']} route(s) tried)")
    for r in skipped:
        lines.append(f"  NOT TRIED  {r['provider']}: {r['why']}")
    return "\n".join(lines)
