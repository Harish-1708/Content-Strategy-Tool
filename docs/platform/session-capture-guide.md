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

## What a check prints about each page (Update 52)

Under every platform that was really checked, the log now says what its page showed, with no secret in it:

- **page:** whether it was read with the browser or over plain HTTP, the HTTP status, the address it landed on (never the part after a `?` or `#`) and the page title;
- **cookies sent (names) / the platform sent back (names):** names only, never a value. A session cookie that is missing from the second list, or replaced, is the platform ending the session;
- **logged-in markers found:** which of the markers were and were not in the page, so a wrong marker can be corrected from a real page;
- **logged-out wording:** looked for in the **visible text** only. A logged-in page can hold that wording in its translations, so wording found only in the page's code is listed but not counted;
- **password field on the page**, a **login or challenge word in the address**, and the **first words of the visible text** (at most 240 characters; a dummy account's page, so nothing private is expected);
- **the worker said:** the worker's own error, if it gave one.

An ACCEPTED platform gets one line. The logged-in markers for each platform are still best guesses: this evidence is how they get corrected.

## X, and the last resort

If x.com shows the runner a bot-check (BLOCKED), the check asks X's own profile interface through twscrape, but only for X, only with the two cookies already stored (and `twid` to know which profile), and it reads one profile. Accepted means ACCEPTED *through twscrape*: X accepts the session, but the browser read of x.com is **not** proved, so the read chain does not use it. A rejection (cookies not accepted, account locked) is REJECTED with X's own reason; anything that could not be judged stays BLOCKED. twscrape's usage reporting is switched off. The environment is installed only when an X session is stored.

## Discovery: when neither the page nor the session can be read

`what = read` now tries the plain reader, then the stored session, then **discovery**: one search about the address through the search layer (Serper, then Tavily, then Exa: the first that has a key; Exa's search here returns titles and links only, so it is asked last), which returns titles and snippets about the address rather than the page, with no login. The result says so. It is off when no search key is passed to the run. Research (the collectors) does not use stored sessions at all, so a rejected session changes nothing there.

## What the session adds: `what = compare` (Update 54)

Before a session is wired into the collectors, the question is what it adds over a plain read. `Actions > Stored login sessions > what = compare` (choose the platform and an address on that platform's own site) reads the **same address twice**: once with no login and once through the stored session (only when the owner has it on and a check passed in the last 14 days). It prints both (characters read, where it landed, the title, the first words) and a verdict: "the session adds content: N more characters and M words the public read did not show", or "adds little or nothing here", or "only the session read works", or "neither works". One attempt each; a session is never sent to another site.

Run it on a handful of real addresses (a competitor's TikTok profile, a Facebook page, a LinkedIn company page, a hashtag or search page) before deciding anything: the answer will differ by platform and by kind of page. For example, X's public profile was readable with no login at all.

## What the first compare runs showed (2026-11-13)

Four addresses were read with no login and through the session. **The sessions added nothing on any of them**, so they are not wired into the collectors:

- **TikTok profile:** the public read returned the profile (name, 405 following, 73.3K followers, 1.8M likes, the bio line); the session read hit a puzzle (captcha) page and returned 38 characters.
- **Facebook page:** neither read returned any text (the page draws itself after it loads; reads now wait for it).
- **LinkedIn company page:** the public read returned the page's About text (2,730 characters); the session read was refused by LinkedIn with an error status, which is its own refusal of an automated visit.
- **X profile:** blocked on the public read this time (X is intermittent: an earlier run read the public profile), and the session was not used because it was never proved.

What that suggests: the stored sessions prove a login works, but from GitHub's servers a logged-in visit is **more** likely to be challenged than a public one. The useful finding is the public read: a competitor's public profile page gives follower counts, likes and the bio, which could be snapshotted over time without any session. That is a different (and simpler) collector from the one the sessions were built for, and it is only worth building if you want it.

X cannot be proved from GitHub's servers by either route: x.com shows a bot-check to the browser, and X's profile interface answers 403 to twscrape.

## Using the proved sessions for research: not wired yet, and why

The collectors read through the existing routes; the stored sessions are not used by them. What a session could add is the **text of a specific page that a visitor without a login does not get**, read through a real browser. It is not a search interface, not an API and not structured data: a read returns what a page shows as text.

If the comparison shows a platform's session adds real content, the wiring would be: for each competitor handle in a brand file, a "social page read" collector reads the profile page through the chain (plain reader, then the stored session if proved, then discovery), stores the text in the corpus with its route, lets the AI judge score it, and shows the route's health in the Collector Health tab (healthy while a check passed in the last 14 days). That is a small, honest gain: the recent posts, captions and visible numbers of a competitor's page. It is not a way to search a platform or pull trending feeds at volume, which is also where accounts get blocked.

For a platform whose session is rejected or blocked, the other paths are the plain public read (often enough: see X above), and discovery (search-engine titles and snippets about the address).

## Reads that return nothing useful

A read that comes back as a script shell (a title, an icon name, an image) is no longer reported as success: image data is replaced by `[image]`, and a page with fewer than 12 real words or 80 characters counts as unreadable, so the chain moves on to the next route (the session, then discovery). A compare says so too. Platforms that draw their page after it loads (Instagram, Facebook, TikTok) are waited for on a read (Instagram also on a check); the page code a check keeps is up to 2,000,000 characters and the log says when it was cut. An error status from the browser (`net::ERR_HTTP_RESPONSE_CODE_FAILURE`, LinkedIn's 999) is shown as a BLOCKED result in plain words, not a failed check.

## What a rejected check can mean

An expiry date is the earliest of the cookies that are all needed and the **latest** of the either-or ones (Reddit's `reddit_session` or `token_v2`): a short token the platform renews from a long cookie does not set it. Reddit's page shows its logged-in menu as visible text ("Open inbox", "Expand user menu", "Open profile menu"), so those words count as evidence. Instagram's page is a script shell until it has finished loading, so its check waits for the network to go quiet; if a page has no visible text, the evidence prints its shape (script tags, element ids, the no-script note). twscrape's own warnings (why it put the account aside) are now part of its answer.

## The Gmail code reader

`contentintel/gmail_otp.py` is built and **unused**. It would only ever step in for a re-login where a platform emails a code, and only after that platform's flow has been proved live. Nothing calls it today, and no re-login is attempted anywhere: a refused or expired session always ends in "capture a new one".
