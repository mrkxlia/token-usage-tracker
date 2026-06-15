"""Codex rollout パーサのテスト（TDD）。"""

from pathlib import Path

import pytest

from tokentracker import db, queries
from tokentracker.ingest import ingest_codex
from tokentracker.models import SOURCE_CODEX
from tokentracker.parsers.codex import CodexParser
from tokentracker.pricing import PriceBook

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def codex_root() -> Path:
    return FIXTURES / "codex_sessions"


def _events(codex_root):
    return list(CodexParser().iter_events(codex_root))


def test_one_event_per_token_count(codex_root):
    """token_count イベント 3 件 → 3 イベント。"""
    evs = _events(codex_root)
    assert len(evs) == 3
    assert all(e.source == SOURCE_CODEX for e in evs)


def test_per_call_uses_last_token_usage_and_deducts_cache(codex_root):
    """per-call は last_token_usage 由来。cached_input は input から控除し cache_read へ。"""
    evs = sorted(_events(codex_root), key=lambda e: e.message_id)
    first = evs[0]
    # last_token_usage: input 1000, cached 200, output 300, reasoning 50
    assert first.input_tokens == 800       # 1000 - 200(cached)
    assert first.cache_read_tokens == 200
    assert first.output_tokens == 300
    assert first.reasoning_output_tokens == 50
    # 2 件目は last_token_usage(累積でない): input 700, cached 60
    second = evs[1]
    assert second.input_tokens == 640      # 700 - 60
    assert second.cache_read_tokens == 60
    assert second.output_tokens == 120
    assert second.reasoning_output_tokens == 20


def test_model_switch_applies_to_following_events(codex_root):
    """turn_context のモデル切替が後続 token_count に反映される。"""
    evs = sorted(_events(codex_root), key=lambda e: e.message_id)
    assert evs[0].model == "gpt-5"
    assert evs[1].model == "gpt-5"
    assert evs[2].model == "gpt-5-mini"


def test_session_and_repo_from_session_meta(codex_root):
    evs = _events(codex_root)
    assert all(e.session_id == "sess-codex-1" for e in evs)
    assert all(e.repo_path == "/home/user/coderepo" for e in evs)


def test_last_usage_total_integrity_helper():
    """last_token_usage の内部整合 total == input + output + reasoning を検証するヘルパ。"""
    good = {"input_tokens": 1000, "output_tokens": 300, "reasoning_output_tokens": 50, "total_tokens": 1350}
    bad = {"input_tokens": 1000, "output_tokens": 300, "reasoning_output_tokens": 50, "total_tokens": 9999}
    assert CodexParser.usage_total_matches(good) is True
    assert CodexParser.usage_total_matches(bad) is False


def test_ingest_idempotent_and_synthetic_key(tmp_path, codex_root):
    conn = db.connect(tmp_path / "c.db")
    n1 = ingest_codex(conn, codex_root, pricebook=PriceBook({}))
    total1 = conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0]
    ingest_codex(conn, codex_root, pricebook=PriceBook({}))
    total2 = conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0]
    assert n1 == total1 == total2 == 3


def test_reasoning_counts_into_unallocated_when_unpriced(tmp_path, codex_root):
    """未価格モデルの行で reasoning も unallocated_tokens に算入される（握りつぶさない）。"""
    conn = db.connect(tmp_path / "c2.db")
    ingest_codex(conn, codex_root, pricebook=PriceBook({}))  # 単価無し
    rows = {r["key"]: r for r in queries.summary(conn, "model")}
    g5 = rows["gpt-5"]
    # 2 イベント分の reasoning(50+20=70) が unallocated に含まれること。
    # unallocated = input+output+reasoning+cache_read = (800+640)+(300+120)+(50+20)+(200+60) = 2190
    assert g5["unallocated_tokens"] == 2190
    # reasoning を除くと 2120 になるので、reasoning が算入されていることを別途確認。
    assert g5["unallocated_tokens"] - 70 == 2120
    assert g5["known_cost_usd"] in (0, 0.0)
