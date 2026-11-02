"""涨停数据采集与细分方向聚类（仅作情绪验证，不参与核心判定）。"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import akshare as ak
import pandas as pd

from ..config import Theme


def _retry(fn, *args, attempts: int = 2, delay: float = 1.0, **kwargs):
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            time.sleep(delay * (i + 1))
    raise last_exc  # type: ignore[misc]


def trend_label(series: list[int]) -> str:
    values = [v for v in series if v is not None]
    if len(values) < 2:
        return "持平"
    recent = values[-3:]
    if recent[-1] > recent[0]:
        return "递增"
    if recent[-1] < recent[0]:
        return "递减"
    return "持平"


def _theme_ids_for_sector(sector: str, themes: list[Theme]) -> list[str]:
    # 东财行业名偶有截断（如“汽车零部”），双向包含判断
    return [
        theme.id for theme in themes
        if any(tag in sector or sector in tag for tag in theme.limit_up_tags)
    ]


def collect(
    config: dict[str, Any],
    themes: list[Theme],
    trading_days: list[date],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str], bool]:
    """返回：聚类明细、各主线涨停热度、失败源、今日数据是否可用。"""
    sources = set(config["sources"].get("limit_up", []))
    if "eastmoney_limitup" not in sources:
        return [], {}, ["涨停源未启用"], False

    days = trading_days[-5:]
    failures: list[str] = []
    day_counts: dict[date, dict[str, int]] = {}
    day_lianban: dict[date, dict[str, int]] = {}
    day_first: dict[date, dict[str, int]] = {}
    available_days: set[date] = set()

    for day in days:
        try:
            df = _retry(ak.stock_zt_pool_em, date=day.strftime("%Y%m%d"))
        except Exception as exc:
            failures.append(f"涨停池 {day}: {type(exc).__name__}")
            day_counts[day] = {}
            day_lianban[day] = {}
            day_first[day] = {}
            continue
        available_days.add(day)
        if df is None or df.empty:
            day_counts[day] = {}
            day_lianban[day] = {}
            day_first[day] = {}
            continue
        df = df.copy()
        df["所属行业"] = df.get("所属行业", "未分类").fillna("未分类").astype(str)
        df["连板数"] = pd.to_numeric(df.get("连板数", 1), errors="coerce").fillna(1)
        day_counts[day] = df.groupby("所属行业").size().to_dict()
        day_lianban[day] = df[df["连板数"] >= 2].groupby("所属行业").size().to_dict()
        day_first[day] = df[df["连板数"] == 1].groupby("所属行业").size().to_dict()

    target = days[-1]
    today_counts = day_counts.get(target, {})
    today_available = target in available_days
    min_count = int(config["limit_up"].get("min_count_per_day", 3))

    rows: list[dict[str, Any]] = []
    for sector, count in sorted(today_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        series = [
            day_counts[day].get(sector, 0) if day in available_days else None
            for day in days
        ]
        theme_ids = _theme_ids_for_sector(sector, themes)
        prev_count = series[-2] if len(series) >= 2 and series[-2] is not None else 0
        note = ""
        if count >= min_count and prev_count >= min_count and not theme_ids:
            note = f"连续2日≥{min_count}家，未在主线池，进入候选观察"
        rows.append(
            {
                "sector": sector,
                "count": int(count),
                "lianban": int(day_lianban.get(target, {}).get(sector, 0)),
                "first_board": int(day_first.get(target, {}).get(sector, 0)),
                "series": series,
                "trend": trend_label(series),
                "in_theme_pool": bool(theme_ids),
                "theme_ids": theme_ids,
                "note": note,
            }
        )

    all_sectors = sorted({sector for counts in day_counts.values() for sector in counts})
    theme_heat: dict[str, dict[str, Any]] = {}
    for theme in themes:
        matched = [
            sector for sector in all_sectors
            if theme.id in _theme_ids_for_sector(sector, themes)
        ]
        series = []
        for day in days:
            if day not in available_days:
                series.append(None)
                continue
            series.append(sum(day_counts[day].get(sector, 0) for sector in matched))
        theme_heat[theme.id] = {
            "count": int(sum(today_counts.get(sector, 0) for sector in matched)) if today_available else None,
            "series": series,
            "trend": trend_label(series) if today_available else None,
            "sectors": matched,
        }

    return rows, theme_heat, failures, today_available
