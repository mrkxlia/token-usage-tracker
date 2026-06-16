"""集計クエリ。

未割当コスト（単価が無いモデル）を ``known_cost_usd`` に紛れ込ませず別計上する。
日次バケットは UTC 保存値を設定 TZ（既定 Asia/Tokyo）のローカル日付で切る。
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import TypedDict
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Tokyo"

# 集計軸 → usage_event のカラム。"daily" は timestamp_utc をローカル日付に変換して使う。
_DIMENSION_COLUMN = {
    "repo": "repo_path",
    "model": "model",
    "session": "session_id",
    "agent": "source",
}

# 集計キーのプレースホルダ。timestamp 欠落の日次バケットと、軸カラムが NULL の行を区別する。
UNKNOWN_KEY = "(unknown)"  # daily で timestamp_utc が無い
NONE_KEY = "(none)"        # 軸カラム（repo_path 等）が NULL


class SummaryRow(TypedDict):
    """``summary()`` が返す 1 行。実体は dict なので JSON 化や ``row["key"]`` 参照は従来どおり。"""

    key: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    known_cost_usd: float
    unallocated_tokens: int
    unallocated_events: int
    events: int


def local_date_bucket(timestamp_utc: str, tz: str = DEFAULT_TZ) -> str:
    """UTC の ISO8601 文字列を、指定 TZ のローカル日付 ``YYYY-MM-DD`` に変換する。"""
    iso = timestamp_utc.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def _pad_date(value: str, days: int) -> str | None:
    """``YYYY-MM-DD`` を days 日ずらして返す。パース不能なら None（プリフィルタ無効化）。"""
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return (d + timedelta(days=days)).isoformat()


def _row_key(row: sqlite3.Row, dimension: str, tz: str) -> str:
    if dimension == "daily":
        ts = row["timestamp_utc"]
        return local_date_bucket(ts, tz) if ts else UNKNOWN_KEY
    column = _DIMENSION_COLUMN.get(dimension)
    if column is None:
        raise ValueError(f"unknown dimension: {dimension}")
    return row[column] if row[column] is not None else NONE_KEY


def summary(
    conn: sqlite3.Connection,
    dimension: str,
    *,
    since: str | None = None,
    until: str | None = None,
    include_subagents: bool = True,
    tz: str = DEFAULT_TZ,
) -> list[SummaryRow]:
    """指定軸で集計した行のリストを返す。

    各行: key / input_tokens / output_tokens / cache_creation_tokens / cache_read_tokens /
    known_cost_usd(単価判明分の合計) / unallocated_tokens(単価不明分のトークン) /
    unallocated_events(単価不明件数) / events。
    """
    # --- SQL 側プリフィルタ（行数を減らす）-----------------------------------
    # サブエージェント除外は厳密。期間は UTC 文字列の日付部分で ±1 日 pad した粗い絞り込みに留め、
    # 厳密な境界はループ内の local_date_bucket で再判定する（TZ オフセット差を吸収）。
    where: list[str] = []
    params: list = []
    if not include_subagents:
        where.append("is_subagent = 0")
    date_filtered = since is not None or until is not None
    if date_filtered:
        where.append("timestamp_utc IS NOT NULL AND timestamp_utc != ''")
        lo = _pad_date(since, -1) if since is not None else None
        if lo is not None:
            where.append("substr(timestamp_utc, 1, 10) >= ?")
            params.append(lo)
        hi = _pad_date(until, 1) if until is not None else None
        if hi is not None:
            where.append("substr(timestamp_utc, 1, 10) <= ?")
            params.append(hi)
    sql = "SELECT * FROM usage_event"
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = conn.execute(sql, params).fetchall()

    # daily か期間フィルタ時のみローカル日付の算出が必要（不要時は per-row パースを避ける）。
    need_day = dimension == "daily" or date_filtered
    buckets: dict[str, SummaryRow] = {}
    for row in rows:
        if not include_subagents and row["is_subagent"]:
            continue
        day = None
        if need_day:
            ts = row["timestamp_utc"]
            day = local_date_bucket(ts, tz) if ts else None
            if since is not None and (day is None or day < since):
                continue
            if until is not None and (day is None or day > until):
                continue
        if dimension == "daily":
            key = day if day is not None else UNKNOWN_KEY
        else:
            key = _row_key(row, dimension, tz)
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
