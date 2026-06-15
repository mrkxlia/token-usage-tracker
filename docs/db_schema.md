# DB スキーマ定義（最適化エージェント向け）

`token-usage-tracker` の SQLite DB（既定 `~/.tokentracker/usage.db`）の定義リファレンス。
将来（M4）、AI エージェントがこの DB を読んで **モデル/コスト最適化** を判断するための
「機械可読な仕様＋分析クエリ集」です。

- 機械可読版（JSON）: `tokentracker schema --json`
- 分析レシピ一覧: `tokentracker schema --recipes`
- このドキュメントと `tokentracker/schema.py`（`SCHEMA_DEFINITION`）が **単一の真実源**で、
  両者の整合は `tests/test_schema.py::test_schema_definition_covers_all_columns` で担保。

## 最適化エージェントの想定フロー
1. `tokentracker schema --json` でスキーマと規約・単価モデルを取得。
2. レシピ（下記）を実 DB に対して実行し、`cost_by_model` / `top_sessions_by_cost` /
   `cache_efficiency_by_session` などで「どこに金がかかっているか」を把握。
3. 必要なら個別セッション（`session_id`）やサブエージェント（`agent_id`/`is_subagent`）の
   行を直接読み、詳細を確認。
4. `expensive_model_on_simple_work` 等のヒューリスティックで「安いモデルで十分そうな」
   セッションを抽出し、`cost_by_model` と突き合わせて代替を提案。

## テーブル: `usage_event`
粒度: **1 API 応答 = 1 行**。冪等キー `UNIQUE(source, message_id)`。

| column | type | null | 意味 |
|--------|------|------|------|
| id | INTEGER | NOT NULL | 代理主キー（分析では使わない） |
| source | TEXT | NOT NULL | ツール種別: `claude_code`/`codex`/`cline`（`agent_id` とは別概念） |
| message_id | TEXT | NOT NULL | ソース内で一意な応答 ID（冪等キー） |
| session_id | TEXT | | タスク/会話セッション ID。**コスト帰属の主単位** |
| agent_id | TEXT | | サブエージェント識別子（`is_subagent=1` で有効） |
| request_id | TEXT | | リクエスト追跡 ID（任意） |
| timestamp_utc | TEXT | | ISO8601(UTC)。日次は設定 TZ でローカル日付に切る |
| repo_path | TEXT | | 実行時 cwd（リポジトリ） |
| git_branch | TEXT | | git ブランチ（任意） |
| model | TEXT | | モデル ID（末尾日付サフィックスは単価解決時に除去） |
| input_tokens | INTEGER | NOT NULL | キャッシュ未ヒットの入力トークン |
| output_tokens | INTEGER | NOT NULL | 応答トークン（reasoning は別列、表示・コストでは畳む） |
| reasoning_output_tokens | INTEGER | NOT NULL | 推論トークン。**output 単価**で計上 |
| cache_creation_tokens | INTEGER | NOT NULL | キャッシュ書込合計（単価計算は 1h/5m 内訳を使用） |
| cache_creation_1h_tokens | INTEGER | NOT NULL | TTL 1h 書込（単価内訳） |
| cache_creation_5m_tokens | INTEGER | NOT NULL | TTL 5m 書込（単価内訳） |
| cache_read_tokens | INTEGER | NOT NULL | キャッシュ供給の入力トークン（割引対象） |
| web_search_requests | INTEGER | NOT NULL | web 検索件数（別建て従量、現状コスト計算外） |
| web_fetch_requests | INTEGER | NOT NULL | web 取得件数（現状コスト計算外） |
| cost_usd | REAL | | ingest 時に算出。**NULL = 単価未登録 = 未割当**（0 円扱いにしない） |
| is_subagent | INTEGER | NOT NULL | 1 = サブエージェントのターン |

補助テーブル `prompt`（会話テキスト保管、コスト集計には未使用）と `ingest_state`
（増分取り込み状態、分析対象外）も `schema --json` に含まれる。

## 集計規約
- **タイムゾーン**: `timestamp_utc` は UTC。日次は設定 TZ（既定 Asia/Tokyo）のローカル日付で切る。
- **reasoning**: `reasoning_output_tokens` は表示上 output に畳み、コストは output 単価。
- **未割当**: `cost_usd IS NULL` のモデルはトークンを未割当として別計上（0 円化しない）。
- **用語**: `source`=ツール種別、`agent_id`/`is_subagent`=サブエージェント識別。**別軸**。
- **派生指標**（ゼロ除算は未定義）:
  - `output_input_ratio` = `output_tokens / input_tokens`
  - `cache_hit_ratio` = `cache_read_tokens / (input_tokens + cache_read_tokens)`
    （キャッシュ書込は一時コストのため分母に含めない。値域 [0,1]）
  - `cost_per_event` = `known_cost_usd / events`

## 単価モデル
- 単位: **USD / 1M tokens**。キー: `input` / `output` / `cache_write_1h` / `cache_write_5m` / `cache_read`。
- 読込優先順位: `TOKENTRACKER_PRICING` > `~/.tokentracker/pricing.toml` > 同梱 `tokentracker/pricing.toml`。
- ⚠️ GPT 系は二次情報。**Azure Foundry の実課金レートで検証・上書き**すること。

## 分析レシピ（`tokentracker schema --recipes` で SQL を出力）
| name | 用途 |
|------|------|
| `cost_by_model` | モデル別の判明コスト/トークン/件数（高コスト順）。最適化の起点 |
| `unpriced_models` | 単価未登録モデルの未割当トークン（pricing.toml 追記候補） |
| `cache_efficiency_by_session` | セッション別キャッシュ命中率（低い順）。`NULLIF` でゼロ除算回避 |
| `top_sessions_by_cost` | コスト上位セッション（削減効果大） |
| `subagent_share` | メイン/サブエージェント別のコスト・トークン内訳 |
| `expensive_model_on_simple_work` | 「安いモデルで十分では?」のヒューリスティック抽出（断定ではない） |

> 注: 日次集計は TZ 依存で Python 側で切るため、TZ 正確な日次は生 SQL ではなく
> `tokentracker daily --json` を使うこと。また queries/dashboard は全件読みで、
> 大規模 DB では `idx_usage_ts` を使った枝刈りが将来課題。
