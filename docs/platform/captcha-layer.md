# Captcha layer — warm-up, search-first, graceful failure, Apify async

Built by Muse at Harish's direct instruction (2026-11-16): implement ALL the
measures from the TikTok captcha research at once, as one additive layer,
generalised across platforms — not TikTok-only. Base: repo HEAD `1d22823`
(Update 68, pushed and verified). Staged only — NOT pushed, no Actions run
dispatched, no message to the developer.

Spec: `hidden_files/tiktok-captcha-research.md` (the ranked methods and the
4-item shortlist). This layer builds the shortlist, not the not-recommended
list. **A puzzle is never solved** — that standing rule is unchanged
everywhere in this package.

## Why this layer exists

Collect #64 (2026-11-16, Swoveralls, fresh, TikTok on) failed on TikTok
only: the page opened but TikTok served a puzzle captcha ("Drag the puzzle
piece into place") and no posts could be read. Meta Ads in the same run
worked (60 items). The research found the failure was on the network layer,
not the behaviour layer — the browser behaved human-like (human scores
35/76/87, real pacing) but the traffic came from a GitHub datacenter IP
with no proxy configured, and TikTok scores IP reputation first. The layer
below lowers the score on every layer it can reach, and stops one captcha
from failing a whole run when it still appears.

## What was built, item by item

### 1. Prevention bundle — warm-up + search-first + captcha detection

**Warm-up (`contentintel/warmup.py`, new).** `warm_up(browser, site_cfg,
log=None)` returns `{"visited": [...], "token_found": bool,
"seconds": float}`. Before any listing-page read it visits one public video
page and one public profile page and polls the cookie jar for the clearance
cookie (TikTok's `_waftokenid`, which the research measured as scoped per
page kind — a video-page token clears video pages only, a profile-page
token clears profile pages only, and a homepage mint gives no token at
all). Every page kind is visited even when the token was already found on
an earlier one. Nothing here ever raises: a stale page or an unreadable
jar is logged plainly and the run carries on without it. It never solves,
never logs in, and it is deliberately not the read path, so warm-up visits
never land in the per-read log.

Wired into `browser.py:open_browser` after the session/browser is ready,
before returning; it prints `warm-up <site>: visited N page(s), clearance
token found: yes/no`. ON by default when URLs are configured; the owner can
turn it off per site (`warmup.enabled: false`). The config is validated in
`load_browser_settings` (https URLs, bool, non-empty token, non-negative
waits).

`browser.yaml` warm-up blocks: TikTok — video
`https://www.tiktok.com/@tiktok/video/7238789584926477594` + profile
`https://www.tiktok.com/@tiktok`, token `_waftokenid`, 5 s wait / 10 s poll.
Instagram — profile `https://www.instagram.com/instagram/` + post
`https://www.instagram.com/p/Cuw6X2mOqXG/`, token `csrftoken` (Instagram has
no WAF clearance cookie; this only confirms session cookies are present).
**Meta Ads: no warm-up block, deliberately** — Ad Library reads are public
pages, not a logged-in session.

**Search-first (`collectors.py`, `browser.yaml`).** The code used to open
`/tag/{tag}` first and fall back to searching for the tag on error
(`hashtag_via_search_on_error`). A per-site flag `hashtag_via_search_first`
(default False when absent) reverses the order: search `#tag` first, tag
page as fallback if the search yields nothing. TikTok has the flag on
(`browser.yaml`); the flag is generic — any source with hashtag-style
pages can use it. Search results are relevance-ranked, not the
recency-ordered tag feed, so "top videos for #tag" may miss the newest
posts; that is acceptable for trend/idea discovery and is stated plainly.

**Captcha detection (Harish's spot, verified against the real page).**
TikTok's `challenge_any` in `browser.yaml` was `['text=Verify to
continue', 'text=Drag the slider', '#captcha-verify-image', 'text=Select 2
objects']` — NONE of those matched the dialog Collect #64 actually showed,
so the code never detected the challenge, `clear_challenge` was never
called, and no search fallback triggered. Verified in the run's own debug
page (`empty-tiktok.html`): the dialog text `Drag the puzzle piece into
place` 1×, `#captcha-verify-container-main-page` 1×, `#captcha_slide_button`
1×, `#captcha_close_button` 1× — and the four older entries 0× each. The
list now also contains `'text=Drag the puzzle piece into place'`,
`'#captcha-verify-container-main-page'`, `'#captcha_slide_button'`; the
four old entries are kept. The X is a real `<button
id="captcha_close_button" aria-label="Close">` in the main DOM (dialog
`aria-modal`, z-index 100000, 0 iframes), so once detection triggers the
existing close-once + search-fallback path runs. **Honest limit: detection
unlocks the close-once + search fallback, it does NOT guarantee posts.**
Behind the Collect #64 modal the page held 0 videos (only grey placeholder
tiles), and "Something went wrong" occurs only as translation strings
inside JS data, so `error_any` would not have fired either. Pressing X
alone would not have saved that read.

The other sources' `challenge_any` lists were audited the same way, from
repo evidence only: Instagram — the 2026-11-15 capture shows only "Log
into Instagram" (already in `signed_out_any`); its "challenge"/"captcha"
hits are CSS variable and JS module names, no visible challenge text — no
change. Meta Ads — no `challenge_any` (public pages, no login); its
Collect #64 read worked — no change. Gemini — no `challenge_any` and no
Gemini artifact in the workspace — no change. No selector was guessed.

**Xvfb (`stealth.py`).** `Xvfb.status` now records `started` /
`already_running` / `not_available` / `failed`, with `status_line()` (e.g.
`"Xvfb: started"`, `"Xvfb: not available — patchright ran headless"`).
`launch_patchright` records `xvfb_status` on the returned context,
`open_browser` puts it on the browser object and into `ReadLog.summary()`
as `xvfb`. This settles the research's open question in code: the next run
says plainly whether patchright ran headed or headless.

**Cookie write-back audit — NOT FOUND.** Proved by code search: no
`storage_state` in `browser.py`, `collectors.py`, `stealth.py`, `cli.py`;
no secret-writing in any workflow (secrets are only read as env). The only
`storage_state` use in the codebase is `sessions.py:capture_session_interactive`,
the manual capture on the owner's own machine (writes to a file he names,
never in a workflow). No guard was needed; a source-scan test
(`test_no_cookie_write_back_in_the_collect_path`) now locks this in. Note:
stealth profiles persist a browser profile on the runner's own git-ignored
disk — that is not Harish's exported cookies and is never written back to
a secret.

**Proxy verification — verified, one genuine gap fixed.** A set-but-unusable
proxy fails the run clearly with the secret name only (never the address);
the read log and identity summary carry only the proxy host. Gap found and
fixed: an out-of-range port (`:99999`) escaped `parse_proxy` as a bare
`ValueError` because `urlparse().port` raises; it now raises the clear
`StealthError`. No proxy is configured — none was invented. The proxy slot
itself is unchanged (`STEALTH_PROXY_URL` / per-account); Harish still
provides it when ready.

### 2. Apify async fix (`contentintel/apify.py`, `contentintel/voice.py`)

The tool called Apify's synchronous endpoint (`run-sync-get-dataset-items`,
`timeout=280`), which has a hard 300 s ceiling: a run that takes longer
returns HTTP 408 while the run keeps going server-side, and the tool
treated 408 as a failure (voice #18's TikTok timeout). `Apify.run` now
takes `mode="auto"` (default): sync only for a small ask (`max_items <=
SYNC_MAX_ITEMS = 10`); anything bigger starts async — `POST
/v2/acts/{actor}/runs`, poll `GET /v2/actor-runs/{runId}` every
`poll_interval` (default 5 s) until SUCCEEDED, then fetch the dataset
items. `mode="sync"`/`"async"` force one way; `max_wait` default 900 s.
The sync path was reimplemented with the same retry semantics as before,
so small asks behave exactly as before.

**408 = "not finished yet".** New exception `ApifyRunUnfinished`. On a
sync 408 with a recoverable run ID, the code polls that run and fetches its
dataset — no second run is started. On a 408 with no run ID, it raises
`ApifyRunUnfinished` ("HTTP 408", "not repeated"); a run still going past
`max_wait` raises the same. FAILED/ABORTED/TIMED-OUT raise `ProviderError`;
the TIMED-OUT wording contains "timed-out" so voice's retry-at-half still
fires. Async start is a single attempt only (a retry could start a second
billed run). `ApifyRunUnfinished` messages contain no "429" and no
"timed-out", so the pool never drops a key for it and voice never retries
it. **Honest limit: the 408 body shape is unconfirmed in public docs** —
the code tries several shapes (`data.id`, `error.data.id`, `id`/`runId`, a
regex on the error message) and falls back to a clear unfinished error.

**Split large asks.** `_split_payload` (`hashtags`, `profiles`,
`searchQueries`): a payload naming 2+ targets becomes one call per target,
each keeping the other fields; merged in order, capped at `max_items`,
calls stop once the cap is reached, the same video under two targets kept
once (dedupe on `"id"`). Meta Ads `startUrls` are never split. Each split
call carries the same `max_cost_usd` ceiling — actual spend is per-result
so splitting does not multiply spend, but the worst case per call is
multiplied (still bounded per call by the ceiling).

**`createTimeISO` filtering.** The clockworks actor's date filter does not
work on hashtags (live side-by-side test: #booktok videos up to 3 years 8
months old), so `filter_by_create_time` filters on the tool's side after
the call when a date window is requested. Rows with no readable date are
kept; `since > until` raises `ValueError`.

**Voice (`voice.py`).** Imports `ApifyRunUnfinished`; the retry guard now
fires retry-at-half-ask only for a run that is over (terminal TIMED-OUT),
never for one that may still be going. Voice passes no new kwargs (its
existing test fakes define `run` without `**kwargs`), so it gets async via
the client's auto default: a 20-item TikTok ask now goes async. Voice
already asked one query per call, so no split change was needed there.

**Collect's Apify provider DOES cover TikTok — config flip, not a code
change.** `collectors.py` `DEFAULT_APIFY_ACTORS` includes `"tiktok":
"clockworks/tiktok-scraper"`; `apify_input` builds `{"hashtags",
"searchQueries", "resultsPerPage"}` for TikTok; `_routes` routes TikTok
through Apify when backend is `apify`/`fallback`/`extra`.
`research.yaml` `collectors.apify.mode` is currently `off` (default). The
flip: set `collectors.apify.mode: fallback` (browser first, Apify when the
browser fails — best for the TikTok captcha) or `always` (Apify only);
`APIFY_TOKEN` must be set (it is). No code change needed. (A test proves
the flip exists in `research.yaml`.)

`ApifyPool` failover is unchanged: a refusal another key cannot cure (own
cost ceiling, failed actor, timeout) is raised at once; a plain 429 after
retries drops the key. Unfinished runs are raised at once, drop no key, no
failover. `usage()`, `budget_report()`, `best_key_left()` unchanged.

### 3. Graceful failure + deferred queue (`contentintel/collectors.py`)

`Challenge`/`LoginWall` are now caught PER SOURCE in the collect loop.
A source stopped by a security check or login wall is recorded as skipped
— with the failing URL, the screenshot name, the human score and the
egress IP — in a `SKIPPED SOURCES` section of the run summary, and the
rest of the run carries on. The report says `PARTIAL SUCCESS: some sources
were collected and some were skipped…` and the run exits 0 (not red). If
EVERY source was stopped and nothing was collected, the report says
`FAILED: every source was stopped…` and the run still fails (exit 1) — a
total failure is never masked. One attempt per source per run, as before;
never a retry loop on a blocked page.

The missed items go onto a deferred queue (`data/_shared/deferred-queue.json`,
runtime state under `data/` like the page log, so it is never committed).
On the next Collect the deferred items are retried first. A missed item is
tried again at most 3 times (`DEFERRED_MAX_DEFERRALS`), then dropped with
a note in the summary. The queue is capped and never grows unboundedly; a
dry run never writes it. The run summary lists the queue state (queued,
collected after deferral, dropped at the cap, still waiting).

### 4. Manual-clear experiment — NOT a build (checklist for Harish)

This is the one shortlist item that is an experiment, not code. It answers
the research's biggest unknown: does a human clear on Harish's machine
survive replay from the runner? Steps (also in BUILD-NOTE.md):

1. In his own Chrome (not the runner, not a new device), log in to TikTok
   as the dummy account.
2. Open the same page that failed (the run summary/screenshot gives the
   URL — the Challenge message now carries the failing URL, brand, account
   and timestamp, plus a link to the `challenge-tiktok` screenshot).
3. Drag the puzzle piece into place by hand. Browse a couple of pages
   normally for a minute or two; don't just solve and close.
4. Export the cookies in the same format the tool already uses (same
   export method as the last working export — do NOT change the password
   first: changing it invalidates stored cookies).
5. Update the TikTok cookies GitHub Secret with the fresh export, promptly
   — same day, before the ~48-hour staleness compounding the research
   noted.
6. Re-run Collect (TikTok on). Record whether it passes and for how long
   — that starts the clock on how long a clear lasts.

If it passes and lasts days, manual clearing is a viable bridge while the
prevention work lands. If TikTok re-challenges immediately on the runner,
that proves the device/IP mismatch dominates and the proxy is
non-negotiable. If it recurs on every weekly Collect, that frequency is
itself the signal to switch to defer + Apify rather than keep hand-clearing.

## What this layer does NOT build

- No captcha solving of any kind (SadCaptcha, 2Captcha, CapSolver,
  Anti-Captcha, local OpenCV self-solve) — Harish has not accepted the
  account risk, and the standing rule is a puzzle is never solved.
- Nothing on the research report's not-recommended list (TikTok-Api as the
  captcha fix, Research API application, signing libraries / HTTP-replay,
  nodriver, undetected-chromedriver, retry-storms, refresh/wait/m.tiktok.com
  tricks).
- No proxy is configured, no provider signed up for, nothing bought.

## Owner controls

Everything in this layer is ON by default and the owner can turn anything
OFF: `warmup.enabled` per site in `browser.yaml`; `hashtag_via_search_first`
per site; the deferred queue is automatic but visible in every run
summary; the Apify async path is the client's default (`mode="auto"`) and
`mode="sync"` forces the old path per call. Nothing is removed or
restricted, and no source that already works changes behaviour: warm-up is
a no-op where it is not configured, search-first only applies where its
flag is set, the async path only changes calls above 10 items, and
graceful failure only changes runs that would otherwise have gone red on
one source.

## What Harish still needs to provide

- The proxy (when ready): one static ISP IP (~$2.70/mo) or the $5
  DataImpulse test, as the `STEALTH_PROXY_URL` GitHub secret. Without it
  the layer still runs, but the traffic still comes from GitHub datacenter
  addresses — the research found that is the single largest signal.
- The manual-clear experiment above (~5 minutes in his own browser + a
  secret update + a re-run), when he wants to answer the cross-machine
  unknown.
- Optional config flip (not a build): `research.yaml`
  `collectors.apify.mode: fallback` to route TikTok Collect through Apify
  when the browser fails.

## Honest limits / unproven items

- No live run has happened yet: warm-up's live value (does `_waftokenid`
  actually appear from a datacenter IP?), the search-first ordering, the
  graceful-failure path, the Apify async path and the deferred queue are
  all proved by local tests only, not by a live Collect.
- The Instagram warm-up post URL (`/p/Cuw6X2mOqXG/`) could not be verified
  live (fetch failed — the post may be private/deleted/restricted, or the
  fetch was blocked) and is flagged as a replaceable example in the yaml
  comment; warm-up tolerates it going stale.
- The 408 body shape is unconfirmed in public docs (see §2).
- `max_wait` default is 900 s (15 min) — a slow async run can hold a
  Collect/voice step that long before reporting unfinished.
- Search-first returns relevance-ranked results, not the recency-ordered
  tag feed.
- Detection unlocks the close-once + search fallback; it does not
  guarantee posts (see §1).
