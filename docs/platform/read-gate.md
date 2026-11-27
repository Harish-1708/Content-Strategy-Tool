# One gate for every read of a careful account (Instagram, TikTok)

Collect and Acceptance read the same accounts. Before Update 73 v6 only Collect kept the careful pace (a 12 hour gap, a daily page cap, a record of when the account was last read); Acceptance's check called the reader directly. On 26 Nov Collect read Instagram at 09:38 UTC and Acceptance read it again at 16:58, inside the gap, and met the login wall.

Now there is **one clock, one daily page count and one record of how the last read ended**, in `data/_shared/page_log.json`:

* **Every read is recorded**, by whoever made it: the time (the gap counts from here), who (`collect` or `acceptance`), how it ended (`ok`, `login_wall`, `challenge`, `warning`, `error`), and its pages (they count toward the day). A read that meets a login wall, a challenge or an account warning still touched the account, so it starts the gap too.
* **Acceptance asks the gate first.** Inside the gap it does **not** read: it reports the route from the last read's outcome, for example "proven by collect's read at 09:38 UTC (7.3 h ago) ... not read again (inside the 12 hour gap)", or "not read again ...; collect's read ended with login wall: ...". Past the gap, with page budget left, it reads and records its own read, so Collect then waits its turn. When the day's pages are used it says so and reports from the last outcome.
* **Collect keeps priority.** Its own gap and caps are unchanged. An Acceptance read only ever happens when Collect would be allowed to read too.
* **Nothing is restricted that was allowed:** the same pages are read, only spaced out. Sites without a gap in `browser.yaml` (Meta ads) are not gated.
* A clock written before outcomes existed still holds the gap; its outcome reads "unknown": the route is neither proven nor refuted until the gap has passed.

Applies to every site with `min_hours_between_runs` in `browser.yaml` (Instagram and TikTok). Not proven live: the sandbox cannot read Instagram; the behaviour is proven with simulated reads and a simulated page log.
