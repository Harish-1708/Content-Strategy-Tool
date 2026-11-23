# MoneyPrinterTurbo on Cloud Run (built, OFF)

MoneyPrinterTurbo is a web application: a GitHub job starts, runs and stops, so it cannot host it, and cookies cannot help (there is no site to log into; it is software you install). It needs a running host. Nothing in this tool uses it until the owner says a host is ready.

1. Create the free Pexels account (no card) and note the key: it goes into MoneyPrinterTurbo's own configuration on the host.
2. Add the secret `GCP_SERVICE_ACCOUNT_JSON` (a service account with Cloud Run admin) and, if wanted, the variable `GCP_REGION`.
3. Run Actions > MoneyPrinterTurbo deploy and type `yes` in `host_ready`. It deploys `harry0703/MoneyPrinterTurbo` as a PRIVATE service (`--no-allow-unauthenticated`).
4. Set the secret `MPT_URL` to the service's https address and `MPT_TOKEN` to a Google identity token for it, then set `enabled: true` in `mpt.yaml`.

What it does when on: one draft video per concept of the latest READY brief (a brief that needs review or has an error is refused), and hook A/B renders of one concept with different opening lines. Every render has a manifest and a quality gate. Publishing is off and has no switch: there is no publish function in the code. The deploy workflow and the client were built from the project's published interface and have not run against a real host.
