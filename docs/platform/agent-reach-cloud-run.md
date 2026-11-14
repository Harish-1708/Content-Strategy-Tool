# Agent-Reach on Google Cloud Run

Agent-Reach's seven OpenCLI channels (instagram, reddit, twitter, facebook, linkedin, xiaohongshu, boss) need OpenCLI with Node and Chrome and a browser logged in to the site, on a machine we control, never a GitHub-hosted runner. So Agent-Reach runs in a container on **our own Google Cloud Run**, with a small HTTP wrapper around its command line, and the platform calls it over HTTPS from Actions (`contentintel/agent_reach_remote.py`). **It is built but not used yet: its live check is deferred until the whole project is complete (docs/deferred-live-checks.md), and nothing here has been run.**

## How it fits

- **The platform side.** For the seven OpenCLI channels, when the repository secrets `AGENT_REACH_URL` and `AGENT_REACH_TOKEN` are both set and the owner's switches allow the channel, the call goes to the service; otherwise it is exactly what it was (the local Agent-Reach, then the platform's own adapters). The owner's switches for each channel, the route and the secrets hand-over are not changed and are checked before anything is called. If the service is down or refuses, the result is the platform's usual Failure with its error code and the platform's own adapter is the backup, as before.
- **The service side** (`cloudrun/agent-reach/`). `server.py` answers `GET /healthz` (which channels have cookies and how the build steps went, never a value) and `POST /run` (token required). Only the seven channels, only the actions the catalogue declares and only the inputs it declares are run; an input that starts with `-` is refused so it can never become an option; one call runs at a time. The token and the cookies are never passed to the command, logged or returned.
- **Human-like.** Cloud Run is on Google data-centre addresses, so the account behind a call must not look like a script. The wrapper runs one call at a time, with a gap that varies the way a person's does and a longer break after every 8 to 14 calls (`human.py`), and the browser's own reads stay the human-like reads of the platform's session browsing. This matters more here, not less.
- **Cookies.** Each channel's cookies live in **Google Secret Manager in our own account** as `agent-reach-cookies-<channel>` (a browser export: a JSON list of cookies, or a Playwright storage state). Cloud Run mounts each as its own folder (`/secrets/<channel>/cookies`); at start the service puts them into the browser profile Agent-Reach uses. A channel with no secret is simply not logged in. Cookies are never in GitHub.

## Setting it up (nothing here is done yet)

1. In Google Cloud, in our project: create a service account for GitHub with these roles: Cloud Run Admin, Cloud Build Editor, Artifact Registry Administrator, Secret Manager Admin, Service Usage Admin, Service Account User and Storage Admin. Create a JSON key for it.
2. In the repository's Settings > Secrets and variables > Actions, add `GCP_SA_KEY` (the JSON key), `GCP_PROJECT_ID` and `AGENT_REACH_TOKEN` (a long random value, at least 24 characters).
3. In Secret Manager, add `agent-reach-cookies-<channel>` for each channel that should be logged in.
4. Actions > **Agent-Reach on Cloud Run** > Run workflow > action `deploy`. The run prints the service address: add it as the repository secret `AGENT_REACH_URL`.
5. Run the same workflow with action `status` to see what the service reports.

## Choices to know about

- **Who may call it.** By default the service is reachable on the internet and protected by the long token (compared in constant time; ten wrong tokens in five minutes and every call waits five minutes). `require_google_sign_in` on deploy also makes Cloud Run itself demand a Google sign-in; the callers then need `AGENT_REACH_ID_TOKEN` too (the platform already sends it when present). Turning that on is the stronger setting and needs a sign-in step added to each calling workflow.
- **Size.** One instance at most, one call at a time, 2 CPUs and 4 GiB, scaling to zero when idle (the first call after a pause waits for a start). The image is pinned to one Agent-Reach commit (`AGENT_REACH_REF` in the Dockerfile), not "latest".
- **What the build records.** `agent-reach channels` must work or the build stops. `agent-reach add <channel>` for each channel is recorded in `/app/bootstrap.json` (ok or failed) and shown by `/healthz`, never hidden.

## What cannot be known until the live check

That the image builds, that Agent-Reach's own `add` steps work in it, that the browser it uses reads cookies written into its profile this way, that each channel's login holds from a Google address, and what the sites do about data-centre addresses and an account used this way. The list is in docs/deferred-live-checks.md.
