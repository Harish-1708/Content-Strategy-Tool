# Connections: using the repositories themselves

The platform runs other people's repositories as they are, with the newest version by default, and keeps its own versions as the backup. Nothing is restricted by the platform: every choice below is the owner's, in one place.

## The six connections

| Connection | What it is | Runs as | Backup when it fails | Needs |
|---|---|---|---|---|
| `agent_reach` | Agent-Reach, 14 channels | its own `agent-reach` command line, installed from its repository | the platform's own OpenCLI, yt-dlp, GitHub and page-reader adapters | Instagram, Reddit, X, Facebook and LinkedIn need a logged-in browser on a machine you control |
| `scrapling` | Scrapling | the library in an isolated environment | the built-in polite reader (Jina, then the built-in fetcher) | a browser installed for the stealth browser |
| `freellmapi` | FreeLLMAPI | a Node server built from the newest release in each job | Claude only | Node 22; every provider key you want it to use |
| `instagram_skill`, `youtube_skill` | hook formulas, scorers, validators | cloned from the repository, newest version | the platform's own labels and scoring | nothing |
| `video_shotcraft` | video templates and effect cards | cloned from the repository | none yet (Creative Studio is not built) | Remotion (its own licence) to render |

## The owner's controls (all in the Controls tab, or `observatory controls --what set --key K --value V`)

| Control | Meaning | Default |
|---|---|---|
| `services.connections.channels.<id>` | ON: the repository may be run. OFF: it is never run | ON |
| `services.connection_routes.routes.<id>` | `external_first` (the repository, then our own version), `builtin_first`, `external_only`, `builtin_only` | `external_first` |
| `services.connection_versions.routes.<id>` | `latest` (always the newest) or `pinned` (the recorded version) | `latest` |
| `services.connection_secrets.channels.<id>` | hand the repository the secrets it declares it needs | ON |

**Secrets are withheld.** A repository's own process gets only the basics (path, home, proxy settings) plus the secrets that connection declares it needs, and only while the switch above is ON. Every other secret (Claude, Google, Instagram logins and the rest) is never passed on. A data repository gets no secret at all.

## Health score

Every real use of a repository is recorded (`data/_shared/connections_health.json`). The score is 0 to 100, with recent attempts counting more: 80 or more is healthy, 40 to 79 degraded, below 40 down, and unknown until something has been tried. A site's own refusal is not counted against a repository, only the repository failing. When the backup answers instead, that is counted as "backup used", not as a repository attempt. See the Connections Health tab on each brand sheet or run Observatory > `connections`.

## Actions (Observatory workflow, action `connections`)

- `what` = list or health: the table above for each connection.
- `what` = check: a real probe of each connection that is ON, recorded in its health.
- `what` = install: install or update each connection according to its version mode (`key` = one connection). Failures say so and the platform's own version is used.

## Guards

Every protection that could stop a request is an owner switch, OFF by default (see docs/observatory/README.md, Guards). With the rest guard OFF, a refusing site is tried again (`retry:` in observatory.yaml) and then marked failed so the run moves on.

## Adding a repository (Stage B)

Send the link and one line on what you want from it. I review it (licence, what it runs on, keys it needs, whether it has a command line, library or API) and decide **connect** (it installs or clones on a runner and can be called) or **connect the data and rebuild the missing piece**. Approved repositories are declared in `connections_extra.yaml` (no code) and get the same four controls and a health score. A repository with no licence, a non-commercial licence, abandoned code, or one that needs a desktop, a GPU or paid hosting is reported with the exact reason.
