"""美股期货、美元、美债、商品与汇率采集。

数据源：新浪全球期货/外汇实时行情 + 东方财富中美国债收益率（akshare）。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import akshare as ak
import requests

from ..logging_setup import now

SINA_URL = "https://hq.sinajs.cn/list="
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.sina.com.cn",
}

FUTURES = [
    ("hf_ES", "标普500期货", "美股期货"),
    ("hf_NQ", "纳斯达克期货", "美股期货"),
    ("hf_YM", "道琼斯期货", "美股期货"),
    ("hf_CHA50CFD", "富时中国A50期货", "A股相关"),
    ("hf_CL", "纽约原油", "商品"),
    ("hf_GC", "纽约黄金", "商品"),
    ("hf_HG", "美铜", "商品"),
]


def _impact(symbol: str, change: float | None, unit: str = "%") -> str:
    if change is None:
        return "数据缺失，无法判断外围环境。"
    if abs(change) < 1e-9:
        return "行情持平，对A股主线影响中性。"
    up = change > 0
    if symbol in {"hf_ES", "hf_NQ", "hf_YM"}:
        tail = "成长风格风险偏好偏暖" if up else "成长风格风险偏好承压"
        return f"美股期货{'走强' if up else '走弱'}，{tail}（AI算力/半导体）。"
    if symbol == "hf_CHA50CFD":
        return f"A50期货{'上涨' if up else '下跌'}，反映外资对A股大盘的情绪{'改善' if up else '转弱'}。"
    if symbol == "DINIW":
        return f"美元指数{'走强' if up else '走弱'}，对黄金/有色等美元计价品种{'偏压制' if up else '偏友好'}。"
    if symbol == "US10Y":
        return f"美债10年期收益率{'上行' if up else '下行'}，对高估值成长风格{'偏压制' if up else '偏友好'}（单位：百分点）。"
    if symbol == "hf_CL":
        return f"油价{'上行' if up else '下行'}，对能源/周期{'偏利好' if up else '偏压制'}，中下游成本端{'承压' if up else '改善'}。"
    if symbol == "hf_GC":
        return f"金价{'上行' if up else '下行'}，对黄金主线{'偏利好' if up else '偏压制'}。"
    if symbol == "hf_HG":
        return f"铜价{'上行' if up else '下行'}，对有色/周期{'偏利好' if up else '偏压制'}。"
    if symbol == "fx_susdcny":
        return f"人民币{'贬值' if up else '升值'}，对出口链{'偏利好' if up else '偏压制'}，进口成本{'上升' if up else '下降'}。"
    return "仅作外围环境参考。"


def _parse_sina(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for match in re.finditer(r'var hq_str_(\w+)="([^"]*)"', text):
        code, payload = match.group(1), match.group(2)
        if payload:
            out[code] = payload.split(",")
    return out


def _fetch_sina(codes: list[str]) -> dict[str, list[str]]:
    resp = requests.get(SINA_URL + ",".join(codes), headers=HEADERS, timeout=25)
    resp.encoding = "gbk"
    resp.raise_for_status()
    return _parse_sina(resp.text)


def _quote_date(quote_time: str | None) -> str | None:
    match = re.search(r"20\d{2}-\d{2}-\d{2}", quote_time or "")
    return match.group(0) if match else None


def _stale_for_target(quote_time: str | None, target_date: date) -> bool:
    """历史回补时，实时源行情只接受当日或次日（跨夜美股时段）的数据。"""
    if target_date == now().date():
        return False
    allowed = {str(target_date), str(target_date + timedelta(days=1))}
    return _quote_date(quote_time) not in allowed


def _quote_row(
    symbol: str,
    name: str,
    category: str,
    latest: float | None,
    change_pct: float | None,
    quote_time: str | None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "name": name,
        "category": category,
        "latest": latest,
        "change_pct": change_pct,
        "note": _impact(symbol, change_pct),
        "quote_time": quote_time,
    }


def fetch_global_quotes(config: dict[str, Any], target_date: date) -> tuple[list[dict], list[str]]:
    failures: list[str] = []
    quotes: list[dict] = []
    sources = set(config["sources"].get("market_api", []))

    if "sina_global" in sources:
        codes = [item[0] for item in FUTURES] + ["DINIW", "fx_susdcny"]
        try:
            parsed = _fetch_sina(codes)
        except Exception as exc:
            parsed = {}
            failures.append(f"新浪外盘行情: {type(exc).__name__}")

        for symbol, name, category in FUTURES:
            fields = parsed.get(symbol)
            if not fields or len(fields) < 14:
                failures.append(f"外盘 {name}: 无数据")
                continue
            try:
                latest = float(fields[0])
                prev = float(fields[7])
                change = (latest - prev) / prev * 100 if prev else None
            except (ValueError, IndexError):
                failures.append(f"外盘 {name}: 解析失败")
                continue
            quote_time = f"{fields[12]} {fields[6]}"
            if _stale_for_target(quote_time, target_date):
                failures.append(f"外盘 {name}: 实时源未提供 {target_date} 历史行情")
                continue
            quotes.append(_quote_row(symbol, name, category, latest, change, quote_time))

        fields = parsed.get("DINIW")
        if fields and len(fields) >= 11:
            try:
                latest = float(fields[8])
                prev = float(fields[3])
                change = (latest - prev) / prev * 100 if prev else None
                quote_time = f"{fields[10]} {fields[0]}"
                if _stale_for_target(quote_time, target_date):
                    failures.append(f"外盘 美元指数: 实时源未提供 {target_date} 历史行情")
                else:
                    quotes.append(
                        _quote_row("DINIW", "美元指数", "汇率", latest, change, quote_time)
                    )
            except (ValueError, IndexError):
                failures.append("美元指数: 解析失败")
        else:
            failures.append("美元指数: 无数据")

        fields = parsed.get("fx_susdcny")
        if fields and len(fields) >= 9:
            try:
                latest = float(fields[8])
                prev = float(fields[3])
                change = (latest - prev) / prev * 100 if prev else None
                quote_time = fields[0]
                for token in reversed(fields):
                    if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", token.strip()):
                        quote_time = f"{token} {fields[0]}"
                        break
                if _stale_for_target(quote_time, target_date):
                    failures.append(f"外盘 人民币汇率: 实时源未提供 {target_date} 历史行情")
                else:
                    quotes.append(
                        _quote_row("fx_susdcny", "在岸人民币", "汇率", latest, change, quote_time)
                    )
            except (ValueError, IndexError):
                failures.append("人民币汇率: 解析失败")
        else:
            failures.append("人民币汇率: 无数据")

    if "eastmoney_bond" in sources:
        try:
            df = ak.bond_zh_us_rate(start_date=str(target_date - timedelta(days=20)))
            df = df.dropna(subset=["美国国债收益率10年"])
            df["日期"] = df["日期"].astype(str)
            df = df[df["日期"] <= str(target_date)].sort_values("日期")
            if df.empty:
                failures.append("美债收益率: 无数据")
            else:
                latest = float(df["美国国债收益率10年"].iloc[-1])
                prev = float(df["美国国债收益率10年"].iloc[-2]) if len(df) > 1 else latest
                change = latest - prev
                quotes.append(
                    _quote_row("US10Y", "美国10年期国债收益率", "利率", latest, change, df["日期"].iloc[-1])
                )
        except Exception as exc:
            failures.append(f"美债收益率: {type(exc).__name__}")

    display_order = [
        "标普500期货", "纳斯达克期货", "道琼斯期货", "富时中国A50期货",
        "美元指数", "美国10年期国债收益率", "纽约原油", "纽约黄金", "美铜", "在岸人民币",
    ]
    order = {name: idx for idx, name in enumerate(display_order)}
    quotes.sort(key=lambda row: order.get(row["name"], 99))
    return quotes, failures
