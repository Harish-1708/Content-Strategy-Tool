# Third-party notices

Parts of this tool re-implement ideas and small techniques from the open-source projects below, and some run beside it as separate programs. Except for one data file (named below), no source file from them is copied in; the code here was written for this tool. They are listed because their licences ask for attribution, and so that you can see where each idea came from.

## Agent-Reach

https://github.com/Panniantong/Agent-Reach, MIT licence, copyright (c) 2025 Agent Eyes.

Used as a pattern: the shared result envelope and the eight error codes with the rule that some are final (`contentintel/contract.py`); the address safety check, the secret scrubber and the login-wall idea (`contentintel/safety.py`); the persistent rest after a rate limit or a failing route (`contentintel/health.py`); and the ledger of real per-action results with failures kept (`contentintel/acceptance.py`). Stage 3 adds: the free hosted Exa search route and the way its answers are read (`contentintel/providers.py`, class `ExaMcp`), and the Jina Reader route with its test for bot-check pages (`contentintel/observatory/readers.py`). Not adopted: its browser-based Reddit and Instagram routes (they need a separate browser program and a person logged in to it, which a hosted runner does not have), and its transcription script.

Also used from Agent-Reach, as a pattern and a way of calling an external program: the OpenCLI wrapper for Instagram and Reddit (`contentintel/agent_reach_instagram.py`, `contentintel/reddit_routes.py`), including its error classification and its refusal to run on a hosted runner. Agent-Reach itself is not bundled or installed by this tool. Agent-Reach has no TikTok route, so none is used.

## OpenCLI

Not bundled and not installed by this tool. The Agent-Reach routes for Instagram and Reddit run the `opencli` program, if you install it yourself, on a machine you control and with a browser a person has logged in to. Its licence and terms are its own; check them before installing. The output field names the tool reads from it are generic mappings that were not verified against the real program.

## Reddit Data API

Not a software dependency. The official Reddit API route calls Reddit's service under Reddit's terms. Reddit's free access is for approved non-commercial use only and commercial use needs a contract; the tool therefore makes no request until the owner has recorded Reddit's approval. The terms summary in `docs/observatory/README.md` comes from secondary reports and should be confirmed with Reddit.

## Scrapling

https://github.com/D4Vinci/Scrapling, BSD 3-Clause licence, copyright (c) 2024 Karim Shoair.

Two uses. As a pattern: removing hidden and zero-width content before text reaches a model (`contentintel/safety.py`), and the polite-crawling ideas (robots.txt that fails closed, a pause that doubles when blocked and obeys Retry-After, a response cache) in `contentintel/observatory/polite.py`. As a program: version 0.5, installed unmodified from PyPI into its own separate environment (`requirements-scrapling.txt`) and run as a worker (`contentintel/observatory/scrapling_worker.py`) for the HTML-to-Markdown conversion, adaptive selectors and, only for sites we own or are authorised to use, its advanced fetching options. Nothing of Scrapling is copied into this repository or its main environment.

Licences of what that environment installs (read from the installed packages, 2026-11-03): BSD, MIT, Apache-2.0 and PSF throughout, plus MPL-2.0 for `certifi` and `orjson`, and `tld`, which offers MPL-1.1, GPL-2.0 or LGPL-2.1 (used here under MPL-1.1, unmodified). No package is GPL-only. The environment is created on the runner for a run and is not distributed. If browsers are downloaded (only when asked), they carry their own licences.

## twscrape

https://pypi.org/project/twscrape/ (version 0.20.1, pinned in `requirements-twscrape.txt`), MIT licence (read from the installed package, 2026-11-12). An unofficial client for X's own web interface.

One use, approved by the owner as a last resort: when x.com shows the runner a bot-check, `contentintel/x_twscrape_check.py` reads ONE profile (the stored session's own account) with the cookies already in the stored session, to tell whether X accepts the session. It runs in its own isolated environment, never searches and never retries. twscrape reports anonymous usage to an outside service (PostHog) unless told not to: the program switches that off (`TWS_TELEMETRY=0`, `DO_NOT_TRACK=1`) before the library is imported, and a test checks the order. Its libraries (aiosqlite, beautifulsoup4, fake-useragent, httpx, loguru, py-machineid, pyotp) are installed only in that environment. It uses X's interface without X's permission, with a dummy account's own cookies: the risk is to that dummy account.

## Protego and defusedxml

Protego (BSD 3-Clause) reads robots.txt; defusedxml (Python Software Foundation licence) reads sitemaps and feeds safely. Both are ordinary dependencies in `requirements.txt`.

## YouTube agent skill

https://github.com/Jakeschincariol/youtube-agent-skill, MIT licence, copyright (c) 2026 Jake Schincariol.

Used: the 21 hook formulas, copied as a data file with its licence beside it (`contentintel/observatory/data/youtube_hooks.json` and `youtube_agent_skill_LICENSE.txt`), and the outlier method (a video's views as a multiple of its own channel's median, with at least four videos), re-implemented in `contentintel/observatory/corpus.py`. Not used: its downloader and transcription steps (YouTube refuses GitHub's servers, and the Data API gives no captions of other people's videos).

## Instagram agent skill

https://github.com/Jakeschincariol/instagram-agent-skill, MIT licence, copyright (c) 2026 Jake Schincariol.

Nothing is used yet. Its formulas and validators are planned and will be credited here when they are added.

## FreeLLMAPI

https://github.com/tashfeenahmed/freellmapi, MIT licence, copyright (c) 2026 Tashfeen Ahmed.

Used at run time, not copied: version 0.16.17 is fetched from GitHub by the AI workflow, built, and run on the runner's own localhost as the single door to the free AI providers that go through a gateway (`contentintel/ai/gateway.py`). Providers it does not cover, such as GitHub Models, are called directly (`contentintel/ai/clients.py`, class `DirectClient`).

## Video Shotcraft

https://github.com/Vincentwei1021/video-shotcraft (Apache License 2.0 for its code; Remotion and the audio it bundles carry separate licences).

Nothing is used yet. It is planned for a later stage and will be listed here, with any change made to it, when it is added.

## GitHub REST API (Agent-Reach's GitHub channel)

The platform's GitHub channel (`contentintel/github_reach.py`) reads repositories, READMEs, issues and pull requests through GitHub's public REST API, as Agent-Reach's own GitHub channel does. It needs no login; without a token GitHub allows 60 requests an hour per address. `GITHUB_TOKEN`, when set, is sent only to api.github.com and only raises that limit. Use is subject to GitHub's Terms of Service and API rate limits.

## Scrapling

Scrapling (BSD-3-Clause) is used through an isolated worker process: HTTP and browser fetching, link extraction, and parser tools (css, xpath, text, regex and similar-element search). Its Spider engine is not used.
