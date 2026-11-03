# Third-party notices

Parts of this tool re-implement ideas and small techniques from the open-source projects below. No source file from them is copied in; the code here was written for this tool. They are listed because their licences ask for attribution, and so that you can see where each idea came from.

## Agent-Reach

https://github.com/Panniantong/Agent-Reach, MIT licence, copyright (c) 2025 Agent Eyes.

Used as a pattern: the shared result envelope and the eight error codes with the rule that some are final (`contentintel/contract.py`); the address safety check, the secret scrubber and the login-wall idea (`contentintel/safety.py`); the persistent rest after a rate limit or a failing route (`contentintel/health.py`); and the ledger of real per-action results with failures kept (`contentintel/acceptance.py`).

## Scrapling

https://github.com/D4Vinci/Scrapling, BSD 3-Clause licence, copyright (c) 2024 Karim Shoair.

Used as a pattern: removing hidden and zero-width content before text reaches a model (`contentintel/safety.py`). The polite-crawling ideas (robots.txt, throttling that slows down when blocked, a response cache) are for a later stage and will be listed here when they are built.

## Instagram agent skill and YouTube agent skill

https://github.com/Jakeschincariol/instagram-agent-skill and https://github.com/Jakeschincariol/youtube-agent-skill, MIT licence, copyright (c) 2026 Jake Schincariol.

Nothing is used yet. Their hook formulas, scoring tools and validators are planned for later stages and will be credited here, with the formula data and the code that adapts it, when they are added.

## FreeLLMAPI and Video Shotcraft

https://github.com/tashfeenahmed/freellmapi (MIT licence, copyright (c) 2026 Tashfeen Ahmed) and https://github.com/Vincentwei1021/video-shotcraft (Apache License 2.0 for its code; Remotion and the audio it bundles carry separate licences).

Nothing is used yet. They are planned for later stages and will be listed here, with any change made to them, when they are added.
