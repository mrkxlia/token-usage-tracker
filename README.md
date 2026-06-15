# token-usage-tracker — AIエージェント トークン消費トラッカー

Claude Code・Codex・Cline などの AI コーディングエージェントが残す**ローカルログ**を解析し、
**どのリポジトリ・どのタスク（セッション）・どのモデル・どのツールで**どれだけトークン／コストを
消費したかを集計・可視化する独立ツールです。

Azure AI Foundry 経由（API キー利用）でも、トークン数・モデル名・リポジトリ（cwd）は
各ツールのローカルログにそのまま残るため、追加のプロキシやクラウド連携なしで集計できます。

> 目的: コスト意識を持つための「見える化」。最終的には「このタスクなら安いモデルで十分では?」と
> いったコスト削減の判断材料に使うことを見据えています。

## 対応状況

| ツール | 状況 | ログ保存場所 |
|--------|------|-------------|
| **Claude Code** | ✅ 対応済み（M1） | `~/.claude/projects/<cwdをハイフン化>/<sessionId>.jsonl`（サブエージェントは `.../subagents/agent-*.jsonl`） |
| **Codex CLI** | ✅ 対応済み（M3） | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| **Cline** | ✅ 対応済み（M3） | VSCode globalStorage `saoudrizwan.claude-dev/tasks/<id>/` |

## クイックスタート（クローン → 集計まで）

パッケージ管理は [uv](https://docs.astral.sh/uv/) に統一しています。未導入なら先に uv を入れます。

```bash
# 0) uv をインストール（未導入の場合のみ）
#    macOS / Linux:
curl -LsSf https://astral.sh/uv/install.sh | sh
#    Windows (PowerShell):
#    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 1) リポジトリを取得（浅いクローンでOK）
git clone --depth 1 https://github.com/mrkxlia/token-usage-tracker.git
cd token-usage-tracker

# 2) 依存をインストール
uv sync                      # CLI のみ
# uv sync --extra dashboard  # Web ダッシュボードも使う場合

# 3) ローカルログを取り込んで集計（既定で 3 ツールの標準保存場所を走査）
uv run tokentracker ingest
uv run tokentracker model    # モデル別の集計表

# 4) （任意）Web ダッシュボードを開く
uv run --extra dashboard streamlit run tokentracker/dashboard.py \
    --server.address=127.0.0.1 --server.headless=true
# ブラウザで http://127.0.0.1:8501 を開く
```

> このツールは独立したリポジトリ `mrkxlia/token-usage-tracker` として配布されています。
> 履歴全体は不要なので上記の浅いクローンで十分です。

## インストール（uv・補足）

既にクローン済みなら、ディレクトリ内で同期するだけです。

```bash
cd token-usage-tracker
uv sync                      # CLI のみ
uv sync --extra dashboard    # Web ダッシュボードも使う場合
```

## 使い方（CLI）

```bash
# 1) ローカルログを取り込む（既定で 3 ツールすべての標準保存場所を走査、~/.tokentracker/usage.db に保存）
uv run tokentracker ingest
# 特定ツールだけ取り込む場合
uv run tokentracker ingest --source codex   # all / claude_code / codex / cline

# 2) 集計表を見る
uv run tokentracker repo       # リポジトリ別
uv run tokentracker model      # モデル別
uv run tokentracker agent      # ツール(エージェント)別
uv run tokentracker session    # セッション(タスク)別
uv run tokentracker daily      # 日次（既定 Asia/Tokyo で日付を区切る）
```

共通オプション:

- `--db PATH` … SQLite ファイル（既定 `~/.tokentracker/usage.db`）
- `--since YYYY-MM-DD` / `--until YYYY-MM-DD` … 期間で絞り込み
- `--tz Asia/Tokyo` … 日次バケットのタイムゾーン
- `--json` … 機械可読な JSON 出力
- `--include-subagents` / `--exclude-subagents` … サブエージェント分の集計ロールアップ切替

`ingest` は何度実行しても安全です（`UNIQUE(source, message_id)` で冪等。重複加算されません）。
cron などで定期実行できます。

## 使い方（Web ダッシュボード）

```bash
uv run --extra dashboard streamlit run tokentracker/dashboard.py \
    --server.address=127.0.0.1 --server.headless=true
```

ローカル完結（外部送信なし）。リポジトリ／モデル／ツール／期間でフィルタし、日次推移グラフと
軸別集計を表示します。

## コスト単価について（重要）

単価は**静的な TOML ファイル**で管理します（コードを編集せず差し替え可能）。同梱の既定値は
`tokentracker/pricing.toml` で、Claude（Anthropic 公式）と GPT（Web 調査）の現行価格が入っています。

> ⚠️ **GPT の単価は二次情報のため、必ず自社の Azure ポータルの実課金レートで検証・上書き**してください。
> 知りたいのは定価ではなく **Azure Foundry の実課金額**です（1M トークンあたりの USD）。

### 単価ファイルの読み込み優先順位
1. 環境変数 `TOKENTRACKER_PRICING=/path/to/pricing.toml`
2. `~/.tokentracker/pricing.toml`（利用者の上書き用）
3. パッケージ同梱の `tokentracker/pricing.toml`（既定値）

現在使われているファイルと登録モデルは `uv run tokentracker pricing` で確認できます。

### 編集方法（モデルが増えたとき）
1. 同梱ファイルは直接編集せず、上書き用にコピー（または `TOKENTRACKER_PRICING` で別ファイル指定）:
   ```bash
   mkdir -p ~/.tokentracker && cp tokentracker/pricing.toml ~/.tokentracker/pricing.toml
   ```
2. `~/.tokentracker/pricing.toml` に `[models."<モデルID>"]` テーブルを追記/編集:
   ```toml
   [models."gpt-5.4"]
   input = 1.25
   output = 10.0
   cache_write_1h = 0.0
   cache_write_5m = 0.0
   cache_read = 0.125
   ```
   キーは `input` / `output` / `cache_write_1h` / `cache_write_5m` / `cache_read`（$/1M）。
   Claude のキャッシュ単価の目安は input に対し read=0.1×・write_5m=1.25×・write_1h=2×。
   OpenAI/GPT 系はキャッシュ書込が無いので write=0、read は cached input 単価。
3. Foundry のデプロイ名が正規 ID と違う場合は `[aliases]` に `"デプロイ名" = "正規ID"` を追加。
   末尾の日付サフィックス（例 `claude-...-20251001`）は自動で基底 ID に解決されます。

### 単価未登録モデルの扱い
- 単価が**未登録のモデル**は `cost_usd=None` となり「未割当トークン」として別計上されます
  （判明コストに紛れ込まず、静かに 0 円化しない）。集計表の「未割当tok」列に出ます。
- **`uv run tokentracker pricing --missing`** で、ログに出たのに単価未登録のモデルだけを
  未割当トークンの多い順に一覧できます。これを見て pricing.toml に追記してください。

## 集計の正確性（実環境ログで検証済み）

- Claude Code は 1 つの API 応答（`message.id`）が**最大 5 行重複**して JSONL に出力されます。
  本ツールは `message.id` を一意キーに **1 件へ畳み**（重複加算を防止）、ストリーミング途中の
  部分行に備えて `output_tokens` が最大の行を採用します。
- サブエージェントは別ファイルに記録され、各行が `agentId`/`cwd` を持つため、
  リポジトリ割当を保ったまま `is_subagent` フラグで識別・集計切替できます。
- `cache_creation` の 1h / 5m TTL を別フィールドに保持し、単価差を反映できます。
- `server_tool_use`（web 検索／取得の件数課金）は件数列として保持しますが、別建て従量課金の
  ため現状コスト計算の対象外です。

### Codex / Cline の正規化（M3）
- **Codex**: `token_count` イベントの `info.last_token_usage`（その API 呼び出し単体の値）を採用する
  ため、累積値の差分計算は不要です。`cached_input_tokens` は `input_tokens` の内訳なので
  `input = input − cached` に控除して `cache_read` へ振り分け（二重計上を回避）。
  `reasoning_output_tokens` は専用列に保持し、コストは **output 単価**で計上します
  （集計表では output 列に畳んで表示）。モデルは直近の `turn_context.model` を適用します。
- **Cline**: `ui_messages.json` の `api_req_started`（`tokensIn/tokensOut/cacheReads/cacheWrites`）を
  1 リクエスト＝1 イベントとして取り込みます。`cacheWrites` は TTL 不明のため **5m と仮定**。
  モデル名は `ClineApiReqInfo` に無いため `task_metadata.json` または
  `api_conversation_history.json`（environment_details）からベストエフォートで解決し、取れなければ
  未割当にします。コストは他ツールと揃えて **pricing.py で再計算**します（Cline が報告する `cost`
  はクロスチェックの参考値として使えますが、本ツールでは保存しません）。

## 開発（TDD）

テスト駆動で開発しています。代表ケース（5重複の dedup、サブエージェント分離、非usage行の除外、
1h/5m 分離、`top-level == sum(iterations)`、未割当コストの別計上、TZ バケット）を
`tests/` に固定しています。

```bash
uv run pytest
```

## ライセンス／参考

MIT License（リポジトリ全体に従う）。設計の参考に
[ccusage](https://github.com/ryoppippi/ccusage)（JSONL パース・コスト計算）と
[tokscale](https://github.com/junhoyeo/tokscale)（Workspace/Session/Model 集計軸）を参照しています
（コードのコピーはせず、設計のみ参照）。
