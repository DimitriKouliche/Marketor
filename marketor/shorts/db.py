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
    "posts": [
        # Which experiment arm this post used, e.g. "hashtags:niche|format:single".
        ("variant", "TEXT"),
        ("metrics_at", "TEXT"),
    ],
    "clips": [
        # Timed on-screen caption beats, as JSON [{text, at}].
        ("beats", "TEXT"),
    ],
}


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
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
) -> int | None:
    """Registers a recording. Returns None if it was already known."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO sources
            (path, duration, width, height, start_utc, telemetry_path,
             offset_seconds, alignment, state, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """,
        (path, duration, width, height, start_utc, telemetry_path,
         offset_seconds, alignment, now()),
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
    cur = conn.execute(
        """
        INSERT INTO clips
            (source_id, start_s, end_s, score, trigger_event, context,
             state, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'selected', ?)
        """,
        (source_id, start_s, end_s, score, trigger_event,
         json.dumps(context), now()),
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
