"""パーサ共通インターフェース。

各エージェント向けパーサは `Parser` を継承し、ローカルログのルートを受け取って
正規化済みの `UsageEvent` を yield する。`source` 単位での dedup（同一 message_id を
1 件に畳む）はこの基底クラスが担う。

増分取り込み（未変更ファイルをスキップ）を可能にするため、契約は **ファイル単位**で
`(ファイルパス, そのファイルの生イベント列)` を返す `_iter_file_events` を基本とし、
既存の `iter_events` / `_iter_raw_events`（dedup 済み / 生の平坦列）はその上に薄く実装する。
"""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from pathlib import Path

from tokentracker.models import UsageEvent


class Parser(ABC):
    """ログパーサの基底クラス。"""

    #: `UsageEvent.source` に入る識別子。
    source: str

    @abstractmethod
    def default_root(self) -> Path:
        """そのエージェントのログ既定ルート（OS 依存パスを解決）。"""

    def extra_roots(self) -> list[Path]:
        """既定ルート以外に走査する追加ルート。

        VS Code Server 等、環境差で別の場所にログが置かれるケースを拾うための拡張点。
        既定は空。存在しないパスを返しても各実装の走査側が安全に無視する。
        """
        return []

    @abstractmethod
    def _iter_file_events(self, root: Path) -> Iterator[tuple[Path, list[UsageEvent]]]:
        """1 ファイル（または 1 タスク）単位で ``(状態キーとなるパス, 生イベント列)`` を yield。

        ここで返すパスは増分取り込みの単位キー（size/mtime を見るファイル）になる。
        生イベント列は dedup 前（同一 message_id の重複を含みうる）。
        """

    def iter_file_events(
        self, root: Path | None = None
    ) -> Iterator[tuple[Path, list[UsageEvent]]]:
        """走査対象を巡回し、ファイル単位で ``(パス, 生イベント列)`` を yield。

        ``root`` 指定時はそのルートのみ。無指定なら ``default_root()`` に加えて
        ``extra_roots()`` も走査する。同一パスは（resolve 済みで）1 度だけ返す。
        """
        roots = [root] if root is not None else [self.default_root(), *self.extra_roots()]
        seen: set[Path] = set()
        for r in roots:
            for path, events in self._iter_file_events(r):
                key = path.resolve()
                if key in seen:
                    continue
                seen.add(key)
                yield path, events

    @staticmethod
    def _dedup(events: Iterable[UsageEvent]) -> list[UsageEvent]:
        """同一 `message_id` を 1 件へ畳む。

        同一 `message_id` が複数行に重複出現する（Claude Code は最大 5 重複を実測）ため、
        `message_id` を一意キーに 1 件へ畳む。重複行で usage が食い違う将来版に備え、
        `output_tokens` が最大の行を採用するフォールバックを入れる。
        """
        best: dict[str, UsageEvent] = {}
        for ev in events:
            cur = best.get(ev.message_id)
            if cur is None or ev.output_tokens > cur.output_tokens:
                best[ev.message_id] = ev
        return list(best.values())

    def _iter_raw_events(self, root: Path) -> Iterator[UsageEvent]:
        """重複を含む生のイベント列を yield する（dedup 前・単一ルート）。"""
        for _path, events in self._iter_file_events(root):
            yield from events

    def iter_events(self, root: Path | None = None) -> Iterator[UsageEvent]:
        """dedup 済みのイベントを yield する（単一ルート。既存挙動を維持）。"""
        root = root or self.default_root()
        yield from self._dedup(self._iter_raw_events(root))


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
