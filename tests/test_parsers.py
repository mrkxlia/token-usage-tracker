"""Claude Code パーサのテスト（TDD: 実装前に赤で用意した代表ケース）。

実環境ログで裏取りした不変条件を fixtures に固定し、回帰を防ぐ。
"""

import json

from tokentracker.models import SOURCE_CLAUDE_CODE, SYNTHETIC_MODEL
from tokentracker.parsers.claude_code import ClaudeCodeParser


def _events_by_id(claude_root):
    parser = ClaudeCodeParser()
    return {e.message_id: e for e in parser.iter_events(claude_root)}


def test_dedup_message_id_keeps_single_event(claude_root):
    """同一 message.id が 5 行重複しても 1 件・usage は重複加算されない。"""
    events = _events_by_id(claude_root)
    assert "msg_A" in events
    a = events["msg_A"]
    # 5 重複を素朴に合算すると output=250 になる。dedup 後は 50。
    assert a.output_tokens == 50
    assert a.input_tokens == 100
    assert a.cache_read_tokens == 1000


def test_event_set_is_exactly_four(claude_root):
    """assistant かつ usage を持つ行のみ・message.id 単位で 4 件。"""
    events = _events_by_id(claude_root)
    assert set(events) == {"msg_A", "msg_B", "msg_C", "11111111-2222-3333-4444-555555555555"}


def test_non_assistant_rows_are_ignored(claude_root):
    """user/attachment/system/queue-operation/last-prompt/mode は集計対象外。"""
    parser = ClaudeCodeParser()
    events = list(parser.iter_events(claude_root))
    # それらの行が紛れ込むと dedup 後合計が増える。input 合計は 130(=100+20+0+10) のはず。
    assert sum(e.input_tokens for e in events) == 130
    assert sum(e.output_tokens for e in events) == 65


def test_subagent_split_by_agent_id(claude_root):
    """サブエージェント別ファイルの行は is_subagent=True かつ agent_id を持つ。"""
    events = _events_by_id(claude_root)
    sub = events["msg_C"]
    assert sub.is_subagent is True
    assert sub.agent_id == "AAA"
    assert sub.session_id == "SESSION1"
    main = events["msg_A"]
    assert main.is_subagent is False
    assert main.agent_id is None


def test_cache_creation_1h_5m_split(claude_root):
    """1h/5m キャッシュ書込トークンを別フィールドに分離保持する。"""
    a = _events_by_id(claude_root)["msg_A"]
    assert a.cache_creation_1h_tokens == 200
    assert a.cache_creation_5m_tokens == 300
    assert a.cache_creation_tokens == 500


def test_server_tool_use_counts(claude_root):
    """server_tool_use の web 検索/取得件数を保持（コスト計算対象外だが記録する）。"""
    b = _events_by_id(claude_root)["msg_B"]
    assert b.web_search_requests == 3
    assert b.web_fetch_requests == 1


def test_repo_and_model_and_source(claude_root):
    a = _events_by_id(claude_root)["msg_A"]
    assert a.repo_path == "/home/user/myrepo"
    assert a.git_branch == "main"
    assert a.model == "claude-sonnet-4-6"
    assert a.source == SOURCE_CLAUDE_CODE


def test_synthetic_row_present_but_zero(claude_root):
    syn = _events_by_id(claude_root)["11111111-2222-3333-4444-555555555555"]
    assert syn.model == SYNTHETIC_MODEL
    assert syn.input_tokens == 0 and syn.output_tokens == 0


def test_iterations_consistency_helper(claude_root):
    """top-level usage == sum(iterations) を検証するヘルパ（将来版の過小カウント検知）。"""
    f = claude_root / "-home-user-myrepo" / "SESSION1.jsonl"
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    msg_a = next(r["message"] for r in rows if r.get("message", {}).get("id") == "msg_A")
    assert ClaudeCodeParser.usage_matches_iterations(msg_a["usage"]) is True
