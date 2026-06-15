"""ログ走査 → 正規化 → コスト付与 → DB UPSERT。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from tokentracker import db
from tokentracker.models import UsageEvent
from tokentracker.parsers.base import Parser
from tokentracker.parsers.claude_code import ClaudeCodeParser
from tokentracker.parsers.cline import ClineParser
from tokentracker.parsers.codex import CodexParser
from tokentracker.pricing import PriceBook, default_pricebook


def _ingest(
    conn: sqlite3.Connection,
    parser: Parser,
    root: Path | None,
    pricebook: PriceBook | None,
) -> int:
    book = pricebook or default_pricebook()
    events: list[UsageEvent] = list(parser.iter_events(root))
    for ev in events:
        ev.cost_usd = book.compute_cost(ev)
    return db.upsert_events(conn, events)


def ingest_claude_code(conn, root: Path | None = None, *, pricebook: PriceBook | None = None) -> int:
    """Claude Code ログを取り込む。戻り値は取り込んだイベント件数。"""
    return _ingest(conn, ClaudeCodeParser(), root, pricebook)


def ingest_codex(conn, root: Path | None = None, *, pricebook: PriceBook | None = None) -> int:
    """Codex CLI の rollout ログを取り込む。"""
    return _ingest(conn, CodexParser(), root, pricebook)


def ingest_cline(conn, root: Path | None = None, *, pricebook: PriceBook | None = None) -> int:
    """Cline のタスクログを取り込む。"""
    return _ingest(conn, ClineParser(), root, pricebook)


# CLI の --source で選べる取り込み関数。各 parser はルート不在なら空を返すので安全。
INGESTORS = {
    "claude_code": ingest_claude_code,
    "codex": ingest_codex,
    "cline": ingest_cline,
}


def ingest_all(
    conn: sqlite3.Connection,
    *,
    sources: Iterable[str] | None = None,
    pricebook: PriceBook | None = None,
) -> dict[str, int]:
    """指定ソース（既定は全て）を順に取り込み、ソース別件数を返す。"""
    selected = list(sources) if sources else list(INGESTORS)
    return {s: INGESTORS[s](conn, pricebook=pricebook) for s in selected}
