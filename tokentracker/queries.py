"""集計クエリ。

未割当コスト（単価が無いモデル）を ``known_cost_usd`` に紛れ込ませず別計上する。
日次バケットは UTC 保存値を設定 TZ（既定 Asia/Tokyo）のローカル日付で切る。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Tokyo"

# 集計軸 → usage_event のカラム。"daily" は timestamp_utc をローカル日付に変換して使う。
_DIMENSION_COLUMN = {
    "repo": "repo_path",
    "model": "model",
    "session": "session_id",
    "agent": "source",
}


def local_date_bucket(timestamp_utc: str, tz: str = DEFAULT_TZ) -> str:
    """UTC の ISO8601 文字列を、指定 TZ のローカル日付 ``YYYY-MM-DD`` に変換する。"""
    iso = timestamp_utc.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def _row_key(row: sqlite3.Row, dimension: str, tz: str) -> str:
    if dimension == "daily":
        ts = row["timestamp_utc"]
        return local_date_bucket(ts, tz) if ts else "(unknown)"
    column = _DIMENSION_COLUMN.get(dimension)
    if column is None:
        raise ValueError(f"unknown dimension: {dimension}")
    return row[column] if row[column] is not None else "(none)"


def summary(
    conn: sqlite3.Connection,
    dimension: str,
    *,
    since: str | None = None,
    until: str | None = None,
    include_subagents: bool = True,
    tz: str = DEFAULT_TZ,
) -> list[dict]:
    """指定軸で集計した行のリストを返す。

    各行: key / input_tokens / output_tokens / cache_creation_tokens / cache_read_tokens /
    known_cost_usd(単価判明分の合計) / unallocated_tokens(単価不明分のトークン) /
    unallocated_events(単価不明件数) / events。
    """
    rows = conn.execute("SELECT * FROM usage_event").fetchall()
    buckets: dict[str, dict] = {}
    for row in rows:
        if not include_subagents and row["is_subagent"]:
            continue
        key = _row_key(row, dimension, tz)
        if since is not None or until is not None:
            day = local_date_bucket(row["timestamp_utc"], tz) if row["timestamp_utc"] else None
            if since is not None and (day is None or day < since):
                continue
            if until is not None and (day is None or day > until):
                continue
        b = buckets.setdefault(
            key,
            {
                "key": key, "input_tokens": 0, "output_tokens": 0,
                "cache_creation_tokens": 0, "cache_read_tokens": 0,
                "known_cost_usd": 0.0, "unallocated_tokens": 0,
                "unallocated_events": 0, "events": 0,
            },
        )
        b["events"] += 1
        b["input_tokens"] += row["input_tokens"]
        # reasoning は表示上 output に畳む（コストは output 単価で別計上）。
        b["output_tokens"] += row["output_tokens"] + row["reasoning_output_tokens"]
        b["cache_creation_tokens"] += row["cache_creation_tokens"]
        b["cache_read_tokens"] += row["cache_read_tokens"]
        tokens = (
            row["input_tokens"] + row["output_tokens"] + row["reasoning_output_tokens"]
            + row["cache_creation_tokens"] + row["cache_read_tokens"]
        )
        if row["cost_usd"] is None:
            # 単価不明 = 未割当コスト。known に混ぜず別計上する。
            b["unallocated_tokens"] += tokens
            b["unallocated_events"] += 1
        else:
            b["known_cost_usd"] += row["cost_usd"]
    return sorted(buckets.values(), key=lambda r: r["key"])
