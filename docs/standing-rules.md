# Standing rules for every update

**What works must keep working.** If something genuinely works and gives good results, an update must not break it or make its results worse. Updating is fine; breaking is not. This is the owner's rule (14 Nov 2026) and it applies to every update from now on.

How it is kept:

1. **What the live runs proved works is locked in tests** (`tests/test_update60.py`). The models that answered in the live check of 14 Nov 2026 must stay approved and in the shared pool. Every lane must still contain every route it had in Update 56, in the same order. The TikTok profile that returned real counts (122.3K followers) and the Instagram snippets that gave counts must still parse into them. The Claude spending cap ($0.50 a day, $10 a month) is asserted.
2. **Every update's PLACEMENT.txt says what already worked and was re-run**, next to what changed.
3. **A deliberate-bug check exists for each change**, so a later edit that undoes it is caught, and the full suite is run in both modes with the real gateway built (a test that is skipped has not been run; the skips are named).
4. **A change that removes something (a model, a route) says why and with what evidence**, and keeps the record (a model is switched off with a dated note, not deleted).
5. **A skipped test is named.** The Tests workflow lists every skipped test with its reason (`-rs`). A test that skips on GitHub but runs in the author's sandbox is told apart in the update's note (for example the Agent-Reach catalogue test needs that program's source, which only its own workflow fetches).
6. **Every update carries full SHA-256 hashes** (64 characters) in `PLACEMENT.txt`, and its wording is checked against the code it describes before it is sent.

7. **Always the main page, never a regional one** (owner, 14 Nov 2026). A brand's Facebook (or other social) page is the main official one; a regional page (@ChobaniAu, @ChobaniNZ) is used only when the main one does not exist. When the tool cannot find a brand's page, or finds two pages with the same name, it looks for it with Serper and a reviewer step through one of our connected AI tools confirms the main official page; it asks the owner only when that is still genuinely unclear. No page or id is ever written into the code.
8. **Deferred is not dropped.** A live check the owner asked to run only after the whole project is complete is listed in docs/deferred-live-checks.md, with what to prove and in what order, and the owner is reminded of it when the project is complete (Agent-Reach on Cloud Run is the first).
