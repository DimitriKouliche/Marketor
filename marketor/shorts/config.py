"""
Pipeline configuration.

Loaded from shorts.yaml at the repo root. Every field has a working default, so
the file is only needed to override things. Secrets stay in .env / environment
variables and are never written here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "shorts.yaml"

# Unity writes to persistentDataPath, which on Windows is
# %USERPROFILE%\AppData\LocalLow\<companyName>\<productName>.
#
# Careful: Unity sanitises the company name for persistentDataPath but not for
# the player log, so "monome.studio" becomes "monome_studio" for the data folder
# while Player.log still sits under the dotted name. Both are checked below.
_UNITY_COMPANY = "monome.studio"
_UNITY_PRODUCT = "This Is No Cave"


def _default_telemetry_dir() -> Path:
    local_low = Path(os.environ.get("USERPROFILE", Path.home())) / "AppData" / "LocalLow"

    sanitised = _UNITY_COMPANY.replace(".", "_")
    candidates = [
        local_low / sanitised / _UNITY_PRODUCT / "Telemetry",
        local_low / _UNITY_COMPANY / _UNITY_PRODUCT / "Telemetry",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


@dataclass
class Paths:
    recordings: Path = REPO_ROOT / "clips"
    telemetry: Path = field(default_factory=_default_telemetry_dir)
    work: Path = REPO_ROOT / "shorts_work"
    output: Path = REPO_ROOT / "shorts_out"
    database: Path = REPO_ROOT / "shorts.db"

    # Recordings are moved here once every clip cut from them has been
    # rendered. The database already refuses to ingest a known path, but that
    # guarantee dies with the database — and `regen --full` deletes it, which
    # is how six recordings once got reprocessed from scratch. Moving the file
    # makes "already done" a fact on disk rather than a row in a table.
    #
    # A sibling of `recordings`, never a subdirectory: discovery walks
    # recursively, so an archive inside it would be picked straight back up.
    archive: Path = REPO_ROOT / "clips_processed"

    def ensure(self) -> None:
        for p in (self.work, self.output):
            p.mkdir(parents=True, exist_ok=True)


@dataclass
class ClipConfig:
    """
    Clip geometry and length.

    The 90 second ceiling is not a style choice: the Instagram Graph API refuses
    Reels longer than that, even though the app itself allows three minutes.
    Staying at or under 60 clears every platform without special-casing.
    """

    width: int = 1080
    height: int = 1350
    # 60, not 30. The recordings are 60 and the game is a fast platformer:
    # halving the rate is visible as judder on exactly the footage worth
    # posting, and every target platform accepts 60.
    fps: int = 60

    # The floor has to clear the caption plan: two beats at five seconds each,
    # plus room either side. Below this the planner starts dropping lines.
    # It is deliberately low enough that a short, isolated moment can stay
    # short — a feed where every clip runs the same length reads as automated
    # whatever the captions say.
    min_duration: float = 14.0
    max_duration: float = 55.0

    # How much footage to keep either side of the triggering event. The run-up
    # is what makes a death or a combo readable; without it the clip opens
    # mid-air with no context.
    #
    # These sum to more than min_duration on purpose. When they summed to less,
    # every clip got padded to exactly the floor and the whole feed came out the
    # same length.
    lead_in: float = 13.0
    lead_out: float = 7.0

    # Grow the window outward through a run of nearby events, so length follows
    # what actually happened instead of being lead_in + lead_out every time.
    follow_action: bool = True
    #: Quiet stretch that ends the run. Larger means longer clips.
    action_gap: float = 6.0
    #: Selection weight an event needs before it counts as part of the action.
    #: Checkpoints and pickups are frequent and dull; they should not stretch
    #: a clip on their own.
    action_min_weight: float = 5.0
    #: Lead-in used when nothing led into the trigger. Past a few seconds of
    #: empty cave the run-up stops being context and starts being dead air.
    quiet_lead_in: float = 7.0

    # Loudness target used by every platform's normalisation.
    lufs: float = -14.0

    # "crop"  - 9:16 window that follows the player (needs viewport telemetry)
    # "blur"  - whole 16:9 frame over a blurred, zoomed copy of itself
    reframe: str = "crop"

    # How far the crop window may sit from frame centre, as a fraction of the
    # distance to the player. The crop is static for the whole clip — computed
    # once from the median player position — so this is framing, not camera
    # motion, and there is no jitter cost to raising it.
    #
    # 1.0 centres the crop exactly on the player; lower values bias toward the
    # middle of the original frame, which keeps more of the level in shot but
    # lets the player drift toward the edge. 0.85 keeps the player comfortably
    # framed even from a 1440p source, where the crop is only a third of the
    # frame width.
    track_damping: float = 0.85

    #: Follow the player over the clip instead of holding one fixed window.
    #: Set false for a static crop.
    tracking: bool = True

    # How far the player may roam from the centre before the camera reacts, as a
    # fraction of the crop's half-width. This is what stops the shot jiggling:
    # without a deadzone the crop chases every bounce, and in a game built on
    # bouncing it ends up mirroring the oscillation. At 0.35 the player can move
    # freely through the middle 70% of frame and the camera simply holds.
    # 0.40, not 0.20. A tighter deadzone means the camera answers every small
    # movement, and measured on the output the picture reversed direction 21
    # times in four seconds — a shimmer, not a pan. At 0.40 with heavier
    # smoothing that falls to 2, against 0 for a locked camera, and it costs
    # nothing: with the 4:5 window the player never leaves frame either way,
    # while average camera travel drops from 583px to 123px.
    track_deadzone: float = 0.60

    # Seconds of moving average applied to player position before following.
    # Removes sample noise; raise it if the camera still feels busy.
    track_smoothing: float = 2.0

    # How far into the crop the player may drift before the camera is forced to
    # contain them, as a fraction of the half-width. Everything else about the
    # tracking is a preference; this is the guarantee.
    #
    # Measured on real footage: the player outruns the pan limit 40% of the
    # time, so smoothing alone cannot keep them framed. 0.86 leaves a 14% band
    # at each edge that the player never enters, which reads as headroom rather
    # than as the camera being dragged.
    track_safe_fraction: float = 0.86

    # Ceiling on the containment correction, in source pixels per second. Well
    # above the ordinary follower's limit, so it only engages when the player is
    # escaping — but finite, because an unbounded correction produced 725px
    # single-frame jumps that read as the camera being yanked. At 700 the worst
    # case is 24px per frame: a firm pan, not a whip.
    track_rescue_px_per_s: float = 700.0

    # Widen the crop on clips where the player's range will not fit the 9:16
    # window, and letterbox the result rather than losing them off the side.
    #
    # Measured over 124 windows of 20s: the player's span is 730px at the
    # median and 1344px at p90, against a 608px window. The follower can cover
    # some of that by panning; past a point it simply cannot, and the choice is
    # between a smaller picture and an absent player.
    widen_when_needed: bool = True
    #: Fraction of the measured span the window itself should cover; the rest is
    #: left to the camera. 1.0 would freeze the camera and letterbox everything.
    widen_coverage: float = 0.80
    #: Never widen past this share of the source, or the gameplay ends up a
    #: strip in the middle of the frame.
    widen_max_fraction: float = 0.72

    # Slow push in over the clip, as a fraction. 0.04 is a 4% creep — below the
    # threshold anyone notices deliberately, above the threshold it stops
    # feeling like a static recording.
    # Off. zoompan samples on whole pixels, so a zoom that is never exactly 1
    # resamples the picture continuously: measured on a deliberately frozen
    # source, a 4% push over the clip made 18% of frames shimmer, against 1
    # frame in 395 with no zoom at all. The punch below is worth its artefact
    # because it lasts two seconds and lands on the loudest moment in the clip;
    # a drift nobody consciously notices is not.
    zoom_push: float = 0.0
    # Extra zoom at the triggering moment, on top of the push. The telemetry
    # knows when it happens, so the camera can lean in for it.
    zoom_punch: float = 0.05
    #: Width of that lean, in seconds either side of the event.
    zoom_punch_seconds: float = 1.1

    # A clip covers one attempt. It may end on a death or a victory — that is
    # the ending — but nothing after it, and it may not open inside the respawn
    # fade of the previous one.
    death_tail: float = 0.40      # 24 frames: reads the death, misses the respawn
    victory_tail: float = 1.6     # nothing follows a win, so it can breathe
    respawn_margin: float = 5.2   # measured: the fade runs ~5s past a death

    # Margins around a scene change. The level_start event fires when the new
    # scene finishes loading, but the picture has already been changing for a
    # moment by then — a fade, a results screen — and the telemetry clock is
    # only aligned to the video to within about a second when the alignment
    # comes from the filename. Half a second of margin sits inside that error,
    # which is how the start of the next level ends up at the end of a clip.
    level_end_margin: float = 2.5
    level_start_margin: float = 1.5

    # Endless mode. The clip has to show enough of the run for the escalation
    # to be legible, which a twenty second cut is not.
    endless_lead_in: float = 22.0
    endless_min_duration: float = 24.0

    # Seconds the last frame is held at the end. The closing caption is the one
    # that decides whether a viewer replays; cutting on it wastes it.
    freeze_end: float = 0.6


@dataclass
class SelectionConfig:
    """What counts as a highlight, and how many to keep."""

    # Target density. The budget for a recording is its length divided by this,
    # bounded by the two below — so a long session yields more than a short one
    # instead of the same flat handful.
    minutes_per_clip: float = 2.0
    min_clips_per_video: int = 3
    max_clips_per_video: int = 14

    # Recording length at which the per-event caps below are worth exactly what
    # they say; longer recordings scale them up in proportion.
    cap_scale_minutes: float = 10.0

    # Two clips from the same stretch of footage look like duplicates on a feed.
    min_gap_between_clips: float = 25.0

    # Most candidates a single event type may contribute per recording. Some
    # events are common by nature: a wall near miss inside 0.7 units at speed 18
    # is a genuine squeeze, and one 12-minute session still logged 59 of them
    # while an 8-minute one logged 2. No fixed threshold fixes that, because the
    # difference is level geometry, so the tightest few per session win instead.
    # Uncapped event types are unaffected.
    max_per_event: dict[str, int] = field(
        default_factory=lambda: {
            # Capped like the other frequent ones: raising its weight on a
            # judgement call made it 44% of the catalogue.
            "bonus_near_miss": 2,
            "wallrun": 2,
            "burst": 2,
            "wall_near_miss": 2,
            "speed_peak": 2,
            "input_flurry": 2,
            "grapple_attach": 2,
            "shield_hit": 2,
            "bonus_pickup": 1,
            "checkpoint": 1,
        }
    )

    # Base score per event type. Everything else is a multiplier on top of this.
    weights: dict[str, float] = field(
        default_factory=lambda: {
            # Going for a diamond and dying for it is the best story the game
            # produces: a visible risk, taken deliberately, that did not pay
            # off. Nothing else in the log has a motive attached.
            "bonus_lost": 11.0,
            # Wavebursting: a burst aimed along the wall-jump direction while
            # wall-running, compounding speed instead of redirecting it. The
            # game reports it explicitly because nothing else in the log tells
            # it apart from an ordinary wall-run, and it is the clearest
            # evidence of someone who knows the movement system.
            "waveburst": 10.5,
            "jelly_combo": 10.0,
            # Racing your own previous run. The stakes are legible on screen
            # with no explanation, which almost nothing else here manages.
            "ghost_lead_change": 9.8,
            # Surviving something by inches is better footage than dying to it,
            # and rarer — the player had to actually pull it off.
            "projectile_near_miss": 9.5,
            "victory": 9.0,
            "bonus_secured": 8.5,
            # Reaching for a diamond and coming away without it. The detour is
            # a decision the player made, off the safe route, and losing it
            # reads better on screen than a clean pickup.
            "bonus_near_miss": 8.0,
            "worm_near": 8.0,
            "wall_near_miss": 7.0,
            "speed_peak": 7.0,
            # Judged by eye against real output: a wall-run sequence carrying
            # the movement tech was the best clip in the bottom twelve, above
            # several speed peaks. It ranked low because the log makes a skilled
            # wall-run and a routine one look identical — which is what the
            # waveburst event above now fixes at the source.
            "wallrun": 7.0,
            "death": 5.5,
            "input_flurry": 5.5,
            "bonus_pickup": 5.0,
            # Frequent and low on its own, but it is what makes a burst-and-
            # wall-run sequence visible to the scorer at all.
            # A single burst is nothing; a window full of them taken off
            # walls is the densest footage the game produces, and judged
            # by eye it beat clips scoring six times higher. Capped below
            # so it cannot flood the catalogue.
            "powerup_chosen": 0.0,
            "burst": 7.0,
            "grapple_attach": 3.0,
            "powerup_pickup": 2.5,
            "shield_hit": 2.0,
            "checkpoint": 0.5,
        }
    )

    # Scenes the game loads that are not gameplay. level_start fires for these
    # too, so without the list a menu or a cinematic can anchor a clip.
    ignore_scenes: list[str] = field(
        default_factory=lambda: [
            "main menu", "psascreen", "introcinematic", "endgamescreen",
            "credits", "title",
        ]
    )

    endless_scenes: list[str] = field(default_factory=lambda: ["endless"])

    # Retry loops. Dying repeatedly in one spot generates a dense event log and
    # nothing anyone wants to watch, so it is scored down rather than up.
    retry_tolerance: int = 2      # deaths in a window before the penalty starts
    retry_penalty: float = 0.6    # strength of the penalty per extra death
    max_deaths_per_clip: int = 4  # above this the window is not a candidate

    # Deaths from being wedged in geometry are a bug report, not a highlight.
    ignore_death_causes: list[str] = field(default_factory=lambda: ["stuck"])

    # Seconds a count is normalised to. Bonuses are computed per this much
    # footage, so a long clip stops out-scoring a short one for being long.
    rate_window: float = 20.0

    # Windows where the player was not playing. Speed below idle_speed counts
    # as motionless; past max_idle_fraction of the window, nobody was at the
    # controls and the events are the level killing an absent player.
    idle_speed: float = 1.0
    max_idle_fraction: float = 0.60

    # A near miss counts as a dodge only if nothing hit the player within this
    # many seconds of it, before or after, and only if it actually missed: the proximity tracker
    # reports the closest approach, and a contact shows up as distance zero.
    near_miss_damage_guard: float = 2.0
    near_miss_min_distance: float = 0.15

    # Screen pressure. hazard_bonus applies per hazard on screen past three,
    # from the game's own count; near_miss_bonus is the proxy that works on
    # recordings made before that count existed.
    hazard_bonus: float = 0.06
    near_miss_bonus: float = 0.05

    # Each burst taken off a wall lifts the window by this much, up to ten.
    wall_burst_bonus: float = 0.10

    # Two clips from the same corner of the same level are the same clip to a
    # viewer, however far apart they were played. In world units.
    # Measured across the catalogue: clips from the same level sit 828 units
    # apart at the median, so most are genuinely different sections. The
    # duplicates cluster under 100 — four clips of one LVL08 passage came in
    # between 40 and 90 units of each other and all survived a radius of 30.
    same_spot_radius: float = 100.0

    # A speed peak within this many seconds of a gas explosion is the gas
    # doing the work. Still footage worth having, just not a feat.
    gas_speed_window: float = 4.0
    gas_speed_discount: float = 0.55

    # Player speed (units/sec) that counts as "fast" when scoring a window.
    fast_speed: float = 18.0


@dataclass
class CopyConfig:
    # Which API to talk to. Both speak the OpenAI chat-completions protocol, so
    # the same client library is used either way — see PROVIDERS in
    # copywriter.py for the base URL, key variable and schema support of each.
    #
    # Changing this means changing `model` too; the model names are not shared.
    #   openai   -> gpt-5.6-terra  (also -sol, -luna)
    #   deepseek -> deepseek-v4-flash  (also deepseek-v4-pro)
    provider: str = "openai"

    # Terra is the balanced tier of the GPT-5.6 family: captions are short and
    # high-volume, so Sol's extra reasoning is not worth 2.5x the price.
    model: str = "gpt-5.6-terra"

    #: Override the provider's default endpoint. Blank uses the preset.
    base_url: str = ""
    #: Override which environment variable holds the key. Blank uses the preset.
    api_key_env: str = ""

    # How to constrain the model's output.
    #   auto        - use whatever the provider supports (recommended)
    #   json_schema - strict schema enforcement, OpenAI only
    #   json_object - plain JSON mode; the schema goes in the prompt and the
    #                 result is validated here instead
    structured_outputs: str = "auto"
    #: The game's name, as its store page styles it.
    brand: str = "This is no cave"
    #: Who made it. Only used to introduce the studio in the prompt.
    team: str = "a two-person team"

    # Editable prompt text, read at run time so edits apply immediately.
    #
    # This is now the only place the writer takes direction from, and it
    # deliberately gives almost none: what the events mean, the studio's
    # history, and the display limits. There is no tone, no house style and no
    # per-mechanic angle sheet — those lived here and in shorts.yaml, and the
    # copy came out sounding like it was working through a checklist. Add facts
    # to the `studio` section; do not add rules.
    # Seconds a single model call may take before it is abandoned.
    request_timeout: float = 120.0

    prompts_file: Path = REPO_ROOT / "prompts.md"
    hashtags: dict[str, list[str]] = field(
        default_factory=lambda: {
            "tiktok": ["#indiegame", "#gamedev", "#platformer"],
            "instagram": ["#indiegame", "#gamedev", "#indiedev", "#platformer"],
            "youtube": ["#shorts", "#indiegame", "#gamedev"],
        }
    )


@dataclass
class RenderConfig:
    # Impact is the closest thing Windows ships to the heavy condensed face this
    # style of caption wants. Override if you install something better.
    font_file: Path = Path("C:/Windows/Fonts/impact.ttf")
    font_name: str = "Impact"
    # 96 was legible from across a room and covered a third of the frame with
    # it. At 64 a 60-character beat fits on two lines instead of three, which
    # is the actual win: every line is gameplay the viewer does not get to see.
    font_size: int = 64
    outline: int = 4

    # Fraction of frame height kept clear at top and bottom. Platform UI
    # (captions, buttons, profile bar) covers roughly this much.
    safe_top: float = 0.14
    safe_bottom: float = 0.20

    # 23 rather than 20: at 1080x1920 the visual difference on gameplay footage
    # is hard to see, and it keeps a 12s clip under Discord's 10 MB attachment
    # cap for non-boosted servers. Lower it if you only post to platforms that
    # transcode anyway.
    # Minimum time any caption stays on screen. The viewer is reading while
    # also watching the gameplay, so this is far slower than silent reading —
    # anything less and lines get half-read.
    caption_min_seconds: float = 5.0
    caption_max_seconds: float = 9.0

    # While the game is paused on a full-screen menu the crop is dropped and the
    # whole 16:9 frame is shown in black bars, because a power-up choice spans
    # the full width and is unreadable cropped.
    #
    # Zero on purpose: the switch should be a hard cut on the exact frame the
    # menu appears and disappears. A pad was tried and it reads as the zoom
    # lingering after the menu has gone. Accuracy comes from aligning the two
    # clocks properly instead — see `refine_with_menus` in ingest.py.
    letterbox_pad: float = 0.0

    # Brought forward by this much, and only at the start. The overlay switches
    # in one frame, so landing 0.11s after the menu appears is visible; landing
    # 0.04s before it is not.
    letterbox_lead: float = 0.15

    # Held this much past the measured freeze. The picture resumes when the game
    # unfreezes, but the cards animate out over the next 0.4s and the frame is
    # still theirs until they are gone.
    letterbox_trail: float = 0.40

    # Fraction of the source width kept when letterboxing a menu. 1.0 shows the
    # whole 16:9 frame; the power-up cards only reach 57% of it, and their
    # layout roots cap them at 70%, so trimming to 0.72 keeps every card intact
    # while making the text 1.4x bigger inside the same bars.
    letterbox_zoom: float = 0.72

    # Every platform re-encodes on upload, so a larger file buys nothing. At
    # 1080x1920 the difference between 20 and 23 on gameplay footage is hard to
    # see.
    crf: int = 23
    preset: str = "medium"


@dataclass
class ExperimentConfig:
    """
    A/B testing for posts.

    Each post is tagged with the variant it used, so performance can be
    attributed later. Variants are assigned round-robin rather than randomly:
    with a handful of posts a day, random assignment takes far too long to
    balance.
    """

    enabled: bool = True

    #: Named hashtag sets per platform. Rotated between, then compared.
    hashtag_variants: dict[str, dict[str, list[str]]] = field(
        default_factory=lambda: {
            "tiktok": {
                "broad": ["#indiegame", "#gamedev", "#platformer"],
                "niche": ["#indiedev", "#pixelart", "#speedrun"],
            },
            "instagram": {
                "broad": ["#indiegame", "#gamedev", "#indiedev", "#platformer"],
                "niche": ["#pixelart", "#gamedevdiary", "#indiegamedev"],
            },
            "youtube": {
                "broad": ["#shorts", "#indiegame", "#gamedev"],
                "niche": ["#shorts", "#pixelart", "#indiedev"],
            },
        }
    )

    #: Below this many posts, a variant's average is noise — reported, not acted on.
    min_samples: int = 5


@dataclass
class OrchestratorConfig:
    """
    The planner. Decides what gets posted, where, and with what angle.

    Deliberately a different model from the writer. Planning is a reasoning job
    over performance data and history where a mistake compounds for weeks;
    writing is a high-volume job where a mistake costs one caption. Paying for
    the stronger model on the decisions and the cheaper one on the prose is the
    right split.

    The plan is a proposal, never a command. Cadence, platform readiness, clip
    availability are all enforced in code after the
    model answers — an autonomous system that can talk itself past its own rate
    limits is one bad run away from a ban.
    """

    enabled: bool = True

    provider: str = "openai"
    model: str = "gpt-5.6-terra"

    # Publish the plan without asking. This is what "runs for months unattended"
    # requires, and it is also the setting that lets a bad clip reach a real
    # audience with nobody looking. Leave it off until the reject rate is low.
    autopost: bool = False

    # Have the planner's model copy-edit the writer's output before it is
    # stored: grammar, spelling and phrasing only, never content. One extra
    # call per clip on the more expensive model, which is worth it because a
    # typo in a burned-in caption cannot be fixed without re-rendering.
    proofread: bool = True

    # Platforms the planner is not allowed to touch. `shorts publish` still
    # posts to them normally — this only reserves them for you. The usual
    # reason is a platform whose app is still in review, where posting has to
    # go through `shorts handoff` by hand anyway.
    manual_platforms: list[str] = field(default_factory=list)

    # Notes the planner writes to itself and reads back on the next run. This is
    # how a strategy survives across months of separate invocations, rather than
    # every run starting from nothing.
    # Seconds a single model call may take before it is abandoned.
    request_timeout: float = 120.0

    memory_file: Path = REPO_ROOT / "strategy.md"

    #: How many past posts of performance data to summarise into the prompt.
    history_limit: int = 40

    # Let the planner nudge selection weights from what performed. Bounded the
    # same way `learn --apply` is; it cannot run away.
    allow_weight_changes: bool = True


@dataclass
class HostingConfig:
    """
    Where to serve a clip from when a platform needs a public HTTPS URL.

    Only Instagram requires this — its Graph API fetches the video itself rather
    than accepting an upload. "github_release" uses a release in a repo you
    already own, which costs nothing; "public_url" assumes you are serving the
    output directory yourself.
    """

    backend: str = "github_release"
    #: github_release: "owner/repo". The release tag is created if absent.
    github_repo: str = ""
    github_release_tag: str = "shorts-hosting"
    #: public_url: base that maps to paths.output, e.g. "https://cdn.example.com/clips"
    public_base_url: str = ""


@dataclass
class PublishConfig:
    #: Platforms to post to, in order.
    platforms: list[str] = field(
        default_factory=lambda: ["youtube", "tiktok", "instagram"]
    )

    # Minimum days between posts on each platform. The queue skips a platform
    # until this much time has passed since its last successful post, so a
    # backlog of ready clips drains at a steady rate instead of all at once.
    # 1.75 days is 42 hours. Deliberately not a whole number of days: a whole
    # one pins every post to the same hour, and a feed that publishes at exactly
    # 18:00 every other day is reading as a scheduler to anyone paying
    # attention. 42 walks the slot around the clock instead.
    # A temporary burst that overrides the cadence, with its own end date so it
    # switches itself off. Empty or a past date means no promotion.
    promo: dict[str, Any] = field(default_factory=dict)

    cadence_days: dict[str, float] = field(
        default_factory=lambda: {
            "youtube": 1.75,
            "tiktok": 1.75,
            "instagram": 1.75,
        }
    )

    # Safety net in case cadence is misconfigured; not the primary control.
    max_posts_per_platform_per_day: int = 2

    youtube_client_secrets: Path = REPO_ROOT / "credentials.json"
    youtube_token: Path = REPO_ROOT / "youtube_token.json"
    #: 20 = Gaming.
    youtube_category_id: str = "20"
    #: "public", "unlisted" or "private". Start unlisted while you're testing.
    youtube_privacy: str = "unlisted"

    tiktok_token: Path = REPO_ROOT / "tiktok_token.json"
    #: Forced to SELF_ONLY until your app passes TikTok's audit.
    tiktok_privacy: str = "SELF_ONLY"
    #: "inbox" uploads to drafts for the creator to post (no audit
    #: needed, no caption carried); "direct" posts to the profile and
    #: needs an audited app, which TikTok refused us.
    tiktok_mode: str = "inbox"

    #: Instagram Business/Creator account id (numeric), from the Graph API.
    instagram_user_id: str = ""

    hosting: HostingConfig = field(default_factory=HostingConfig)


@dataclass
class RedditConfig:
    """
    A second output profile, for a different audience and a different feed.

    Reddit is read on a desktop by people who play on one. A 4:5 clip there
    sits in a narrow column with empty space either side, which reads as
    something made for a phone and reposted — the exact impression to avoid
    when the point is reaching PC players. 4:3 is cropped from the 1920x1080
    source at 1440x1080, so it uses three quarters of the frame width against
    the 864 px the vertical cut keeps, and never upscales.

    Nothing here posts. It produces a small pile of finished files and a
    manifest; a person picks where each one goes.
    """

    #: 1440x1080 is 4:3; 1080x1080 is square. Both crop from the source
    #: without upscaling, which is why neither costs any sharpness.
    width: int = 1440
    height: int = 1080

    #: How many clips to keep, out of everything every recording offers.
    #: The vertical feed wants volume; this wants the highlights only.
    count: int = 10

    # Longer than the vertical cut. There are no captions to read here and no
    # thumb waiting to scroll, so a moment can be given its run-up and its
    # aftermath instead of being trimmed to the beat.
    #: 90 was tried and every clip came out at exactly 90: the window grows
    #: through nearby action, and with a gap that wide the action never ends.
    #: A cap every clip reaches is not a cap, it is a fixed length.
    min_duration: float = 22.0
    max_duration: float = 60.0
    lead_in: float = 14.0
    lead_out: float = 8.0
    action_gap: float = 7.0

    # Visibly better than the feed encode, because this is watched on a large
    # screen and often paused. crf 16 against 23 roughly triples the file,
    # which matters not at all for ten files nobody uploads by API.
    crf: int = 16
    preset: str = "slow"

    output: Path = REPO_ROOT / "reddit_out"


@dataclass
class Config:
    paths: Paths = field(default_factory=Paths)
    clip: ClipConfig = field(default_factory=ClipConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    copy: CopyConfig = field(default_factory=CopyConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    experiments: ExperimentConfig = field(default_factory=ExperimentConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    reddit: RedditConfig = field(default_factory=RedditConfig)

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    #: Where this config came from. Anything that writes settings back — the
    #: learning loop, mainly — must land in the file that was actually loaded,
    #: not in the default one.
    source_path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        cfg = cls()
        cfg.source_path = path
        if not path.exists():
            return cfg

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        _merge(cfg, raw)
        cfg.source_path = path
        return cfg

    def write_default(self, path: Path | None = None) -> Path:
        path = Path(path) if path else (self.source_path or DEFAULT_CONFIG_PATH)
        data = asdict(self)
        # Bookkeeping, not a setting.
        data.pop("source_path", None)
        path.write_text(
            yaml.safe_dump(_stringify(data), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return path


def _merge(target: Any, raw: dict) -> None:
    """
    Applies a parsed YAML dict onto a dataclass instance, in place.

    Plain dict fields — selection weights, cadences, hashtag sets — are merged
    key by key rather than replaced. A saved config predates any key added since
    it was written, and replacing the whole dict would silently drop those
    defaults: adding a weight for a new event type would do nothing until the
    file was regenerated. The file still wins for every key it does define.

    Lists are replaced outright, because a list is an ordered choice rather than
    a set of independent settings.
    """
    for key, value in raw.items():
        if not hasattr(target, key):
            continue

        current = getattr(target, key)

        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge(current, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            merged = dict(current)
            merged.update(value)
            setattr(target, key, merged)
        elif isinstance(current, Path):
            setattr(target, key, Path(value))
        else:
            setattr(target, key, value)


def _stringify(obj: Any) -> Any:
    """Paths are not YAML-serialisable; everything else passes through."""
    if isinstance(obj, dict):
        return {k: _stringify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj
