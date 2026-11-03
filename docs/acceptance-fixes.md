# Fixing the first Acceptance run (9 passed, 7 failed)

Written for the person running the tool. Nothing here needs a computer of your own: every step is on GitHub or in a website.

## At a glance

| Route that failed | What it really was | Who has to act |
|---|---|---|
| Meta ads through Apify | A bug in the tool (Apify answers a good run with the number 201; the tool only accepted 200). **Fixed.** | You: upload the files and run Acceptance again. |
| Instagram through Apify | The same bug. **Fixed.** | The same. |
| TikTok through Apify | The check's spending ceiling ($0.25) is below what that actor allows ($0.50). Nothing was run and nothing was spent. | **You decide** whether to approve $0.50 (below). Until then the check is shown as skipped. |
| TikTok in the browser | Expired cookies, then a backup password login that could not find its box. Fresh cookies fixed it; the password is now a controlled backup and failures leave evidence. | Done: passed on the second run. |
| Instagram in the browser | The same; Instagram is also given more time to load. | Done: passed on the second run. |
| Gemini Deep Research | Google did not accept the saved cookies (the page still showed its Sign in button). A credential that has to be renewed; not a bug. | You: export fresh cookies the careful way (below). |
| YouTube | YouTube refuses `yt-dlp` from GitHub's servers ("Sign in to confirm you're not a bot"). A restriction on their side. The tool now uses YouTube's official Data API instead. | You: create a free API key (below). |

The nine routes that passed are untouched.

## 1. Apify: Meta ads and Instagram

Apify's own documentation says a synchronous run that works answers **201 Created** with the results. Both runs in your log returned real posts and ads (the log shows an Instagram post with its caption and short code), but the tool treated anything other than 200 as a failure. The same mistake would have broken every real Apify collection, not only the test. It now accepts 200 and 201, still treats other answers as failures, and never repeats a run that timed out (repeating could charge twice).

**Spending note.** Those two test runs really ran on Apify, each with a ceiling of $0.25. The actual amount is on your Apify account under Billing > Usage; I cannot see it. Running Acceptance again repeats both (each capped at $0.25) because the key is set. If you do not want that, remove the `APIFY_TOKEN` secret first, or use the `only` box to test other routes.

## 2. TikTok through Apify: needs your decision

The TikTok actor refuses any run whose cost ceiling is below **$0.50**. The Acceptance check uses $0.25, so Apify refused it before running anything. Your real collections use a ceiling of $1.00, so they are not affected.

- **Do nothing:** the check is shown as *skipped* ("needs a higher ceiling") and the route stays unverified.
- **Approve $0.50 for the check only:** in `research.yaml` change `check_max_cost_usd: 0.25` to `0.50`. A run cannot cost more than that; a small test normally costs far less, but I cannot promise the exact amount. I have not changed it. Nothing is spent unless you do.

## 3. TikTok and Instagram in the browser

### What it was

The message "could not find the username box" came from the backup password login. That backup ran because the page was not recognised as logged in (expired cookies, an unfamiliar page, or a block page), and the old code threw away which one it was. The second Acceptance run, with fresh cookies, passed both routes, and the Instagram picture from that run shows a logged-in page.

### Cookies expire, so the password stays as the backup

You asked to keep the usernames and passwords because cookies alone will not last. They stay. Each site now has a setting in `browser.yaml`:

- `password_fallback: no_cookies_only` (the built-in default): the password is used only when no cookies are given.
- `password_fallback: when_cookies_fail`: the password is also used when cookies were given and the site plainly refused them (its login wall appears). **Instagram and TikTok are set to this.** The run prints a warning line telling you the cookies were refused and need replacing.
- A page that merely looks unfamiliar is never logged into, because that does not prove the cookies failed.
- Google (Gemini) never gets a password login.

Honest limits: this backup has **not run live yet** (the cookies have worked since), and its login steps in `browser.yaml` are still first guesses. If it is ever used and cannot find a box, the run saves a picture and lists the page's boxes, which is what is needed to fix the steps. A password login from GitHub's servers can also stop at a code or a puzzle; the tool then stops and rests the account, as before.

### Instagram is given time to draw its grid

Instagram's grid loads slowly, and the old waits were short (3 seconds after opening a page, 3 after each scroll). Instagram now waits **10 seconds** after opening a page and **6 seconds** after each scroll (`settle_seconds` and `pacing: scroll_pause_seconds` under `instagram` in `browser.yaml`). Other sites keep the general values. The test run's picture is taken after a further short wait. Note that the test deliberately stops as soon as it has the 3 items it needs, so its picture shows a partly loaded page; that is expected. A full-page picture also never scrolls, so tiles lower down can look blank even when the data was read. I could not test these timings on the live site; if a Collect still returns too few Instagram posts, raise the two numbers.

### What you do

1. Keep the secrets as they are. When a run prints the warning about refused cookies, replace `INSTAGRAM_COOKIES` or `TIKTOK_COOKIES` with a fresh export.
2. If a route fails, open that run, download the `debug` artifact under Artifacts, and look at the pictures named `login-wall-...`, `unknown-state-...` or `login-form-...`. Send me the picture and the error line.

## 4. Gemini Deep Research

The log says 25 cookies loaded, 3 already expired, and the page still showed Sign in. Google does not accept a cookie set that was exported from a window that stayed open, because it replaces the cookies while you keep browsing. Do this exactly:

1. Open a **private (incognito) window**. Log in as the dummy Google account. Open `gemini.google.com/app` and check you can type a question.
2. With a cookie extension (for example Cookie-Editor): Export > JSON, with hidden (HttpOnly) cookies included. All cookies for google.com.
3. **Close the window without clicking Sign out and without browsing further.**
4. In GitHub: Settings > Secrets and variables > Actions > `GEMINI_COOKIES` > Update secret > paste > Update.
5. Run Acceptance with **only** `deep_research` and **clear** `deep_research`.

The tool only ever prints counts and names of cookies, never values. If it fails again with fresh cookies, that is Google refusing a data-centre login; I cannot confirm that from here, and the README already warns that a password cannot replace cookies for Google.

## 5. YouTube

Root cause: YouTube answers requests from GitHub's servers with "Sign in to confirm you're not a bot". It treats data-centre addresses as bots. `yt-dlp` itself suggests sending a logged-in account's cookies, which this tool does not do for YouTube. That route is now switched off in `sources.yaml` with the reason recorded, so it no longer fails every run.

Supported alternative: **YouTube Data API v3** (YouTube's official API). An API key is enough for public videos. Its free quota is 10,000 units a day: looking up a video costs 1 unit and a search costs 100. It gives title, description, counts, duration and a channel's uploads. It does **not** give transcripts of other people's videos; to my knowledge captions can only be downloaded by the video's owner, so public transcripts remain unavailable from GitHub.

Create the key (free; no payment card; do not turn on billing):

1. Go to `console.cloud.google.com`, sign in, and create a project (for example "Kelson content tool").
2. APIs and services > Library > search "YouTube Data API v3" > Enable.
3. APIs and services > Credentials > Create credentials > API key. Copy it.
4. Edit the key: under API restrictions choose **Restrict key** > YouTube Data API v3 only > Save.
5. In GitHub: Settings > Secrets and variables > Actions > New repository secret, name `YOUTUBE_API_KEY`, paste the key.
6. Run Acceptance with **only** `youtube_public`.

## 6. After uploading the new files

1. Actions > Tests should be green.
2. Do the cookie and key steps above.
3. Actions > Acceptance > Run workflow. Use **clear** `tiktok,instagram,deep_research,youtube_public` for the routes that failed before.

## Secrets summary

| Secret | Action |
|---|---|
| `INSTAGRAM_COOKIES`, `TIKTOK_COOKIES`, `GEMINI_COOKIES` | Replace with fresh exports. |
| `YOUTUBE_API_KEY` | New. |
| `INSTAGRAM_USERNAME`, `INSTAGRAM_PASSWORD`, `TIKTOK_USERNAME`, `TIKTOK_PASSWORD` | Keep. They are the backup when the cookies are refused (see section 3). |
| `APIFY_TOKEN` | Keep. Each Acceptance run makes small capped runs on Apify (see section 1). |

## What was and was not tested

Tested here with stand-ins for the websites: the Apify answers (200, 201, 400, 408, 5xx), the cost-ceiling skip, the settings, the password-login rule, the evidence capture, the YouTube API answers and their error codes, the workflow wiring and these documents. **Not** tested: any real website, real cookies, a real browser page (the script that lists a page's boxes has not run in a real browser), or the YouTube API with a real key. The first real run of each is your next Acceptance run.


## 7. The second Acceptance run

Five routes passed (TikTok and Instagram in the browser, Instagram through Apify, Gemini Deep Research, YouTube through the Data API) and one was skipped as designed (TikTok through Apify, which needs your decision in section 2).

Two rows still say "degraded" only because of the **first** run's false failures, which a later run has not yet replaced:

- **Meta ads through Apify:** it was not part of the second run. Run Acceptance with only `meta_ads` and clear `meta_ads`. It makes one small Apify run capped at $0.25.
- **TikTok through Apify:** it stays that way until the check can run, which needs the $0.50 decision in section 2.

A healthy row can still show an old "last error". That is the last error the route ever had, not a current problem.
