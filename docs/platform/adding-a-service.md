# Adding a service, a collector or a repository to the platform

This is the long-term pattern for every remaining stage and for every repository that will use these services. Build a capability once as a shared service; never inside one module, command, competitor record or repository.

Build a capability once and let every module use it. The platform has one place where a shared service is declared, one place where the owner controls it, and one interface through which modules ask for it. This page is the pattern for a new service, and for another repository that wants to use the existing ones.

## Using the existing services from another repository

**1. Install this repository as a package.** It now has a `pyproject.toml`. From the other repository's `requirements.txt` (or its workflow's install step):

```
contentintel @ git+https://github.com/<owner>/Content-Strategy-Tool@main
```

(A private repository needs a read-only token in the URL: put it in a secret, never in the file.) This was checked by building the wheel, installing it into an empty folder and importing it from somewhere else, which works; installing from GitHub itself has not been tried, because that needs the real repository and Actions, which are on hold. If you would rather not install it, copy the `contentintel` folder in, or call this repository's workflows with `workflow_dispatch`.

**2. Register your repository's kinds of work and services.** Two ways, neither needs a change here:

- *No code, from the GitHub page:* a `services_extra.yaml` next to `observatory.yaml` (the path can be set in `CONTENTINTEL_SERVICES_FILE`):

  ```yaml
  purposes: [outreach, {name: reporting, ai_route: true}]
  services:
    - name: firecrawl
      label: Firecrawl (page reading)
      channels: [scrape, crawl]
      purposes: [content_research, outreach]
  ```

- *As a plugin:* the other repository's package declares an entry point, and installing it next to this one is enough.

  ```toml
  [project.entry-points."contentintel.services"]
  myrepo = "myrepo.platform:register"
  ```

  ```python
  def register(api):                       # api.register_purpose, api.register_service
      api.register_purpose("outreach", ai_route=True)
      api.register_service("mailer_enrich", label="Mailer enrichment", channels=("lookup",), purposes=("outreach",))
  ```

A new purpose gets a switch on **every** shared service (Scrapling, Agent-Reach, search, the AI Gateway, the page readers) and a Controls row; a new service gets a master switch, a switch for each purpose and for each channel, and rows in the Controls tab and `observatory controls`. A broken plugin or declaration is reported in `services.NOTES` and skipped; it never stops another repository's registration or the platform's own work.

**3. Use the interfaces with your purpose.**

```python
from contentintel import services as SV

settings = SV.load_settings("observatory.yaml", "data")     # central settings + the owner's Controls changes, with everyone's registrations applied
SV.allowed(settings, "scrapling", "outreach")                 # (True, "") or (False, "which switch is off")
SV.read_page(settings, url, "outreach", {"core": my_fetch})   # the owner's reader choice; engines are your callables, so you keep your own plumbing
SV.search_providers(settings, "outreach", os.environ)         # Google-backed search, Tavily, Exa, Agent-Reach search: filtered by the owner's switches
SV.scrapling_page(settings, url, "outreach", contact=..., python=..., snaps=...)
SV.agent_reach_client(settings, "youtube", "outreach", environ=os.environ)
SV.ai_client(settings, "outreach", claude, stack_factory=..., now_fn=...)
```

**Authentication and dependencies.** Every credential is a named secret read from the environment by the service that needs it (search keys, `YOUTUBE_API_KEY`, proxies and cookies named in the settings, `OBSERVATORY_CONTACT`); nothing is stored in a settings file or passed between repositories. Dependencies are the service's own: Scrapling's worker environment and browsers, OpenCLI and a logged-in browser for Agent-Reach's Instagram and Reddit channels (never a hosted runner), yt-dlp for its YouTube channel, the built gateway and provider keys for the AI Gateway. A service that lacks one says so in its message and is skipped; it does not stop the caller.

**Overrides.** The owner's central settings apply to every repository's purposes. Scrapling's request options can also be overridden for a host (`match: "*.partner.example"`) or a purpose (`match: "purpose:outreach"`), and a repository's own target settings win over both.

**What is proven.** `tests/test_external_repository.py` simulates a second repository with a throwaway package: it registers by plugin and by declaration file, is switched in the Controls tab, and uses every interface; a wheel is built, installed in a clean folder and used. **No real second repository has been tried.**

## Adding a new service

1. **Declare it once.** In `contentintel/observatory/config.py`, or from your own module at import time:

   ```python
   from contentintel import services as SV
   SV.register_service("firecrawl", label="Firecrawl (page reading)", channels=("scrape", "crawl"),
                       purposes=("content_research", "general_research"),
                       routes={"purposes": ("content_research",), "choices": ("old", "new"), "default": "old"})
   ```

   `channels` are named switches (providers, sites, channels), `routes` is a choice per purpose, `options=True` gives it central request options and overrides like Scrapling's.
2. **That is all the plumbing.** Validation of `services.firecrawl` in `observatory.yaml`, its defaults, its rows in the Controls tab and `observatory controls`, and edits made in the sheet all follow from the declaration.
3. **Ask before you act.** Call `SV.allowed(settings, "firecrawl", purpose, channel)` and use its message when it says no. Never record an owner's switch as a failure of the route.
4. **Keep what exists working, and turn the new thing on.** A new service is on for every purpose and channel, and a new route defaults to the most capable choice (`auto` for readers, `gateway_first` for models), because the owner expects every supported capability to be available. Keep the module's old behaviour as the fallback when the service has nothing to offer, and let the owner turn it off or choose another route in one place.
5. **Add the matrix row** to `docs/platform/integration-matrix.md`. `tests/test_platform_matrix.py` fails if a module that makes outbound calls is missing, if a "Connected" row names a function or test that does not exist, or if a row claims to be live-tested.
6. **Test the connection** the way `tests/test_services.py` tests a demo service: validated, defaulted, controlled, shown, editable, and refused with the switch's name when off.

## Adding a collector or a route

A collector or route is declared in `sources.yaml` (its tiers, login type and limits), run through `contract.run_action` so its health is recorded per route, and given a pinned acceptance check. A new collector should complement the existing routes for its platform: merge by a shared item id so a post found twice is stored once, keep the established route's values, and report disagreements.

## What is a genuine limit and must be written down

A service that needs a logged-in browser (Agent-Reach), a built gateway and provider credentials (the AI Gateway), a real browser (Scrapling's browser path) or a provider's approval (Reddit) says so in its row and in its message. Anything else that looks like a restriction is a bug in the architecture: make it a setting.
