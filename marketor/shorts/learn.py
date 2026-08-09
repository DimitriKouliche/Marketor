"""
Turns post performance into decisions.

Three questions, answered separately because they have different answers per
platform: which hashtag set works, whether montages beat single clips, and which
kinds of moment are worth clipping at all.

A deliberate split on what gets automated. Selection weights are a continuous
knob and are safe to nudge from data, so `--apply` will adjust them. Hashtag sets
and post format are discrete choices, and auto-picking a winner would quietly end
the experiment that is still gathering evidence — those are reported for you to
decide.

Everything is compared strictly within one platform. A YouTube view and a Bluesky
like are not the same unit and averaging across them would be meaningless.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from typing import Any

from . import db
from .config import Config


@dataclass
class Group:
    label: str
    values: list[float]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values) if self.values else 0.0

    @property
    def spread(self) -> float:
        """Standard deviation; a large one next to a small n means 'don't act'."""
        return statistics.stdev(self.values) if len(self.values) > 1 else 0.0


def _collect(cfg: Config) -> dict[str, Any]:
    """Reads measured posts and buckets them by the things we want to compare."""
    by_variant: dict[tuple[str, str], Group] = {}
    by_trigger: dict[str, Group] = {}
    measured = 0

    with db.connect(cfg.paths.database) as conn:
        rows = conn.execute(
            """
            SELECT p.platform, p.variant, p.metrics, c.trigger_event
              FROM posts p JOIN clips c ON c.id = p.clip_id
             WHERE p.state = 'posted' AND p.metrics IS NOT NULL
            """
        ).fetchall()

    for row in rows:
        try:
            primary = float((json.loads(row["metrics"]) or {}).get("primary") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        measured += 1
        platform = row["platform"]
        variant = row["variant"] or "default"

        # Labels are "hashtags:<arm>", possibly with a legacy "|..." suffix.
        # Take the arm, ignore the rest.
        hashtag_arm = variant
        for part in variant.split("|"):
            if part.startswith("hashtags:"):
                hashtag_arm = part.split(":", 1)[1]

        by_variant.setdefault((platform, hashtag_arm), Group(hashtag_arm, [])).values.append(primary)
        by_trigger.setdefault(row["trigger_event"] or "unknown", Group("", [])).values.append(primary)

    return {
        "measured": measured,
        "by_variant": by_variant,
        "by_trigger": by_trigger,
    }


def _report_pairs(title: str, groups: dict[tuple[str, str], Group], min_samples: int) -> None:
    if not groups:
        return

    print(f"\n{title}")
    platforms = sorted({p for p, _ in groups})

    for platform in platforms:
        arms = {label: g for (p, label), g in groups.items() if p == platform}
        print(f"  {platform}")
        for label, group in sorted(arms.items(), key=lambda kv: -kv[1].mean):
            flag = "" if group.n >= min_samples else "  (too few to trust)"
            print(f"    {label:<14} mean {group.mean:>8.1f}  n={group.n}{flag}")

        usable = {k: v for k, v in arms.items() if v.n >= min_samples}
        if len(usable) >= 2:
            ranked = sorted(usable.items(), key=lambda kv: -kv[1].mean)
            best, second = ranked[0], ranked[1]
            lift = (best[1].mean / second[1].mean - 1) * 100 if second[1].mean else 0.0
            # A gap smaller than the spread is not a result.
            if lift >= 10 and best[1].mean - second[1].mean > best[1].spread / 2:
                print(f"    -> '{best[0]}' leads by {lift:.0f}%")
            else:
                print("    -> no clear winner yet")


def report(cfg: Config, *, apply: bool = False) -> int:
    data = _collect(cfg)
    minimum = cfg.experiments.min_samples

    if not data["measured"]:
        print(
            "No measured posts yet.\n"
            "Publish some clips, wait a day or two, then run 'shorts metrics'."
        )
        return 0

    print(f"Learning from {data['measured']} measured post(s).")

    _report_pairs("Hashtag sets (per platform):", data["by_variant"], minimum)

    triggers = data["by_trigger"]
    if triggers:
        print("\nClip type (all platforms pooled, relative):")
        overall = statistics.fmean(
            [v for g in triggers.values() for v in g.values]
        ) or 1.0
        for name, group in sorted(triggers.items(), key=lambda kv: -kv[1].mean):
            rel = group.mean / overall
            print(f"  {name:<16} {rel:>5.2f}x  n={group.n}")

    if apply:
        return _apply_weights(cfg, triggers, overall, minimum)

    print("\nRe-run with --apply to fold clip-type performance into selection weights.")
    return 0


def _apply_weights(
    cfg: Config, triggers: dict[str, Group], overall: float, minimum: int
) -> int:
    """
    Nudges selection weights toward the clip types that actually performed.

    Damped at half the observed effect and clamped to a 4x band, because early
    samples are noisy and a runaway weight would collapse the feed onto one kind
    of clip — which is the failure mode this whole loop exists to avoid.
    """
    weights = dict(cfg.selection.weights)
    changes: list[str] = []

    for name, group in triggers.items():
        if group.n < minimum or name not in weights:
            continue

        rel = (group.mean / overall) if overall else 1.0
        old = weights[name]
        new = old * (1 + 0.5 * (rel - 1))
        new = max(old / 4, min(old * 4, round(new, 2)))

        if abs(new - old) >= 0.05:
            weights[name] = new
            changes.append(f"  {name:<16} {old:>6.2f} -> {new:<6.2f} (n={group.n})")

    if not changes:
        print(f"\nNo weight changes: nothing has {minimum}+ samples with a real effect yet.")
        return 0

    cfg.selection.weights = weights
    path = cfg.write_default()

    print("\nUpdated selection weights:")
    for line in changes:
        print(line)
    print(f"\nWritten to {path}")
    return 0
