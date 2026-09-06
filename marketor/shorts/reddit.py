"""
A second output profile: a few long, wide, caption-free clips.

The vertical pipeline optimises for a feed — many clips, short, square-ish,
text burned on so a muted autoplay still reads. Reddit is the opposite case.
People arrive on a desktop, click deliberately, and watch. So this produces
roughly ten files instead of seventy, each longer, cropped 4:3 instead of 4:5,
encoded well above feed quality, and with nothing written over the picture.

Nothing here posts or touches the posting database. It writes files and a
manifest; where each one goes is a person's decision.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from . import db, ffmpeg, render, select
from .config import Config
from .select import Candidate


def _profile(cfg: Config) -> Config:
    """
    The main config, re-pointed at the Reddit profile.

    Built by substitution rather than written out again so that everything
    tuned on the vertical pipeline — the level boundaries, the death tails,
    the idle filtering, the dodge validation — applies here unchanged. Only
    geometry, length and encode quality differ, which is the whole intent.
    """
    r = cfg.reddit
    return replace(
        cfg,
        clip=replace(
            cfg.clip,
            width=r.width,
            height=r.height,
            min_duration=r.min_duration,
            max_duration=r.max_duration,
            lead_in=r.lead_in,
            lead_out=r.lead_out,
            action_gap=r.action_gap,
        ),
        render=replace(cfg.render, crf=r.crf, preset=r.preset),
    )


def _rank(
    cands: list[tuple[Candidate, object]], count: int, radius: float
) -> list[tuple[Candidate, object]]:
    """
    Best first, with one clip per spot across the whole set.

    select_for_source already drops repeats inside one recording, but two
    recordings of the same level produce the same jump twice, and a set of ten
    highlights that shows one corridor three times is not a set of ten
    highlights.
    """
    ordered = sorted(cands, key=lambda cs: cs[0].score, reverse=True)

    # Scoring ranks moments against each other, not sets against each other,
    # so the top of the list is whatever the scorer likes most — and it likes
    # near-misses. Ten of those is one clip shown ten times. A ceiling per
    # trigger costs a little score and buys a set that is worth scrolling.
    per_trigger = max(2, count // 4)
    seen: dict[str, int] = {}

    kept: list[tuple[Candidate, object]] = []
    for cand, source in ordered:
        if radius > 0 and any(
            select._same_spot(cand, other, radius) for other, _ in kept
        ):
            continue
        if seen.get(cand.trigger, 0) >= per_trigger:
            continue
        seen[cand.trigger] = seen.get(cand.trigger, 0) + 1
        kept.append((cand, source))
        if len(kept) >= count:
            break

    # Backfill if diversity left the set short: a thinner set of ten is worse
    # than an eleventh near-miss.
    if len(kept) < count:
        chosen_ids = {id(c) for c, _ in kept}
        for cand, source in ordered:
            if id(cand) in chosen_ids:
                continue
            if radius > 0 and any(
                select._same_spot(cand, other, radius) for other, _ in kept
            ):
                continue
            kept.append((cand, source))
            if len(kept) >= count:
                break
    return kept


def prepare(cfg: Config, *, count: int | None = None, verbose: bool = True) -> int:
    """Renders the best few clips into the Reddit profile. Returns how many."""
    profile = _profile(cfg)
    wanted = count or cfg.reddit.count
    out_dir = cfg.reddit.output
    out_dir.mkdir(parents=True, exist_ok=True)

    pool: list[tuple[Candidate, object]] = []
    with db.connect(cfg.paths.database) as conn:
        sources = list(conn.execute("SELECT * FROM sources"))

    if not sources:
        print("no recordings ingested yet")
        return 0

    for source in sources:
        if not Path(source["path"]).exists():
            continue
        try:
            found = select.select_for_source(profile, source, verbose=verbose)
        except Exception as exc:
            print(f"  !! {Path(source['path']).name}: {exc}")
            continue
        pool.extend((cand, source) for cand in found)

    if not pool:
        print("nothing worth cutting")
        return 0

    # A wider radius than the feed uses. Ten clips can carry ten different
    # places; seventy cannot, so the vertical pipeline has to tolerate near
    # neighbours that this one has no reason to.
    chosen = _rank(pool, wanted, cfg.selection.same_spot_radius * 2.0)
    if verbose:
        print(f"\n  {len(pool)} candidates -> keeping the best {len(chosen)}")

    manifest: list[dict] = []
    made = 0

    for index, (cand, source) in enumerate(chosen, start=1):
        out_path = out_dir / f"reddit_{index:02d}.mp4"
        median_x, series = render._viewport_for_clip(source, cand.start, cand.end)
        menus = render._menu_windows_for_source(source)

        trigger_at = cand.context.get("trigger_at")
        try:
            trigger_at = float(trigger_at) if trigger_at is not None else None
        except (TypeError, ValueError):
            trigger_at = None

        try:
            render.render_clip(
                profile,
                source_path=Path(source["path"]),
                start=cand.start,
                end=cand.end,
                hook="",
                out_path=out_path,
                viewport_x=median_x,
                viewport_series=series,
                beats=None,
                menu_windows=menus,
                trigger_at=trigger_at,
                captions_on=False,
            )
        except ffmpeg.FFmpegError as exc:
            print(f"  !! {out_path.name} failed: {exc}")
            continue

        ctx = cand.context
        level = ctx.get("level") or {}
        # Difficulty is a real distinction in a built level and a setting with
        # no effect in endless. Reporting "hardcore" on an endless run states
        # something the game does not do.
        difficulty = (
            level.get("difficulty") if ctx.get("mode") != "endless" else None
        )
        manifest.append({
            "file": out_path.name,
            "seconds": round(cand.duration, 1),
            "score": round(cand.score, 1),
            "mode": ctx.get("mode"),
            "scene": ctx.get("scene"),
            "difficulty": difficulty,
            "shows": cand.trigger,
            "peak_speed": ctx.get("peak_speed"),
            "recording": Path(source["path"]).name,
            "at": f"{cand.start:.1f}-{cand.end:.1f}s",
        })
        made += 1
        if verbose:
            print(f"  + {out_path.name}  {cand.duration:4.1f}s  {cand.trigger}")

    _write_manifest(cfg, out_dir, manifest)
    return made


def _write_manifest(cfg: Config, out_dir: Path, rows: list[dict]) -> None:
    """A plain-text index, so choosing where each clip goes needs no database."""
    (out_dir / "clips.json").write_text(
        json.dumps(rows, indent=2), encoding="utf-8"
    )

    lines = [
        f"{cfg.copy.brand} — {len(rows)} clips for manual posting",
        f"{cfg.reddit.width}x{cfg.reddit.height}, no burned-in text, crf {cfg.reddit.crf}",
        "",
    ]
    for row in rows:
        where = row["scene"] or row["mode"] or "?"
        if row.get("difficulty"):
            where = f"{where}, {row['difficulty']}"
        speed = f", peak {row['peak_speed']:.0f} km/h" if row.get("peak_speed") else ""
        lines.append(f"{row['file']}  {row['seconds']:.0f}s")
        lines.append(f"    {where} / {row['shows']}{speed}")
        lines.append(f"    from {row['recording']} at {row['at']}")
        lines.append("")

    (out_dir / "clips.txt").write_text("\n".join(lines), encoding="utf-8")
