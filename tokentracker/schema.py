"""DB スキーマの機械可読定義と分析レシピ（M4: 最適化エージェント向け）。

将来、AI エージェントが SQLite(`usage_event`) を読んでモデル/コスト最適化を
判断するための **単一の真実源**。各カラムの意味・単位・nullable、集計規約、
単価モデル、そして既製の分析 SQL（recipes）をここに集約する。

人間可読版は `docs/db_schema.md`。`tokentracker schema [--json|--recipes]` で出力できる。
"""

from __future__ import annotations

# 比率の定義文字列は queries._finalize_ratios / CLI / docs と同一表現で統一する。
CACHE_HIT_RATIO_DEF = (
    "cache_read_tokens / (input_tokens + cache_read_tokens) "
    "（キャッシュ書込は一時コストのため分母に含めない。値域 [0,1]）"
)
OUTPUT_INPUT_RATIO_DEF = "output_tokens / input_tokens（output は reasoning を畳んだ値）"

SCHEMA_DEFINITION: dict = {
    "version": "1",
    "tables": {
        "usage_event": {
            "grain": "1 API 応答 = 1 行。冪等キー UNIQUE(source, message_id)。",
            "columns": [
                {"name": "id", "type": "INTEGER", "nullable": False,
                 "semantics": "代理主キー（AUTOINCREMENT）。分析では使わない。"},
                {"name": "source", "type": "TEXT", "nullable": False,
                 "semantics": "ツール（エージェント）種別: claude_code | codex | cline。"
                              "サブエージェント識別子 agent_id とは別概念。"},
                {"name": "message_id", "type": "TEXT", "nullable": False,
                 "semantics": "ソース内で一意な API 応答 ID（冪等キー）。"},
                {"name": "session_id", "type": "TEXT", "nullable": True,
                 "semantics": "タスク/会話セッション ID。コスト帰属の主単位。"},
                {"name": "agent_id", "type": "TEXT", "nullable": True,
                 "semantics": "サブエージェント識別子（is_subagent=1 の行で有効）。source とは別軸。"},
                {"name": "request_id", "type": "TEXT", "nullable": True,
                 "semantics": "リクエスト追跡 ID（任意）。"},
                {"name": "timestamp_utc", "type": "TEXT", "nullable": True,
                 "unit": "ISO8601(UTC)",
                 "semantics": "UTC 保存。日次バケットは設定 TZ（既定 Asia/Tokyo）でローカル日付に切る。"},
                {"name": "repo_path", "type": "TEXT", "nullable": True,
                 "semantics": "実行時の作業ディレクトリ（リポジトリ）。"},
                {"name": "git_branch", "type": "TEXT", "nullable": True,
                 "semantics": "git ブランチ（任意）。"},
                {"name": "model", "type": "TEXT", "nullable": True,
                 "semantics": "モデル ID（例 claude-opus-4-8, gpt-5）。末尾日付サフィックスは単価解決時に除去。"},
                {"name": "input_tokens", "type": "INTEGER", "nullable": False, "unit": "tokens",
                 "semantics": "キャッシュ未ヒットの入力（プロンプト）トークン。"},
                {"name": "output_tokens", "type": "INTEGER", "nullable": False, "unit": "tokens",
                 "semantics": "応答トークン（reasoning は別列。表示・コストでは output に畳む）。"},
                {"name": "reasoning_output_tokens", "type": "INTEGER", "nullable": False, "unit": "tokens",
                 "semantics": "推論トークン。表示上 output_tokens に畳み、コストは output 単価で計上。"},
                {"name": "cache_creation_tokens", "type": "INTEGER", "nullable": False, "unit": "tokens",
                 "semantics": "キャッシュ書込トークン合計。単価計算では 1h/5m に分割した列を使う。"},
                {"name": "cache_creation_1h_tokens", "type": "INTEGER", "nullable": False, "unit": "tokens",
                 "semantics": "TTL 1h のキャッシュ書込（単価計算用の内訳）。"},
                {"name": "cache_creation_5m_tokens", "type": "INTEGER", "nullable": False, "unit": "tokens",
                 "semantics": "TTL 5m のキャッシュ書込（単価計算用の内訳）。"},
                {"name": "cache_read_tokens", "type": "INTEGER", "nullable": False, "unit": "tokens",
                 "semantics": "キャッシュから供給された入力トークン（割引対象）。"},
                {"name": "web_search_requests", "type": "INTEGER", "nullable": False, "unit": "count",
                 "semantics": "server tool use の web 検索件数。別建て従量のため現状コスト計算外。"},
                {"name": "web_fetch_requests", "type": "INTEGER", "nullable": False, "unit": "count",
                 "semantics": "server tool use の web 取得件数。現状コスト計算外。"},
                {"name": "cost_usd", "type": "REAL", "nullable": True, "unit": "USD",
                 "semantics": "ingest 時に pricing.toml から算出。NULL = 単価未登録モデル = 未割当（0 円扱いにしない）。"},
                {"name": "is_subagent", "type": "INTEGER", "nullable": False, "unit": "bool(0/1)",
                 "semantics": "1 = サブエージェントのターン。--exclude-subagents で集計から除外可能。"},
            ],
        },
        "prompt": {
            "grain": "会話テキストの保管（分析では補助的、コスト集計には未使用）。",
            "columns": [
                {"name": "id", "type": "INTEGER", "nullable": False, "semantics": "主キー。"},
                {"name": "source", "type": "TEXT", "nullable": False, "semantics": "ツール種別。"},
                {"name": "session_id", "type": "TEXT", "nullable": True, "semantics": "セッション ID。"},
                {"name": "role", "type": "TEXT", "nullable": True, "semantics": "user/assistant など。"},
                {"name": "text", "type": "TEXT", "nullable": True, "semantics": "本文。"},
                {"name": "timestamp_utc", "type": "TEXT", "nullable": True, "semantics": "UTC タイムスタンプ。"},
            ],
        },
        "ingest_state": {
            "grain": "ファイル単位の取り込み状態（増分取り込み用、分析対象外）。",
            "columns": [
                {"name": "file_path", "type": "TEXT", "nullable": False, "semantics": "ログファイルパス（主キー）。"},
                {"name": "size", "type": "INTEGER", "nullable": True, "semantics": "前回サイズ。"},
                {"name": "mtime", "type": "REAL", "nullable": True, "semantics": "前回更新時刻。"},
                {"name": "last_message_id", "type": "TEXT", "nullable": True, "semantics": "最後に取り込んだ message_id。"},
            ],
        },
    },
    "conventions": {
        "timezone": "timestamp_utc は UTC。日次は設定 TZ（既定 Asia/Tokyo）のローカル日付で切る。",
        "reasoning": "reasoning_output_tokens は表示上 output に畳み、コストは output 単価で計上。",
        "unallocated": "cost_usd IS NULL のモデルはトークンを未割当として別計上し、0 円扱いにしない。",
        "agent_terms": "source = ツール種別(claude_code/codex/cline)。agent_id/is_subagent = サブエージェント識別。両者は別軸。",
        "ratios": {
            "output_input_ratio": OUTPUT_INPUT_RATIO_DEF,
            "cache_hit_ratio": CACHE_HIT_RATIO_DEF,
            "cost_per_event": "known_cost_usd / events",
        },
    },
    "pricing_model": {
        "unit": "USD per 1M tokens",
        "rate_keys": ["input", "output", "cache_write_1h", "cache_write_5m", "cache_read"],
        "source": "pricing.toml（env TOKENTRACKER_PRICING > ~/.tokentracker > 同梱）",
        "note": "GPT 系は二次情報。Azure Foundry の実課金レートで検証・上書きすること。",
    },
    "recipes": {
        "cost_by_model": {
            "description": "モデル別の判明コストとトークン・件数（高コスト順）。最適化の起点。",
            "sql": (
                "SELECT model, SUM(cost_usd) AS cost, "
                "SUM(input_tokens+output_tokens+reasoning_output_tokens"
                "+cache_creation_tokens+cache_read_tokens) AS tokens, COUNT(*) AS n "
                "FROM usage_event WHERE cost_usd IS NOT NULL "
                "GROUP BY model ORDER BY cost DESC"
            ),
        },
        "unpriced_models": {
            "description": "単価未登録（cost_usd IS NULL）のモデル。pricing.toml 追記候補。",
            "sql": (
                "SELECT model, SUM(input_tokens+output_tokens+reasoning_output_tokens"
                "+cache_creation_tokens+cache_read_tokens) AS unallocated_tokens, COUNT(*) AS n "
                "FROM usage_event WHERE cost_usd IS NULL GROUP BY model "
                "ORDER BY unallocated_tokens DESC"
            ),
        },
        "cache_efficiency_by_session": {
            "description": "セッション別キャッシュ命中率（低い順）。キャッシュ再利用が弱いセッションを抽出。",
            "sql": (
                "SELECT session_id, SUM(cache_read_tokens) AS cr, SUM(input_tokens) AS inp, "
                "1.0*SUM(cache_read_tokens)/NULLIF(SUM(input_tokens)+SUM(cache_read_tokens),0) "
                "AS cache_hit_ratio FROM usage_event GROUP BY session_id "
                "ORDER BY cache_hit_ratio ASC"
            ),
        },
        "top_sessions_by_cost": {
            "description": "コスト上位セッション。削減効果の大きい対象。",
            "sql": (
                "SELECT session_id, SUM(cost_usd) AS cost, COUNT(*) AS n "
                "FROM usage_event WHERE cost_usd IS NOT NULL "
                "GROUP BY session_id ORDER BY cost DESC LIMIT 20"
            ),
        },
        "subagent_share": {
            "description": "メイン/サブエージェント別のコスト・トークン内訳。",
            "sql": (
                "SELECT is_subagent, SUM(cost_usd) AS cost, "
                "SUM(input_tokens+output_tokens+reasoning_output_tokens"
                "+cache_creation_tokens+cache_read_tokens) AS tokens, COUNT(*) AS n "
                "FROM usage_event GROUP BY is_subagent"
            ),
        },
        "expensive_model_on_simple_work": {
            "description": (
                "高価モデルを使いつつ 1 件あたりトークンが小さく out/in 比も低い "
                "セッション。『安いモデルで十分では?』のヒューリスティック抽出（断定ではない。"
                "cost_by_model と突き合わせて判断する）。"
            ),
            "sql": (
                "SELECT model, session_id, COUNT(*) AS n, "
                "1.0*SUM(input_tokens+output_tokens)/COUNT(*) AS tokens_per_event, "
                "1.0*SUM(output_tokens)/NULLIF(SUM(input_tokens),0) AS output_input_ratio, "
                "SUM(cost_usd) AS cost "
                "FROM usage_event WHERE cost_usd IS NOT NULL "
                "GROUP BY model, session_id "
                "ORDER BY cost DESC"
            ),
        },
    },
    "notes": {
        "daily_buckets": (
            "日次集計は TZ 依存で Python 側で切る。TZ 正確な日次が必要なら生 SQL ではなく "
            "`tokentracker daily --json` を使うこと。"
        ),
        "full_scan": "queries/dashboard は全件読み。大規模 DB では idx_usage_ts での枝刈りが将来課題。",
    },
}
