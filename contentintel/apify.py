"""Apify: ready-made scrapers run on Apify's servers. An optional extra, off unless switched on in
research.yaml (collectors.apify.mode). Needs APIFY_TOKEN.

Apify's synchronous endpoint answers 201 Created (not 200) with the dataset items when the run succeeded, 400 with an
error type when the request is refused, and 408 when the run did not finish in 300 seconds."""
import re

from .providers import Provider, ProviderConfigError, ProviderError


class Apify(Provider):
    name = "apify"
    label = "Apify"
    env_var = "APIFY_TOKEN"
    base = "https://api.apify.com/v2"

    def run(self, actor, payload, *, max_items, max_cost_usd, timeout=280):
        """Run an actor and wait for its items (Apify allows up to 300 seconds)."""
        url = f"{self.base}/acts/{actor.replace('/', '~')}/run-sync-get-dataset-items"
        params = {"timeout": timeout, "maxItems": max_items, "maxTotalChargeUsd": max_cost_usd}
        try:
            body = self._call("POST", url, params=params, json_body=payload, timeout=timeout + 30, ok=(200, 201), stop=(408,),
                              headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        except ProviderError as exc:
            if "max-total-charge-usd-below-minimum" in str(exc):
                found = re.search(r"minimum of \$([0-9]+(?:\.[0-9]+)?)", str(exc))
                minimum = f"${found.group(1)}" if found else "a higher amount"
                raise ProviderConfigError(
                    f"{self.label}: the cost ceiling for this run (${max_cost_usd:g}) is below the minimum this actor accepts ({minimum}). "
                    f"Nothing was run and nothing was spent. Using this actor needs a ceiling of at least {minimum} per run (that is the most one run can cost); "
                    "raise it only if you approve that spending") from None
            raise
        if not isinstance(body, list):
            raise ProviderError(f"{self.label}: expected a list of items, got {type(body).__name__}")
        return body[:max_items]
