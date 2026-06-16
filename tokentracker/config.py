"""共通設定の単一の真実源（パス等の定数）。

CLI / ダッシュボードなど複数モジュールから参照される定数をここへ集約し、
重複定義によるズレを防ぐ。
"""

from __future__ import annotations

from pathlib import Path

# 既定の SQLite DB パス。CLI / ダッシュボード双方が参照する。
DEFAULT_DB = Path.home() / ".tokentracker" / "usage.db"
