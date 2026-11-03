"""读取 breakout_v3/daily 下的持仓与观察清单，仅用于 D 区块展示。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def daily_dir(config: dict[str, Any]) -> Path | None:
    value = str((config.get("breakout") or {}).get("daily_dir") or "").strip()
    return Path(value) if value else None


def _read_csv(path: Path) -> list[dict[str, str]] | None:
    """文件不存在或读取失败返回 None，与“文件存在但无数据”区分。"""
    if not path.exists():
        return None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            with open(path, "r", encoding=encoding, newline="") as fh:
                return [dict(row) for row in csv.DictReader(fh)]
        except UnicodeDecodeError:
            continue
        except (OSError, csv.Error):
            return None
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_holdings(config: dict[str, Any]) -> list[dict[str, str]] | None:
    """持仓：代码 / 名称 / 信号 / 成本价 / 买入日期 / 层级。"""
    root = daily_dir(config)
    if root is None:
        return None
    rows = _read_csv(root / "holdings.csv")
    if rows is None:
        return None
    out: list[dict[str, str]] = []
    for row in rows:
        code = _text(row.get("代码"))
        if not code:
            continue
        out.append(
            {
                "code": code,
                "name": _text(row.get("名称")) or code,
                "signal": _text(row.get("信号")) or "—",
                "cost": _text(row.get("成本价")) or "—",
                "buy_date": _text(row.get("买入日期")) or "—",
                "tier": _text(row.get("层级")) or "主线",
            }
        )
    return out


def load_watchlist(config: dict[str, Any]) -> list[dict[str, str]] | None:
    """个股观察清单：代码 / 名称 / 层级 / 备注。"""
    root = daily_dir(config)
    if root is None:
        return None
    rows = _read_csv(root / "watchlist.csv")
    if rows is None:
        return None
    out: list[dict[str, str]] = []
    for row in rows:
        code = _text(row.get("代码"))
        if not code:
            continue
        out.append(
            {
                "code": code,
                "name": _text(row.get("名称")) or code,
                "tier": _text(row.get("层级")) or "观察",
                "note": _text(row.get("备注")) or "—",
            }
        )
    return out
