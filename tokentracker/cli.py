"""tokentracker の CLI。

使い方:
  tokentracker ingest                # ~/.claude のログを取り込む
  tokentracker repo                  # リポジトリ別の集計表
  tokentracker model                 # モデル別
  tokentracker agent                 # ツール(エージェント)別
  tokentracker session               # セッション別
  tokentracker daily                 # 日次(Asia/Tokyo バケット)
共通オプション: --db / --since / --until / --tz / --json /
  --include-subagents / --exclude-subagents
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from tokentracker import db, queries
from tokentracker.config import DEFAULT_DB
from tokentracker.ingest import INGESTORS, ingest_all
from tokentracker.pricing import (
    BUNDLED_PRICING,
    active_pricing_path,
    default_pricebook,
)
from tokentracker.queries import DEFAULT_TZ

app = typer.Typer(add_completion=False, help="AIエージェントのトークン消費トラッカー")
console = Console()


def _open(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db.connect(db_path)


# 集計軸の単一定義: 軸名 → (テーブル見出しラベル, コマンドヘルプ)。
# テーブル描画とコマンド登録の双方がこれを参照する（ラベル重複の解消）。
DIMENSIONS: dict[str, tuple[str, str]] = {
    "repo": ("リポジトリ別", "リポジトリ別の集計"),
    "model": ("モデル別", "モデル別の集計"),
    "agent": ("ツール別", "ツール(エージェント)別の集計"),
    "session": ("セッション別", "セッション別の集計"),
    "daily": ("日次", "日次の集計"),
}


@app.command()
def ingest(
    db_path: Path = typer.Option(DEFAULT_DB, "--db", help="SQLite ファイルパス"),
    source: str = typer.Option(
        "all", "--source",
        help="取り込むソース: all / claude_code / codex / cline",
    ),
    full: bool = typer.Option(
        False, "--full",
        help="全ファイルを再走査（既定は前回から変更/新規のファイルだけの増分取り込み）。"
             "単価表を更新して既存ログのコストを再計算したいときに使う。",
    ),
) -> None:
    """ローカルログを取り込む（Claude Code / Codex / Cline、既定の保存場所を走査）。

    既定は増分取り込み: ``ingest_state`` に記録した size/mtime と一致する未変更ファイルは
    開かずにスキップするため、繰り返し実行しても新規/変更分だけを収集する。
    """
    if source != "all" and source not in INGESTORS:
        raise typer.BadParameter(f"--source は all/{'/'.join(INGESTORS)} のいずれか")
    sources = None if source == "all" else [source]
    conn = _open(db_path)
    before = db.count_events(conn)
    per_source = ingest_all(conn, sources=sources, incremental=not full)
    total = db.count_events(conn)
    added = total - before
    detail = " / ".join(f"{s}:{n}" for s, n in per_source.items())
    mode = "全件再走査" if full else "増分"
    console.print(
        f"[green]取り込み完了[/]({mode}): 新規 {added} 件 / DB 合計 {total} 件"
        f"  [dim]処理 {detail}[/] -> {db_path}"
    )


@app.command()
def pricing(
    db_path: Path = typer.Option(DEFAULT_DB, "--db", help="SQLite ファイルパス"),
    missing: bool = typer.Option(
        False, "--missing",
        help="DB に出現したが単価未登録のモデルだけを表示",
    ),
) -> None:
    """使用中の単価ファイルと登録モデルを表示（--missing で未登録モデルを抽出）。"""
    if missing:
        conn = _open(db_path)
        book = default_pricebook()
        rows = conn.execute(
            "SELECT model, "
            "SUM(input_tokens+output_tokens+reasoning_output_tokens"
            "    +cache_creation_tokens+cache_read_tokens) AS toks, COUNT(*) AS n "
            "FROM usage_event GROUP BY model"
        ).fetchall()
        table = Table(title="単価未登録のモデル（TOML に追記すべき対象）")
        table.add_column("model", overflow="fold")
        table.add_column("未割当tok", justify="right")
        table.add_column("件数", justify="right")
        unpriced = [
            r for r in rows
            if r["model"] and book.compute_cost(_probe(r["model"])) is None
        ]
        for r in sorted(unpriced, key=lambda r: r["toks"], reverse=True):
            table.add_row(str(r["model"]), f"{r['toks']:,}", f"{r['n']:,}")
        if unpriced:
            console.print(table)
            console.print("[yellow]※ pricing.toml に [models.\"<ID>\"] を追記すると次回 ingest から反映されます。[/]")
        else:
            console.print("[green]未登録モデルはありません。[/]")
        return

    path = active_pricing_path()
    console.print(f"使用中の単価ファイル: [cyan]{path or '(なし)'}[/]")
    console.print(f"同梱の既定ファイル  : {BUNDLED_PRICING}")
    book = default_pricebook()
    table = Table(title="登録モデル単価（$/1M tokens）")
    table.add_column("model", overflow="fold")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("cache_read", justify="right")
    for model, rate in sorted(book.prices.items()):
        table.add_row(
            model, f"{rate.get('input', 0):g}", f"{rate.get('output', 0):g}",
            f"{rate.get('cache_read', 0):g}",
        )
    console.print(table)
    if book.aliases:
        console.print("エイリアス: " + ", ".join(f"{k}→{v}" for k, v in book.aliases.items()))


def _probe(model: str):
    """単価判定用の最小 UsageEvent（compute_cost が None かどうかだけを見る）。"""
    from tokentracker.models import UsageEvent

    return UsageEvent(source="", message_id="", session_id="", model=model,
                      timestamp_utc="", input_tokens=1)


def _compute_totals(rows) -> dict:
    """集計行の列合計（テーブル末尾の「合計」行用）。"""
    tot = {"input": 0, "output": 0, "cw": 0, "cr": 0, "cost": 0.0, "un": 0}
    for r in rows:
        tot["input"] += r["input_tokens"]
        tot["output"] += r["output_tokens"]
        tot["cw"] += r["cache_creation_tokens"]
        tot["cr"] += r["cache_read_tokens"]
        tot["cost"] += r["known_cost_usd"]
        tot["un"] += r["unallocated_tokens"]
    return tot


def _build_table(rows, tot: dict, dimension: str, tz: str) -> Table:
    """集計行から rich テーブル（合計行付き）を組み立てる。"""
    label, _help = DIMENSIONS.get(dimension, (dimension, ""))
    title = f"{label}({tz})" if dimension == "daily" else label
    table = Table(title=f"トークン消費 — {title}")
    table.add_column(dimension, overflow="fold")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")
    table.add_column("cache_w", justify="right")
    table.add_column("cache_r", justify="right")
    table.add_column("判明コスト$", justify="right")
    table.add_column("未割当tok", justify="right")
    for r in rows:
        table.add_row(
            str(r["key"]),
            f"{r['input_tokens']:,}",
            f"{r['output_tokens']:,}",
            f"{r['cache_creation_tokens']:,}",
            f"{r['cache_read_tokens']:,}",
            f"{r['known_cost_usd']:.4f}",
            f"{r['unallocated_tokens']:,}" if r["unallocated_tokens"] else "-",
        )
    table.add_section()
    table.add_row(
        "合計", f"{tot['input']:,}", f"{tot['output']:,}", f"{tot['cw']:,}",
        f"{tot['cr']:,}", f"{tot['cost']:.4f}", f"{tot['un']:,}" if tot["un"] else "-",
        style="bold",
    )
    return table


def _render(
    dimension: str,
    db_path: Path,
    since: Optional[str],
    until: Optional[str],
    tz: str,
    as_json: bool,
    include_subagents: bool,
) -> None:
    conn = _open(db_path)
    rows = queries.summary(
        conn, dimension, since=since, until=until,
        include_subagents=include_subagents, tz=tz,
    )
    if as_json:
        console.print_json(jsonlib.dumps(rows, ensure_ascii=False))
        return
    tot = _compute_totals(rows)
    console.print(_build_table(rows, tot, dimension, tz))
    if tot["un"]:
        console.print("[yellow]※ 未割当tok = 単価未登録モデルのトークン。pricing.py に単価を追加すると判明コストに反映されます。[/]")


def _agg_command(dimension: str):
    def command(
        db_path: Path = typer.Option(DEFAULT_DB, "--db"),
        since: Optional[str] = typer.Option(None, "--since", help="YYYY-MM-DD 以降"),
        until: Optional[str] = typer.Option(None, "--until", help="YYYY-MM-DD 以前"),
        tz: str = typer.Option(DEFAULT_TZ, "--tz"),
        as_json: bool = typer.Option(False, "--json", help="JSON で出力"),
        include_subagents: bool = typer.Option(
            True, "--include-subagents/--exclude-subagents",
            help="サブエージェントを集計に含める",
        ),
    ) -> None:
        _render(dimension, db_path, since, until, tz, as_json, include_subagents)

    return command


for _dim, (_label, _help) in DIMENSIONS.items():
    app.command(name=_dim, help=_help)(_agg_command(_dim))


if __name__ == "__main__":
    app()
