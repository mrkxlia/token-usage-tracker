"""ローカル Web ダッシュボード（Streamlit）。

起動:
  uv run --extra dashboard streamlit run tokentracker/dashboard.py \
      --server.address=127.0.0.1 --server.headless=true

外部送信なし・ローカル完結。SQLite(`~/.tokentracker/usage.db`)を読み、
リポジトリ/モデル/ツール/期間でフィルタして集計・推移を表示する。
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from tokentracker.pricing import default_pricebook
from tokentracker.queries import local_date_bucket

DEFAULT_DB = Path.home() / ".tokentracker" / "usage.db"


def add_ratio_columns(agg: pd.DataFrame) -> pd.DataFrame:
    """集計済み DataFrame に派生指標列を付与する（純関数・Streamlit 非依存）。

    定義は queries._finalize_ratios と同一。ゼロ分母は NaN（表示層で "-"）。
    """
    inp, cr, ev = agg["input_tokens"], agg["cache_read_tokens"], agg["events"]
    agg["out/in"] = (agg["output_tokens"] / inp).where(inp > 0)
    agg["cache_hit"] = (cr / (inp + cr)).where((inp + cr) > 0)
    agg["cost_per_event"] = (agg["known_cost_usd"] / ev).where(ev > 0)
    return agg


def filter_by_date_range(df: pd.DataFrame, start_s: str, end_s: str) -> pd.DataFrame:
    """tz 派生済みの文字列 date 列で期間を絞る（境界含む・None は除外）。純関数。"""
    m = df["date"].notna() & (df["date"] >= start_s) & (df["date"] <= end_s)
    return df[m]


@st.cache_data(show_spinner=False)
def load(db_path: str, tz: str) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query("SELECT * FROM usage_event", conn)
    finally:
        conn.close()
    if df.empty:
        return df
    df["date"] = df["timestamp_utc"].apply(
        lambda t: local_date_bucket(t, tz) if t else None
    )
    df["total_tokens"] = (
        df["input_tokens"] + df["output_tokens"] + df["reasoning_output_tokens"]
        + df["cache_creation_tokens"] + df["cache_read_tokens"]
    )
    df["is_subagent"] = df["is_subagent"].astype(bool)
    return df


def main() -> None:
    st.set_page_config(page_title="Token Usage Tracker", layout="wide")
    st.title("AIエージェント トークン消費トラッカー")

    db_path = st.sidebar.text_input("DB パス", str(DEFAULT_DB))
    tz = st.sidebar.text_input("タイムゾーン", "Asia/Tokyo")
    if not Path(db_path).exists():
        st.warning(f"DB が見つかりません: {db_path}\n先に `tokentracker ingest` を実行してください。")
        return

    df = load(db_path, tz)
    if df.empty:
        st.info("データがありません。`tokentracker ingest` を実行してください。")
        return

    # フィルタ
    with st.sidebar:
        st.header("フィルタ")
        repos = sorted(df["repo_path"].dropna().unique())
        models = sorted(df["model"].dropna().unique())
        sources = sorted(df["source"].dropna().unique())
        sel_repo = st.multiselect("リポジトリ", repos, default=repos)
        sel_model = st.multiselect("モデル", models, default=models)
        sel_source = st.multiselect("ツール", sources, default=sources)
        include_sub = st.checkbox("サブエージェントを含む", value=True)

        # 期間フィルタ: date 列は tz 派生済みの YYYY-MM-DD 文字列（辞書順比較が正しい）。
        valid_dates = df["date"].dropna()
        date_range = None
        if valid_dates.empty:
            st.caption("※ タイムスタンプが無いため期間フィルタは利用できません。")
        else:
            min_d = date.fromisoformat(valid_dates.min())
            max_d = date.fromisoformat(valid_dates.max())
            sel = st.date_input(
                "期間", value=(min_d, max_d), min_value=min_d, max_value=max_d,
            )
            # 選択途中は 1 要素タプルが返るため長さをガード。
            if isinstance(sel, (tuple, list)):
                start, end = (sel[0], sel[1]) if len(sel) == 2 else (sel[0], sel[0])
            else:
                start = end = sel
            date_range = (start.isoformat(), end.isoformat())

    mask = (
        df["repo_path"].isin(sel_repo)
        & df["model"].isin(sel_model)
        & df["source"].isin(sel_source)
    )
    if not include_sub:
        mask &= ~df["is_subagent"]
    if date_range is not None:
        start_s, end_s = date_range
        mask &= df["date"].notna() & (df["date"] >= start_s) & (df["date"] <= end_s)
    f = df[mask]

    # サマリ指標
    known_cost = f.loc[f["cost_usd"].notna(), "cost_usd"].sum()
    unalloc = f.loc[f["cost_usd"].isna(), "total_tokens"].sum()
    savings = _cache_savings_total(f)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("判明コスト (USD)", f"${known_cost:,.2f}")
    c2.metric("総トークン", f"{int(f['total_tokens'].sum()):,}")
    c3.metric("未割当トークン", f"{int(unalloc):,}")
    c4.metric("イベント数", f"{len(f):,}")
    c5.metric("キャッシュ節約額(推定)", f"${savings:,.2f}")
    if unalloc:
        st.caption("※ 未割当トークン = 単価未登録モデル分。pricing.py に単価を追加すると判明コストへ反映。")
    st.caption("※ キャッシュ節約額 = cache_read を都度フル input 単価で払った場合との差分（読取分のみの近似、単価判明モデルのみ）。")

    # エクスポート（フィルタ後の生データ）
    e1, e2 = st.columns(2)
    e1.download_button("CSV ダウンロード", f.to_csv(index=False), "usage.csv", "text/csv")
    e2.download_button("JSON ダウンロード", f.to_json(orient="records", force_ascii=False),
                       "usage.json", "application/json")

    # 日次推移（トークン／コスト）
    st.subheader("日次トークン推移")
    daily = f.groupby("date")[["input_tokens", "output_tokens", "cache_read_tokens"]].sum()
    st.line_chart(daily)
    st.subheader("日次コスト推移 (USD)")
    daily_cost = f.groupby("date")["cost_usd"].sum()
    st.line_chart(daily_cost)

    # 軸別集計
    st.subheader("集計")
    dim = st.radio("集計軸", ["repo_path", "model", "source", "session_id"], horizontal=True)
    agg = (
        f.groupby(dim)
        .agg(
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
            cache_read_tokens=("cache_read_tokens", "sum"),
            known_cost_usd=("cost_usd", "sum"),
            events=("message_id", "count"),
        )
        .sort_values("input_tokens", ascending=False)
    )
    agg = add_ratio_columns(agg)
    st.dataframe(agg, use_container_width=True)

    # コスト上位セッション（コスト削減で最も注視すべき対象）
    st.subheader("コスト上位セッション")
    top = (
        f.groupby("session_id")
        .agg(
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
            cache_read_tokens=("cache_read_tokens", "sum"),
            known_cost_usd=("cost_usd", "sum"),
            events=("message_id", "count"),
        )
    )
    top = add_ratio_columns(top).sort_values("known_cost_usd", ascending=False).head(15)
    st.dataframe(top, use_container_width=True)

    # リポジトリ × モデルのトークン内訳（高価なモデルの使いどころを俯瞰）
    st.subheader("リポジトリ × モデル トークン内訳")
    pivot = f.pivot_table(
        index="repo_path", columns="model", values="total_tokens", aggfunc="sum",
    )
    st.dataframe(pivot, use_container_width=True)


def _cache_savings_total(f: pd.DataFrame) -> float:
    """フィルタ後データのキャッシュ節約額合計（単価判明モデルのみ）。"""
    if f.empty:
        return 0.0
    book = default_pricebook()
    total = 0.0
    for model, cr in zip(f["model"], f["cache_read_tokens"]):
        if not cr or model is None:
            continue
        s = book.cache_savings(model, int(cr))
        if s is not None:
            total += s
    return total


if __name__ == "__main__":
    main()
