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
import sys
from pathlib import Path

from . import copywriter, db, env, ingest, learn, manual, metrics, orchestrator
from . import tiktok_auth
from . import prompts, publish, qc
from . import config as config_module
from . import render, select
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

    full = args.full or existing == 0
    if existing == 0 and not args.full:
        print("No clips yet — running the full pipeline.")

    if args.dry_run:
        print("Would:")
        if full:
            print(f"  delete {cfg.paths.database}")
        else:
            print(f"  reset copy and render state on {existing} clip(s)")
        print(f"  clear rendered files from {cfg.paths.output}")
        print("  then: " + " -> ".join(
            (["ingest", "select"] if full else []) + ["write", "render", "qc"]
        ))
        return 0

    if full:
        if cfg.paths.database.exists():
            cfg.paths.database.unlink()
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

    _clear_output(cfg)

    steps = [cmd_ingest, cmd_select] if full else []
    steps += [cmd_write, cmd_render, cmd_qc]

    for step in steps:
        code = step(args)
        if code != 0:
            return code

    return 0


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

    print()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    with db.connect(cfg.paths.database) as conn:
        clips = db.clips_in_state(conn, args.state)

    if not clips:
        print(f"No clips in state {args.state!r}")
        return 0

    for clip in clips:
        duration = float(clip["end_s"]) - float(clip["start_s"])
        print(
            f"[{clip['id']:>4}] {clip['score']:>6.1f}  {duration:>5.1f}s  "
            f"{clip['trigger_event']:<16} {clip['hook'] or ''}"
        )
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
    print("1/2  Processing recordings")
    print("=" * 60)
    for step in (cmd_ingest, cmd_select, cmd_write, cmd_render, cmd_qc):
        code = step(args)
        if code != 0:
            # A failed stage means the queue is in an unknown state; planning
            # on top of that is how a half-rendered clip gets posted.
            print("")
            print(f"!! {step.__name__} failed — not planning this run")
            return code

    print("")
    print("=" * 60)
    print("2/2  Planning and publishing")
    print("=" * 60)
    return cmd_orchestrate(args)


def cmd_orchestrate(args: argparse.Namespace) -> int:
    cfg = _cfg(args)

    if not cfg.orchestrator.enabled:
        print("orchestrator.enabled is false in shorts.yaml")
        return 0

    print(f"Planning with {cfg.orchestrator.provider} / {cfg.orchestrator.model}...")
    result = orchestrator.plan(cfg)
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
        print("Platform readiness:")
        for platform, (ready, reason) in publish.check(cfg).items():
            if not ready and platform in cfg.orchestrator.manual_platforms:
                # Not a problem to fix — this one is posted by hand on purpose.
                print(f"  -> {platform:<11} manual: shorts handoff {platform}")
                continue
            mark = "ok" if ready else "--"
            print(f"  {mark} {platform:<11} {reason}")
        return 0

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
    sub.add_parser("status", help="show queue state").set_defaults(func=cmd_status)
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
    listing.set_defaults(func=cmd_list)

    rg = sub.add_parser("regen", help="rewrite copy and re-render everything")
    rg.add_argument("--full", action="store_true",
                    help="also re-ingest and re-select from the recordings")
    rg.add_argument("--dry-run", action="store_true", help="show the plan only")
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
    orc.add_argument("--dry-run", action="store_true",
                     help="plan only; change nothing, not even the notes")
    orc.set_defaults(func=cmd_orchestrate)

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
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
