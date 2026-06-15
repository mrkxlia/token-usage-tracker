"""Cline タスクログ パーサのテスト（TDD）。"""

from pathlib import Path

import pytest

from tokentracker import db, queries
from tokentracker.ingest import ingest_cline
from tokentracker.models import SOURCE_CLINE
from tokentracker.parsers.cline import ClineParser
from tokentracker.pricing import PriceBook

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cline_root() -> Path:
    return FIXTURES / "cline_tasks"


def _events(cline_root):
    return list(ClineParser().iter_events(cline_root))


def test_multiple_api_reqs_become_separate_events(cline_root):
    """taskA は api_req_started × 2 → 2 イベント、taskB は 1 → 計 3。"""
    evs = _events(cline_root)
    assert len(evs) == 3
    assert all(e.source == SOURCE_CLINE for e in evs)
    a = [e for e in evs if e.session_id == "taskA"]
    assert len(a) == 2


def test_token_field_mapping(cline_root):
    """tokensIn/Out, cacheReads→cache_read, cacheWrites→cache_creation_5m。"""
    evs = sorted([e for e in _events(cline_root) if e.session_id == "taskA"], key=lambda e: e.message_id)
    first = evs[0]
    assert first.input_tokens == 50
    assert first.output_tokens == 20
    assert first.cache_read_tokens == 10
    assert first.cache_creation_5m_tokens == 5


def test_repo_path_from_task_metadata(cline_root):
    evs = _events(cline_root)
    a = next(e for e in evs if e.session_id == "taskA")
    b = next(e for e in evs if e.session_id == "taskB")
    assert a.repo_path == "/home/user/clinerepo"
    assert b.repo_path == "/home/user/other"


def test_model_resolved_from_history_else_unallocated(tmp_path, cline_root):
    book = PriceBook({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write_1h": 6.0, "cache_write_5m": 3.75, "cache_read": 0.3}})
    conn = db.connect(tmp_path / "cl.db")
    ingest_cline(conn, cline_root, pricebook=book)
    rows = {r["key"]: r for r in queries.summary(conn, "model")}
    # taskA は environment_details からモデル解決 → 単価あり
    assert "claude-sonnet-4-6" in rows
    assert rows["claude-sonnet-4-6"]["known_cost_usd"] > 0
    # taskB はモデル不明（空文字キー）→ 未割当
    assert rows[""]["unallocated_tokens"] > 0
    assert rows[""]["known_cost_usd"] in (0, 0.0)


def test_ms_timestamp_converted_to_utc_iso():
    assert ClineParser.ms_to_iso(0) == "1970-01-01T00:00:00+00:00"
    iso = ClineParser.ms_to_iso(1781755201000)
    assert iso.startswith("2026-06-")


def test_ingest_idempotent(tmp_path, cline_root):
    conn = db.connect(tmp_path / "cl2.db")
    n1 = ingest_cline(conn, cline_root, pricebook=PriceBook({}))
    t1 = conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0]
    ingest_cline(conn, cline_root, pricebook=PriceBook({}))
    t2 = conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0]
    assert n1 == t1 == t2 == 3
