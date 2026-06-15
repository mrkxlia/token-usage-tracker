"""SQLite スキーマと UPSERT。

冪等キーは ``UNIQUE(source, message_id)``。全再読して何度 ingest しても重複行が入らない。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tokentracker.models import UsageEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_event (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    source                   TEXT NOT NULL,
    message_id               TEXT NOT NULL,
    session_id               TEXT,
    agent_id                 TEXT,
    request_id               TEXT,
    timestamp_utc            TEXT,
    repo_path                TEXT,
    git_branch               TEXT,
    model                    TEXT,
    input_tokens             INTEGER NOT NULL DEFAULT 0,
    output_tokens            INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens    INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens        INTEGER NOT NULL DEFAULT 0,
    web_search_requests      INTEGER NOT NULL DEFAULT 0,
    web_fetch_requests       INTEGER NOT NULL DEFAULT 0,
    cost_usd                 REAL,
    is_subagent              INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, message_id)
);

CREATE INDEX IF NOT EXISTS idx_usage_repo  ON usage_event(repo_path);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_event(model);
CREATE INDEX IF NOT EXISTS idx_usage_ts    ON usage_event(timestamp_utc);

CREATE TABLE IF NOT EXISTS prompt (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    session_id    TEXT,
    role          TEXT,
    text          TEXT,
    timestamp_utc TEXT
);

CREATE TABLE IF NOT EXISTS ingest_state (
    file_path       TEXT PRIMARY KEY,
    size            INTEGER,
    mtime           REAL,
    last_message_id TEXT
);
"""

# usage_event の挿入カラム（id を除く）。UsageEvent と 1:1 で対応。
_COLUMNS = [
    "source", "message_id", "session_id", "agent_id", "request_id", "timestamp_utc",
    "repo_path", "git_branch", "model", "input_tokens", "output_tokens",
    "reasoning_output_tokens",
    "cache_creation_tokens", "cache_creation_1h_tokens", "cache_creation_5m_tokens",
    "cache_read_tokens", "web_search_requests", "web_fetch_requests", "cost_usd",
    "is_subagent",
]


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _event_row(ev: UsageEvent) -> tuple:
    return (
        ev.source, ev.message_id, ev.session_id, ev.agent_id, ev.request_id,
        ev.timestamp_utc, ev.repo_path, ev.git_branch, ev.model,
        ev.input_tokens, ev.output_tokens, ev.reasoning_output_tokens,
        ev.cache_creation_tokens,
        ev.cache_creation_1h_tokens, ev.cache_creation_5m_tokens, ev.cache_read_tokens,
        ev.web_search_requests, ev.web_fetch_requests, ev.cost_usd,
        1 if ev.is_subagent else 0,
    )


def upsert_events(conn: sqlite3.Connection, events: list[UsageEvent]) -> int:
    """イベントを UPSERT する。戻り値は対象イベント件数。"""
    placeholders = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS if c not in ("source", "message_id"))
    sql = (
        f"INSERT INTO usage_event ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(source, message_id) DO UPDATE SET {updates}"
    )
    rows = [_event_row(e) for e in events]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def count_events(conn: sqlite3.Connection) -> int:
    """``usage_event`` の行数。取り込み前後の差分で「新規 N 件」を出すのに使う。"""
    return conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0]


def load_ingest_state(conn: sqlite3.Connection) -> dict[str, tuple[int, float]]:
    """取り込み済みファイルの状態を ``{file_path: (size, mtime)}`` で返す。

    増分取り込みで「未変更ファイル（size と mtime が一致）」をスキップ判定するのに使う。
    """
    rows = conn.execute("SELECT file_path, size, mtime FROM ingest_state").fetchall()
    return {r["file_path"]: (r["size"], r["mtime"]) for r in rows}


def update_ingest_state(
    conn: sqlite3.Connection,
    file_path: str,
    size: int,
    mtime: float,
    last_message_id: str | None = None,
) -> None:
    """ファイルの取り込み状態を UPSERT する（次回以降のスキップ判定に使う）。"""
    conn.execute(
        "INSERT INTO ingest_state (file_path, size, mtime, last_message_id) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(file_path) DO UPDATE SET "
        "size=excluded.size, mtime=excluded.mtime, last_message_id=excluded.last_message_id",
        (file_path, size, mtime, last_message_id),
    )
    conn.commit()
