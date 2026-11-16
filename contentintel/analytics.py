"""The stores' own numbers, ready for the day access is granted, and the public signals that need no access.

GA4 (Google Analytics Data API) and Search Console (Search Analytics API) are read through the Google service account the tool already uses for Sheets
(GOOGLE_SERVICE_ACCOUNT_JSON), with READ-ONLY scopes. Until the store owner adds that account as a Viewer on a GA4 property and as a user on a Search Console
property, Google answers 403 and this module says so: the status is `no_access`, there are no rows, and `needs_input()` lists exactly what is missing. A number
is NEVER invented, estimated or filled in from anywhere else: a skill that needs the store's own numbers stays on NEEDS_INPUT until a read succeeded.

The public side needs no access: `serper_rank` asks a search engine (Serper) where a domain stands for a keyword, a signal anyone can see.

Per brand, an optional brands/<slug>/analytics.yaml holds ga4_property_id, search_console_site, domain and rank_keywords; with none of it the brand is `not_configured`."""
import json
import re
from pathlib import Path
from urllib.parse import quote

import yaml

SCOPES = ("https://www.googleapis.com/auth/analytics.readonly", "https://www.googleapis.com/auth/webmasters.readonly")
GA4_URL = "https://analyticsdata.googleapis.com/v1beta/properties/{property}:runReport"
GSC_URL = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
OK, NO_ACCESS, NOT_CONFIGURED, ERROR = "ok", "no_access", "not_configured", "error"


def load_config(brands_dir, slug):
    """The brand's analytics.yaml as a dict (empty when absent or unreadable). ga4_property_id is kept as digits only."""
    path = Path(brands_dir) / slug / "analytics.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        data = {}
    data = data if isinstance(data, dict) else {}
    prop = re.sub(r"\D", "", str(data.get("ga4_property_id") or ""))
    return {"ga4_property_id": prop, "search_console_site": str(data.get("search_console_site") or "").strip(), "domain": str(data.get("domain") or "").strip().lower(),
            "rank_keywords": [str(k).strip() for k in (data.get("rank_keywords") or []) if str(k).strip()] if isinstance(data.get("rank_keywords"), list) else []}


def connect(key_json):
    """(an authorised session with read-only analytics and Search Console scopes, the service account's e-mail). The key is never echoed."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    info = json.loads(key_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=list(SCOPES))
    return AuthorizedSession(creds), info.get("client_email", "")


def _read(session, url, body, what, email):
    """A dict with status, reason and rows: rows only on success."""
    try:
        reply = session.post(url, json=body, timeout=60)
    except Exception as exc:                                                        # noqa: BLE001 - a network failure is reported, not raised
        return {"status": ERROR, "reason": f"{what}: the request failed ({type(exc).__name__})", "rows": None}
    code = getattr(reply, "status_code", 0)
    if code in (401, 403):
        return {"status": NO_ACCESS, "reason": f"{what}: Google refused ({code}): the service account {email or '(its e-mail is in GOOGLE_SERVICE_ACCOUNT_JSON as client_email)'} has no access yet; "
                                                "the store owner must add it as a Viewer (GA4) or a user (Search Console), and the API must be switched on in its Google Cloud project", "rows": None}
    if code != 200:
        return {"status": ERROR, "reason": f"{what}: Google answered HTTP {code}", "rows": None}
    try:
        return {"status": OK, "reason": "", "rows": reply.json().get("rows") or []}
    except ValueError:
        return {"status": ERROR, "reason": f"{what}: the reply was not readable", "rows": None}


def ga4_report(session, property_id, email="", days=28):
    if not property_id:
        return {"status": NOT_CONFIGURED, "reason": "no ga4_property_id in the brand's analytics.yaml", "rows": None}
    body = {"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}], "dimensions": [{"name": "sessionDefaultChannelGroup"}],
            "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}, {"name": "conversions"}, {"name": "totalRevenue"}]}
    return _read(session, GA4_URL.format(property=property_id), body, f"GA4 property {property_id}", email)


def search_console_report(session, site, email="", days=28, today=None):
    if not site:
        return {"status": NOT_CONFIGURED, "reason": "no search_console_site in the brand's analytics.yaml", "rows": None}
    from datetime import date, timedelta
    end = today or date.today()
    body = {"startDate": str(end - timedelta(days=days)), "endDate": str(end), "dimensions": ["query"], "rowLimit": 100}
    return _read(session, GSC_URL.format(site=quote(site, safe="")), body, f"Search Console {site}", email)


def serper_rank(search, keyword, domain, depth=10):
    """Where `domain` stands for `keyword` in the search results (1 = first), or None when it is not in the first `depth`; a public signal that needs no access.
    `search(query)` returns result dicts with a url. {'keyword', 'position', 'url'}: the url is the domain's own result, if any."""
    domain = (domain or "").lower().removeprefix("www.")
    for pos, r in enumerate(list(search(keyword) or [])[:depth], 1):
        host = re.sub(r"^https?://(www\.)?", "", str(r.get("url") or "").lower()).split("/")[0]
        if domain and (host == domain or host.endswith("." + domain)):
            return {"keyword": keyword, "position": pos, "url": r.get("url", "")}
    return {"keyword": keyword, "position": None, "url": ""}


def needs_input(report):
    """What a skill that needs the store's own numbers must ask for instead of inventing them: one line per source that has no data."""
    out = []
    for name, label in (("ga4", "GA4 numbers (sessions, engaged sessions, conversions, revenue)"), ("search_console", "Search Console queries, clicks and impressions")):
        part = (report or {}).get(name) or {}
        if part.get("status") != OK:
            out.append(f"NEEDS_INPUT: {label}. {part.get('reason') or 'not read yet'}")
    return out


def build_report(brands_dir, slug, key_json="", search=None, fetch=None):
    """The full picture for one brand: GA4 and Search Console (only with access) and the public Serper ranks. `fetch` is for tests (an already-connected (session, email))."""
    cfg = load_config(brands_dir, slug)
    broken = ""
    try:
        session, email = fetch if fetch is not None else ((None, "") if not (key_json or "").strip() else connect(key_json))
    except Exception as exc:                                                       # noqa: BLE001 - a damaged key is a reason in the report, never a crash
        session, email, broken = None, "", f"GOOGLE_SERVICE_ACCOUNT_JSON could not be used ({type(exc).__name__}): check that the whole key file was pasted"
    if session is None:
        why = broken or "GOOGLE_SERVICE_ACCOUNT_JSON is not set in this run"
        ga4 = {"status": (ERROR if broken else NO_ACCESS) if cfg["ga4_property_id"] else NOT_CONFIGURED, "reason": why if cfg["ga4_property_id"] else "no ga4_property_id in the brand's analytics.yaml", "rows": None}
        gsc = {"status": (ERROR if broken else NO_ACCESS) if cfg["search_console_site"] else NOT_CONFIGURED, "reason": why if cfg["search_console_site"] else "no search_console_site in the brand's analytics.yaml", "rows": None}
    else:
        ga4, gsc = ga4_report(session, cfg["ga4_property_id"], email), search_console_report(session, cfg["search_console_site"], email)
    ranks = [serper_rank(search, k, cfg["domain"]) for k in cfg["rank_keywords"]] if search is not None and cfg["domain"] else []
    report = {"brand": slug, "ga4": ga4, "search_console": gsc, "serper_ranks": ranks, "public_signals_only": not (ga4["status"] == OK or gsc["status"] == OK)}
    report["needs_input"] = needs_input(report)
    return report
