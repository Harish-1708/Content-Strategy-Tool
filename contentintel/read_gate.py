"""One gate for every read of a careful account (Instagram, TikTok): one last-read clock, one daily page count, and the outcome of the last read, shared by Collect and Acceptance.

Before this, Collect enforced the gap (min_hours_between_runs) and the daily page cap and recorded when it last read an account, while Acceptance's check called the reader directly: it ignored the gap, did not count toward the day and was
not recorded. On 26 Nov Collect read Instagram at 09:38 UTC and Acceptance read it again at 16:58, inside the 12 hour gap, and met the login wall.
Now:
* every read, by either, is recorded in data/_shared/page_log.json: when (the gap counts from here), by whom, how it ended (ok, login wall, challenge, warning, error) and how many pages it opened (they count toward the day);
* Acceptance asks the gate first (decide()). Inside the gap it does NOT read: it reports the route from the last read's outcome ("proven by Collect's read at 09:38 UTC, 7.3 h ago"). Past the gap, with page budget left, it reads and records its own read, so Collect then waits its turn;
* Collect keeps priority: it is the real use of the account, and its own gap and caps are unchanged; an Acceptance read only ever happens when Collect would also be allowed to read.
The same pages are read, spaced out: nothing here limits what is read, only when."""
from datetime import datetime

OK, LOGIN_WALL, CHALLENGE, WARNING, ERROR = "ok", "login_wall", "challenge", "warning", "error"
HISTORY = 10


def _hours_since(then, now):
    try:
        return (datetime.fromisoformat(str(now).replace("Z", "+00:00")) - datetime.fromisoformat(str(then).replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        return None


def classify(detail):
    """How a read ended, from its message: the words a login wall, a challenge or an account warning use."""
    text = str(detail or "").lower()
    if "warn" in text or any(w in text for w in ("automated behavior", "action blocked", "suspended")):
        return WARNING
    if any(w in text for w in ("challenge", "checkpoint", "security check", "captcha")):
        return CHALLENGE
    if any(w in text for w in ("login page", "not accepted", "not logged in", "login wall", "log in")):
        return LOGIN_WALL
    return ERROR


def record(page_log, site, now, *, by, state, pages=0, detail=""):
    """Write one read into the page log: the clock (the gap counts from now), the day's page count, and the outcome. `page_log` is store.read_page_log()'s dict; the caller writes it back."""
    page_log.setdefault("last", {})[site] = now
    if pages:
        day = page_log.setdefault("days", {}).setdefault(str(now)[:10], {})
        day[site] = day.get(site, 0) + pages
    entry = {"at": now, "by": by, "state": state, "pages": pages, "detail": " ".join(str(detail or "").split())[:240]}
    page_log.setdefault("outcomes", {})[site] = entry
    hist = page_log.setdefault("history", {}).setdefault(site, [])
    hist.append(entry)
    del hist[:-HISTORY]
    return entry


def decide(page_log, site, site_cfg, now):
    """('read', None) when a read is allowed now, ('reuse', outcome) inside the gap, ('capped', outcome) when the day's pages are used. `outcome` is the last recorded read (None when nothing recorded its outcome)."""
    gap, cap = site_cfg.get("min_hours_between_runs"), site_cfg.get("max_pages_per_day")
    last = (page_log.get("last") or {}).get(site)
    outcome = (page_log.get("outcomes") or {}).get(site)
    if gap and last:
        gone = _hours_since(last, now)
        if gone is not None and gone < gap:
            return "reuse", outcome or {"at": last, "by": "an earlier run", "state": "unknown", "pages": 0, "detail": ""}
    if cap and ((page_log.get("days") or {}).get(str(now)[:10]) or {}).get(site, 0) >= cap:
        return "capped", outcome
    return "read", None


def reuse_row(source, action, outcome, site_cfg, now):
    """The Acceptance row when the gate says not to read: the route reported from the last read's own outcome."""
    outcome = outcome or {}
    when = str(outcome.get("at", ""))
    ago = _hours_since(when, now)
    where = f"{outcome.get('by', 'an earlier run')}'s read at {when[11:16] or '?'} UTC" + (f" ({ago:.1f} h ago)" if ago is not None else "")
    state = outcome.get("state", "unknown")
    why = (f"inside the {site_cfg.get('min_hours_between_runs'):g} hour gap" if action == "reuse" else f"the day's {site_cfg.get('max_pages_per_day')} pages are used")
    if state == OK:
        return {"source": source, "ok": True, "detail": f"proven by {where}: {outcome.get('detail') or 'the login looked fine and pages were read'}; not read again ({why})"}
    if state == "unknown":
        return {"source": source, "ok": False, "detail": f"not read again ({why}) and {where} recorded no outcome, so this route is neither proven nor refuted now; it is checked after the gap"}
    return {"source": source, "ok": False, "detail": f"not read again ({why}); {where} ended with {state.replace('_', ' ')}: {outcome.get('detail') or 'no detail was recorded'}"}
