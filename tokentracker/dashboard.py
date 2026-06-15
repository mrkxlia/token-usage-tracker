"""ローカル Web ダッシュボード（Streamlit）。

起動:
  uv run --extra dashboard streamlit run tokentracker/dashboard.py \
      --server.address=127.0.0.1 --server.headless=true

外部送信なし・ローカル完結。SQLite(`~/.tokentracker/usage.db`)を読み、
リポジトリ/モデル/ツール/期間でフィルタして集計・推移を表示する。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

from tokentracker.queries import local_date_bucket

DEFAULT_DB = Path.home() / ".tokentracker" / "usage.db"


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

    mask = (
        df["repo_path"].isin(sel_repo)
        & df["model"].isin(sel_model)
        & df["source"].isin(sel_source)
    )
    if not include_sub:
        mask &= ~df["is_subagent"]
    f = df[mask]

    # サマリ指標
    known_cost = f.loc[f["cost_usd"].notna(), "cost_usd"].sum()
    unalloc = f.loc[f["cost_usd"].isna(), "total_tokens"].sum()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("判明コスト (USD)", f"${known_cost:,.2f}")
    c2.metric("総トークン", f"{int(f['total_tokens'].sum()):,}")
    c3.metric("未割当トークン", f"{int(unalloc):,}")
    c4.metric("イベント数", f"{len(f):,}")
    if unalloc:
        st.caption("※ 未割当トークン = 単価未登録モデル分。pricing.py に単価を追加すると判明コストへ反映。")

    # 日次推移
    st.subheader("日次トークン推移")
    daily = f.groupby("date")[["input_tokens", "output_tokens", "cache_read_tokens"]].sum()
    st.line_chart(daily)

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
    st.dataframe(agg, use_container_width=True)


if __name__ == "__main__":
    main()
