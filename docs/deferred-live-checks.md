# Live checks that are deferred, not dropped

These are built and tested as far as the sandbox allows, and are **not used yet**. The owner asked that their live run happens only after the entire project is complete, and that the owner is **reminded when that point is reached**. Whoever works on the project at completion must raise this list first.

## Agent-Reach on Google Cloud Run (built in Update 62)

Built: the container (`cloudrun/agent-reach/`), the HTTP wrapper, the cookie seeding from Secret Manager, the deploy workflow (`agent-reach-cloudrun.yml`) and the client in the platform (`contentintel/agent_reach_remote.py`). Not run: the image has never been built, the service has never been deployed, and no call has ever been made.

At the live check, in this order:

1. Do the setup in docs/platform/agent-reach-cloud-run.md, then run the workflow with `deploy`. The build must finish; read `bootstrap` in the status: every `add` step should say ok, and a failed one is the first thing to fix.
2. Run `status`: each channel that has a cookies secret must say `seeded`.
3. For each of the seven channels, one real read of a known public account through the platform, and compare with what the account shows in a browser. Proves the browser Agent-Reach uses accepts cookies written into its profile this way.
4. Watch the accounts for a week of normal use: any warning or suspension means the gaps in the wrapper (and the browser's own pacing) are to be slowed, and the account rested.
5. Look at what each site does about Google data-centre addresses (Instagram in particular); if it blocks them, the route needs another address, which is a separate decision.
6. Decide whether to turn on `require_google_sign_in`, and check the cost on the Google Cloud bill after a month.

## Reminder to give

When the project is complete, tell the owner: "The Agent-Reach on Cloud Run live check is still waiting (docs/deferred-live-checks.md). Do you want to run it now?"
