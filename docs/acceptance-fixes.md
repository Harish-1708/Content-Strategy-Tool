# Fixing the first Acceptance run (9 passed, 7 failed)

Written for the person running the tool. Nothing here needs a computer of your own: every step is on GitHub or in a website.

## At a glance

| Route that failed | What it really was | Who has to act |
|---|---|---|
| Meta ads through Apify | A bug in the tool (Apify answers a good run with the number 201; the tool only accepted 200). **Fixed.** | You: upload the files and run Acceptance again. |
| Instagram through Apify | The same bug. **Fixed.** | The same. |
| TikTok through Apify | The check's spending ceiling ($0.25) is below what that actor allows ($0.50). Nothing was run and nothing was spent. | **You decide** whether to approve $0.50 (below). Until then the check is shown as skipped. |
| TikTok in the browser | Not yet known: see "Why the cause is not known yet". The tool no longer tries a password login when cookies were given, and it now saves a picture and a list of the page's boxes. | You: put fresh cookies in the secret, then run Acceptance. |
| Instagram in the browser | The same. | The same. |
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

### Why the cause is not known yet

The message "could not find the username box" comes from the backup password login. That backup ran because the page was not recognised as logged in, which has three possible causes: the saved cookies had expired, the site showed a page we do not recognise, or GitHub's servers were shown a block page. The old code threw away which one it was. I could not open those sites from where I work, so I have **not** changed any selector in `browser.yaml`: guessing would risk breaking a login that may be fine.

### What changed

- **No more password login over cookies.** The README already says the username and password are the backup for "no cookies". When cookies are given and refused, a password login from a data-centre address is the likeliest way to get the dummy account warned. Now the run stops with a plain "not logged in, the cookies may have expired" message, with counts (never values) of what the cookies contained.
- **Evidence.** If the backup login is ever used and cannot find a box, the run saves a picture (`login-form-instagram` or `login-form-tiktok`) and the error lists the page's boxes (kind, name, placeholder; never a value).

### What you do

1. Export fresh cookies for the dummy Instagram and TikTok accounts (steps are in the README under "cookies"; use a normal browser window logged in as the dummy account, export as JSON).
2. In GitHub: Settings > Secrets and variables > Actions > open `INSTAGRAM_COOKIES` > Update secret > paste > Update. The same for `TIKTOK_COOKIES`. Do not paste cookies into a chat or an issue.
3. Optional but safer: delete the `INSTAGRAM_USERNAME`, `INSTAGRAM_PASSWORD`, `TIKTOK_USERNAME` and `TIKTOK_PASSWORD` secrets. The tool no longer uses them when cookies exist.
4. Actions > Acceptance > Run workflow with **clear** set to `tiktok,instagram` (a failed route rests for a while and a run skips resting routes; this lifts the rest).
5. If a route still fails, open that run, scroll to Artifacts, download `debug`, and look at the pictures named `login-wall-...`, `unknown-state-...` or `login-form-...`. Send me the picture and the error line: that is the evidence the selectors need.

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
| `INSTAGRAM_USERNAME`, `INSTAGRAM_PASSWORD`, `TIKTOK_USERNAME`, `TIKTOK_PASSWORD` | Optional: delete (no longer used while cookies exist). |
| `APIFY_TOKEN` | Keep. Each Acceptance run makes small capped runs on Apify (see section 1). |

## What was and was not tested

Tested here with stand-ins for the websites: the Apify answers (200, 201, 400, 408, 5xx), the cost-ceiling skip, the settings, the password-login rule, the evidence capture, the YouTube API answers and their error codes, the workflow wiring and these documents. **Not** tested: any real website, real cookies, a real browser page (the script that lists a page's boxes has not run in a real browser), or the YouTube API with a real key. The first real run of each is your next Acceptance run.
