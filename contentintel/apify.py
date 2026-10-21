"""Apify: ready-made scrapers run on Apify's servers. An optional extra, off unless switched on in
research.yaml (collectors.apify.mode). Needs APIFY_TOKEN."""
from .providers import Provider, ProviderError


class Apify(Provider):
    name = "apify"
    label = "Apify"
    env_var = "APIFY_TOKEN"
    base = "https://api.apify.com/v2"

    def run(self, actor, payload, *, max_items, max_cost_usd, timeout=280):
        """Run an actor and wait for its items (Apify allows up to 300 seconds)."""
        url = f"{self.base}/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
        params = {"timeout": timeout, "maxItems": max_items, "maxTotalChargeUsd": max_cost_usd}
        body = self._call("POST", url, params=params, json_body=payload, timeout=timeout + 30,
                          headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        if not isinstance(body, list):
            raise ProviderError(f"{self.label}: expected a list of items, got {type(body).__name__}")
        return body[:max_items]
