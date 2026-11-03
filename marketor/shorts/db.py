"""
Job state for the pipeline.

SQLite is deliberate: the whole pipeline is one machine, one operator, and a few
thousand rows a year. The point of persisting state is that an expensive render
survives a failed upload, and that a clip is never posted twice.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    path            TEXT NOT NULL UNIQUE,
    duration        REAL NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    -- Wall clock at the first video frame. Everything in the pipeline maps
    -- telemetry to video time through this.
    start_utc       TEXT,
    telemetry_path  TEXT,
    -- Seconds to add to a telemetry timestamp to get a video timestamp.
    offset_seconds  REAL,
    alignment       TEXT,
    state           TEXT NOT NULL DEFAULT 'new',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    start_s         REAL NOT NULL,
    end_s           REAL NOT NULL,
    score           REAL NOT NULL DEFAULT 0,
    trigger_event   TEXT,
    context         TEXT,
    hook            TEXT,
    caption         TEXT,
    hashtags        TEXT,
    render_path     TEXT,
    qc              TEXT,
    state           TEXT NOT NULL DEFAULT 'selected',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id         INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    platform        TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'queued',
    remote_id       TEXT,
    url             TEXT,
    error           TEXT,
    metrics         TEXT,
    posted_at       TEXT,
    created_at      TEXT NOT NULL,
    -- One post per clip per platform, enforced by the database rather than by
    -- remembering to check.
    UNIQUE (clip_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_clips_state ON clips(state);
CREATE INDEX IF NOT EXISTS idx_posts_state ON posts(state);
"""

# Columns added after the first schema shipped. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so they are applied by inspection rather than by version number.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "sources": [
        # Menu stretches measured against the picture rather than taken from the
        # event log, as JSON [[start, end], ...] in video seconds. The log
        # brackets a menu more generously than the freeze it describes, so the
        # letterbox was landing over live gameplay at both ends.
        ("menu_windows", "TEXT"),
    ],
    "posts": [
        # A post made outside its promotional slot to make up for one that was
        # missed. It belongs to the day that was lost, not the day it was sent,
        # so it deliberately does not count against today's quota — otherwise
        # catching up yesterday silently cancels today.
        ("catch_up", "INTEGER DEFAULT 0"),
        # Which experiment arm this post used, e.g. "hashtags:niche|format:single".
        ("variant", "TEXT"),
        ("metrics_at", "TEXT"),
        # What this clip *was*, snapshotted at publish time: mode, difficulty,
        # trigger, speed, power-ups taken. Copied rather than joined because
        # the clip it came from is not durable — re-selection renumbers every
        # clip and recomputes every context, so a question asked in six weeks
        # about posts made today has nothing left to join against. A post is
        # the only row that is never rewritten.
        ("dimensions", "TEXT"),
        # What this post actually said, snapshotted at publish time. The clip's
        # copy is not durable: every clip goes to all three platforms in turn,
        # and each turn rewrites hook, caption and title to that run's brief.
        # The text a post went out with was therefore erased by the next
        # platform's rewrite, which makes "did this wording work" a question
        # the database could not answer about its own posts.
        ("copy", "TEXT"),
    ],
    "clips": [
        # Timed on-screen caption beats, as JSON [{text, at}].
        ("beats", "TEXT"),
        # Identifies the footage itself: recording name plus rounded timecodes.
        # The (clip, platform) unique index stops a clip being posted twice, but
        # it lives in this database — and `regen --full` deletes it, after which
        # the same seconds of gameplay can be republished under a new clip id.
        # This survives that, because it is derived from the source.
        ("fingerprint", "TEXT"),
    ],
}


def fingerprint(source_path: str, start_s: float, end_s: float) -> str:
    """
    Stable identity for a stretch of footage.

    Rounded to the second on purpose: re-selection after a config change can
    shift a boundary by a few frames, and that should still count as the same
    moment rather than a fresh one.
    """
    from pathlib import Path

    return f"{Path(source_path).name}@{start_s:.0f}-{end_s:.0f}"


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # The hourly scheduled run and a manual command can land on this file at
    # the same time. Five seconds of default patience is not enough when the
    # other side is halfway through writing copy for sixty clips, and the loser
    # dies with "database is locked" partway through its own run.
    conn = sqlite3.connect(path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000")
    try:
        # Readers stop blocking the writer, which is most of the contention:
        # the planner reads the whole queue while the renderer writes to it.
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_source(
    conn: sqlite3.Connection,
    *,
    path: str,
    duration: float,
    width: int,
    height: int,
    start_utc: str | None,
    telemetry_path: str | None,
    offset_seconds: float | None,
    alignment: str,
    menu_windows: str | None = None,
) -> int | None:
    """Registers a recording. Returns None if it was already known."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO sources
            (path, duration, width, height, start_utc, telemetry_path,
             offset_seconds, alignment, menu_windows, state, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """,
        (path, duration, width, height, start_utc, telemetry_path,
         offset_seconds, alignment, menu_windows, now()),
    )
    return cur.lastrowid if cur.rowcount else None


def add_clip(
    conn: sqlite3.Connection,
    *,
    source_id: int,
    start_s: float,
    end_s: float,
    score: float,
    trigger_event: str,
    context: dict[str, Any],
) -> int:
    source = conn.execute(
        "SELECT path FROM sources WHERE id = ?", (source_id,)
    ).fetchone()
    mark = fingerprint(source["path"] if source else str(source_id), start_s, end_s)

    cur = conn.execute(
        """
        INSERT INTO clips
            (source_id, start_s, end_s, score, trigger_event, context,
             state, created_at, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, 'selected', ?, ?)
        """,
        (source_id, start_s, end_s, score, trigger_event,
         json.dumps(context), now(), mark),
    )
    return cur.lastrowid


def set_state(conn: sqlite3.Connection, table: str, row_id: int, state: str) -> None:
    if table not in {"sources", "clips", "posts"}:
        raise ValueError(f"unknown table {table!r}")
    conn.execute(f"UPDATE {table} SET state = ? WHERE id = ?", (state, row_id))


def clips_in_state(conn: sqlite3.Connection, state: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clips WHERE state = ? ORDER BY score DESC", (state,)
    ).fetchall()


def source_for_clip(conn: sqlite3.Connection, clip_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT s.* FROM sources s JOIN clips c ON c.source_id = s.id WHERE c.id = ?",
        (clip_id,),
    ).fetchone()
