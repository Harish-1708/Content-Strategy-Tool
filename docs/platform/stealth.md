# Stealth / human-behaviour layer for logged-in sessions

A plain headless browser with one fixed user agent and one fixed viewport, coming from a datacenter address, is the shape of an automated read — and the shape an account warning names. This layer gives each logged-in account a human shape instead. It is **on by default** and the owner can turn it off; with it off every read behaves exactly as it did before it existed.

## What it does

- **A stealth browser for session reads.** Camoufox first (its fingerprint is drawn by a model and its headless mode runs headed on a private X server), patchright headed under Xvfb next, plain Playwright last. The engine is picked per process and degrades gracefully: a missing engine is skipped, a failed launch falls to the next one, and a missing browser is never a failed run. The plain path is unchanged (one Chromium per site on the shared Playwright, no automation-hiding flag, `--no-sandbox` only).
- **One stable identity per account.** A pinned user agent, viewport, timezone and locale, written to `data/_shared/stealth-identities.json` the first time the account is seen and reused on every later run. The identity never rotates per run and never changes mid-run. A profile directory per account (`data/_shared/stealth-profiles/<site>`) holds the session between runs, so the session stays logged in and never has to log in again from the runner.
- **A sticky proxy per account.** The proxy address comes from a secret, never from a file: `STEALTH_PROXY_<SITE>` for one account (for example `STEALTH_PROXY_INSTAGRAM`), falling back to the shared `STEALTH_PROXY_URL`. The proxy is optional — with no proxy configured the stealth browser and the human pacing still apply. The proxy address and any credentials in it are never logged, never written to a file and never appear in an error message. A secret that is set but unusable fails the run, because silently ignoring a configured proxy would send the account's traffic from a datacenter IP while the owner believes it is proxied.
- **Cookie-only sessions.** A stealth session never logs in from the runner. A missing or expired cookie means "nothing was read", never a fresh login from a new device and IP. This is the identity's `allow_login: false` and the `password_fallback: never` value in `browser.yaml` (both are honoured; either one stops the login). The `password_fallback` values already in `browser.yaml` are unchanged, so turning the layer off returns the old fallback behaviour exactly as before.
- **Human pacing on every read, including cookie reads.** The mouse follows a curve that overshoots and settles back; the dwell after a page opens is proportional to how much text the page holds; typing happens one keystroke at a time with the occasional typo that is noticed and corrected (long text is pasted, the way a person pastes a long prompt); the planned pages are read in a shuffled order (careful mode keeps its own rotation and is never shuffled); each page is opened with the previous page as its referer, so a session does not arrive from nowhere. All of this extends `contentintel/human.py` in place — there is no second copy of it.
- **A per-read log.** One JSONL line per page read in the run's debug directory (`<debug>/<site>/read-log.jsonl`): page, timing, dwell, mouse moves, scrolls, pauses, the engine, the identity, the proxy host, the egress IP and a 0–100 human score. One summary line is added to the run report. The egress IP is read through the browser's own context, so it goes through the proxy when one is configured.

## Owner controls (all in `browser.yaml`, `stealth:`)

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `true` | The whole layer. `false` restores the old behaviour for every site at once. |
| `engine` | `auto` | `auto` (Camoufox, then patchright, then plain), or one of `camoufox`, `patchright`, `plain`. A pinned engine that is missing still degrades to plain. |
| `humanize` | `true` | Camoufox: move the mouse and type the way a person does. |
| `geoip` | `true` | Camoufox: derive the timezone and locale from the proxy's address, so the browser's story matches where it comes from. |
| `identities_file` | `data/_shared/stealth-identities.json` | Where the pinned identities live. It holds no secret, only the pinned values and the *name* of the secret that holds the proxy address. |
| `profiles_root` | `.stealth-profiles` (outside `data/` on purpose: Collect commits `data/`, and a profile holds session cookies) | Where the persistent browser profiles live, one directory per account. |
| `proxy_secret` | `STEALTH_PROXY_URL` | The shared secret holding the proxy address. |
| `log_reads` | `true` | Write the per-read log and add the summary line to the run report. |
| `shuffle_reads` | `true` | Read the planned pages in a shuffled order. |

## What the owner still needs to provide

A residential or mobile proxy service, about $2–8 a month, one stable address per account (or one address for every account to start with). The owner picks the provider; the code takes the address from the secret, so no code change is needed when the provider changes. Without a proxy the layer still runs — the stealth browser, the pinned identity and the human pacing all apply — but the traffic still comes from GitHub's datacenter addresses, which is the single largest warning signal the research found.

## Installation notes for the runner

- `requirements-browser.txt` gains `patchright==1.64.4` only. **Camoufox is not pinned there**: its latest release (0.5.9) requires `playwright<1.63`, which conflicts with the repo's `playwright==1.64.0` in one pip environment, so it cannot be installed alongside it. Until a Camoufox release supports Playwright 1.64, it runs from its own isolated environment (the same pattern the repo already uses for Scrapling) or not at all — and the layer degrades gracefully without it.
- `patchright install chromium` is needed on the runner for its own browser build (its driver is separate from Playwright's). No workflow file is changed by this package; the install step is a follow-up for the workflow that installs the browser requirements.
- Headed patchright needs an X server. The layer starts its own Xvfb when one is available and falls back to headless when there is none. If the runner's image does not have Xvfb, `apt-get install xvfb` (or the workflow's equivalent) makes headed mode available.

## The human score

40 points for dwelling long enough to read the page (about 200 words a minute, capped at two minutes), 25 for moving the mouse, 20 for scrolling and 15 for pausing between actions. A read that opened a page and left instantly scores near zero; a read that lingered, moved and scrolled scores near a hundred. The score is a trace, not a verdict — it exists so a future warning can be traced back to what the account actually did, not to prove a read was human.

## Not yet proved live

- **The real Camoufox fingerprint result.** The code launches Camoufox with the account's pinned identity, but no live run has yet shown the fingerprint it produces, or whether a persistent profile keeps the drawn fingerprint across launches (Camoufox draws a fresh one when no `fingerprint_preset` is recorded; the identity file has a place to store one once a live run shows how to read it back). Until that is proved, the fingerprint is the one part of the identity that may still rotate.
- **The real proxy.** No proxy address has been configured yet, so no live run has proved the proxy is actually used for the account's traffic, or what egress IP the reads come from. The first live run with a proxy should check the read log's `egress_ip` against the proxy's address.
- **patchright headed under Xvfb on the runner.** The Xvfb start and the headed launch are written and tested with fakes; they have not yet run on a GitHub runner, where Xvfb may need installing first.
- **The full live round.** The tests in `tests/test_stealth.py` prove the wiring with fakes (the engine choice, the pinned identity, the cookie-only rule, the proxy parsing, the pacing on cookie reads, the read log). A live Collect with the layer on is still needed to prove the reads themselves look human and return the same content as before.
