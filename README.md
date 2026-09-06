# Marketor

Two pipelines: **shorts** turns gameplay recordings into vertical clips and
publishes them; **influencers** runs the key-giveaway outreach campaign.

## Shorts

Drop recordings in `clips/` and run:

```
marketor auto            # ingest -> select -> write -> render -> qc -> plan -> post
```

That is the whole autopilot. The `Marketor` scheduled task runs it every hour
from 17:00 for six hours; cadence gates decide when anything actually goes out,
so most of those runs do nothing.

| Command | What it does |
|---|---|
| `marketor status` | queue state, plus recent failures and their errors |
| `marketor auto --dry-run` | plan the run without posting |
| `marketor publish --check` | live API calls per platform, and token expiry |
| `marketor tiktok-auth` | authorise TikTok, report audit status |
| `marketor regen` | rewrite copy and re-render, keeping the clip choices |
| `marketor regen --reselect` | also re-choose the clips, from the sources already ingested |
| `marketor regen --full` | delete the database and rebuild from the recordings |
| `marketor handoff` | what to post by hand, for platforms in `manual_platforms` |

`marketor` is a wrapper for `shorts`; the two are interchangeable.

**`--full` vs `--reselect`.** Once a recording's clips are all rendered it moves
to `clips_processed/`, which `ingest` skips. `--full` re-ingests, so after
archiving it rebuilds around no footage. `--reselect` keeps the sources and
re-runs selection on them, which is what you want after changing selection
rules.

**Both delete clips, and `posts.clip_id` cascades.** Deleting a clip deletes its
publication record, and with it the only thing stopping the planner reposting
something already live. Both commands ask first when posts exist.

### What it actually does

The short version: **you drop a gameplay recording in a folder, and clips appear
on YouTube, Instagram and TikTok over the following weeks.** Nothing else is
manual.

The unusual part is how it decides what to show. Most tools of this kind look at
the video and guess — loud moment, fast motion, cut there. This one doesn't look
at the video at all for that decision. The game itself writes a log while you
play: it died here, at this speed, this close to the wall, holding this many
diamonds, on this level. The tool reads that log, so it knows what happened
rather than inferring it.

That has consequences you can see in the output:

- **It picks real moments.** A projectile that missed by inches, a five-jelly
  chain, taking the lead back off your own ghost. Not "there was a loud noise
  here."
- **It knows what to avoid.** Dying to the same rock nine times in a row
  generates a lot of activity and is unwatchable, so it is scored down, not up.
  Menus, cinematics and level transitions are excluded outright.
- **The captions are true.** The writer is given the actual event log for that
  clip, so it can say "died to the same trap eleven times" because that is what
  the log says. It is not writing plausible gaming copy.

A second, stronger model then proofreads what the first one wrote. It fixes
mechanics — spelling, agreement, a doubled word — and repairs lines that do not
make sense: a truncated clause, a pronoun with nothing to refer back to, a
sentence that says a thing and then unsays it. Where a repair needs a fact it is
given the same event log the writer had, and it may use nothing else; where the
log does not have what the line needs, it leaves the line alone rather than
invent. It also sees the same studio notes the writer had, which is what lets it
rebuild a compressed punchline: "the fireball got closer than the publishers
ever did" scans perfectly and means nothing, because the two halves measure
different things.

On hashtags it does one job — removing a tag that repeats another in the same
caption, or that the clip's facts contradict. It cannot add or restyle one.
Duplicates between a caption's inline tags and the appended set are removed in
code at assembly rather than by the model, since that is a comparison, not a
judgement. Everything else is protected — fragments, lowercase openings, missing
final periods and the project's own vocabulary are house style, not errors.

Then it edits. Gameplay is filmed wide and the clips are vertical, so a virtual
camera follows the player through each clip the way an operator would — holding
still, gliding when it has to, widening the shot on the clips where the action
genuinely doesn't fit. It adds a slow push-in, leans in on the moment the log
says is the payoff, and holds the last frame so the closing line can be read.

A second AI acts as the social media manager. Once a day it looks at what is
ready, what went out recently, how it performed, and what day of the week it is,
then decides what to post where and with what angle. It keeps written notes
between runs, because it has no other memory. It is allowed to decide that
nothing is good enough today and post nothing.

Everything it proposes is checked in code before anything goes out: posting
limits, spacing between posts, and a rotation that walks each clip across the
platforms one at a time without ever repeating on the same account. Clips that
fail automated checks — wrong length, wrong size, silent, black at the start —
never reach that stage.

See [SHORTS.md](SHORTS.md) for the pipeline, telemetry, platforms and tuning.

## Influencer campaign

```
Day 1: Setup
make install
make setup
# Configure API keys in both Python files

# Day 2: Discovery
make discover
# Review influencers_priority_top50.csv

# Day 3: Test
make generate-test
# Check Gmail drafts, verify formatting

# Day 4-20: Campaign
make generate
# Send 10-20 per day manually from Gmail
# Track responses in key_assignments.json

# Day 27: Follow-ups
make followup
# Send to non-responders
```
