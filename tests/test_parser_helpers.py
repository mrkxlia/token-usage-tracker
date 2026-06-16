"""パーサ共通ヘルパー（base.py の集約関数）のテスト。"""

from __future__ import annotations

import logging

from tokentracker.parsers.base import (
    iter_jsonl_objects,
    read_json_file,
    read_text_file,
    safe_int,
)


def test_safe_int_coerces_missing_none_and_empty():
    obj = {"a": "5", "b": None, "c": "", "d": 7}
    assert safe_int(obj, "a") == 5
    assert safe_int(obj, "b") == 0          # None → default
    assert safe_int(obj, "c") == 0          # 空文字 → default
    assert safe_int(obj, "missing") == 0    # 欠落 → default
    assert safe_int(obj, "d") == 7
    assert safe_int(obj, "missing", default=3) == 3


def test_iter_jsonl_objects_skips_blank_broken_and_non_dict():
    text = '\n'.join([
        '{"x": 1}',
        '',                 # 空行
        '   ',              # 空白のみ
        '{"y": 2}',
        '{"broken": ',      # 不完全行（ストリーミング途中）
        '[1, 2, 3]',        # dict 以外
        '"a string"',       # dict 以外
    ])
    out = list(iter_jsonl_objects(text))
    assert out == [{"x": 1}, {"y": 2}]


def test_read_json_file_missing_returns_none(tmp_path):
    assert read_json_file(tmp_path / "nope.json") is None


def test_read_json_file_broken_logs_and_returns_none(tmp_path, caplog):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tokentracker.parsers"):
        assert read_json_file(p) is None
    assert any("解析に失敗" in r.message for r in caplog.records)


def test_read_text_file_ok_and_missing(tmp_path, caplog):
    p = tmp_path / "a.txt"
    p.write_text("hello", encoding="utf-8")
    assert read_text_file(p) == "hello"
    # ディレクトリを渡すと OSError → 警告のうえ None。
    with caplog.at_level(logging.WARNING, logger="tokentracker.parsers"):
        assert read_text_file(tmp_path) is None
    assert any("読めずスキップ" in r.message for r in caplog.records)
