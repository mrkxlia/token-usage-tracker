"""Cline (VSCode 拡張) のタスクログ パーサ。

保存場所: ``<globalStorage>/saoudrizwan.claude-dev/tasks/<task-id>/``
  - ``ui_messages.json``: ``say == "api_req_started"`` の ``text`` に JSON 文字列
    ``{tokensIn, tokensOut, cacheReads, cacheWrites, cost, request}``（**model は無い**）。
    1 タスクに複数 api_req（各呼び出し値・累積でない）。
  - ``task_metadata.json``: ``cwdOnTaskInitialization``（無ければ ``shadowGitConfigWorkTree``）。
  - ``api_conversation_history.json``: ``<environment_details>`` 等に含まれるモデル ID を
    ベストエフォートで抽出（取れなければモデル不明 → 未割当）。

正規化:
  - ``cacheWrites`` は TTL 不明のため ``cache_creation_5m_tokens`` に割り当てる（README に明記）。
  - cost はユーザー選択により pricing.py で再計算（Cline 報告 cost は使わない）。
  - 合成キー ``f"{task_id}#{api_req出現順index}"``。session_id = task_id。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tokentracker.models import SOURCE_CLINE, UsageEvent
from tokentracker.parsers.base import (
    Parser,
    home,
    read_json_file,
    safe_int,
    vscode_global_storage_dir,
)

_CLINE_TASKS_SUBPATH = ("saoudrizwan.claude-dev", "tasks")

# environment_details 等から拾うモデル ID のベストエフォート抽出。
_MODEL_RE = re.compile(r"\b(claude-[\w.-]+|gpt-[\w.-]+|o\d[\w.-]*)\b")


def _iter_strings(obj: Any) -> Iterator[str]:
    """JSON 由来のネスト構造から文字列リーフを再帰的に列挙する。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


class ClineParser(Parser):
    source = SOURCE_CLINE

    def default_root(self) -> Path:
        return vscode_global_storage_dir().joinpath(*_CLINE_TASKS_SUBPATH)

    def extra_roots(self) -> list[Path]:
        # VS Code Server（リモート/コンテナ実行）では globalStorage が ~/.vscode-server 配下に
        # 置かれる。デスクトップ版と重複しないタスク ID 単位なので安全に追加できる。
        return [home() / ".vscode-server" / "data" / "User" / "globalStorage" / Path(*_CLINE_TASKS_SUBPATH)]

    def _iter_file_events(self, root: Path) -> Iterator[tuple[Path, list[UsageEvent]]]:
        if not root.exists():
            return
        for task_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            ui = task_dir / "ui_messages.json"
            if not ui.exists():
                continue
            # 状態キーは追記される ui_messages.json。タスク進行で size/mtime が変わり再取り込みされる。
            yield ui, list(self._iter_task(task_dir, ui))

    def _iter_task(self, task_dir: Path, ui: Path) -> Iterator[UsageEvent]:
        task_id = task_dir.name
        repo_path = self._read_repo_path(task_dir)
        model = self._read_model(task_dir)
        messages = read_json_file(ui)
        if not isinstance(messages, list):
            return
        index = 0
        for msg in messages:
            if msg.get("say") != "api_req_started":
                continue
            info = self._parse_text_json(msg.get("text"))
            if info is None:
                continue
            yield UsageEvent(
                source=self.source,
                message_id=f"{task_id}#{index}",
                session_id=task_id,
                model=model,
                timestamp_utc=self.ms_to_iso(msg.get("ts")) if msg.get("ts") else "",
                input_tokens=safe_int(info, "tokensIn"),
                output_tokens=safe_int(info, "tokensOut"),
                cache_read_tokens=safe_int(info, "cacheReads"),
                # TTL 不明のため 5m と仮定して割り当て。
                cache_creation_5m_tokens=safe_int(info, "cacheWrites"),
                repo_path=repo_path,
            )
            index += 1

    @staticmethod
    def _parse_text_json(text) -> dict | None:
        if not isinstance(text, str):
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    @staticmethod
    def _read_repo_path(task_dir: Path) -> str | None:
        meta = read_json_file(task_dir / "task_metadata.json")
        if isinstance(meta, dict):
            return meta.get("cwdOnTaskInitialization") or meta.get("shadowGitConfigWorkTree")
        return None

    @staticmethod
    def _read_model(task_dir: Path) -> str:
        # 1) task_metadata の明示フィールド
        meta = read_json_file(task_dir / "task_metadata.json")
        if isinstance(meta, dict):
            for key in ("model", "apiModelId", "modelId"):
                if meta.get(key):
                    return str(meta[key])
        # 2) api_conversation_history のデコード済み文字列値からベストエフォート抽出。
        #    生テキストを直接 search すると JSON の `\n` エスケープで語境界が崩れるため、
        #    json.loads して実際の文字列値（改行が実改行）に対して検索する。
        hist = read_json_file(task_dir / "api_conversation_history.json")
        for s in _iter_strings(hist):
            m = _MODEL_RE.search(s)
            if m:
                return m.group(1)
        return ""  # 不明 → 未割当

    @staticmethod
    def ms_to_iso(ms: Any) -> str:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
