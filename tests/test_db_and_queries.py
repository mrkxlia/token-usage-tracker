"""DB 取り込み(冪等)と集計クエリ(未割当コスト併記・TZ バケット)のテスト。"""

from pathlib import Path

from tokentracker import db, queries
from tokentracker.ingest import ingest_claude_code, ingest_cline, ingest_codex
from tokentracker.pricing import PriceBook

FIXTURES = Path(__file__).parent / "fixtures"


def _book():
    return PriceBook(
        {
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write_1h": 6.0, "cache_write_5m": 3.75, "cache_read": 0.3},
            "claude-opus-4-8": {"input": 15.0, "output": 75.0, "cache_write_1h": 30.0, "cache_write_5m": 18.75, "cache_read": 1.5},
        }
    )


def _conn_with_data(tmp_path, claude_root):
    conn = db.connect(tmp_path / "usage.db")
    ingest_claude_code(conn, claude_root, pricebook=_book())
    return conn


def test_ingest_is_idempotent(tmp_path, claude_root):
    """2 回 ingest しても UNIQUE(source, message_id) で行数が増えない。"""
    conn = db.connect(tmp_path / "usage.db")
    n1 = ingest_claude_code(conn, claude_root, pricebook=_book())
    total1 = conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0]
    ingest_claude_code(conn, claude_root, pricebook=_book())
    total2 = conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0]
    assert total1 == total2 == n1 == 4


def test_summary_by_repo_totals(tmp_path, claude_root):
    conn = _conn_with_data(tmp_path, claude_root)
    rows = queries.summary(conn, "repo")
    assert len(rows) == 1
    r = rows[0]
    assert r["key"] == "/home/user/myrepo"
    assert r["input_tokens"] == 130
    assert r["output_tokens"] == 65


def test_summary_by_model_splits_models(tmp_path, claude_root):
    conn = _conn_with_data(tmp_path, claude_root)
    by = {r["key"]: r for r in queries.summary(conn, "model")}
    assert by["claude-sonnet-4-6"]["input_tokens"] == 110  # msg_A(100)+msg_C(10)
    assert by["claude-opus-4-8"]["input_tokens"] == 20


def test_exclude_subagents_drops_subagent_tokens(tmp_path, claude_root):
    conn = _conn_with_data(tmp_path, claude_root)
    rows = queries.summary(conn, "repo", include_subagents=False)
    # msg_C(input 10) が落ちて 120 になる。
    assert rows[0]["input_tokens"] == 120


def test_unknown_model_surfaces_as_unallocated(tmp_path, claude_root):
    """単価が無いモデルは known cost に混ざらず、未割当として別計上される。"""
    # opus の単価を抜いた価格表で取り込み直す。
    conn = db.connect(tmp_path / "u2.db")
    book = PriceBook({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write_1h": 6.0, "cache_write_5m": 3.75, "cache_read": 0.3}})
    ingest_claude_code(conn, claude_root, pricebook=book)
    rows = queries.summary(conn, "model")
    by = {r["key"]: r for r in rows}
    # opus は未割当: known_cost_usd は None/0 ではなく「未割当トークン」として現れる。
    assert by["claude-opus-4-8"]["unallocated_tokens"] > 0
    assert by["claude-opus-4-8"]["known_cost_usd"] in (0, 0.0)
    # sonnet は単価ありなので未割当 0。
    assert by["claude-sonnet-4-6"]["unallocated_tokens"] == 0
    assert by["claude-sonnet-4-6"]["known_cost_usd"] > 0


def test_daily_bucket_uses_local_timezone(tmp_path, claude_root):
    """UTC 保存・Asia/Tokyo バケット。01:00Z は JST 10:00 で同日 06-14。"""
    conn = _conn_with_data(tmp_path, claude_root)
    rows = queries.summary(conn, "daily", tz="Asia/Tokyo")
    days = {r["key"] for r in rows}
    assert days == {"2026-06-14"}


def test_agent_dimension_spans_three_sources(tmp_path, claude_root):
    """3 ソースを投入すると agent 軸に claude_code/codex/cline が並ぶ。"""
    conn = db.connect(tmp_path / "all.db")
    ingest_claude_code(conn, claude_root, pricebook=_book())
    ingest_codex(conn, FIXTURES / "codex_sessions", pricebook=_book())
    ingest_cline(conn, FIXTURES / "cline_tasks", pricebook=_book())
    agents = {r["key"] for r in queries.summary(conn, "agent")}
    assert agents == {"claude_code", "codex", "cline"}


def test_daily_bucket_crosses_midnight_in_utc_minus():
    """UTC 22:00 は America/New_York では前日になることをバケット関数で確認。"""
    # 純関数のTZバケットを直接検証（DB非依存）。
    assert queries.local_date_bucket("2026-06-14T01:00:10.000Z", "Asia/Tokyo") == "2026-06-14"
    assert queries.local_date_bucket("2026-06-14T02:00:00.000Z", "America/New_York") == "2026-06-13"


def test_date_filter_boundaries_are_exact(tmp_path, claude_root):
    """SQL プリフィルタ(±1日pad)後もローカル日付境界が厳密であること。全件は 06-14。"""
    conn = _conn_with_data(tmp_path, claude_root)
    # 当日を含む since は全件残る。
    assert queries.summary(conn, "repo", since="2026-06-14")[0]["input_tokens"] == 130
    # 翌日以降の since は pad で SQL には残るが、ループ再判定で全件除外され空になる。
    assert queries.summary(conn, "repo", since="2026-06-15") == []
    # until が前日なら全件除外。
    assert queries.summary(conn, "repo", until="2026-06-13") == []
    # until が当日なら全件残る。
    assert queries.summary(conn, "repo", until="2026-06-14")[0]["input_tokens"] == 130


def test_compute_cost_is_non_destructive():
    """ingest の単価付与は元イベントを変更せず新インスタンスへ cost_usd を埋める(H2)。"""
    from dataclasses import replace

    from tokentracker.models import UsageEvent

    ev = UsageEvent(
        source="claude_code", message_id="m", session_id="s",
        model="claude-sonnet-4-6", timestamp_utc="2026-06-14T01:00:00Z", input_tokens=1000,
    )
    priced = replace(ev, cost_usd=_book().compute_cost(ev))
    assert ev.cost_usd is None           # 元は非破壊
    assert priced.cost_usd is not None   # 新インスタンスに付与
    assert priced is not ev


def test_event_row_matches_columns_and_normalizes_bool():
    """_event_row は _COLUMNS の順序・要素数に一致し、bool を 0/1 に正規化する(H3)。"""
    from tokentracker.models import UsageEvent

    ev = UsageEvent(
        source="claude_code", message_id="m", session_id="s",
        model="x", timestamp_utc="2026-06-14T01:00:00Z", is_subagent=True,
    )
    row = db._event_row(ev)
    assert len(row) == len(db._COLUMNS)
    assert row[db._COLUMNS.index("is_subagent")] == 1
    assert row[db._COLUMNS.index("source")] == "claude_code"


def test_columns_cover_all_dataclass_fields():
    """_COLUMNS と UsageEvent のフィールド集合が一致（追加忘れの無言バグ防止, H3）。"""
    from tokentracker.models import UsageEvent

    assert set(db._COLUMNS) == set(UsageEvent.column_names())
