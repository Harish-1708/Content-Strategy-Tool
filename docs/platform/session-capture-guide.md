# Stored login sessions: how to set one up (owner guide)

**What this is for.** Instagram, X, TikTok, Reddit, Facebook, LinkedIn, Xiaohongshu and Boss cannot be read from a GitHub-hosted runner the way the other channels can, because they need a logged-in browser. A **stored session** is a copy of the login you made once, kept in a GitHub Secret. The tool uses it as the **second** route: the normal route is tried first, the stored session second, the search layer third, and if none works the channel is **skipped with the true reason**. A missing or expired session never turns a run red.

**What it is not.** It is not a password stored anywhere, and the tool never logs in with a password on a run. It does not make keyword search work on Instagram, TikTok, Xiaohongshu, Facebook or LinkedIn from a GitHub machine (those sites check the browser, the address and the login together). It buys: **real search on X**, and **single posts, profiles or Pages** on the others, at low volume. For keyword search on those, the honest routes are a broker (Apify, your decision) or the search layer.

**Use a dedicated account, not your personal one.** Automated use from a data-centre address can get an account locked, especially on Facebook and Instagram. Since most of these accounts have no two-factor login, a leaked session is a full takeover: the session secret is the only thing standing between a thief and the account. Keep it in GitHub Secrets only, never in chat, email or a file in the repository.

## The easy way (no Python, no Playwright): export the cookies from your browser

This is the same thing you already do for `GEMINI_COOKIES` and the Instagram cookies.

1. In the browser, log in to the platform with the dedicated account.
2. Open your cookie-export extension on that site and export as **JSON**. Copy all of it.
3. In the repository: **Settings > Secrets and variables > Actions > New repository secret.** Name it exactly as in the table below. Paste the JSON as the value. Save.
4. Log out of that account in that browser profile **only if you want the session revoked**. (Logging out ends the session, so do not log out if you want to keep using it.)

## The other way (a saved file, from your own machine)

Only if you want the full browser state (cookies plus saved page data). You need Python on your own machine, once:

```
pip install playwright
playwright install chromium
python -m contentintel.sessions capture --platform x
python -m contentintel.sessions encode --in x_session.json
```

A browser opens; log in yourself; come back and press Enter. The second command writes `x_session.json.b64.txt`; open it, copy all of its text, paste it as the secret, then **delete both files** from your machine. (The files are ignored by git, so they cannot be committed by accident.)

## The secrets

| Platform | Secret name | What the session can do | Roughly how long it lasts |
|---|---|---|---|
| X | `X_SESSION_B64` | search, timelines, users (the best case; the spare account must be phone-verified for search) | months |
| Instagram | `INSTAGRAM_SESSION_B64` | single posts and profiles, low volume; not keyword search | about 90 days |
| TikTok | `TIKTOK_SESSION_B64` | single known videos; not search | about 30 days |
| Reddit | `REDDIT_SESSION_B64` | reads, only while Reddit allows them (its own API closes in March 2027) | weeks to months |
| Facebook | `FACEBOOK_SESSION_B64` | named public Pages; not keyword search | about 90 days |
| LinkedIn | `LINKEDIN_SESSION_B64` | public post reads where possible (high detection risk) | about a year |
| Xiaohongshu | `XIAOHONGSHU_SESSION_B64` | single notes; not search | weeks |
| Boss (Zhipin) | `BOSS_SESSION_B64` | to be decided by a live check | unknown |

The value can be the cookie export pasted as it is, the base64 text from the `encode` command, or a base64 of either. Your existing `INSTAGRAM_COOKIES`, `TIKTOK_COOKIES` and `GEMINI_COOKIES` secrets stay exactly as they are.

## Checking a session without any request

`python -m contentintel.sessions status` (or the Connections Health tab, once wired) shows, for each platform: **unknown** (never set up), **down** (the secret cannot be used: the reason says why), **degraded** (expired, or expiring within 7 days: the reason gives the date) or **healthy**. It reads the cookies' own expiry dates, so it tells you a session has expired **before** any request is made. "Healthy" means *present and not expired*; until a real run has proved the platform accepts it from a runner, it is also marked **not yet proved against the live site**.

## When a session stops working

The tool says which platform, and why: `expired on 2026-12-01`, `missing the cookie sessionid (the capture was not logged in)`, or `the page ended up at a login, checkpoint or challenge address`. Then: log in again with the dedicated account in a browser, export again, and replace the secret. Nothing is retried in a loop and nothing is logged in automatically.

If you ever think a session leaked: on that platform, **log out of all sessions** (and change the password), then capture a new one.

## What is checked on every use

Before a session is used the tool checks, with no request: the secret decodes, it holds the platform's own cookies (not another site's), the cookies that matter are all there and none has expired. When a page is loaded with the session, it must really be the logged-in site: the address is compared in lower case (so `/Login` counts as a login), a redirect to a login, checkpoint or challenge address fails, the logged-out wording fails, and a page with no sign of being logged in fails. **Any doubt means "not logged in".**


## Proving each platform live (Update 50)

Nothing is proved until a real run has shown the platform accepts the session from a GitHub runner. Two places do that, and each platform always gets **its own line**, so you can see which one works and which one does not:

- **Actions > Stored login sessions > Run workflow.** `what = check` (the default) loads each platform's stored session, opens that platform's own page in the Scrapling browser and judges whether it shows a logged-in state; `what = list` shows what is stored with no request at all; `what = read` reads one logged-in page (choose the platform and paste an address on that platform's own site). Pick one platform or all.
- **Actions > Acceptance** runs the same check as a step of its own (`Stored login sessions`), and its summary has a `Stored login sessions` section.

The result for each platform is one of:

| Result | Meaning | Does it turn the run red? |
|---|---|---|
| ACCEPTED | the platform's own page showed a logged-in state | no |
| UNCONFIRMED | the browser could not be used, so the page was read over plain HTTP and this platform may only show its logged-in state in a browser. The line gives the browser library's own reason | no |
| BLOCKED | the platform showed the runner a bot-check or challenge page, even with the stored session, so the session itself could not be judged. The session is asked for **once only**, never again after a challenge. This is the platform blocking the runner, not a bad session | no |
| REJECTED | the platform showed a login or checkpoint page, the wrong site, or answered 401/403: capture a new session (a repeat means the platform blocks runners) | **yes** |
| EXPIRED | a cookie that matters has passed its date: capture a new session | **yes** |
| BAD CAPTURE | the secret is not a usable session (wrong site, not logged in, damaged): capture again | **yes** |
| CANNOT CHECK | the check could not be made here (no worker, no network, a timeout): nothing was learned | no |
| NOT SET UP | the secret is not added yet | no |
| OFF | you switched it off (`services.stored_sessions.channels.<platform>`, or the master switch) | no |

**ACCEPTED means** the platform's own page, loaded with the stored session from the runner, showed a logged-in state. It does **not** mean every kind of read works: the table above lists what each platform is expected to allow, and a keyword search through a session is not expected to work on most of them. The logged-in markers for each platform are best guesses until a live run confirms them, which is why any doubt counts as not logged in.

A passed check is live proof for **14 days**. A later refusal, an expiry, or removing the secret voids it. The fallback chain (`what = read`) tries the plain reader first, then the stored session, then an honest skip, and it only uses a session on that platform's own hosts, only while the owner has it on, and only while a passed check is current. A session is never sent to any other site.

The states and dates are saved to `data/_shared/sessions_health.json` (never a secret value). Dummy accounts only, never a main account.

## The Gmail code reader

`contentintel/gmail_otp.py` is built and **unused**. It would only ever step in for a re-login where a platform emails a code, and only after that platform's flow has been proved live. Nothing calls it today, and no re-login is attempted anywhere: a refused or expired session always ends in "capture a new one".
