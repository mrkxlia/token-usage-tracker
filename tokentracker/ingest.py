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
    *,
    incremental: bool = True,
) -> int:
    """パーサ 1 つ分を取り込む。戻り値は今回 UPSERT したイベント件数。

    ``incremental=True`` のときは ``ingest_state`` を参照し、size と mtime が前回と一致する
    未変更ファイルを開かずにスキップする（真の差分取り込み）。単価表を更新した等で全件を
    再計算したい場合は ``incremental=False`` を指定する。
    """
    book = pricebook or default_pricebook()
    state = db.load_ingest_state(conn) if incremental else None
    raw: list[UsageEvent] = []
    pending_state: list[tuple[str, int, float]] = []
    for path, file_events in parser.iter_file_events(root):
        try:
            st = path.stat()
        except OSError:
            continue
        if state is not None and state.get(str(path)) == (st.st_size, st.st_mtime):
            continue  # 未変更ファイルはスキップ
        raw.extend(file_events)
        if state is not None:
            pending_state.append((str(path), st.st_size, st.st_mtime))
    # dedup は同一 message_id 単位。Claude Code の重複は同一ファイル内に閉じる（実測）ため、
    # 変更ファイルだけを対象にしても畳み込みは成立する。万一クロスファイルで衝突しても
    # UNIQUE(source, message_id) の UPSERT が冪等に解決する。
    events = parser._dedup(raw)
    for ev in events:
        ev.cost_usd = book.compute_cost(ev)
    n = db.upsert_events(conn, events)
    # 重要: ingest_state はイベント確定後にのみ前進させる。upsert 前にコミットすると、
    # 失敗/中断時に「state だけ進む → 次回スキップ → 取り込み漏れ」が起きる。イベントを先に
    # 確定させておけば、最悪 state 更新前に中断しても次回そのファイルを再取り込み（冪等）するだけ。
    for fp, size, mtime in pending_state:
        db.update_ingest_state(conn, fp, size, mtime)
    return n


def ingest_claude_code(
    conn, root: Path | None = None, *, pricebook: PriceBook | None = None, incremental: bool = True
) -> int:
    """Claude Code ログを取り込む。戻り値は取り込んだイベント件数。"""
    return _ingest(conn, ClaudeCodeParser(), root, pricebook, incremental=incremental)


def ingest_codex(
    conn, root: Path | None = None, *, pricebook: PriceBook | None = None, incremental: bool = True
) -> int:
    """Codex CLI の rollout ログを取り込む。"""
    return _ingest(conn, CodexParser(), root, pricebook, incremental=incremental)


def ingest_cline(
    conn, root: Path | None = None, *, pricebook: PriceBook | None = None, incremental: bool = True
) -> int:
    """Cline のタスクログを取り込む。"""
    return _ingest(conn, ClineParser(), root, pricebook, incremental=incremental)


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
    incremental: bool = True,
) -> dict[str, int]:
    """指定ソース（既定は全て）を順に取り込み、ソース別件数を返す。

    ``incremental=False`` で全ファイルを再走査（単価変更の反映等）。
    """
    selected = list(sources) if sources else list(INGESTORS)
    return {s: INGESTORS[s](conn, pricebook=pricebook, incremental=incremental) for s in selected}
