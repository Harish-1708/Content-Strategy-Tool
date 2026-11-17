# OmniRoute's free providers: the research, recorded (2026-11-17)

The planner's research is kept machine-readable in `ai/omniroute_free_providers.yaml` (a record: nothing reads it at run time, and `ai_providers.yaml` is still what the tool routes by). This page says what it means.

**Direction:** include all available free providers at once and test them together in the pool retest. This extends the routed client that already exists (the free pool first, Claude as the fallback and coordinator): the refresh and the foundation chain already call it. It adds no second path.

**Where the new providers stand:** NaraRouter, xKiro, SambaNova, llm7, SiliconFlow and Pollinations are already LISTED in `ai_providers.yaml`, switched OFF, with their terms unverified and no model approved. Turning one on takes three things only the owner can supply: its key as a GitHub secret, its terms read and recorded, and its first approved model id (learned from the pool retest, because the remote catalogue stays off). The tool does not flip them, and the loader would refuse an unverified provider anyway. OpenCode Zen is not listed at all.

**What the tool does NOT do yet:** start OmniRoute as a second gateway beside FreeLLMAPI. That needs a live proof (install, port 20128, each provider's own account and key in OmniRoute's dashboard, the pool retest, the failover test), which cannot be done or faked from here. Nothing in the tool pretends it is wired.

**Rules that stay:** a provider is switched on only by the owner, with a key, after reading its terms; the five providers whose terms forbid proxying (Kiro, DuckDuckGo AI Chat, Blackbox, Coze, Muse Spark web) are never wired; pin a free model id per provider (a rotating roster must not swap models silently); no personal or confidential data on a provider that logs or trains on prompts (Kilo's Auto Free); when a provider stops working the tool moves to the next working one, and when none can do the job the run says so plainly.

**Video:** nothing in the pool is a reliable free video route (see the `video:` block of the data file). Pollinations stays a text-and-image lane to test last.
