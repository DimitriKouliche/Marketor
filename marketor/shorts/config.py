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
    height: int = 1920
    fps: int = 30

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
    track_deadzone: float = 0.35

    # Seconds of moving average applied to player position before following.
    # Removes sample noise; raise it if the camera still feels busy.
    track_smoothing: float = 1.5


@dataclass
class SelectionConfig:
    """What counts as a highlight, and how many to keep."""

    max_clips_per_video: int = 6

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
            "jelly_combo": 10.0,
            # Racing your own previous run. The stakes are legible on screen
            # with no explanation, which almost nothing else here manages.
            "ghost_lead_change": 9.8,
            # Surviving something by inches is better footage than dying to it,
            # and rarer — the player had to actually pull it off.
            "projectile_near_miss": 9.5,
            "victory": 9.0,
            "bonus_secured": 8.5,
            "worm_near": 8.0,
            "wall_near_miss": 7.0,
            "speed_peak": 7.0,
            "wallrun": 6.0,
            "death": 5.5,
            "input_flurry": 5.5,
            "bonus_pickup": 5.0,
            "grapple_attach": 3.0,
            "powerup_pickup": 2.5,
            "shield_hit": 2.0,
            "checkpoint": 0.5,
        }
    )

    # Deaths from being wedged in geometry are a bug report, not a highlight.
    ignore_death_causes: list[str] = field(default_factory=lambda: ["stuck"])

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
    cadence_days: dict[str, float] = field(
        default_factory=lambda: {
            "youtube": 2.0,
            "tiktok": 2.0,
            "instagram": 2.0,
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

    #: Instagram Business/Creator account id (numeric), from the Graph API.
    instagram_user_id: str = ""

    hosting: HostingConfig = field(default_factory=HostingConfig)


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
