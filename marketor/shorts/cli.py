"""
Command line entry point for the shorts pipeline.

    shorts init      write a default shorts.yaml
    shorts ingest    register new recordings and pair them with telemetry
    shorts select    pick highlight clips from the event log
    shorts write     generate hooks and captions
    shorts render    cut, reframe and caption the clips
    shorts qc        run automated checks on rendered clips
    shorts run       all of the above, in order
    shorts status    what is in the queue
    shorts approve   mark clips ready to publish
    shorts publish   post the approved clips
    shorts handoff   what to post by hand while an app is in review
    shorts orchestrate  let the planner decide what goes out, and where
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from . import copywriter, db, env, health, ingest, learn, manual, metrics, orchestrator
from . import tiktok_auth
from . import prompts, publish, qc
from . import config as config_module
from . import render, select, telemetry
from .config import Config


def _cfg(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config)
    cfg.paths.ensure()
    return cfg


def _clear_output(cfg: Config, *, verbose: bool = True) -> int:
    """Deletes rendered artifacts. Locked files are reported, not fatal."""
    removed = 0
    for path in sorted(cfg.paths.output.glob("*")):
        if not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            print(f"  !! {path.name} is in use, left in place ({exc.strerror})")
    if verbose:
        print(f"  cleared {removed} file(s) from {cfg.paths.output}")
    return removed


def _source_count(cfg) -> int:
    with db.connect(cfg.paths.database) as conn:
        return conn.execute("SELECT COUNT(*) n FROM sources").fetchone()["n"]


def _lock_handle(path: Path):
    """
    Takes an OS-level exclusive lock on a file, or returns None.

    A lock the operating system owns, rather than a file whose existence means
    "busy". A run that is killed — Ctrl-C, a stopped background task, a power
    cut — never gets to clean up after itself, and a plain marker file left
    behind then blocks every scheduled run until someone notices. This kind of
    lock is dropped by the OS when the process ends, however it ends.
    """
    handle = open(path, "a+")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _acquire_pipeline_lock(cfg):
    """
    Claims the right to run the pipeline, or returns None if someone else has it.

    Two passes over the same database is not a race worth handling gracefully:
    they render the same clips twice, and one dies mid-write when the other
    takes the database's write lock. The scheduled hourly run landing on top of
    a manual regen is exactly that, and it is the common case rather than the
    exotic one.
    """
    path = cfg.paths.database.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)

    handle = _lock_handle(path)
    if handle is None:
        return None

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()} {db.now()}")
    handle.flush()
    return handle


def _release_pipeline_lock(handle) -> None:
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()


def _reattach_posts(cfg, saved: list[dict]) -> int:
    """
    Puts the publication record back onto the clips that replaced the old ones.

    Re-selection renumbers every clip and posts hang off clip ids, so without
    this a re-select silently erases the fact that something is already live —
    and with it the only thing stopping the planner sending the same video out
    again. Matching is by overlap rather than by identical window, because
    re-selection is normally run precisely *because* the windows are moving.
    """
    if not saved:
        return 0

    reattached = 0
    orphans: list[dict] = []

    with db.connect(cfg.paths.database) as conn:
        clips = conn.execute("SELECT id, source_id, start_s, end_s FROM clips").fetchall()

        for post in saved:
            best, best_overlap = None, 0.0
            for clip in clips:
                if clip["source_id"] != post["source_id"]:
                    continue
                overlap = (
                    min(clip["end_s"], post["end_s"])
                    - max(clip["start_s"], post["start_s"])
                )
                shorter = min(
                    clip["end_s"] - clip["start_s"], post["end_s"] - post["start_s"]
                )
                if shorter > 0 and overlap > 0.5 * shorter and overlap > best_overlap:
                    best, best_overlap = clip, overlap

            if best is None:
                orphans.append(post)
                continue

            conn.execute(
                """
                INSERT INTO posts (clip_id, platform, state, remote_id, url,
                                   metrics, posted_at, created_at, variant)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (clip_id, platform) DO NOTHING
                """,
                (best["id"], post["platform"], post["state"], post["remote_id"],
                 post["url"], post["metrics"], post["posted_at"],
                 post["created_at"], post["variant"]),
            )
            reattached += 1
        conn.commit()

    print(f"  {reattached} publication(s) rattachee(s) aux nouveaux clips")
    for post in orphans:
        # Loud, because this is a live video with nothing left to stop the
        # planner posting it again.
        print(f"  !! sans equivalent : {post['platform']} {post['url'] or ''}")
        print("     cette video est en ligne et n'est plus protegee")

    return 0


def cmd_realign(args: argparse.Namespace) -> int:
    """
    Recomputes the telemetry-to-video offset for recordings already ingested.

    Alignment is the one number every later stage trusts blindly: the tracking
    crop, the caption timing and the menu letterbox are all placed by it, and an
    error of a second is invisible in the first two and glaring in the third.
    When the way it is measured improves, the recordings already in the database
    keep whatever they were given at ingest — so this exists to go back and
    correct them without re-ingesting or losing anything downstream.
    """
    cfg = _cfg(args)
    changed = 0

    with db.connect(cfg.paths.database) as conn:
        sources = conn.execute(
            "SELECT * FROM sources WHERE telemetry_path IS NOT NULL "
            "AND offset_seconds IS NOT NULL"
        ).fetchall()

        for source in sources:
            path = Path(source["path"])
            session_path = Path(source["telemetry_path"])
            name = path.name
            if not path.exists() or not session_path.exists():
                print(f"  -- {name}: fichier absent, ignore")
                continue

            session = telemetry.load_session(session_path)
            try:
                start_utc = datetime.fromisoformat(source["start_utc"])
            except (TypeError, ValueError):
                print(f"  -- {name}: pas d'heure de depart enregistree")
                continue

            # Back to the wall-clock estimate before refining, so a correction
            # is never applied twice.
            base = (session.start_utc - start_utc).total_seconds() - session.events[0].t

            # One pass over the video, used for both the offset and the menu
            # windows: it is by far the most expensive part of this.
            motion = ingest.motion_series(cfg, path)
            refined = ingest.refine_with_menus(cfg, path, session, base, motion=motion)
            offset, method = refined if refined else (base, "filename")

            windows = ingest.measure_menu_windows(session, offset, motion)
            before = float(source["offset_seconds"])
            moved = abs(offset - before) >= 0.02

            conn.execute(
                "UPDATE sources SET offset_seconds = ?, alignment = ?, "
                "menu_windows = ? WHERE id = ?",
                (offset, method,
                 json.dumps([[round(a, 3), round(b, 3)] for a, b in windows])
                 if windows else None,
                 source["id"]),
            )

            measured = f", {len(windows)} menu(s) mesure(s)" if windows else ""
            if moved:
                changed += 1
                print(f"  +  {name}: {before:+.2f}s -> {offset:+.2f}s  ({method}{measured})")
            else:
                print(f"  =  {name}: cale inchange ({method}{measured})")
        conn.commit()

    print()
    print(f"{changed} enregistrement(s) recale(s) sur {len(sources)}")
    if changed:
        print("Relancez 'marketor regen --reselect' pour en tenir compte.")
    return 0


def _slots_at_risk(cfg) -> list[str]:
    """Publishing slots still due today that a regeneration would swallow."""
    from datetime import date

    at_risk: list[str] = []
    try:
        with db.connect(cfg.paths.database) as conn:
            for platform in cfg.publish.platforms:
                if platform in cfg.orchestrator.manual_platforms:
                    continue
                slot = publish.promo_slot(cfg, platform, date.today())
                ready, _ = publish.due(conn, cfg, platform)
                if ready:
                    at_risk.append(f"{platform}: du maintenant")
                elif slot is not None and datetime.now().hour < slot:
                    at_risk.append(f"{platform}: creneau a {slot:02d}h")
    except Exception:
        return []
    return at_risk


def cmd_regen(args: argparse.Namespace) -> int:
    """
    Redoes everything downstream of clip selection.

    The common case after editing prompts.md: the same moments are still the
    right moments, so re-ingesting and re-selecting is wasted work. Copy and
    render are what actually change, and render is unavoidable because the
    captions are burned into the video.

    --full also re-selects, for when the selection weights or clip length
    changed rather than the wording.
    """
    cfg = _cfg(args)

    existing = 0
    if cfg.paths.database.exists():
        with db.connect(cfg.paths.database) as conn:
            existing = conn.execute("SELECT COUNT(*) n FROM clips").fetchone()["n"]

    reselect = bool(getattr(args, "reselect", False))
    saved_posts: list[dict] = []

    # A regeneration empties the publishable catalogue before refilling it: copy
    # is cleared, renders are deleted, and for the hour or more it runs there is
    # nothing for the planner to post. Started inside a posting window that is
    # exactly what happens — measured once, an Instagram slot at 20h was lost to
    # a regeneration begun at 19h40, and the day's post never went out.
    if not args.yes:
        blocked = _slots_at_risk(cfg)
        if blocked:
            print("Un creneau de publication est encore ouvert aujourd'hui :")
            for line in blocked:
                print(f"  {line}")
            print("La regeneration va vider le catalogue pendant plus d'une heure.")
            if input("Lancer quand meme ? [o/N] ").strip().lower() not in ("o", "oui", "y", "yes"):
                print("annule - relancez apres la fenetre de publication")
                return 1
    full = (args.full or existing == 0) and not reselect
    if existing == 0 and not args.full and not reselect:
        print("No clips yet — running the full pipeline.")

    if args.dry_run:
        print("Would:")
        if full:
            print(f"  delete {cfg.paths.database}")
        elif reselect:
            print(f"  delete {existing} clip(s) and re-select from the stored sources")
        else:
            print(f"  reset copy and render state on {existing} clip(s)")
        print(f"  clear rendered files from {cfg.paths.output}")
        print("  then: " + " -> ".join(
            (["ingest", "select"] if full else (["select"] if reselect else []))
            + ["write", "render", "qc"]
        ))
        return 0

    if full and not args.yes:
        # --full deletes the database, and with it the (clip, platform)
        # uniqueness that is the only thing stopping already-published footage
        # going out a second time. Worth one question.
        posted = 0
        if cfg.paths.database.exists():
            with db.connect(cfg.paths.database) as conn:
                posted = conn.execute(
                    "SELECT COUNT(*) n FROM posts WHERE state = 'posted'"
                ).fetchone()["n"]
        if posted:
            print(f"--full supprime la base : {posted} publication(s) enregistree(s)")
            print("perdues, y compris la protection anti-republication.")
            if input("Continuer ? [o/N] ").strip().lower() not in ("o", "oui", "y", "yes"):
                print("annule")
                return 1

    if reselect:
        # Re-selection without re-ingestion. Once a recording is archived the
        # ingest step will not find it again, so --full would rebuild the
        # database around no footage at all. This keeps the sources and throws
        # away only the choices made from them.
        with db.connect(cfg.paths.database) as conn:
            posted = conn.execute(
                "SELECT COUNT(*) n FROM posts WHERE state = 'posted'"
            ).fetchone()["n"]
            if posted and not args.yes:
                print(f"{posted} publication(s) enregistree(s). Elles seront")
                print("rattachees aux nouveaux clips par recouvrement de fenetre;")
                print("celles qui n'ont plus d'equivalent seront signalees.")
                if input("Continuer ? [o/N] ").strip().lower() not in ("o", "oui", "y", "yes"):
                    print("annule")
                    return 1

            # Kept across the delete and matched back on afterwards, so the
            # record of what is already live survives the renumbering.
            saved_posts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT p.platform, p.state, p.remote_id, p.url, p.metrics,
                           p.posted_at, p.created_at, p.variant,
                           c.source_id, c.start_s, c.end_s
                      FROM posts p JOIN clips c ON c.id = p.clip_id
                     WHERE p.state = 'posted'
                    """
                )
            ]

            removed = conn.execute("DELETE FROM clips").rowcount
            conn.execute("UPDATE sources SET state = 'new'")
            conn.commit()
        print(f"  {removed} clip(s) supprime(s), {_source_count(cfg)} source(s) a reselectionner")

    if full:
        if cfg.paths.database.exists():
            cfg.paths.database.unlink()
    elif reselect:
        pass
    else:
        with db.connect(cfg.paths.database) as conn:
            conn.execute(
                """
                UPDATE clips
                   SET state = 'selected', hook = NULL, caption = NULL,
                       hashtags = NULL, beats = NULL, render_path = NULL, qc = NULL
                """
            )
        print(f"  reset {existing} clip(s) for rewrite")

    # Only wipe the folder when the clips themselves are being rebuilt. A plain
    # regen keeps every clip id, so each render overwrites its own file and the
    # old ones stay watchable until the moment they are replaced — emptying the
    # folder up front just takes the previous batch away from whoever is looking
    # at it for the twenty minutes the new one takes to appear.
    if full or reselect:
        _clear_output(cfg)

    steps = [cmd_ingest, cmd_select] if full else ([cmd_select] if reselect else [])
    if reselect and saved_posts:
        steps.append(lambda _args: _reattach_posts(cfg, saved_posts))
    steps += [cmd_write, cmd_render, cmd_qc]

    for step in steps:
        code = step(args)
        if code != 0:
            return code

    if not (full or reselect):
        _remove_orphans(cfg)

    return 0


def _remove_orphans(cfg: Config) -> int:
    """Deletes rendered files no clip points at any more."""
    with db.connect(cfg.paths.database) as conn:
        kept = {
            row["render_path"]
            for row in conn.execute(
                "SELECT render_path FROM clips WHERE render_path IS NOT NULL"
            )
        }

    removed = 0
    for path in sorted(cfg.paths.output.glob("*")):
        if not path.is_file() or str(path) in kept:
            continue
        # The sidecars a render leaves next to its video.
        if path.suffix in (".ass", ".txt") and any(
            k.startswith(str(path.with_suffix(""))) for k in kept
        ):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"  cleaned up {removed} stale file(s)")
    return removed


def cmd_prompts(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    path = cfg.copy.prompts_file

    if path.exists() and not args.reset:
        sections = prompts.parse(path.read_text(encoding="utf-8"))
        missing = sorted(set(prompts.DEFAULTS) - set(sections))
        print(f"{path}")
        print(f"  {len(sections)} section(s) defined: {', '.join(sorted(sections))}")
        if missing:
            print(f"  using built-in defaults for: {', '.join(missing)}")
        print("\nEdit the file and re-run 'shorts write'. Use --reset to restore.")
        return 0

    written = prompts.write_default(path)
    print(f"Wrote {written}")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """
    Writes shorts.yaml, preserving an existing one.

    init doubles as the upgrade path — re-running it fills in fields added since
    the file was written, which is exactly how `provider` came to be missing.
    So it loads first and keeps your values; --reset is how you start over.
    """
    path = Path(args.config) if args.config else config_module.DEFAULT_CONFIG_PATH
    existed = path.exists()

    cfg = Config() if (args.reset or not existed) else Config.load(path)
    written = cfg.write_default(path)

    if not cfg.copy.prompts_file.exists():
        prompts.write_default(cfg.copy.prompts_file)
        print(f"Wrote {cfg.copy.prompts_file}")

    if existed and not args.reset:
        print(f"Updated {written} (existing values kept)")
    else:
        print(f"Wrote {written}")
    print(f"  recordings : {cfg.paths.recordings}")
    print(f"  telemetry  : {cfg.paths.telemetry}")
    print(f"  output     : {cfg.paths.output}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("Ingesting recordings...")
    added = ingest.ingest(cfg)
    print(f"{len(added)} new recording(s)")
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("Selecting clips...")
    count = select.select(cfg)
    print(f"{count} clip(s) selected")
    return 0


def cmd_write(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("Writing copy...")
    count = copywriter.write_pending(cfg, limit=args.limit)
    print(f"{count} clip(s) written")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("Rendering...")
    count = render.render_pending(cfg, limit=args.limit)
    print(f"{count} clip(s) rendered")
    return 0


def cmd_qc(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("Running QC...")
    passed, failed = qc.check_pending(cfg)
    print(f"{passed} passed, {failed} failed")

    # Recordings whose clips are all cut and rendered no longer need to sit in
    # the inbox. Archiving here rather than at ingest time is deliberate: the
    # renderer reads the source file, so it can only move once nothing needs it.
    moved = ingest.archive_processed(cfg)
    if moved:
        print(f"{moved} recording(s) archived to {cfg.paths.archive}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    for step in (cmd_ingest, cmd_select, cmd_write, cmd_render, cmd_qc):
        code = step(args)
        if code != 0:
            return code
    return cmd_status(args)


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    with db.connect(cfg.paths.database) as conn:
        sources = conn.execute(
            "SELECT state, COUNT(*) n FROM sources GROUP BY state"
        ).fetchall()
        clips = conn.execute(
            "SELECT state, COUNT(*) n FROM clips GROUP BY state"
        ).fetchall()
        posts = conn.execute(
            "SELECT platform, state, COUNT(*) n FROM posts GROUP BY platform, state"
        ).fetchall()

        recent = conn.execute(
            """
            SELECT p.platform, p.clip_id, p.posted_at, p.url, p.metrics,
                   p.variant, c.hook
              FROM posts p LEFT JOIN clips c ON c.id = p.clip_id
             WHERE p.state = 'posted'
             ORDER BY p.posted_at DESC LIMIT ?
            """,
            (max(0, int(getattr(args, "recent", 5))),),
        ).fetchall()

    print("\nSources:")
    for row in sources or []:
        print(f"  {row['state']:<12} {row['n']}")
    if not sources:
        print("  (none)")

    print("\nClips:")
    for row in clips or []:
        print(f"  {row['state']:<12} {row['n']}")
    if not clips:
        print("  (none)")

    if posts:
        print("\nPosts:")
        for row in posts:
            print(f"  {row['platform']:<12} {row['state']:<10} {row['n']}")

    # What actually went out. Counts alone cannot answer "what did it post last
    # night", which is the question this command is opened for.
    if recent:
        print("\nDerniers envois:")
        for row in recent:
            views = ""
            try:
                measured = json.loads(row["metrics"] or "{}").get("primary")
                if measured is not None:
                    views = f"   {measured}"
            except (json.JSONDecodeError, TypeError):
                pass

            when = (row["posted_at"] or "")[:16].replace("T", " ")
            print(f"  {when}  {row['platform']:<10} clip {row['clip_id']}"
                  f"  [{row['variant'] or '-'}]{views}")
            if row["hook"]:
                print(f"      {row['hook'][:70]}")
            if row["url"]:
                print(f"      {row['url']}")


    # Recent failures, with the reason. The pipeline runs unattended, so a
    # platform that has quietly stopped working is invisible otherwise — the
    # post is retried next run and fails identically until someone reads this.
    with db.connect(cfg.paths.database) as conn:
        failures = conn.execute(
            """
            SELECT platform, clip_id, error, created_at
              FROM posts
             WHERE state = 'failed' AND error IS NOT NULL
             ORDER BY created_at DESC LIMIT 5
            """
        ).fetchall()

    if failures:
        print("")
        print("Recent failures:")
        for row in failures:
            reason = " ".join((row["error"] or "").split())
            print(f"  {row['platform']:<10} clip {row['clip_id']}")
            print(f"             {reason[:150]}")
    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    with db.connect(cfg.paths.database) as conn:
        clips = db.clips_in_state(conn, args.state)

        # Which recording each clip was cut from. Without it a fresh batch is
        # indistinguishable from the rest of the catalogue, and "what did my
        # last session produce" has no answer.
        names = {
            row["id"]: Path(row["path"]).stem
            for row in conn.execute("SELECT id, path FROM sources")
        }

    wanted = (args.source or "").strip().lower()
    if wanted:
        clips = [
            c for c in clips
            if wanted == str(c["source_id"])
            or wanted in names.get(c["source_id"], "").lower()
        ]

    if not clips:
        where = f" from source {args.source!r}" if wanted else ""
        print(f"No clips in state {args.state!r}{where}")
        return 0

    for clip in clips:
        duration = float(clip["end_s"]) - float(clip["start_s"])
        origin = names.get(clip["source_id"], f"source {clip['source_id']}")
        print(
            f"[{clip['id']:>4}] {clip['score']:>6.1f}  {duration:>5.1f}s  "
            f"{clip['trigger_event']:<16} {clip['hook'] or ''}"
        )
        print(f"       {origin}  ({clip['start_s']:.0f}s)")
        if clip["render_path"]:
            print(f"       {clip['render_path']}")
        if clip["qc"]:
            try:
                notes = json.loads(clip["qc"]).get("notes") or []
                for note in notes:
                    print(f"       ! {note}")
            except json.JSONDecodeError:
                pass
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    state = "rejected" if args.reject else "approved"
    with db.connect(cfg.paths.database) as conn:
        for clip_id in args.ids:
            db.set_state(conn, "clips", clip_id, state)
    print(f"{len(args.ids)} clip(s) {state}")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("Fetching metrics...")
    count = metrics.fetch(cfg)
    print(f"{count} post(s) updated")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    return learn.report(_cfg(args), apply=args.apply)


def cmd_auto(args: argparse.Namespace) -> int:
    """
    The whole pipeline, end to end: new recordings in, posts out.

    `run` turns recordings into finished clips; `orchestrate` decides what goes
    where. Neither is useful alone on a schedule — generating clips nobody
    posts, or planning against a queue nobody refilled — so this is the command
    a scheduled task should call.

    Safe to run when nothing has changed. Each stage no-ops on an empty queue,
    and the cadence gate means a planner run with nothing due simply posts
    nothing.
    """
    cfg = _cfg(args)

    print("=" * 60)
    print("1/3  Processing recordings")
    print("=" * 60)
    for step in (cmd_ingest, cmd_select, cmd_write, cmd_render, cmd_qc):
        code = step(args)
        if code != 0:
            # A failed stage means the queue is in an unknown state; planning
            # on top of that is how a half-rendered clip gets posted.
            print("")
            print(f"!! {step.__name__} failed — not planning this run")
            return code

    # Before planning, not after: the planner is explicitly told to ignore
    # performance below a sample threshold, and without this it was told there
    # were zero measured posts on every run forever. Nothing ever refreshed
    # them, so the one part of the system that is supposed to learn could not.
    # A failure here is not fatal — a plan made on slightly stale numbers is
    # better than no plan.
    print("")
    print("=" * 60)
    print("2/3  Refreshing performance data")
    print("=" * 60)
    try:
        cmd_metrics(args)
    except Exception as exc:
        print(f"  !! metrics failed, planning on what we have: {exc}")

    print("")
    print("=" * 60)
    print("3/3  Planning and publishing")
    print("=" * 60)
    return cmd_orchestrate(args)


def cmd_orchestrate(args: argparse.Namespace) -> int:
    cfg = _cfg(args)

    if not cfg.orchestrator.enabled:
        print("orchestrator.enabled is false in shorts.yaml")
        return 0

    print(f"Planning with {cfg.orchestrator.provider} / {cfg.orchestrator.model}...")
    only = getattr(args, 'only', None)
    if only and only not in cfg.publish.platforms:
        print(f"unknown platform: {only}")
        return 1
    result = orchestrator.plan(
        cfg, force=getattr(args, 'force', False), only=only
    )
    if result is None:
        return 1

    # A run that does not post is a run that did not happen, as far as the
    # planner's memory is concerned. Writing notes about posts that were never
    # made would have it reasoning next week from a history it invented.
    dry = args.dry_run or not (cfg.orchestrator.autopost or args.post)
    if dry and not args.dry_run:
        print("(orchestrator.autopost is false — planning only, nothing posted)")

    print(f"\n{result['situation']}\n")

    if not result["posts"]:
        print("Nothing to post this run.")
    else:
        count = orchestrator.execute(cfg, result, dry_run=dry)
        print(f"\n{count} post(s){' (dry run)' if dry else ''}")

    if dry:
        if result["weight_changes"]:
            print(f"\nWould adjust weights: {result['weight_changes']}")
        return 0

    if result["weight_changes"]:
        orchestrator.apply_weights(cfg, result["weight_changes"])

    if result["strategy_notes"]:
        path = orchestrator.save_memory(cfg, result["strategy_notes"])
        print(f"\nNotes written to {path}")

    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    manual.handoff(_cfg(args), args.platform, clip_id=args.clip)
    return 0


def cmd_reddit(args: argparse.Namespace) -> int:
    from . import reddit as reddit_mod

    made = reddit_mod.prepare(_cfg(args), count=args.count)
    if made:
        print("")
        print(f"  {made} clip(s) in {_cfg(args).reddit.output}")
    return 0 if made else 1


def cmd_tiktok_draft(args: argparse.Namespace) -> int:
    return 0 if manual.draft_to_tiktok(_cfg(args), clip_id=args.clip) else 1


def cmd_record(args: argparse.Namespace) -> int:
    ok = manual.record(
        _cfg(args), args.clip, args.platform,
        url=args.url, metric=args.metric,
    )
    return 0 if ok else 1


def cmd_tiktok_auth(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    print("TikTok authorisation")
    if not tiktok_auth.authorise(cfg):
        return 1
    print("")
    print("Checking what the audit allows...")
    _report_tiktok_status(cfg)
    return 0


def _report_tiktok_status(cfg: Config) -> None:
    """
    Reads back what the account may actually post.

    privacy_level_options is the honest signal for audit state: an unaudited
    app only ever gets SELF_ONLY, whatever the portal says.
    """
    from .publishers.tiktok import TikTokPublisher

    try:
        publisher = TikTokPublisher()
        info = publisher._creator_info(publisher._token(cfg))
    except Exception as exc:
        print(f"  !! could not read creator info: {str(exc)[:300]}")
        return

    import os

    options = info.get("privacy_level_options") or []
    sandbox = os.environ.get("TIKTOK_CLIENT_KEY", "").startswith("sbaw")

    print(f"  creator        : {info.get('creator_nickname') or '(unknown)'}")
    print(f"  environment    : {'SANDBOX' if sandbox else 'production'}")
    print(f"  privacy options: {', '.join(options) or '(none returned)'}")

    if sandbox:
        # A sandbox reports the full option set regardless of review state, so
        # reading it as an audit result is wrong — it says the plumbing works,
        # nothing about whether production may post publicly.
        print("  -> sandbox always reports full options; this says the")
        print("     integration works, NOT that the audit has passed.")
    elif options and options != ["SELF_ONLY"]:
        print("  -> audit PASSED: public posting is available")
    else:
        print("  -> audit NOT passed: every post is forced to SELF_ONLY")


def cmd_publish(args: argparse.Namespace) -> int:
    cfg = _cfg(args)

    if args.check:
        # Live calls, not a credential inspection. Reporting "ok" for a
        # platform that has been switched off upstream is worse than useless.
        return 0 if health.report(cfg) else 1

    print("Publishing..." + (" (dry run)" if args.dry_run else ""))
    count = publish.publish_ready(cfg, dry_run=args.dry_run)
    print(f"{count} post(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shorts", description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="path to shorts.yaml")

    sub = parser.add_subparsers(dest="command", required=True)

    ini = sub.add_parser("init", help="write or upgrade shorts.yaml")
    ini.add_argument("--reset", action="store_true",
                     help="discard existing values and restore defaults")
    ini.set_defaults(func=cmd_init)
    sub.add_parser("ingest", help="register new recordings").set_defaults(func=cmd_ingest)
    sub.add_parser("select", help="pick highlight clips").set_defaults(func=cmd_select)
    st = sub.add_parser("status", help="queue state and what went out recently")
    st.add_argument("--recent", type=int, default=5,
                    help="how many recent posts to list (default 5)")
    st.set_defaults(func=cmd_status)
    sub.add_parser("qc", help="run automated checks").set_defaults(func=cmd_qc)

    write = sub.add_parser("write", help="generate hooks and captions")
    write.add_argument("--limit", type=int, default=None)
    write.set_defaults(func=cmd_write)

    rend = sub.add_parser("render", help="cut, reframe and caption")
    rend.add_argument("--limit", type=int, default=None)
    rend.set_defaults(func=cmd_render)

    run = sub.add_parser("run", help="ingest through qc")
    run.add_argument("--limit", type=int, default=None)
    run.set_defaults(func=cmd_run)

    listing = sub.add_parser("list", help="list clips in a state")
    listing.add_argument("state", nargs="?", default="ready")
    listing.add_argument("--source", default=None,
                         help="only clips from this recording (id, or part of "
                              "the filename, e.g. --source 09-02)")
    listing.set_defaults(func=cmd_list)

    ra = sub.add_parser(
        "realign", help="recompute telemetry alignment for ingested recordings")
    ra.set_defaults(func=cmd_realign)

    rg = sub.add_parser("regen", help="rewrite copy and re-render everything")
    rg.add_argument("--full", action="store_true",
                    help="also re-ingest and re-select from the recordings")
    rg.add_argument("--reselect", action="store_true",
                    help="re-select clips from the sources already ingested, "
                         "leaving the archived recordings alone")
    rg.add_argument("--dry-run", action="store_true", help="show the plan only")
    rg.add_argument("--yes", "-y", action="store_true",
                    help="skip the confirmation prompt")
    rg.add_argument("--limit", type=int, default=None)
    rg.set_defaults(func=cmd_regen)

    pr = sub.add_parser("prompts", help="show or reset the editable prompt file")
    pr.add_argument("--reset", action="store_true", help="restore shipped defaults")
    pr.set_defaults(func=cmd_prompts)

    sub.add_parser("metrics", help="refresh performance data").set_defaults(func=cmd_metrics)

    lrn = sub.add_parser("learn", help="what is working, and why")
    lrn.add_argument("--apply", action="store_true",
                     help="fold clip-type performance into selection weights")
    lrn.set_defaults(func=cmd_learn)

    auto = sub.add_parser(
        "auto", help="everything: process recordings, then plan and post")
    auto.add_argument("--post", action="store_true",
                      help="publish even if autopost is off")
    auto.add_argument("--dry-run", action="store_true",
                      help="plan only; change nothing")
    auto.add_argument("--limit", type=int, default=None)
    auto.set_defaults(func=cmd_auto)

    orc = sub.add_parser("orchestrate", help="let the planner decide and post")
    orc.add_argument("--post", action="store_true",
                     help="publish the plan even if autopost is off")
    orc.add_argument("--force", action="store_true",
                     help="ignore the cadence and promo gates for this run, to "
                          "catch up a slot that was missed")
    orc.add_argument("--only", metavar="PLATFORM",
                     help="restrict this run to one platform, so forcing a "
                          "catch-up does not spend another one's slot")
    orc.add_argument("--dry-run", action="store_true",
                     help="plan only; change nothing, not even the notes")
    orc.set_defaults(func=cmd_orchestrate)

    red = sub.add_parser("reddit",
                         help="cut the best few clips wide, long and untitled")
    red.add_argument("--count", type=int,
                     help="how many to keep (default reddit.count)")
    red.set_defaults(func=cmd_reddit)

    tdr = sub.add_parser("tiktok-draft",
                         help="upload the next clip to TikTok drafts")
    tdr.add_argument("--clip", type=int, help="a specific clip instead of the next")
    tdr.set_defaults(func=cmd_tiktok_draft)

    hand = sub.add_parser("handoff", help="what to post by hand, and where")
    hand.add_argument("platform")
    hand.add_argument("--clip", type=int, help="a specific clip instead of the next due")
    hand.set_defaults(func=cmd_handoff)

    rec = sub.add_parser("record", help="mark a hand-made post as posted")
    rec.add_argument("clip", type=int)
    rec.add_argument("platform")
    rec.add_argument("--url", default="", help="permalink, for your own reference")
    rec.add_argument("--metric", type=int,
                     help="performance number, for the learning loop")
    rec.set_defaults(func=cmd_record)

    tta = sub.add_parser("tiktok-auth", help="authorise TikTok and read audit status")
    tta.set_defaults(func=cmd_tiktok_auth)

    pub = sub.add_parser("publish", help="post approved clips")
    pub.add_argument("--dry-run", action="store_true", help="show what would post")
    pub.add_argument("--check", action="store_true", help="report platform readiness")
    pub.set_defaults(func=cmd_publish)

    approve = sub.add_parser("approve", help="approve clips for publishing")
    approve.add_argument("ids", nargs="+", type=int)
    approve.add_argument("--reject", action="store_true")
    approve.set_defaults(func=cmd_approve)

    return parser


class _Tee:
    """Writes to the terminal and to the log file at once."""

    def __init__(self, stream, handle):
        self._stream, self._handle = stream, handle

    def write(self, text):
        self._stream.write(text)
        try:
            self._handle.write(text)
        except Exception:
            pass  # a full disk must not take the pipeline down
        return len(text)

    def flush(self):
        self._stream.flush()
        try:
            self._handle.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _start_log(cfg: Config):
    """
    Mirrors output to marketor.log, trimmed to the last ~2000 lines.

    Scheduled runs write to a terminal nobody is watching. When the 17:00 run
    posts nothing, the reason existed only in a window that closed — this is
    what makes last night answerable in the morning.
    """
    path = cfg.paths.database.parent / "marketor.log"
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]
            path.write_text(chr(10).join(tail) + chr(10), encoding="utf-8")
        handle = path.open("a", encoding="utf-8")
    except Exception:
        return None

    import datetime as _dt

    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    handle.write(chr(10) + "===== " + stamp + " " + " ".join(sys.argv[1:]) + " =====" + chr(10))
    sys.stdout = _Tee(sys.stdout, handle)
    sys.stderr = _Tee(sys.stderr, handle)
    return handle


def main(argv: list[str] | None = None) -> int:
    # Almost everything printed here is model output — captions, corrections,
    # titles — and it routinely contains em dashes and curly quotes. Redirected
    # to a file on Windows, stdout defaults to cp1252 and a single unencodable
    # character raises UnicodeEncodeError, which killed an entire `shorts write`
    # run from inside a progress message.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # Credentials live in .env; the publishers read them from the environment.
    env.ensure_loaded()

    args = build_parser().parse_args(argv)

    # Long-running commands are the ones that run unattended; the rest are
    # interactive and would only clutter the log.
    handle = None
    if getattr(args, "func", None) in (cmd_auto, cmd_orchestrate, cmd_run, cmd_publish):
        handle = _start_log(_cfg(args))

    # One pipeline pass at a time. Read-only and single-step commands are free
    # to run alongside one; only the full passes are serialised.
    lock = None
    if getattr(args, "func", None) in (cmd_auto, cmd_run, cmd_regen):
        lock = _acquire_pipeline_lock(_cfg(args))
        if lock is None:
            print("Un autre passage du pipeline est deja en cours; rien a faire.")
            return 0

    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception:
        import traceback

        traceback.print_exc()   # goes to the log too, via the tee
        return 1
    finally:
        _release_pipeline_lock(lock)
        if handle is not None:
            sys.stdout = getattr(sys.stdout, "_stream", sys.stdout)
            sys.stderr = getattr(sys.stderr, "_stream", sys.stderr)
            handle.close()


if __name__ == "__main__":
    sys.exit(main())
