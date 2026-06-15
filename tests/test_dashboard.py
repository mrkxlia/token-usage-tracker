"""ダッシュボードの純粋ヘルパー（比率付与・期間フィルタ）のテスト。

Streamlit UI は起動せず、純関数だけを検証する。pandas/streamlit が無い環境
（dashboard extra 未導入）ではスキップする。
"""

import pytest

pytest.importorskip("pandas")
pytest.importorskip("streamlit")

import pandas as pd  # noqa: E402

from tokentracker.dashboard import add_ratio_columns, filter_by_date_range  # noqa: E402


def test_add_ratio_columns_formula():
    agg = pd.DataFrame(
        {
            "input_tokens": [1000, 0],
            "output_tokens": [500, 0],
            "cache_read_tokens": [4000, 0],
            "known_cost_usd": [0.05, 0.0],
            "events": [2, 0],
        }
    )
    out = add_ratio_columns(agg)
    assert out["out/in"].iloc[0] == 0.5
    assert out["cache_hit"].iloc[0] == 4000 / 5000
    assert out["cost_per_event"].iloc[0] == 0.025
    # ゼロ分母の行は NaN。
    assert pd.isna(out["out/in"].iloc[1])
    assert pd.isna(out["cache_hit"].iloc[1])
    assert pd.isna(out["cost_per_event"].iloc[1])


def test_filter_by_date_range_string_compare():
    df = pd.DataFrame({"date": ["2026-06-13", "2026-06-14", "2026-06-15", None]})
    out = filter_by_date_range(df, "2026-06-14", "2026-06-15")
    # 境界は包含、None は除外。
    assert sorted(out["date"].tolist()) == ["2026-06-14", "2026-06-15"]
