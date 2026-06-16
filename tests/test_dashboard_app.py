"""ダッシュボード（Streamlit）の純関数ユニット＋ AppTest による画面テスト。

streamlit 未インストール環境ではモジュールごとスキップする。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from tokentracker import db  # noqa: E402
from tokentracker.ingest import ingest_claude_code  # noqa: E402
from tokentracker.models import UsageEvent  # noqa: E402
from tokentracker.pricing import PriceBook  # noqa: E402
from tokentracker import dashboard as dash  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
APP = str(Path(dash.__file__))


def _book() -> PriceBook:
    return PriceBook({"claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3}})


def _seed_db(tmp_path) -> str:
    path = tmp_path / "usage.db"
    conn = db.connect(path)
    ingest_claude_code(conn, FIXTURES / "claude_projects", pricebook=_book())
    conn.close()
    return str(path)


# === 純関数 =================================================================

def test_compute_kpis_matches_expected(tmp_path):
    df = dash._load_df(_seed_db(tmp_path), "Asia/Tokyo")
    k = dash.compute_kpis(df)
    assert k["events"] == 4
    assert k["total_tokens"] > 0
    # 構成は入力/出力/キャッシュ読の 3 キー。
    assert set(k["composition"]) == {"input_tokens", "output_tokens", "cache_read_tokens"}
    assert k["composition"]["input_tokens"] == 130


def test_filter_df_by_source_and_subagent(tmp_path):
    df = dash._load_df(_seed_db(tmp_path), "Asia/Tokyo")
    # サブエージェント除外で msg_C(input 10) が落ちる。
    f = dash.filter_df(
        df, repos=list(df["repo_path"].dropna().unique()),
        models=list(df["model"].dropna().unique()),
        sources=["claude_code"], include_sub=False,
    )
    assert f["input_tokens"].sum() == 120


def test_filter_df_by_date_range(tmp_path):
    df = dash._load_df(_seed_db(tmp_path), "Asia/Tokyo")
    # 全データは 2026-06-14。範囲外にすると空、含めると残る。
    empty = dash.filter_df(
        df, repos=list(df["repo_path"].dropna().unique()),
        models=list(df["model"].dropna().unique()),
        sources=["claude_code"], include_sub=True,
        since="2026-06-15", until="2026-06-16",
    )
    assert empty.empty
    full = dash.filter_df(
        df, repos=list(df["repo_path"].dropna().unique()),
        models=list(df["model"].dropna().unique()),
        sources=["claude_code"], include_sub=True,
        since="2026-06-14", until="2026-06-14",
    )
    assert len(full) == 4


def test_filter_df_date_range_handles_none_dates():
    """timestamp 空で date=None の行があっても期間フィルタが例外にならない。"""
    import pandas as pd

    df = pd.DataFrame(
        {
            "repo_path": ["r", "r"],
            "model": ["m", "m"],
            "source": ["codex", "codex"],
            "is_subagent": [False, False],
            "date": ["2026-06-14", None],
        }
    )
    out = dash.filter_df(
        df, repos=["r"], models=["m"], sources=["codex"], include_sub=True,
        since="2026-06-14", until="2026-06-14",
    )
    assert len(out) == 1  # None 日付の行は期間外として除外


def test_aggregate_and_chart(tmp_path):
    df = dash._load_df(_seed_db(tmp_path), "Asia/Tokyo")
    agg = dash.aggregate(df, "model")
    assert "input_tokens" in agg.columns
    assert agg["input_tokens"].sum() == 130
    chart = dash.build_daily_chart(df)
    import altair as alt
    assert isinstance(chart, alt.TopLevelMixin)


def test_build_daily_chart_empty_returns_none():
    """空 DataFrame では None を返し、呼び出し側が描画をスキップできる。"""
    import pandas as pd

    assert dash.build_daily_chart(pd.DataFrame()) is None


def test_valid_tz_helper():
    assert dash._valid_tz("Asia/Tokyo") is True
    assert dash._valid_tz("America/New_York") is True
    assert dash._valid_tz("Not/AZone") is False
    assert dash._valid_tz("") is False


def test_run_ingest_reports_new_count(tmp_path, monkeypatch):
    """run_ingest は取り込み前後の行数差を新規件数として返す。"""
    def fake_ingest_all(conn, *, sources=None, incremental=True):
        db.upsert_events(conn, [UsageEvent("claude_code", "m1", "s", "claude-sonnet-4-6", "")])
        return {"claude_code": 1}

    monkeypatch.setattr(dash, "ingest_all", fake_ingest_all)
    added, per = dash.run_ingest(str(tmp_path / "u.db"), ["claude_code"])
    assert added == 1
    assert per == {"claude_code": 1}
    # 2 回目は同じ message_id なので新規 0（冪等）。
    added2, _ = dash.run_ingest(str(tmp_path / "u.db"), ["claude_code"])
    assert added2 == 0


# === AppTest（画面） =========================================================

def test_app_empty_state_when_no_db(tmp_path):
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.text_input(key="db_path").set_value(str(tmp_path / "missing.db")).run()
    assert not at.exception
    # 空状態カードの文言と取り込みボタンが存在する。
    assert any("まだデータがありません" in m.value for m in at.markdown)
    assert any(b.label == "取り込み実行" for b in at.button)


def test_app_renders_kpis_with_data(tmp_path):
    db_path = _seed_db(tmp_path)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.text_input(key="db_path").set_value(db_path).run()
    assert not at.exception
    # KPI カード（イベント数）とトークン構成バーが描画される。
    md = " ".join(m.value for m in at.markdown)
    assert "イベント数" in md
    assert "トークン構成" in md
    # 集計の軸ラジオが出ている＝データ経路まで到達。
    assert any(r.label == "集計軸" for r in at.radio)


def test_app_invalid_timezone_falls_back(tmp_path):
    """無効な TZ を入れてもエラー表示のうえ既定にフォールバックし、画面は描画される。"""
    db_path = _seed_db(tmp_path)
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.text_input(key="db_path").set_value(db_path).run()
    at.text_input(key="tz").set_value("Not/AZone").run()
    assert not at.exception
    assert any("無効なタイムゾーン" in e.value for e in at.error)
    # フォールバックして KPI まで到達する。
    assert any("イベント数" in m.value for m in at.markdown)


def test_app_shows_error_for_unreadable_db(tmp_path):
    # ディレクトリを DB パスに与えると sqlite が開けず st.error に落ちる。
    bad = tmp_path / "as_dir"
    bad.mkdir()
    at = AppTest.from_file(APP, default_timeout=30)
    at.run()
    at.text_input(key="db_path").set_value(str(bad)).run()
    assert not at.exception
    assert any("読み込めませんでした" in e.value for e in at.error)
