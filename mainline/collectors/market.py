"""A 股行情采集：指数载体、龙头个股、基准指数。

数据源：
- 指数日线：腾讯（akshare stock_zh_index_daily_tx），含成交额；
- 个股日线：新浪（akshare stock_zh_a_daily，前复权），含成交额。
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import akshare as ak
import pandas as pd

from ..config import Theme


def _retry(fn, *args, attempts: int = 3, delay: float = 1.0, **kwargs):
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # 单源失败由上层降级
            last_exc = exc
            time.sleep(delay * (i + 1))
    raise last_exc  # type: ignore[misc]


def index_symbol(code: str) -> str:
    return ("sz" if code.startswith(("399", "980")) else "sh") + code


def stock_symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "9")) else "sz") + code


def _normalize(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    for col in columns:
        if col not in out.columns:
            out[col] = 0.0
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
    return out[["date", *columns]]


def fetch_index_daily(code: str) -> pd.DataFrame:
    df = _retry(ak.stock_zh_index_daily_tx, symbol=index_symbol(code))
    return _normalize(df, ["open", "close", "high", "low", "amount"])


def fetch_stock_daily(code: str) -> pd.DataFrame:
    df = _retry(ak.stock_zh_a_daily, symbol=stock_symbol(code), adjust="qfq")
    if "amount" not in df.columns:
        df["amount"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(
            df.get("volume", 0), errors="coerce"
        )
    return _normalize(df, ["open", "close", "high", "low", "amount"])


def build_basket(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """龙头等权组合：以共同交易日首日为基准归一化，成交额求和。"""
    merged: pd.DataFrame | None = None
    for code, df in frames.items():
        if df.empty:
            continue
        part = df[["date", "close", "amount"]].rename(
            columns={"close": f"c_{code}", "amount": f"a_{code}"}
        )
        merged = part if merged is None else merged.merge(part, on="date", how="inner")
    if merged is None or merged.empty:
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "amount"])

    merged = merged.sort_values("date").tail(80).reset_index(drop=True)
    close_cols = [c for c in merged.columns if c.startswith("c_")]
    amount_cols = [c for c in merged.columns if c.startswith("a_")]
    normalized = merged[close_cols].div(merged[close_cols].iloc[0])
    level = normalized.mean(axis=1) * 1000.0
    return pd.DataFrame(
        {
            "date": merged["date"],
            "open": level,
            "close": level,
            "high": level,
            "low": level,
            "amount": merged[amount_cols].sum(axis=1),
        }
    )


def fetch_theme_market_data(
    config: dict[str, Any], themes: list[Theme], target_date: date
) -> dict[str, Any]:
    market_sources = set(config["sources"].get("market_api", []))
    failures: list[str] = []
    notes: dict[str, str] = {}

    baseline_code = str(config["baseline_index"])
    baseline: pd.DataFrame | None = None
    if "tencent_index" in market_sources:
        try:
            baseline = fetch_index_daily(baseline_code)
        except Exception as exc:
            failures.append(f"基线指数 {baseline_code}: {type(exc).__name__}")

    leader_frames: dict[str, pd.DataFrame] = {}
    leader_codes = sorted({code for theme in themes for code in theme.leader_codes})
    if "sina_stock" in market_sources:
        for code in leader_codes:
            try:
                leader_frames[code] = fetch_stock_daily(code)
            except Exception as exc:
                failures.append(f"龙头行情 {code}: {type(exc).__name__}")

    carrier_frames: dict[str, pd.DataFrame | None] = {}
    for theme in themes:
        if theme.carrier_type == "index":
            if "tencent_index" not in market_sources:
                carrier_frames[theme.id] = None
                failures.append(f"载体 {theme.id}: 指数源未启用")
                continue
            try:
                carrier_frames[theme.id] = fetch_index_daily(theme.index_code or "")
            except Exception as exc:
                carrier_frames[theme.id] = None
                failures.append(f"载体 {theme.id}: {type(exc).__name__}")
        else:
            members = {
                code: frame for code, frame in leader_frames.items() if code in theme.leader_codes
            }
            if not members:
                carrier_frames[theme.id] = None
                failures.append(f"载体 {theme.id}: 龙头行情全部缺失")
                continue
            if len(members) < len(theme.leaders):
                notes[theme.id] = f"组合成分缺失 {len(theme.leaders) - len(members)} 只"
            carrier_frames[theme.id] = build_basket(members)

    for theme in themes:
        frame = carrier_frames.get(theme.id)
        if frame is None or frame.empty:
            continue
        latest = frame["date"].iloc[-1]
        if latest < target_date:
            notes[theme.id] = (
                notes.get(theme.id, "")
                + f"载体最新日期 {latest} 未到 {target_date}"
            ).strip()

    return {
        "baseline": baseline,
        "carriers": carrier_frames,
        "leaders": leader_frames,
        "failures": failures,
        "notes": notes,
    }
