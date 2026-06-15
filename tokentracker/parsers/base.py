"""パーサ共通インターフェース。

各エージェント向けパーサは `Parser` を継承し、ローカルログのルートを受け取って
正規化済みの `UsageEvent` を yield する。`source` 単位での dedup（同一 message_id を
1 件に畳む）はこの基底クラスが担う。
"""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from collections.abc import Iterator, Iterable
from pathlib import Path

from tokentracker.models import UsageEvent


class Parser(ABC):
    """ログパーサの基底クラス。"""

    #: `UsageEvent.source` に入る識別子。
    source: str

    @abstractmethod
    def default_root(self) -> Path:
        """そのエージェントのログ既定ルート（OS 依存パスを解決）。"""

    @abstractmethod
    def _iter_raw_events(self, root: Path) -> Iterator[UsageEvent]:
        """重複を含む生のイベント列を yield する（dedup 前）。"""

    def iter_events(self, root: Path | None = None) -> Iterator[UsageEvent]:
        """dedup 済みのイベントを yield する。

        同一 `message_id` が複数行に重複出現する（Claude Code は最大 5 重複を実測）ため、
        `message_id` を一意キーに 1 件へ畳む。重複行で usage が食い違う将来版に備え、
        `output_tokens` が最大の行を採用するフォールバックを入れる。
        """
        root = root or self.default_root()
        best: dict[str, UsageEvent] = {}
        for ev in self._iter_raw_events(root):
            cur = best.get(ev.message_id)
            if cur is None or ev.output_tokens > cur.output_tokens:
                best[ev.message_id] = ev
        yield from best.values()


def home() -> Path:
    return Path.home()


def vscode_global_storage_dir() -> Path:
    """VSCode の globalStorage ルートを OS 別に解決（Cline 用、M3 で使用）。"""
    system = platform.system()
    if system == "Darwin":
        return home() / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
    if system == "Windows":
        import os

        appdata = os.environ.get("APPDATA", str(home() / "AppData" / "Roaming"))
        return Path(appdata) / "Code" / "User" / "globalStorage"
    return home() / ".config" / "Code" / "User" / "globalStorage"


def iter_jsonl_files(root: Path, pattern: str = "**/*.jsonl") -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(root.glob(pattern))
