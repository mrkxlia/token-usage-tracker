"""M4: DB スキーマ定義と分析レシピのテスト。"""

import json
import sqlite3

from tokentracker import db
from tokentracker.ingest import ingest_claude_code
from tokentracker.pricing import PriceBook
from tokentracker.schema import CACHE_HIT_RATIO_DEF, SCHEMA_DEFINITION


def test_schema_definition_covers_all_columns():
    """usage_event の全カラム(db._COLUMNS)が定義に存在（ドキュメントドリフト防止）。

    注: prompt/ingest_state には _COLUMNS 相当の定数が無いため、自動ドリフト
    検出は usage_event のみ対象（他テーブルは手動メンテ）。
    """
    defined = {c["name"] for c in SCHEMA_DEFINITION["tables"]["usage_event"]["columns"]}
    for col in db._COLUMNS:
        assert col in defined, f"schema 定義に {col} が無い"


def test_recipes_are_valid_sql():
    """各レシピが空 DB（実スキーマ）でパース・カラム参照できる。"""
    conn = db.connect(":memory:")
    for name, rec in SCHEMA_DEFINITION["recipes"].items():
        # 実行できればパース＆カラム参照とも妥当（空 DB なので結果は 0 行で良い）。
        conn.execute(rec["sql"]).fetchall()


def test_cache_efficiency_recipe_values(tmp_path, claude_root):
    """レシピの論理を実データで検証（パースだけでなく値の正しさ）。"""
    book = PriceBook({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0,
                                            "cache_write_1h": 6.0, "cache_write_5m": 3.75,
                                            "cache_read": 0.3}})
    conn = db.connect(tmp_path / "u.db")
    ingest_claude_code(conn, claude_root, pricebook=book)
    sql = SCHEMA_DEFINITION["recipes"]["cache_efficiency_by_session"]["sql"]
    for row in conn.execute(sql).fetchall():
        cr, inp, ratio = row["cr"], row["inp"], row["cache_hit_ratio"]
        if inp + cr > 0:
            assert abs(ratio - cr / (inp + cr)) < 1e-9
        else:
            assert ratio is None


def test_schema_json_roundtrips():
    """JSON シリアライズ可能で、cache_hit_ratio 定義文字列を含む。"""
    s = json.dumps(SCHEMA_DEFINITION, ensure_ascii=False)
    assert CACHE_HIT_RATIO_DEF in s
    assert SCHEMA_DEFINITION["conventions"]["ratios"]["cache_hit_ratio"] == CACHE_HIT_RATIO_DEF


def test_schema_cli_json_exits_zero():
    """`tokentracker schema --json` が exit 0 で有効な JSON を出す（初の CLI テスト）。"""
    from typer.testing import CliRunner

    from tokentracker.cli import app

    result = CliRunner().invoke(app, ["schema", "--json"])
    assert result.exit_code == 0
    # print_json 出力はそのままパース可能。
    json.loads(result.stdout)
