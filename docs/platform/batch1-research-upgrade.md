# Update 64: the Batch 1 research upgrade

What was added, where it plugs in, and how to switch it off. Every external repository is a connection with the owner's four controls (ON/OFF, route, version, secrets) in the Controls tab; the platform's own version stays as the backup.

| Tool | Connection | Used where | Backup if it fails or is switched off |
|---|---|---|---|
| Audience voice: what people say, with real engagement | `last30days` (last30days-skill) | the `voice` command and the Research tools workflow; kept per brand; sent to the scoring prompts, briefs, research-topic suggestions and the competitor view | the platform's own Hacker News and Reddit reader |
| Video facts: views, likes, sound, captions, who posted, outlier against the poster's followers, top comments | `yt_dlp` | read for each video item before it is scored; shown in the Idea Bank columns outlier and sound; sent to scoring and briefs | no video facts (cover pictures and page text only) |
| Video pace: shot length, cuts in the first seconds, loudness, first sound | ffmpeg (the platform's own) | measured on each video as it is saved to Drive; column pace; sent to scoring; briefs state the common pace of the approved videos | not measured |
| Free AI capacity candidates | `omniroute_catalog` (data only) | the `pool` command lists providers worth adding; nothing is changed | the snapshot in ai/omniroute-free-catalog.json |
| Motion formats from 703 prompts | `opus_video_prompts` (data only) | briefs get up to three close formats, with authors credited | the snapshot in reference/motion-prompts.json |
| Licence-safe pictures and sounds | Openverse service | the `assets` command; kept per brand and offered to briefs with their credit line | Wikimedia Commons (pictures) |
| Second reader of Meta Ad Library pages | `fb_ads_library_mcp` | after the first reader, on the same page: fills the headline strip and landing address when missing, and supplies ads only when the first reader found none | the first reader alone |
| Better matching of a brand to its Facebook page | (method from last30days-skill, re-written) | the main-page finder ranks pages whose name carries the brand's name above lookalikes and leaves a lookalike choice to a person | the earlier ranking |

## What was not proven

Nothing here has been run against the live internet: no live last30days run, no real yt-dlp output from TikTok, Instagram or Facebook, no real Openverse call, and the second Ad Library reader has not read a real page. Each row in docs/platform/integration-matrix.md says "Not live-tested" for the same reason. The first Research tools run, a Collect run and an Idea Bank run on GitHub will show what they do for real.

## The first runs to make

1. Actions > Research tools > tool `install`, then `voice` for one brand, then `analyze` with one public TikTok address.
2. Actions > Collect > the usual run: the Idea Bank now reads video facts before scoring and measures each saved video's pace.
3. Actions > Research tools > `pool` to see the free-capacity candidates.
