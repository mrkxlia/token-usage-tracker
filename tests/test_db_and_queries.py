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


def test_summary_includes_ratio_keys(tmp_path, claude_root):
    """各行に派生指標キー（out/in 比・cache率・件あたりコスト）が付与される。"""
    conn = _conn_with_data(tmp_path, claude_root)
    for r in queries.summary(conn, "model"):
        assert "output_input_ratio" in r
        assert "cache_hit_ratio" in r
        assert "cost_per_event" in r


def test_cache_hit_ratio_formula(tmp_path, claude_root):
    """cache_hit_ratio == cache_read / (input + cache_read)（同一行の値から再計算で検証）。"""
    conn = _conn_with_data(tmp_path, claude_root)
    for r in queries.summary(conn, "repo"):
        inp, cr = r["input_tokens"], r["cache_read_tokens"]
        if inp + cr > 0:
            assert r["cache_hit_ratio"] == cr / (inp + cr)
        else:
            assert r["cache_hit_ratio"] is None
        if inp > 0:
            assert r["output_input_ratio"] == r["output_tokens"] / inp


def test_ratio_divide_by_zero_is_none(tmp_path):
    """input=0 かつ cache_read=0 のイベントでは比率が None になる。"""
    from tokentracker.models import UsageEvent

    conn = db.connect(tmp_path / "zero.db")
    db.upsert_events(conn, [UsageEvent(
        source="claude_code", message_id="z1", session_id="s", model="unknown-x",
        timestamp_utc="2026-06-14T01:00:00.000Z", input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cost_usd=None,
    )])
    r = queries.summary(conn, "session")[0]
    assert r["output_input_ratio"] is None
    assert r["cache_hit_ratio"] is None
    # events=1 なので cost_per_event は割れる（未割当なので 0.0）。None ではない。
    assert r["cost_per_event"] == 0.0


def test_existing_summary_keys_unchanged(tmp_path, claude_root):
    """派生指標の後付けが既存キーの値を壊さない回帰ガード。"""
    conn = _conn_with_data(tmp_path, claude_root)
    r = queries.summary(conn, "repo")[0]
    assert r["key"] == "/home/user/myrepo"
    assert r["input_tokens"] == 130
    assert r["output_tokens"] == 65
