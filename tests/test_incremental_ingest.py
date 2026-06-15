"""増分取り込み（未変更ファイルのスキップ）と ingest_state / file-events のテスト。

設計の要は「ログは追記式 → size+mtime が変われば再取り込み、変わらなければスキップ」。
冪等性（二重計上しない）は従来の UNIQUE(source, message_id) UPSERT が担保する。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from tokentracker import db
from tokentracker.ingest import ingest_claude_code
from tokentracker.models import SOURCE_CLAUDE_CODE, UsageEvent
from tokentracker.parsers.base import Parser
from tokentracker.parsers.claude_code import ClaudeCodeParser
from tokentracker.parsers.cline import ClineParser
from tokentracker.pricing import PriceBook

FIXTURES = Path(__file__).parent / "fixtures"


def _book() -> PriceBook:
    return PriceBook({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3}})


# --- db: ingest_state の往復 -------------------------------------------------

def test_ingest_state_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    assert db.load_ingest_state(conn) == {}
    db.update_ingest_state(conn, "/a/b.jsonl", 123, 456.5, "msg_X")
    assert db.load_ingest_state(conn) == {"/a/b.jsonl": (123, 456.5)}
    # 同一パスは UPSERT で更新される。
    db.update_ingest_state(conn, "/a/b.jsonl", 999, 789.0)
    assert db.load_ingest_state(conn) == {"/a/b.jsonl": (999, 789.0)}


def test_count_events_helper(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    assert db.count_events(conn) == 0
    ingest_claude_code(conn, FIXTURES / "claude_projects", pricebook=_book())
    assert db.count_events(conn) == 4


# --- 増分: 2 回目はスキップ ---------------------------------------------------

def test_second_ingest_skips_unchanged(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    n1 = ingest_claude_code(conn, FIXTURES / "claude_projects", pricebook=_book())
    n2 = ingest_claude_code(conn, FIXTURES / "claude_projects", pricebook=_book())
    assert n1 == 4
    assert n2 == 0  # 未変更ファイルは開かずスキップ
    assert db.count_events(conn) == 4
    # ingest_state に走査したファイルが記録されている。
    assert len(db.load_ingest_state(conn)) >= 1


def test_incremental_false_rescans_all(tmp_path):
    conn = db.connect(tmp_path / "u.db")
    ingest_claude_code(conn, FIXTURES / "claude_projects", pricebook=_book())
    # incremental=False は全件再走査（単価変更の反映用）。
    n2 = ingest_claude_code(conn, FIXTURES / "claude_projects", pricebook=_book(), incremental=False)
    assert n2 == 4
    assert db.count_events(conn) == 4  # 冪等: 行数は増えない


# --- 増分: 変更ファイルだけ再取り込み ----------------------------------------

def _write_session(path: Path, message_id: str, output: int) -> None:
    row = {
        "type": "assistant",
        "sessionId": "S1",
        "timestamp": "2026-06-14T01:00:00.000Z",
        "cwd": "/repo",
        "message": {
            "id": message_id,
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 10, "output_tokens": output},
        },
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_modified_file_is_reingested(tmp_path):
    root = tmp_path / "projects" / "-repo"
    root.mkdir(parents=True)
    f = root / "S1.jsonl"
    _write_session(f, "msg_1", output=5)

    conn = db.connect(tmp_path / "u.db")
    assert ingest_claude_code(conn, tmp_path / "projects", pricebook=_book()) == 1

    # ファイルに新しいメッセージを追記（size/mtime が変わる）。
    import os
    import time

    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "assistant",
            "sessionId": "S1",
            "timestamp": "2026-06-14T02:00:00.000Z",
            "cwd": "/repo",
            "message": {"id": "msg_2", "model": "claude-sonnet-4-6",
                         "usage": {"input_tokens": 7, "output_tokens": 9}},
        }) + "\n")
    # mtime 解像度に依存しないよう、明示的に未来の mtime を設定。
    future = time.time() + 10
    os.utime(f, (future, future))

    n2 = ingest_claude_code(conn, tmp_path / "projects", pricebook=_book())
    assert n2 == 2  # 変更ファイルを丸ごと再取り込み
    ids = {r[0] for r in conn.execute("SELECT message_id FROM usage_event").fetchall()}
    assert ids == {"msg_1", "msg_2"}


def test_cross_file_duplicate_message_id_stays_idempotent(tmp_path):
    """同一 message_id が 2 ファイルに跨っても、増分再取り込みで二重計上しない。

    増分では変更ファイルだけを dedup するが、UNIQUE(source, message_id) UPSERT が
    最終的な一意性を保証する（行数は増えない）。
    """
    proj = tmp_path / "projects"
    (proj / "-a").mkdir(parents=True)
    (proj / "-b").mkdir(parents=True)
    _write_session(proj / "-a" / "S.jsonl", "dup", output=5)
    _write_session(proj / "-b" / "S.jsonl", "dup", output=9)

    conn = db.connect(tmp_path / "u.db")
    ingest_claude_code(conn, proj, pricebook=_book())
    # 同一 message_id なので 1 行に収束。
    assert db.count_events(conn) == 1

    # 片方のファイルだけ変更して増分取り込み → 依然 1 行（二重計上なし）。
    import os
    import time

    f = proj / "-a" / "S.jsonl"
    _write_session(f, "dup", output=11)
    future = time.time() + 10
    os.utime(f, (future, future))
    ingest_claude_code(conn, proj, pricebook=_book())
    assert db.count_events(conn) == 1


def test_state_not_advanced_when_event_write_fails(tmp_path, monkeypatch):
    """upsert が失敗したら ingest_state を進めない（次回に再取り込みされる）。

    state を events より先に確定すると、失敗時にファイルがスキップされ恒久的に欠落する。
    それを防ぐ「events 確定後に state 更新」の回帰テスト。
    """
    root = tmp_path / "projects" / "-repo"
    root.mkdir(parents=True)
    _write_session(root / "S1.jsonl", "msg_1", output=5)
    conn = db.connect(tmp_path / "u.db")

    # 1 回目: upsert を失敗させる。
    from tokentracker import ingest as ingest_mod

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ingest_mod.db, "upsert_events", boom)
    try:
        ingest_claude_code(conn, tmp_path / "projects", pricebook=_book())
    except RuntimeError:
        pass
    assert db.load_ingest_state(conn) == {}  # state は前進していない

    # 2 回目: 正常な upsert に戻すと、同じファイルがちゃんと取り込まれる。
    monkeypatch.undo()
    n = ingest_claude_code(conn, tmp_path / "projects", pricebook=_book())
    assert n == 1
    assert db.count_events(conn) == 1
    assert len(db.load_ingest_state(conn)) == 1


# --- file-events / extra_roots の機構 ----------------------------------------

def test_iter_file_events_yields_path_and_events():
    parser = ClaudeCodeParser()
    root = FIXTURES / "claude_projects"
    pairs = list(parser.iter_file_events(root))
    assert len(pairs) == 2  # SESSION1.jsonl と subagents/agent-AAA.jsonl
    for path, events in pairs:
        assert isinstance(path, Path)
        assert all(isinstance(e, UsageEvent) for e in events)
    # 平坦化した生イベントは _iter_raw_events と一致（回帰）。
    flat = [e.message_id for _p, evs in pairs for e in evs]
    raw = [e.message_id for e in parser._iter_raw_events(root)]
    assert sorted(flat) == sorted(raw)


def test_cline_extra_roots_includes_vscode_server():
    roots = ClineParser().extra_roots()
    assert any(".vscode-server" in str(r) and r.name == "tasks" for r in roots)


class _FakeParser(Parser):
    """default_root と extra_roots の両方を走査することを確認するためのテスト用パーサ。"""

    source = SOURCE_CLAUDE_CODE

    def __init__(self, root_a: Path, root_b: Path):
        self._a, self._b = root_a, root_b

    def default_root(self) -> Path:
        return self._a

    def extra_roots(self) -> list[Path]:
        return [self._b]

    def _iter_file_events(self, root: Path) -> Iterator[tuple[Path, list[UsageEvent]]]:
        for p in sorted(root.glob("*.txt")):
            yield p, [UsageEvent(SOURCE_CLAUDE_CODE, p.stem, "", "", "")]


def test_iter_file_events_scans_default_and_extra_roots(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "x.txt").write_text("x")
    (b / "y.txt").write_text("y")
    parser = _FakeParser(a, b)
    ids = {e.message_id for _p, evs in parser.iter_file_events(None) for e in evs}
    assert ids == {"x", "y"}  # 既定＋追加ルートの双方が走査される
