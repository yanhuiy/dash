"""主线指标计算：RS、趋势、成交额倍数、龙头宽度、催化计数、涨停热度。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from .config import Theme


def _tail_until(df: pd.DataFrame, target: date) -> pd.DataFrame:
    return df[df["date"] <= target].reset_index(drop=True)


def _ret(closes: pd.Series, n: int) -> float | None:
    if len(closes) <= n:
        return None
    base = closes.iloc[-1 - n]
    if not base:
        return None
    return float(closes.iloc[-1] / base - 1)


def _rs_history(carrier: pd.DataFrame, baseline: pd.DataFrame, lookback: int = 4) -> list[float]:
    merged = (
        carrier[["date", "close"]]
        .merge(baseline[["date", "close"]], on="date", suffixes=("_c", "_b"))
        .sort_values("date")
        .reset_index(drop=True)
    )
    values: list[float] = []
    start = max(20, len(merged) - lookback)
    for i in range(start, len(merged)):
        c0, c1 = merged["close_c"].iloc[i - 20], merged["close_c"].iloc[i]
        b0, b1 = merged["close_b"].iloc[i - 20], merged["close_b"].iloc[i]
        if c0 and b0:
            values.append(float(c1 / c0 - b1 / b0))
    return values


def compute_theme_metrics(
    theme: Theme,
    carrier: pd.DataFrame | None,
    leader_frames: dict[str, pd.DataFrame],
    baseline: pd.DataFrame | None,
    target_date: date,
    catalyst_count: int,
    limit_up: dict[str, Any] | None,
    market_note: str = "",
    rs_trend_days: int = 3,
    leader_high_return: float = 0.20,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """rs_trend_days / leader_high_return 由 config.yaml 的 thresholds 传入。

    allow_stale=True 用于盘前快照：行情尚未更新到目标日时，
    使用各标的最近一个交易日的数据，而不是把龙头判为缺失。
    """
    trend_days = max(int(rs_trend_days), 1)

    extras: dict[str, Any] = {"limit_up_series": None, "limit_up_sectors": None}
    notes: list[str] = []
    if market_note:
        notes.append(market_note)

    empty = {
        "theme_id": theme.id,
        "name": theme.name,
        "tier": theme.tier,
        "carrier": theme.carrier_label,
        "carrier_type": theme.carrier_type,
        "rs": None,
        "rs_prev": None,
        "rs_rising": False,
        "trend_above_ma20": False,
        "trend_ma5_over_ma10": False,
        "trend_golden_cross": False,
        "volume_ratio": None,
        "breadth": None,
        "breadth_covered": 0,
        "catalyst_count": catalyst_count,
        "limit_up_count": (limit_up or {}).get("count"),
        "limit_up_trend": (limit_up or {}).get("trend"),
        "ret_5d": None,
        "note": "",
        "extras": extras,
    }

    if carrier is None or carrier.empty or baseline is None or baseline.empty:
        notes.append("载体或基准行情缺失")
        empty["note"] = "；".join(notes)
        return empty

    carrier = _tail_until(carrier, target_date)
    baseline = _tail_until(baseline, target_date)
    if len(carrier) < 22 or len(baseline) < 22:
        notes.append("历史数据不足22个交易日")
        empty["note"] = "；".join(notes)
        return empty

    closes = carrier["close"]
    lookback = max(trend_days + 1, 4)
    rs_values = _rs_history(carrier, baseline, lookback=lookback)
    rs = rs_values[-1] if rs_values else None
    rs_prev = rs_values[-2] if len(rs_values) >= 2 else None
    recent_rs = rs_values[-trend_days:]
    rs_rising = len(recent_rs) == trend_days and all(
        recent_rs[i] > recent_rs[i - 1] for i in range(1, len(recent_rs))
    )
    rs_falling = len(recent_rs) == trend_days and all(
        recent_rs[i] < recent_rs[i - 1] for i in range(1, len(recent_rs))
    )

    ma20 = float(closes.iloc[-20:].mean())
    above_ma20 = bool(closes.iloc[-1] > ma20)
    ma5 = float(closes.iloc[-5:].mean())
    ma10 = float(closes.iloc[-10:].mean())
    ma5_over_ma10 = bool(ma5 > ma10)
    prev_ma5 = float(closes.iloc[-6:-1].mean())
    prev_ma10 = float(closes.iloc[-11:-1].mean())
    golden_cross = bool(ma5_over_ma10 and prev_ma5 <= prev_ma10)

    amounts = carrier["amount"]
    volume_ratio = None
    if len(amounts) >= 21:
        avg_amount = float(amounts.iloc[-21:-1].mean())
        if avg_amount > 0:
            volume_ratio = float(amounts.iloc[-1] / avg_amount)

    ret_5d = _ret(closes, 5)

    covered = 0
    below = 0
    high_ret5 = 0
    stale_leaders: list[str] = []
    for leader in theme.leaders:
        frame = leader_frames.get(leader["code"])
        if frame is None or frame.empty:
            continue
        frame = _tail_until(frame, target_date)
        if frame.empty:
            continue
        if frame["date"].iloc[-1] != target_date and not allow_stale:
            stale_leaders.append(leader["code"])
            continue
        if len(frame) < 20:
            continue
        covered += 1
        leader_closes = frame["close"]
        if leader_closes.iloc[-1] <= float(leader_closes.iloc[-20:].mean()):
            below += 1
        leader_ret5 = _ret(leader_closes, 5)
        if leader_ret5 is not None and leader_ret5 > leader_high_return:
            high_ret5 += 1

    breadth = (covered - below) / covered if covered else None
    leader_high_ratio = high_ret5 / covered if covered else None

    extras.update(
        {
            "rs_series": [round(value, 6) for value in rs_values],
            "rs_trend_days": trend_days,
            "rs_rising": rs_rising,
            "rs_falling": rs_falling,
            "leaders_total": len(theme.leaders),
            "leaders_covered": covered,
            "leaders_stale": stale_leaders,
            "leaders_below_ma20": below,
            "leader_ret5_high_ratio": leader_high_ratio,
            "leader_ret5_high_threshold": leader_high_return,
            "carrier_last_date": str(carrier["date"].iloc[-1]),
            "baseline_last_date": str(baseline["date"].iloc[-1]),
            "limit_up_series": (limit_up or {}).get("series"),
            "limit_up_sectors": (limit_up or {}).get("sectors"),
        }
    )

    if covered < len(theme.leaders):
        notes.append(f"龙头行情覆盖 {covered}/{len(theme.leaders)}")
    if stale_leaders:
        notes.append(f"{len(stale_leaders)} 只龙头行情未更新到当日")

    row = dict(empty)
    row.update(
        {
            "rs": rs,
            "rs_prev": rs_prev,
            "rs_rising": rs_rising,
            "trend_above_ma20": above_ma20,
            "trend_ma5_over_ma10": ma5_over_ma10,
            "trend_golden_cross": golden_cross,
            "volume_ratio": volume_ratio,
            "breadth": breadth,
            "breadth_covered": covered,
            "ret_5d": ret_5d,
            "note": "；".join(notes),
        }
    )
    return row
