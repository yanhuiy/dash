"""本地参考项目数据接入（breakout_v3 / ice-rebound）。

仅服务于回测与稳健性检验，不改变生产流水线：
- 载体历史延长：用本地 ETF（588200）作为半导体等主题的更长历史代理；
- 龙头行情补全：ice-rebound/data_market 提供 45 只龙头的收盘价（无成交额）；
- 概念宽度：breakout_v3/data_ths_index 提供同花顺概念指数收盘价，
  用“概念指数站上 20 日线的比例 + 概念 20 日超额收益”替代 5 只龙头的宽度；
- 市场情绪：ice-rebound/output/sentiment_daily.csv 提供全市场宽度与连板数量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

COLUMNS = ["date", "open", "close", "high", "low", "amount", "volume"]


def _normalize(frame: pd.DataFrame) -> pd.DataFrame | None:
    if frame is None or frame.empty or "date" not in frame.columns:
        return None
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
    out = out.dropna(subset=["date"])
    for col in COLUMNS[1:]:
        if col not in out.columns:
            out[col] = np.nan
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"])
    out = out.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    return out[COLUMNS]


def _read_csv(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    try:
        return _normalize(pd.read_csv(path))
    except Exception:
        return None


@dataclass
class LocalStore:
    """按需加载本地 CSV，避免一次性读入 5000+ 个文件。"""

    breakout_dir: Path
    ice_dir: Path
    concept_map: dict[str, list[str]] = field(default_factory=dict)
    carrier_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    _cache: dict[str, pd.DataFrame | None] = field(default_factory=dict)

    def _cached(self, key: str, loader) -> pd.DataFrame | None:
        if key not in self._cache:
            self._cache[key] = loader()
        return self._cache[key]

    def etf(self, code: str) -> pd.DataFrame | None:
        code = str(code)
        return self._cached(
            f"etf:{code}", lambda: _read_csv(self.ice_dir / "data_etf" / f"{code}.csv")
        )

    def index_file(self, code: str) -> pd.DataFrame | None:
        code = str(code)
        prefix = "sh" if code.startswith(("000", "6")) else "sz"
        return self._cached(
            f"idx:{code}",
            lambda: _read_csv(self.ice_dir / "data_index" / f"{prefix}{code}.csv"),
        )

    def stock_close(self, code: str) -> pd.DataFrame | None:
        code = str(code)
        return self._cached(
            f"stock:{code}", lambda: _read_csv(self.ice_dir / "data_market" / f"{code}.csv")
        )

    def breakout_stock(self, code: str) -> pd.DataFrame | None:
        code = str(code)
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
        return self._cached(
            f"bk:{code}",
            lambda: _read_csv(self.breakout_dir / "data" / f"{prefix}.{code}.csv"),
        )

    def concept_index(self, name: str) -> pd.DataFrame | None:
        return self._cached(
            f"concept:{name}", lambda: _read_csv(self.breakout_dir / "data_ths_index" / f"{name}.csv")
        )

    def concepts_for(self, theme_id: str) -> list[tuple[str, pd.DataFrame]]:
        frames: list[tuple[str, pd.DataFrame]] = []
        for name in self.concept_map.get(theme_id, []):
            frame = self.concept_index(name)
            if frame is not None and not frame.empty:
                frames.append((name, frame))
        return frames

    def carrier_override(self, theme_id: str) -> pd.DataFrame | None:
        spec = self.carrier_overrides.get(theme_id)
        if not spec:
            return None
        kind = str(spec.get("type", "")).lower()
        code = str(spec.get("code", ""))
        if not code:
            return None
        if kind == "etf":
            return self.etf(code)
        if kind == "index_file":
            return self.index_file(code)
        if kind == "stock":
            return self.breakout_stock(code)
        return None

    def sentiment(self, filename: str = "output/sentiment_daily.csv") -> pd.DataFrame | None:
        def load() -> pd.DataFrame | None:
            path = self.ice_dir / filename
            if not path.is_file():
                return None
            try:
                frame = pd.read_csv(path)
            except Exception:
                return None
            if frame.empty or "date" not in frame.columns:
                return None
            frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
            frame = frame.dropna(subset=["date"])
            for col in frame.columns:
                if col != "date":
                    frame[col] = pd.to_numeric(frame[col], errors="coerce")
            frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
            total = frame.get("total_count", pd.Series(np.nan, index=frame.index))
            safe_total = total.where(total > 0)
            if "advance_count" in frame.columns:
                frame["market_advance_ratio"] = frame["advance_count"] / safe_total
            if "below_ma20_count" in frame.columns:
                frame["market_above_ma20_ratio"] = 1 - frame["below_ma20_count"] / safe_total
            if "lianban" in frame.columns:
                frame["market_lianban"] = frame["lianban"]
            keep = [
                col
                for col in (
                    "date",
                    "market_advance_ratio",
                    "market_above_ma20_ratio",
                    "market_lianban",
                )
                if col in frame.columns
            ]
            return frame[keep]

        return self._cached("sentiment", load)


def make_store(config: dict[str, Any]) -> LocalStore | None:
    raw = config.get("local_data") or {}
    if not raw.get("enabled", False):
        return None
    breakout = Path(str(raw.get("breakout_dir", "")))
    ice = Path(str(raw.get("ice_rebound_dir", "")))
    if not breakout.is_dir() or not ice.is_dir():
        return None
    concept_map = {
        str(theme_id): [str(name) for name in names]
        for theme_id, names in (raw.get("concept_map") or {}).items()
    }
    overrides = {
        str(theme_id): {str(key): str(value) for key, value in spec.items()}
        for theme_id, spec in (raw.get("carrier_overrides") or {}).items()
        if isinstance(spec, dict)
    }
    return LocalStore(
        breakout_dir=breakout,
        ice_dir=ice,
        concept_map=concept_map,
        carrier_overrides=overrides,
    )


def build_concept_metrics(
    frames: list[tuple[str, pd.DataFrame]],
    baseline: pd.DataFrame | None,
    trend_days: int = 6,
) -> pd.DataFrame:
    """按交易日聚合概念指数：站上 20 日线比例与 20 日超额收益。

    返回列：concept_breadth、concept_rs、concept_rs_series、concept_coverage。
    """
    closes: dict[str, pd.Series] = {}
    for name, frame in frames:
        series = frame.set_index("date")["close"]
        series = series[~series.index.duplicated(keep="last")].sort_index()
        if not series.empty:
            closes[name] = series
    if not closes:
        return pd.DataFrame(
            columns=["concept_breadth", "concept_rs", "concept_rs_series", "concept_coverage"]
        )

    close_df = pd.DataFrame(closes).sort_index()
    if baseline is not None and not baseline.empty:
        bench = baseline.set_index("date")["close"]
        bench = bench[~bench.index.duplicated(keep="last")].sort_index()
        close_df = close_df.reindex(close_df.index.union(bench.index)).ffill()

    ma20 = close_df.rolling(20, min_periods=20).mean()
    valid = ma20.notna()
    coverage = valid.sum(axis=1)
    above = (close_df > ma20) & valid
    breadth = above.sum(axis=1) / coverage.replace(0, np.nan)

    ret20 = close_df / close_df.shift(20) - 1
    if baseline is not None and not baseline.empty:
        bench = baseline.set_index("date")["close"]
        bench = bench[~bench.index.duplicated(keep="last")].sort_index()
        bench = bench.reindex(close_df.index).ffill()
        bench_ret20 = bench / bench.shift(20) - 1
        rs = ret20.sub(bench_ret20, axis=0).mean(axis=1)
    else:
        rs = ret20.mean(axis=1)

    out = pd.DataFrame(
        {
            "concept_breadth": breadth,
            "concept_rs": rs,
            "concept_coverage": coverage,
        }
    )
    out["concept_rs_series"] = [
        [None if pd.isna(value) else round(float(value), 6) for value in rs.iloc[max(0, i - trend_days + 1) : i + 1].tolist()]
        for i in range(len(rs))
    ]
    return out


def sentiment_regime(frame: pd.DataFrame | None) -> pd.Series:
    """按全市场站上 20 日线比例划三档：risk_on / neutral / risk_off。"""
    if frame is None or frame.empty or "market_above_ma20_ratio" not in frame.columns:
        return pd.Series(dtype=object)
    ratio = pd.to_numeric(frame["market_above_ma20_ratio"], errors="coerce")
    labels = np.where(ratio >= 0.5, "risk_on", np.where(ratio >= 0.35, "neutral", "risk_off"))
    series = pd.Series(labels, index=frame["date"].to_numpy(), dtype=object)
    series = series[ratio.notna().to_numpy()]
    return series[~series.index.duplicated(keep="last")].sort_index()


def latest_date(frame: pd.DataFrame | None) -> date | None:
    if frame is None or frame.empty or "date" not in frame.columns:
        return None
    return frame["date"].max()
