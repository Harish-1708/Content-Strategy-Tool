# Retired AI providers

A retired provider is **kept as a record and never called**. The registry (`ai_providers.yaml`) refuses to load a retired provider that is enabled or that still has an endpoint or a credential name; a lane that routes to one is refused; and the client refuses its address before any request is made.

## GitHub Models: retired 30 July 2026

- **What happened:** GitHub announced the retirement on 1 July 2026 and completed it on 30 July 2026. The playground, model catalogue, inference API and bring-your-own-key endpoints are no longer available to any customer, including customers with active usage. Source: https://github.blog/changelog/2026-07-30-github-models-is-now-retired/
- **What we saw (2026-11-11):** every address on `models.github.ai` answered `200`, `text/plain`, body `OK`: the chat endpoint with the job's own token, with a fine-grained token with the Models: read permission, and with no token, and the model catalogue too. The same token gets a normal JSON answer from `api.github.com`, so the token was fine and nothing we sent could have changed the answer.
- **What was removed:** the provider's routes in `ai_lanes.yaml`, its endpoint and credential names, its owner switch (`services.ai_gateway.channels.github`), the `models: read` permission and the `GH_MODELS_TOKEN` secret from the workflows, and the GitHub-specific request code. The newer FreeLLMAPI release also removed its GitHub platform (see freellmapi-version-comparison.md).
- **What was NOT touched:** Agent-Reach's GitHub search and the GitHub public API client. They use `api.github.com`, a different and working service, and the job's own automatic `GITHUB_TOKEN`.
- **Replacement:** the providers already live: Groq, OpenRouter, Google, Cloudflare and Claude. Microsoft Foundry is GitHub's suggested alternative, but it needs an Azure subscription and a card, so it is not used.
- **If you delete the secret:** `GH_MODELS_TOKEN` (and the fine-grained token behind it) can be deleted in GitHub: nothing reads it any more.
