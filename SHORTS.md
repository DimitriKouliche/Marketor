# Automated shorts pipeline

Turns long *This is no cave* gameplay recordings into finished vertical clips and
publishes them. Nothing is bought: every platform is talked to directly, and all
three are free to post to.

```
ingest → select → write → render → qc → (approve) → publish
```

Each stage writes to a SQLite job table, so a failed upload never costs a
re-render and a clip can't be posted to the same platform twice.

## Why the game logs telemetry

The game writes a JSONL event log while you record. That log is why clip
selection works: it knows a jelly combo hit four chains, that the worm came
within 2.1 units, that the wall-run lasted 5.2 seconds. Nothing has to be
inferred from pixels.

It also records where the player was on screen each frame, which is what lets the
renderer crop a 9:16 window that follows the action instead of taking the middle
of a 16:9 frame and losing the player out of shot.

**Telemetry is opt-in and off by default.** Enable it with the `-telemetry`
command line flag or `TINC_TELEMETRY=1`:

```
"This Is No Cave.exe" -telemetry
```

Logs land in `%USERPROFILE%\AppData\LocalLow\monome.studio\This Is No Cave\Telemetry`.
Press **F9** during play to drop a sync marker if you ever need frame-accurate
alignment; otherwise wall-clock alignment against the OBS filename is used.

## What the game logs

| event | what it is |
|---|---|
| `sample` | 5 Hz speed, position, on-screen position, worm distance |
| `death` / `victory` / `checkpoint` | the obvious ones; death carries a cause |
| `jelly_combo` | bounced off N jellyfish without touching ground |
| `wallrun` / `grapple_attach` | movement, with duration |
| `worm_near` / `projectile_near_miss` / `wall_near_miss` | survived something by inches |
| `shield_hit` / `powerup_pickup` | took a hit that was absorbed; picked an ability |
| `menu_open` / `menu_close` | power-up choice screen up; drives letterboxing, and is in the glossary because the writer otherwise reads it as the player pausing |
| **`bonus_pickup`** | took one of the three diamonds |
| **`bonus_lost`** | died with a diamond in hand, before banking it |
| **`bonus_secured`** | reached a checkpoint with diamonds in hand |
| **`speed_peak`** | top speed of a fast stretch |
| **`input_flurry`** | a lot of separate presses in a couple of seconds |
| **`ghost_lead_change`** | passed, or was passed by, the player's own best run |

`level_start` carries the difficulty tier, which reaches the writer as
`difficulty: hardcore` — hardcore removes the shield and speeds the game up, so
the same near miss means something different there.

### The diamonds

Three per level, off the safe route, and — this is the part that matters — they
do not count until you reach a checkpoint. Die holding one and it goes back
where it was.

That makes `bonus_lost` the best-scoring event in the pipeline (weight 11.0,
above `jelly_combo`). It is the only thing in the log with a *motive* attached:
a visible risk, taken deliberately, that did not pay off. Everything else is
something that happened to the player.

`held_seconds` is what separates a story from a coincidence. Dying three
seconds after the grab means the detour is what killed them; dying forty
seconds later means the diamond was incidental. Scoring weights recency, so the
former wins.

### Thresholds are deliberately generous

`speed_peak` fires from the 95th percentile up — about twenty an hour, more than
are wanted. That is intentional. Which speed is impressive depends on the level,
so the emit threshold stays loose and `selection.max_per_event` keeps the
session's fastest two. Setting a tight threshold in the game means a rebuild and
a re-record to change your mind; the same lesson `wall_near_miss` taught the hard
way, where a bar tuned on one session logged 59 events on the next.

`input_flurry`'s threshold has *not* been measured against a real capture — it
is a guess at nine presses in two seconds, capped at two per session. Check it
against a recording before trusting it.

## Setup

```
python -m venv .venv
.venv\Scripts\pip install -e .
winget install Gyan.FFmpeg          # ffmpeg 9 or later, on PATH
.venv\Scripts\shorts init           # writes shorts.yaml
```

Fill in `.env` (credentials) and `shorts.yaml` (handles and account ids).

## Daily use

```
shorts run                  # ingest through qc
shorts list ready           # review what came out
shorts approve 12 13        # or: shorts approve 14 --reject
shorts publish              # post the approved ones
```

`shorts status` shows what is in each stage. `shorts publish --dry-run` shows
what would go out without posting, and `shorts publish --check` reports which
platforms are actually configured.

Or hand the last two steps to the planner and stop deciding:

```
shorts run
shorts orchestrate --dry-run    # see the plan; drop --dry-run once you trust it
```

See *The orchestrator* below.

## Platforms

| Platform | Cost | Approval | What it's for |
|---|---|---|---|
| YouTube Shorts | free | none | evergreen; clips keep getting found months later |
| TikTok | free | app audit, 2–4 wks | raw reach |
| Instagram Reels | free | Meta review, 2–6 wks | reach |

Default `publish.platforms` is all three. Only YouTube works without waiting on
someone else's review queue.

`tutorial.md` has the click-by-click version of everything below.

**YouTube.** Enable *YouTube Data API v3* in the same Google Cloud project as
your existing `credentials.json`, then run `shorts publish` once — it opens a
browser and saves `youtube_token.json`. Leave `youtube_privacy: unlisted` until
you're happy with the output. Quota allows roughly six uploads a day.

**TikTok.** Register an app, add the Content Posting API product and Login Kit,
and complete the OAuth flow once with scope `video.publish`, saving the result to
`tiktok_token.json`:

```json
{ "access_token": "...", "refresh_token": "..." }
```

The pipeline refreshes and rewrites that file from then on. Until your app passes
audit every post is forced to `SELF_ONLY` — the publisher detects this and
downgrades rather than failing, so you can exercise the whole path while waiting.

**Instagram.** Needs a Business or Creator account linked to a Facebook Page, and
a Meta app with Advanced Access to `instagram_content_publish`. Use Graph API
Explorer to get a long-lived Page token → `IG_ACCESS_TOKEN`, and put the numeric
account id in `publish.instagram_user_id`.

Long-lived Page tokens expire about every 60 days. Instagram will silently stop
posting when that happens, and it will look like a pipeline bug — set a reminder.

Instagram fetches the video from a URL instead of accepting an upload, so it
needs somewhere public to fetch from. The default `github_release` backend
uploads the clip as a release asset in a repo you own — free and durable. Set
`hosting.github_repo` and a `GITHUB_TOKEN` with `Contents: Read and write`. If
you already serve the output directory yourself, switch `hosting.backend` to
`public_url`.

### Reddit is not one of them

It would be the best fit here — a clip on r/IndieGaming reaches more genuinely
interested players than a much larger TikTok view count. Reddit closed
self-service API registration in **November 2025**; new tokens need an approval
ticket, and a weekly self-promotion upload is close to the exact case that
review exists to refuse.

The publisher, the compilation builder and the subreddit rotation have all been
deleted rather than left dormant. Nothing here is written for a platform the
project does not post to. Posting the odd clip there by hand costs nothing —
they are ordinary files in `shorts_out/`.

## Experiments and learning

Every post is tagged with the variant that produced it, so results can be
attributed later:

```
hashtags:niche|format:single
```

Hashtag sets are defined per platform in `experiments.hashtag_variants` and
assigned **round-robin**, not randomly — at a couple of posts a day, random
assignment takes months to balance out.

```
shorts metrics      # pull views/likes/comments back from the platforms
shorts learn        # what is working
shorts learn --apply
```

When the orchestrator supplies hashtags they are matched back against the
configured sets, so a set it echoes lands in that set's bucket rather than
starting a new one. Anything it invents is filed under the arm `planner`, which
makes "the model's own picks" a measurable arm like any other.

`metrics` works for YouTube today. TikTok and Instagram expose insights too, but
behind the same approvals as posting.

`learn` compares strictly **within** a platform — a YouTube view and a TikTok
view are not counted the same way, and averaging across them would be
meaningless. It
reports hashtag sets, post format, and which clip types earn their place, and
refuses to call a winner below `experiments.min_samples` posts or when the gap
is smaller than the spread.

**What `--apply` will and won't do.** It adjusts `selection.weights` from clip
type performance, damped to half the observed effect and clamped to a 4× band —
a runaway weight would collapse the feed onto one kind of clip, which is the
failure this loop exists to prevent. It deliberately does **not** auto-pick
hashtag or format winners: those are discrete choices, and switching to the
current leader would quietly end an experiment that is still gathering evidence.
Those are reported for you to decide.

## Clip length

A clip used to be `lead_in + lead_out` seconds, every time. That produced a feed
of identical 20-second cuts, which reads as machine output whatever the captions
say — length is one of the few things a viewer notices before reading anything.

Length now follows what happened. From the triggering event the window grows
outward through nearby events, stopping at the first quiet gap longer than
`clip.action_gap`, so a stretch with four things going on earns the extra
seconds. Only events scoring at least `clip.action_min_weight` count — a
checkpoint should not stretch a clip on its own.

The other half is the opposite case. When nothing led into the trigger, the
lead-in drops to `clip.quiet_lead_in`, because past a few seconds of empty cave
the run-up stops being context and starts being dead air. That is where the
short clips come from.

Measured on one recording, six clips came out at 14.0, 16.0, 16.8, 20.2, 22.6
and 27.1 seconds — six distinct lengths where before they were all 20.

Set `clip.follow_action: false` for the old fixed-length behaviour.

### How many captions

The writer chooses, within a ceiling the clip can actually hold. Every line has
to stay up `render.caption_min_seconds` to be readable while the viewer is also
watching the gameplay, so a 14-second clip fits two and a 27-second one fits
four. The ceiling goes into the prompt rather than being enforced afterwards:
generating four and silently dropping two is what produced identical three-beat
clips in the first place.

The prompt is explicit that the count is a choice and not a quota — a short clip
with one good line beats the same clip padded out.

## Copy generation

Captions come from an LLM using the clip's actual event context, so they can say
something specific rather than generic. Each platform gets its own caption and
its own title.

`copy.provider` is the **writer**. The planner is configured separately under
`orchestrator.provider` and does not share this setting — see *The orchestrator*.
Both keys can be set at once; they are used for different things.

If the provider's API key is unset, or the API is unavailable or returns
something malformed, the pipeline falls back to deterministic templates rather
than stalling. The clip still renders and the QC gate still lets you reject it —
but the captions will be repetitive, and `write` says so loudly.

### The prompt gives no editorial direction

`prompts.md` has four sections and none of them says how to write:

| section | what it is |
|---|---|
| `system` | one paragraph on what the game is |
| `glossary` | what each telemetry event means, in player terms |
| `studio` | the studio's real history — the true stories to draw on |
| `beats` | display limits: five seconds a line, how many fit, 70 characters |

That is deliberate, and it is a reversal. Earlier versions carried a house
style, a per-mechanic angle sheet, self-reference constraints, banned
constructions and a tone string in `shorts.yaml`. Each was added to fix a real
complaint, and together they produced copy that read like something working
through a checklist — which was the original complaint.

The `beats` limits stay because they are physical: the text is burned into the
video, and a line that arrives two seconds after the last one cannot be read.
Everything else is the writer's call.

**Add facts, not rules.** The `studio` section is the place — a true story lands
better than an invented one, and it is the only input that makes the copy
specific to this game rather than to platformers in general.

Note that `orchestrator` still sends a per-post `brief` when it plans a post,
and the proofreader still fixes grammar afterwards. Neither is a style rule, but
both are direction of a kind; `orchestrator.proofread: false` turns the second
one off.

### Choosing a provider

Both providers speak the OpenAI chat protocol, so switching is two lines in
`shorts.yaml` plus a key in `.env`:

```yaml
copy:
  provider: openai            # gpt-5.6-terra
  model: gpt-5.6-terra
```

```yaml
copy:
  provider: deepseek          # deepseek-v4-flash (also deepseek-v4-pro)
  model: deepseek-v4-flash
```

**The model name is not optional when you switch** — they share no names, and
setting only the key changes nothing.

| | OpenAI | DeepSeek |
|---|---|---|
| Key | `OPENAI_API_KEY` | `DEEPSEEK_API_KEY` |
| Output constraint | strict `json_schema` | `json_object` + validation here |
| Cost | higher | much lower |
| Speed | faster | noticeably slower |

The difference that matters: OpenAI's `json_schema` mode makes the API guarantee
the response matches the schema. DeepSeek's chat endpoint only offers
`json_object`, which guarantees valid JSON and nothing about its contents — so
the schema goes into the prompt and the result is validated locally. A response
that fails validation falls back to a template instead of reaching the renderer.

`deepseek-chat` and `deepseek-reasoner` were **retired on 24 July 2026**. Use the
`v4` names.

For any other OpenAI-compatible endpoint, leave `provider` alone and set
`base_url`, `api_key_env` and `structured_outputs` directly.

### Proofreading

Every piece of copy the writer produces is then read by the planner's model,
which fixes grammar, spelling and phrasing — and nothing else. Cheap models drop
articles, double words and produce plurals that do not agree; none of that shows
up in the JSON, and all of it shows up burned into a video.

The editor is barred from having an opinion about content. It may not change the
angle, the claim, or the joke. That matters more now that the writer takes no
editorial direction at all — the editor must not become the style guide by the
back door.

Most of its prompt is about what to leave alone, because the house style is full
of things that look like errors: sentence fragments, lowercase openings, missing
final periods, "Wallrunning" as one word, colloquial pronouns like "me and the
walls", a hyphen where a copy editor would reach for an em dash, and the game's
own title casing. A fragment being deliberate does not excuse a missing article,
though, and that distinction is the whole calibration problem.

Measured against 13 clips (143 fields) it changed **1.4%**, and both changes were
real. Against planted errors it caught 7 of 7. Corrections to timed beats are
additionally rejected if the length moves more than 25%, since those are fitted
to the video by character count.

Turn it off with `orchestrator.proofread: false`.

## Posting by hand

Two of the three platforms cannot post until someone else approves an app, and
that takes weeks. Rather than idle until then, the pipeline can stop one step
short: it picks the clip, writes the copy and hands you the file.

```
shorts handoff tiktok                          # what to post, and with what
shorts record 2 tiktok --url <permalink>
shorts record 2 tiktok --metric 340            # performance, whenever you have it
```

`record` is not bookkeeping for its own sake — it advances the cadence gate and
gives `shorts learn` a data point. A platform the system cannot see is one it
plans badly around. Re-running it only fills in what you pass; the permalink
survives.

Manual posts are labelled `hashtags:manual|format:…`, so they compare against
the automated arms in the learning report instead of forming a blind spot.

Pair it with `orchestrator.manual_platforms` so the planner stops proposing a
platform it cannot reach.

## The orchestrator

Two models, with different jobs.

**The planner** (`orchestrator.provider`, OpenAI by default) decides *what* goes
out: which clip, to which platform, with which hashtags, and with what angle
It sees the ready clips and their scores, which platforms are
due and credentialled, the last 40 posts, the aggregated performance numbers, and
its own notes from previous runs.

**The writer** (`copy.provider`, DeepSeek by default) decides *how it is worded*.
It gets the planner's brief prepended to the clip's telemetry and writes to it.
It never sees the strategy and never chooses a platform.

The split is deliberate. Planning mistakes compound — posting the wrong thing to
the wrong audience costs weeks of reach, a bad caption costs one post — so the
expensive model does the part where being wrong is expensive, and the cheap one
does the part that runs several times a week.

```
shorts orchestrate --dry-run     # plan and show it; change nothing
shorts orchestrate               # plan; posts only if autopost is true
shorts orchestrate --post        # plan and post, whatever autopost says
```

### The plan is a proposal

Everything the planner returns is re-checked in code before anything happens:
cadence windows, platform credentials, whether the clip exists and is the right
and one post per platform per run. Anything that
fails is dropped with a printed reason. The model is told these rules, but being
told is not being prevented — and a system meant to run for months unattended
cannot be allowed to reason its way past its own rate limits.

It is also told, explicitly, that it has no live trend data, because otherwise it
will confidently invent some.

### Memory

`strategy.md` is the planner's only memory. It reads the file, and writes a full
replacement at the end of each run. Nothing else carries across invocations, so
forty scheduled runs behave like one continuous strategy rather than forty cold
starts. You can edit it by hand; it reads whatever is there as standing
direction.

Notes and weight changes are only written on runs that actually posted. A
planning-only run does not update the memory, because notes describing posts that
were never made would have it reasoning from a history it invented.

### Autopost

`orchestrator.autopost` defaults to **false**: the planner plans, prints what it
would do, and stops. This is the setting that decides whether a bad clip can
reach a real audience with nobody looking. Leave it off until your reject rate is
low, then turn it on and put `shorts orchestrate` on a schedule:

```powershell
schtasks /create /tn "Marketor orchestrate" /sc daily /st 10:00 `
  /tr "Q:\Development\Marketor\.venv\Scripts\shorts.exe orchestrate"
```

Daily is right even with a two-day cadence — the cadence gate is what spaces the
posts out, and a daily run means a missed day is caught the next morning rather
than four days later. The planner is free to post nothing.

Note that this path posts clips in the `ready` state, skipping the manual
`approved` gate that `shorts publish` waits on. That gate is a person, and the
premise here is that there is not one. QC still applies.

`orchestrator.manual_platforms` reserves platforms for you. Anything listed there
is not mentioned to the planner at all — a planner that knows about a platform it
may not use will keep proposing it — while `shorts publish` still posts to it
normally — the right setting for a platform you post to by hand.

`orchestrator.allow_weight_changes` lets the planner nudge `selection.weights`
from measured performance. Changes are clamped to a 4× band per run so months of
small nudges cannot walk a weight to an extreme and collapse the feed onto one
kind of clip. Set it to false to keep selection under your control.

## Tuning

Everything worth adjusting is in `shorts.yaml`.

- `selection.weights` — what counts as a highlight. Raise `jelly_combo` if
  chained bounces perform well; drop `checkpoint` to nearly zero.
- `clip.lead_in` / `lead_out` — how much run-up and payoff to keep. The run-up is
  what makes a death readable; without it the clip opens mid-air.
- `clip.follow_action` / `action_gap` / `quiet_lead_in` — what makes clips vary
  in length. See *Clip length* below.
- `clip.reframe` — `crop` follows the player (needs telemetry). `blur` keeps the
  whole 16:9 frame over a blurred copy of itself; nothing is lost but the
  gameplay ends up small.
- `clip.track_damping` — how hard the crop follows the player. Lower is calmer.
- `selection.min_gap_between_clips` — two clips cut from the same stretch read as
  duplicates on a feed.
- `selection.max_per_event` — most candidates one event type may contribute per
  recording. Some events are common by nature and no threshold fixes that: a
  wall near miss inside 0.7 units at speed 18 is a real squeeze, and one
  12-minute session logged 59 while an 8-minute one logged 2. Level geometry,
  not player skill. Capping per session keeps the tightest few and lets the rest
  of the feed stay varied.
- `copy.tone` — the voice used for hooks and captions.
- `render.crf` — quality vs file size.
- `render.letterbox_zoom` — how much of the frame to show while a menu is up.
  See *Menus* below.

## Menus

The 9:16 crop throws away most of the frame width. That is right for gameplay,
where the camera follows the player, and wrong for the power-up screen, which
puts two cards and their descriptions side by side across the middle of the
frame. Cropped, the choice is unreadable — and reading it is the only reason the
game paused.

So while a menu is up the crop is dropped and the frame is letterboxed instead,
in black bars. `TelemetryMenus` watches the screen object and logs `menu_open` /
`menu_close`; the renderer overlays the letterboxed copy over those seconds.

Because these events also reach the writer through `nearby_events`, the glossary
has to say what they mean. Left unexplained the model reads "a menu opened" as
the player choosing to pause — it produced "Paused to compose yourself" over a
screen the game puts up by itself.

`render.letterbox_zoom` (default `0.72`) is how much of the source width to
keep. Not 1.0, because the cards only reach 57% of the frame and their layout
roots cap them at 70% — trimming the rest makes the text 1.4× bigger inside the
same bars. Raise it if a menu ever gets clipped at the edges; lower it if the
text is still too small and you know nothing lives out there.

The switch is a hard cut on the exact frame the menu opens and closes — no pad,
no transition. `render.letterbox_pad` exists to widen the window and defaults to
zero; a non-zero value reads as the zoom lingering after the menu has gone.

Landing on the right frame is an alignment problem, not a rendering one. See
*Aligning the clocks*.

Sessions recorded before `TelemetryMenus` existed have no menu events, and their
clips render exactly as they did before.

### Clips from other people

The pipeline can process a recording someone else made, but it needs both files
and it needs them raw:

1. The video, **untrimmed**, from OBS with its default filename. The timestamp
   in that name is the only record of when the first frame happened — trimming
   the front leaves the name pointing at the original start, and other
   recorders (Steam's own, ShadowPlay, Medal) name files differently and fall
   back to file mtime, which any copy or re-encode invalidates.
2. The matching `session-*.jsonl` from
   `%USERPROFILE%\AppData\LocalLow\monome_studio\This Is No Cave\Telemetry\`.

They also have to have had telemetry on. Steam passes launch options straight
to the exe, so **Properties → Launch Options → `-telemetry`** is enough; nothing
needs rebuilding. It stays off for everyone else, and when off the telemetry
components are never even created.

Nothing is transmitted anywhere. The log is written locally and stays there
until someone chooses to send it.

**Timezones.** The recorder names files in local time and the log is in UTC, so
something has to bridge them. Assuming the offset of the machine running the
pipeline is right for your own captures and wrong for everyone else's — a clip
from a player seven hours away lands seven hours off, overlaps no session at
all, and silently degrades to audio-peak selection, which scores around 2
against 20–50 for a telemetry-backed clip.

So the game records its own UTC offset in `session_start`, and `ingest` tries
each candidate session in *its* timezone and keeps whichever overlaps most.
That is a loop rather than a lookup because of a chicken-and-egg: the filename
can only be read once the timezone is known, and the timezone comes from the
session that reading it is supposed to find.

Sessions logged before this was added carry no offset and fall back to the
local machine — correct for your own recordings, which is where they came from.
The alignment method is recorded on the source (`filename` vs
`filename-localtz`) so you can tell which happened.

This also protects your own captures across a DST change: the offset is
captured per session, so an October recording still aligns in November.

### Aligning the clocks

The recorder and the game are separate processes, so every clip depends on
knowing what wall-clock instant the first video frame corresponds to. That comes
from the OBS filename, which is whole seconds — good enough for a crop that
moves smoothly, and not good enough for a cut. Measured on one capture the
letterbox was arriving **1.2 seconds late**.

Menus fix this, because they are the one event visible in both clocks at once.
Opening one dims the level and lays two lit cards over it, which changes mean
frame brightness by around 45x the surrounding frame-to-frame variation — a
single unmistakable step. `ingest` looks for that step near each `menu_open` and
takes the median correction across the recording.

On the capture above it found 17 menus, 11 of which agreed to within 18
milliseconds, and the resulting offset put a menu boundary **4 ms** from the
true frame. The method is recorded on the source as `menu-freeze`, and it
improves the tracking crop as much as the letterbox.

ffmpeg's own `freezedetect` was the obvious tool and is not usable here: it
reports its start about a second into the frozen stretch, which corrected the
offset the wrong way. A manual F9 sync marker still wins over both.

## The approval gate

`shorts run` stops at `ready`; nothing publishes without `shorts approve`. Keep
it that way for the first few weeks. Fully automated posting from day one trains
each platform's algorithm on your worst output during exactly the period when it
is deciding what your account is. Once your reject rate settles below about 10%,
wire `shorts publish` into a scheduled task.

## Volume

Both YouTube and TikTok demote repetitive mass-produced content. One or two posts
per platform per day across varied clip types beats twenty near-identical ones;
`publish.max_posts_per_platform_per_day` defaults to 2 for that reason.

## Music

Clips keep the game's own audio. Don't wire in scraped trending sounds — that's a
copyright strike against your own brand account. If you want a music bed, use a
licensed library.
