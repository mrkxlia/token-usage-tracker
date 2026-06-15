"""ローカル Web ダッシュボード（Streamlit）。

起動:
  uv run --extra dashboard streamlit run tokentracker/dashboard.py \
      --server.address=127.0.0.1 --server.headless=true

外部送信なし・ローカル完結。SQLite(`~/.tokentracker/usage.db`)を読み、
リポジトリ/モデル/ツール/期間でフィルタして集計・推移・派生指標を表示する。
サイドバーの「取り込み実行」からその場で ingest（増分）も実行できる。

デザイン: クリーンSaaS（ライト）。インディゴのアクセント1色、数値は等幅フォントで
「データ感」を出し、KPI 直下のトークン構成バーを唯一のシグネチャ要素とする。
ロジックは AppTest / ユニットテストしやすいよう純粋関数へ分割している。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from tokentracker import db
from tokentracker.ingest import INGESTORS, ingest_all
from tokentracker.pricing import default_pricebook
from tokentracker.queries import local_date_bucket

DEFAULT_DB = Path.home() / ".tokentracker" / "usage.db"

# --- デザイントークン --------------------------------------------------------
ACCENT = "#4F46E5"          # インディゴ（コスト/プライマリ）
INK = "#111827"             # 主テキスト
MUTED = "#6B7280"           # 補助テキスト
BORDER = "#E5E7EB"
# トークン構成バーは単一色相のトーナル・ランプで規律を保つ（凡例で識別）。
COMP_COLORS = {
    "input_tokens": "#4F46E5",
    "output_tokens": "#818CF8",
    "cache_read_tokens": "#C7D2FE",
}
COMP_LABELS = {
    "input_tokens": "入力",
    "output_tokens": "出力",
    "cache_read_tokens": "キャッシュ読",
}


# === データ層 ================================================================

def _load_df(db_path: str, tz: str) -> pd.DataFrame:
    """SQLite から DataFrame を組み立てる（キャッシュ無し・テスト用にも使う純関数）。"""
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


@st.cache_data(show_spinner=False)
def load(db_path: str, tz: str) -> pd.DataFrame:
    return _load_df(db_path, tz)


def run_ingest(db_path: str, sources: list[str] | None, *, full: bool = False) -> tuple[int, dict]:
    """その場で ingest を実行し ``(新規件数, ソース別処理件数)`` を返す。

    新規件数は取り込み前後の行数差。既定は増分（未変更ファイルはスキップ）。
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(db_path)
    try:
        before = db.count_events(conn)
        per_source = ingest_all(conn, sources=sources, incremental=not full)
        after = db.count_events(conn)
    finally:
        conn.close()
    return after - before, per_source


def filter_by_date_range(df: pd.DataFrame, start_s: str, end_s: str) -> pd.DataFrame:
    """tz 派生済みの文字列 date 列で期間を絞る（境界含む・None は除外）。純関数。"""
    m = df["date"].notna() & (df["date"] >= start_s) & (df["date"] <= end_s)
    return df[m]


def filter_df(
    df: pd.DataFrame, *, repos, models, sources, include_sub: bool, since=None, until=None
) -> pd.DataFrame:
    """サイドバーの全条件を適用する（リポジトリ/モデル/ツール/サブエージェント/期間）。"""
    mask = (
        df["repo_path"].isin(repos)
        & df["model"].isin(models)
        & df["source"].isin(sources)
    )
    if not include_sub:
        mask &= ~df["is_subagent"]
    # date は None を含みうる（timestamp 空のイベント）。期間指定時は None 行を除外して比較する。
    if since is not None:
        mask &= df["date"].notna() & (df["date"] >= str(since))
    if until is not None:
        mask &= df["date"].notna() & (df["date"] <= str(until))
    return df[mask]


def compute_kpis(df: pd.DataFrame) -> dict:
    """KPI とトークン構成（入力/出力/キャッシュ読）を集計する。"""
    known_cost = float(df.loc[df["cost_usd"].notna(), "cost_usd"].sum())
    unalloc = int(df.loc[df["cost_usd"].isna(), "total_tokens"].sum())
    comp = {k: int(df[k].sum()) for k in COMP_COLORS}
    return {
        "known_cost": known_cost,
        "total_tokens": int(df["total_tokens"].sum()),
        "unalloc": unalloc,
        "events": int(len(df)),
        "composition": comp,
    }


def add_ratio_columns(agg: pd.DataFrame) -> pd.DataFrame:
    """集計済み DataFrame に派生指標列を付与する（純関数・Streamlit 非依存）。

    定義は queries._finalize_ratios と同一。ゼロ分母は NaN（表示層で "-"）。
    """
    inp, cr, ev = agg["input_tokens"], agg["cache_read_tokens"], agg["events"]
    agg["out/in"] = (agg["output_tokens"] / inp).where(inp > 0)
    agg["cache_hit"] = (cr / (inp + cr)).where((inp + cr) > 0)
    agg["cost_per_event"] = (agg["known_cost_usd"] / ev).where(ev > 0)
    return agg


def aggregate(df: pd.DataFrame, dim: str) -> pd.DataFrame:
    return (
        df.groupby(dim)
        .agg(
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
            cache_read_tokens=("cache_read_tokens", "sum"),
            known_cost_usd=("cost_usd", "sum"),
            events=("message_id", "count"),
        )
        .sort_values("input_tokens", ascending=False)
    )


def cache_savings_total(df: pd.DataFrame) -> float:
    """フィルタ後データのキャッシュ読取による推定節約額合計（単価判明モデルのみ）。"""
    if df.empty:
        return 0.0
    book = default_pricebook()
    total = 0.0
    for model, cr in zip(df["model"], df["cache_read_tokens"]):
        if not cr or model is None:
            continue
        s = book.cache_savings(model, int(cr))
        if s is not None:
            total += s
    return total


def build_daily_chart(df: pd.DataFrame) -> alt.Chart:
    """日次のトークン推移（積み上げエリア、アクセント配色に統一）。"""
    daily = (
        df.groupby("date")[list(COMP_COLORS)].sum().reset_index()
        .melt("date", var_name="種別", value_name="トークン")
    )
    daily["種別"] = daily["種別"].map(COMP_LABELS)
    order = [COMP_LABELS[k] for k in COMP_COLORS]
    return (
        alt.Chart(daily)
        .mark_area(opacity=0.9)
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%m/%d", labelColor=MUTED, grid=False)),
            y=alt.Y("トークン:Q", title=None, stack=True, axis=alt.Axis(labelColor=MUTED, grid=True, gridColor=BORDER)),
            color=alt.Color(
                "種別:N",
                scale=alt.Scale(domain=order, range=[COMP_COLORS[k] for k in COMP_COLORS]),
                legend=alt.Legend(orient="top", title=None, labelColor=INK),
            ),
            order=alt.Order("種別:N", sort="ascending"),
            tooltip=["date:T", "種別:N", alt.Tooltip("トークン:Q", format=",")],
        )
        .properties(height=280)
        .configure_view(strokeWidth=0)
    )


def build_daily_cost_chart(df: pd.DataFrame) -> alt.Chart:
    """日次の判明コスト推移（アクセント色のライン）。"""
    daily = df.groupby("date")["cost_usd"].sum().reset_index()
    return (
        alt.Chart(daily)
        .mark_line(point=True, color=ACCENT)
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%m/%d", labelColor=MUTED, grid=False)),
            y=alt.Y("cost_usd:Q", title=None, axis=alt.Axis(labelColor=MUTED, grid=True, gridColor=BORDER, format="$,.2f")),
            tooltip=["date:T", alt.Tooltip("cost_usd:Q", format="$,.4f")],
        )
        .properties(height=220)
        .configure_view(strokeWidth=0)
    )


# === 表示層（HTML/CSS） ======================================================

def _inject_theme() -> None:
    st.markdown(
        f"""
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap');
        html, body, [class*="css"] {{ font-family: 'Inter', system-ui, sans-serif; }}
        .stApp {{ background: #F7F8FA; }}
        .ttk-title {{ font-size: 1.55rem; font-weight: 700; color: {INK}; letter-spacing: -0.02em; margin: 0 0 .15rem; }}
        .ttk-sub {{ color: {MUTED}; font-size: .9rem; margin-bottom: 1.1rem; }}
        .ttk-cards {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; }}
        @media (max-width: 900px) {{ .ttk-cards {{ grid-template-columns: repeat(2, 1fr); }} }}
        .ttk-card {{ background: #fff; border: 1px solid {BORDER}; border-radius: 14px; padding: 16px 18px; }}
        .ttk-card .lab {{ color: {MUTED}; font-size: .76rem; font-weight: 500; text-transform: uppercase; letter-spacing: .04em; }}
        .ttk-card .val {{ font-family: 'IBM Plex Mono', monospace; font-size: 1.55rem; font-weight: 600; color: {INK}; margin-top: 6px; line-height: 1.1; }}
        .ttk-card.accent .val {{ color: {ACCENT}; }}
        .ttk-card .val small {{ font-size: .9rem; color: {MUTED}; font-weight: 500; }}
        .ttk-bar-wrap {{ background:#fff; border:1px solid {BORDER}; border-radius:14px; padding:16px 18px; margin-top:14px; }}
        .ttk-bar-head {{ display:flex; justify-content:space-between; color:{MUTED}; font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; margin-bottom:10px; }}
        .ttk-bar {{ display:flex; height:12px; border-radius:7px; overflow:hidden; background:{BORDER}; }}
        .ttk-bar span {{ display:block; height:100%; }}
        .ttk-legend {{ display:flex; gap:18px; margin-top:12px; flex-wrap:wrap; }}
        .ttk-legend .it {{ display:flex; align-items:center; gap:7px; color:{INK}; font-size:.82rem; }}
        .ttk-legend .dot {{ width:10px; height:10px; border-radius:3px; }}
        .ttk-legend .num {{ font-family:'IBM Plex Mono', monospace; color:{MUTED}; }}
        section[data-testid="stSidebar"] {{ background:#fff; border-right:1px solid {BORDER}; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_kpis(k: dict, savings: float) -> None:
    def fmt(n: int) -> str:
        return f"{n:,}"

    cards = f"""
    <div class="ttk-cards">
      <div class="ttk-card accent"><div class="lab">判明コスト</div><div class="val"><small>$</small>{k['known_cost']:,.2f}</div></div>
      <div class="ttk-card"><div class="lab">総トークン</div><div class="val">{fmt(k['total_tokens'])}</div></div>
      <div class="ttk-card"><div class="lab">未割当トークン</div><div class="val">{fmt(k['unalloc'])}</div></div>
      <div class="ttk-card"><div class="lab">イベント数</div><div class="val">{fmt(k['events'])}</div></div>
      <div class="ttk-card"><div class="lab">キャッシュ節約額(推定)</div><div class="val"><small>$</small>{savings:,.2f}</div></div>
    </div>
    """
    st.markdown(cards, unsafe_allow_html=True)
    _render_composition_bar(k["composition"])


def _render_composition_bar(comp: dict) -> None:
    total = sum(comp.values()) or 1
    segs = "".join(
        f'<span style="width:{comp[key] / total * 100:.3f}%;background:{COMP_COLORS[key]}"></span>'
        for key in COMP_COLORS
    )
    legend = "".join(
        f'<div class="it"><span class="dot" style="background:{COMP_COLORS[key]}"></span>'
        f'{COMP_LABELS[key]} <span class="num">{comp[key]:,}</span></div>'
        for key in COMP_COLORS
    )
    st.markdown(
        f"""
        <div class="ttk-bar-wrap">
          <div class="ttk-bar-head"><span>トークン構成</span><span>{total:,} tok</span></div>
          <div class="ttk-bar">{segs}</div>
          <div class="ttk-legend">{legend}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# === サイドバー =============================================================

def _sidebar_ingest(db_path: str) -> None:
    st.sidebar.subheader("ログを取り込む")
    src = st.sidebar.multiselect(
        "ソース", list(INGESTORS), default=list(INGESTORS), key="ingest_sources",
        help="既定の保存場所（~/.claude, ~/.codex, VS Code）を走査します。",
    )
    full = st.sidebar.checkbox(
        "全件を再スキャン", value=False, key="ingest_full",
        help="単価表を更新したとき等に既存ログのコストを再計算。既定は新規/変更分のみ（増分）。",
    )
    if st.sidebar.button("取り込み実行", type="primary", key="ingest_run", use_container_width=True):
        try:
            with st.spinner("取り込み中…"):
                added, per_source = run_ingest(db_path, src or None, full=full)
            load.clear()
            detail = " / ".join(f"{s}:{n}" for s, n in per_source.items())
            st.sidebar.success(f"新規 {added} 件を追加（処理 {detail}）")
            st.rerun()
        except Exception as exc:  # 取り込み失敗は原因を出して操作を促す
            st.sidebar.error(f"取り込みに失敗しました: {exc}")


def _empty_state(db_path: str) -> None:
    st.markdown(
        f"""
        <div class="ttk-bar-wrap" style="text-align:center;padding:40px 18px;">
          <div style="font-size:1.1rem;font-weight:600;color:{INK};">まだデータがありません</div>
          <div style="color:{MUTED};margin-top:8px;">
            左サイドバーの「取り込み実行」で、ローカルのログ（Claude Code / Codex / Cline）を読み込みます。
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# === メイン =================================================================

def main() -> None:
    st.set_page_config(page_title="Token Usage Tracker", layout="wide")
    _inject_theme()
    st.markdown('<div class="ttk-title">AIエージェント トークン消費トラッカー</div>', unsafe_allow_html=True)
    st.markdown('<div class="ttk-sub">ローカル完結 · どのリポジトリ / モデル / ツールがトークンとコストを消費したかを可視化</div>', unsafe_allow_html=True)

    db_path = st.sidebar.text_input("DB パス", str(DEFAULT_DB), key="db_path")
    tz = st.sidebar.text_input("タイムゾーン", "Asia/Tokyo", key="tz")
    st.sidebar.divider()
    _sidebar_ingest(db_path)
    st.sidebar.divider()

    if not Path(db_path).exists():
        _empty_state(db_path)
        return

    try:
        df = load(db_path, tz)
    except Exception as exc:
        st.error(f"DB を読み込めませんでした: {exc}")
        return
    if df.empty:
        _empty_state(db_path)
        return

    # フィルタ
    with st.sidebar:
        st.subheader("フィルタ")
        repos = sorted(df["repo_path"].dropna().unique())
        models = sorted(df["model"].dropna().unique())
        sources = sorted(df["source"].dropna().unique())
        sel_repo = st.multiselect("リポジトリ", repos, default=repos, key="f_repo")
        sel_model = st.multiselect("モデル", models, default=models, key="f_model")
        sel_source = st.multiselect("ツール", sources, default=sources, key="f_source")
        include_sub = st.checkbox("サブエージェントを含む", value=True, key="f_sub")
        days = sorted(d for d in df["date"].dropna().unique())
        since = until = None
        if days:
            lo, hi = days[0], days[-1]
            rng = st.date_input(
                "期間",
                value=(pd.to_datetime(lo).date(), pd.to_datetime(hi).date()),
                key="f_range",
            )
            if isinstance(rng, (tuple, list)) and len(rng) == 2:
                since, until = rng
        else:
            st.caption("※ タイムスタンプが無いため期間フィルタは利用できません。")

    f = filter_df(
        df, repos=sel_repo, models=sel_model, sources=sel_source,
        include_sub=include_sub, since=since, until=until,
    )

    if f.empty:
        st.info("選択した条件に一致するデータがありません。フィルタを広げてください。")
        return

    k = compute_kpis(f)
    savings = cache_savings_total(f)
    _render_kpis(k, savings)
    if k["unalloc"]:
        st.caption("未割当トークン = 単価未登録モデル分。pricing.toml に単価を追加し「全件を再スキャン」で判明コストへ反映されます。")
    st.caption("キャッシュ節約額(推定) = cache_read を都度フル input 単価で払った場合との差分（読取分のみの近似、単価判明モデルのみ）。")

    # エクスポート（フィルタ後の生データ）
    e1, e2 = st.columns(2)
    e1.download_button("CSV ダウンロード", f.to_csv(index=False), "usage.csv", "text/csv")
    e2.download_button("JSON ダウンロード", f.to_json(orient="records", force_ascii=False),
                       "usage.json", "application/json")

    st.subheader("日次トークン推移")
    st.altair_chart(build_daily_chart(f), use_container_width=True)
    st.subheader("日次コスト推移 (USD)")
    st.altair_chart(build_daily_cost_chart(f), use_container_width=True)

    st.subheader("集計")
    dim = st.radio("集計軸", ["repo_path", "model", "source", "session_id"], horizontal=True, key="agg_dim")
    st.dataframe(add_ratio_columns(aggregate(f, dim)), use_container_width=True)

    # コスト上位セッション（コスト削減で最も注視すべき対象）
    st.subheader("コスト上位セッション")
    top = add_ratio_columns(aggregate(f, "session_id")).sort_values(
        "known_cost_usd", ascending=False
    ).head(15)
    st.dataframe(top, use_container_width=True)

    # リポジトリ × モデルのトークン内訳（高価なモデルの使いどころを俯瞰）
    st.subheader("リポジトリ × モデル トークン内訳")
    pivot = f.pivot_table(
        index="repo_path", columns="model", values="total_tokens", aggfunc="sum",
    )
    st.dataframe(pivot, use_container_width=True)


if __name__ == "__main__":
    main()
