"""Codex CLI の rollout JSONL パーサ。

保存場所: ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``
各行: ``{"type": ..., "payload": {...}, "timestamp": ...}``
  - ``session_meta``: ``payload.id``(=session_id) / ``payload.cwd``
  - ``turn_context``: ``payload.model``（モデル切替で複数回出現しうる → 直近値を保持）
  - ``event_msg`` かつ ``payload.type == "token_count"``:
    ``payload.info.last_token_usage`` が**その API 呼び出し単体**の usage（累積差分は不要）。

正規化:
  - ``cached_input_tokens`` は ``input_tokens`` の内訳のため、二重計上を避けて
    ``input = input - cached``、``cache_read = cached`` に割り当てる。
  - ``reasoning_output_tokens`` は専用列へ（コストは output 単価で計上）。
  - 安定した message.id が無いため、合成キー ``f"{file_stem}#{出現順index}"`` を用いる。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from tokentracker.models import SOURCE_CODEX, UsageEvent
from tokentracker.parsers.base import Parser, iter_jsonl_files


class CodexParser(Parser):
    source = SOURCE_CODEX

    def default_root(self) -> Path:
        return Path.home() / ".codex" / "sessions"

    def _iter_file_events(self, root: Path) -> Iterator[tuple[Path, list[UsageEvent]]]:
        for path in iter_jsonl_files(root, "**/rollout-*.jsonl"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            yield path, list(self._iter_file(path, text))

    def _iter_file(self, path: Path, text: str) -> Iterator[UsageEvent]:
        session_id = ""
        cwd: str | None = None
        model = ""
        token_count_index = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_type = data.get("type")
            payload = data.get("payload") or {}
            if item_type == "session_meta":
                session_id = payload.get("id", session_id)
                cwd = payload.get("cwd", cwd)
            elif item_type in ("turn_context", "turnContext"):
                model = payload.get("model", model) or model
            elif item_type == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                # last_token_usage は「直近ターン単体」の usage（per-call）。
                # total_token_usage はセッション累積なので、last が無い旧版ログ用の
                # フォールバックに留める（累積をそのまま計上すると過大になる）。
                last = info.get("last_token_usage") or info.get("total_token_usage") or {}
                ev = self._usage_to_event(
                    last, session_id=session_id, cwd=cwd, model=model,
                    message_id=f"{path.stem}#{token_count_index}",
                    timestamp=data.get("timestamp", ""),
                )
                token_count_index += 1
                yield ev

    def _usage_to_event(
        self, usage: dict, *, session_id: str, cwd: str | None, model: str,
        message_id: str, timestamp: str,
    ) -> UsageEvent:
        raw_input = int(usage.get("input_tokens", 0) or 0)
        cached = int(usage.get("cached_input_tokens", 0) or 0)
        return UsageEvent(
            source=self.source,
            message_id=message_id,
            session_id=session_id,
            model=model,
            timestamp_utc=timestamp,
            # cached は input の内訳。二重計上を避けて分離する。
            input_tokens=max(0, raw_input - cached),
            cache_read_tokens=cached,
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            reasoning_output_tokens=int(usage.get("reasoning_output_tokens", 0) or 0),
            repo_path=cwd,
        )

    @staticmethod
    def usage_total_matches(usage: dict) -> bool:
        """``total_tokens == input + output + reasoning`` を検証（cached は input の内訳）。

        version 差で内部整合が崩れた版を検知するためのガード。total が無ければ True。
        """
        total = usage.get("total_tokens")
        if total is None:
            return True
        summed = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("output_tokens", 0) or 0)
            + int(usage.get("reasoning_output_tokens", 0) or 0)
        )
        return int(total) == summed
