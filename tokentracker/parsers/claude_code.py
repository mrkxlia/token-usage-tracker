"""Claude Code のセッションログ(JSONL)パーサ。

保存場所: ``~/.claude/projects/<cwdをハイフン化>/<sessionId>.jsonl``
サブエージェント: ``<sessionId>/subagents/agent-*.jsonl``（別ファイル・各行が cwd 等を保持）。

実環境で確認した不変条件:
- assistant 行のみが ``message.usage`` を持つ（他 type は対象外）。
- 同一 ``message.id`` が最大 5 行重複し、usage も stop_reason も全行同一。
  → dedup は ``message.id`` 単位（基底クラスが担当）。
- サブエージェント行は top-level ``agentId`` と ``isSidechain: true`` を持つ。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from tokentracker.models import SOURCE_CLAUDE_CODE, UsageEvent
from tokentracker.parsers.base import (
    Parser,
    iter_jsonl_files,
    iter_jsonl_objects,
    read_text_file,
    safe_int,
)


class ClaudeCodeParser(Parser):
    source = SOURCE_CLAUDE_CODE

    def default_root(self) -> Path:
        return Path.home() / ".claude" / "projects"

    def _iter_file_events(self, root: Path) -> Iterator[tuple[Path, list[UsageEvent]]]:
        # 注: アーカイブ ``~/.claude/transcripts`` は extra_roots に加えない。projects と
        # 同一 message.id を持ちうるうえ、サブエージェント階層を欠くため is_subagent が
        # 反転する（UNIQUE(source, message_id) の UPSERT で上書きされる）リスクがあるため。
        for path in iter_jsonl_files(root):
            is_subagent = "subagents" in path.parts
            text = read_text_file(path)
            if text is None:
                continue
            events = [
                ev
                for obj in iter_jsonl_objects(text)
                if (ev := self._row_to_event(obj, is_subagent=is_subagent)) is not None
            ]
            yield path, events

    def _row_to_event(self, obj: dict, *, is_subagent: bool) -> UsageEvent | None:
        # ホワイトリスト方式: assistant かつ usage を持つ行のみ（type は今後増えうる）。
        if obj.get("type") != "assistant":
            return None
        message = obj.get("message") or {}
        usage = message.get("usage")
        message_id = message.get("id")
        if not usage or not message_id:
            return None

        cache_creation = usage.get("cache_creation") or {}
        server_tool = usage.get("server_tool_use") or {}

        return UsageEvent(
            source=self.source,
            message_id=message_id,
            session_id=obj.get("sessionId", ""),
            model=message.get("model", ""),
            timestamp_utc=obj.get("timestamp", ""),
            input_tokens=safe_int(usage, "input_tokens"),
            output_tokens=safe_int(usage, "output_tokens"),
            cache_creation_tokens=safe_int(usage, "cache_creation_input_tokens"),
            cache_creation_1h_tokens=safe_int(cache_creation, "ephemeral_1h_input_tokens"),
            cache_creation_5m_tokens=safe_int(cache_creation, "ephemeral_5m_input_tokens"),
            cache_read_tokens=safe_int(usage, "cache_read_input_tokens"),
            web_search_requests=safe_int(server_tool, "web_search_requests"),
            web_fetch_requests=safe_int(server_tool, "web_fetch_requests"),
            agent_id=obj.get("agentId") if is_subagent else None,
            request_id=obj.get("requestId"),
            repo_path=obj.get("cwd"),
            git_branch=obj.get("gitBranch"),
            is_subagent=is_subagent,
        )

    @staticmethod
    def usage_matches_iterations(usage: dict) -> bool:
        """top-level usage が ``iterations[]`` の合算と一致するか検証する。

        将来 1 message.id 内に複数推論が入る版で、top-level がその合算でなくなった場合に
        サイレントな過小カウントへ気づくためのガード。``iterations`` が無ければ True。
        """
        iterations = usage.get("iterations")
        if not iterations:
            return True
        for field in ("input_tokens", "output_tokens"):
            top = safe_int(usage, field)
            summed = sum(safe_int(it, field) for it in iterations)
            if top != summed:
                return False
        return True
