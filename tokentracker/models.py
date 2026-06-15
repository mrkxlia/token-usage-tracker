"""正規化されたトークン使用イベントのデータモデル。

3エージェント(Claude Code / Codex / Cline)のログを単一の `UsageEvent` に正規化し、
SQLite の `usage_event` テーブル 1 行に対応させる。
"""

from __future__ import annotations

from dataclasses import dataclass, fields

# `source` の値域（エージェント識別子）。要望の第4主軸「ツール別」に対応。
SOURCE_CLAUDE_CODE = "claude_code"
SOURCE_CODEX = "codex"
SOURCE_CLINE = "cline"

# Foundry のデプロイ名ではなくローカル生成の no-op 行に付くモデル名。コスト集計から除外する。
SYNTHETIC_MODEL = "<synthetic>"


@dataclass
class UsageEvent:
    """API 応答 1 件 ＝ 1 イベント。`usage_event` テーブルの 1 行に対応する。"""

    source: str
    message_id: str
    session_id: str
    model: str
    timestamp_utc: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_creation_1h_tokens: int = 0
    cache_creation_5m_tokens: int = 0
    cache_read_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    agent_id: str | None = None
    request_id: str | None = None
    repo_path: str | None = None
    git_branch: str | None = None
    is_subagent: bool = False
    # 単価マスタが見つかった場合に ingest 時へ計算して埋める。未知モデルは None（未割当コスト）。
    cost_usd: float | None = None

    @classmethod
    def column_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]
