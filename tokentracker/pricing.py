"""モデル別単価とコスト計算。

単価は **静的 TOML ファイル**から読み込む（コードを編集せず差し替え可能）。
読み込み優先順位（最初に見つかったものを使用）:
  1. 環境変数 ``TOKENTRACKER_PRICING`` が指すファイル
  2. ``~/.tokentracker/pricing.toml``（利用者の上書き用）
  3. パッケージ同梱の ``tokentracker/pricing.toml``（既定値）

知りたいのは定価ではなく **Azure Foundry の実課金額** なので、利用者は上記 1/2 に置いた
ファイルで自由に上書きできる。単価が無いモデルは ``cost_usd=None`` を返し、集計側で
「未割当コスト」として可視化する（0 円化しない）。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from tokentracker.models import SYNTHETIC_MODEL, UsageEvent

# 末尾の日付サフィックス（例 -20251001）。単価キーは日付なしの基底IDで持つ。
_DATE_SUFFIX = re.compile(r"-\d{8}$")

# 1M トークンあたりの USD。種別ごとに別単価（cache_write は 1h>5m、read は割引）。
PriceTable = dict[str, dict[str, float]]

#: 同梱の既定単価ファイル。
BUNDLED_PRICING = Path(__file__).with_name("pricing.toml")


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get("TOKENTRACKER_PRICING")
    if env:
        paths.append(Path(env))
    paths.append(Path.home() / ".tokentracker" / "pricing.toml")
    paths.append(BUNDLED_PRICING)
    return paths


def load_price_table(path: str | Path | None = None) -> tuple[PriceTable, dict[str, str], dict]:
    """単価 TOML を読み込み ``(prices, aliases, meta)`` を返す。

    ``path`` 指定時はそれを、無指定なら優先順位に従って最初に存在するファイルを読む。
    """
    candidates = [Path(path)] if path is not None else _candidate_paths()
    for p in candidates:
        if p.exists():
            with p.open("rb") as f:
                data = tomllib.load(f)
            prices: PriceTable = {
                model: {k: float(v) for k, v in rate.items()}
                for model, rate in (data.get("models") or {}).items()
            }
            aliases = {str(k): str(v) for k, v in (data.get("aliases") or {}).items()}
            return prices, aliases, (data.get("meta") or {})
    # どれも見つからない場合は空（全モデルが未割当になる）。
    return {}, {}, {}


@dataclass
class PriceBook:
    prices: PriceTable
    aliases: dict[str, str] | None = None

    def resolve_model(self, model: str) -> str:
        aliases = self.aliases or {}
        return aliases.get(model, model)

    def _rate_for(self, model: str) -> dict[str, float] | None:
        """モデル名→単価 dict を引く。未知モデルは None。

        alias 解決 → 日付サフィックス除去（claude-...-YYYYMMDD → claude-...）の
        2 段フォールバック。compute_cost / cache_savings から共用する単一実装。
        """
        resolved = self.resolve_model(model)
        rate = self.prices.get(resolved)
        if rate is None:
            rate = self.prices.get(_DATE_SUFFIX.sub("", resolved))
        return rate

    def compute_cost(self, ev: UsageEvent) -> float | None:
        """イベントの判明コスト(USD)。未知モデルは None（未割当）。

        ``<synthetic>``（トークン 0 のローカル no-op 行）は 0.0 を返し、未割当ノイズに出さない。
        """
        if ev.model == SYNTHETIC_MODEL:
            return 0.0
        rate = self._rate_for(ev.model)
        if rate is None:
            return None
        per_million = (
            ev.input_tokens * rate.get("input", 0.0)
            # reasoning は output 側のトークンなので output 単価で計上（output_tokens とは別計上）。
            + (ev.output_tokens + ev.reasoning_output_tokens) * rate.get("output", 0.0)
            + ev.cache_creation_1h_tokens * rate.get("cache_write_1h", 0.0)
            + ev.cache_creation_5m_tokens * rate.get("cache_write_5m", 0.0)
            + ev.cache_read_tokens * rate.get("cache_read", 0.0)
        )
        return per_million / 1_000_000

    def cache_savings(self, model: str, cache_read_tokens: int) -> float | None:
        """キャッシュ読取による推定節約額(USD)。未知モデル/単価欠落は None。

        節約額 = cache_read_tokens × (input単価 − cache_read単価) / 1e6。
        同じトークンをフル input 単価で払った場合との差分（読取分のみ。書込コストの
        償却は含まない近似）。input < cache_read という異常単価では負にしないため
        max(0, …) でクリップする。
        """
        rate = self._rate_for(model)
        if rate is None:
            return None
        delta = rate.get("input", 0.0) - rate.get("cache_read", 0.0)
        return max(0.0, cache_read_tokens * delta / 1_000_000)


def active_pricing_path() -> Path | None:
    """実際に使われる単価ファイルのパス（存在する最初の候補）。"""
    for p in _candidate_paths():
        if p.exists():
            return p
    return None


def default_pricebook() -> PriceBook:
    prices, aliases, _meta = load_price_table()
    return PriceBook(prices=prices, aliases=aliases)
