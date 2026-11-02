"""三大方向选择、切换雷达与描述性结论。"""

from __future__ import annotations

from typing import Any

from .labels import status_label


STRONG_STATES = {"strengthen", "crowded"}
SWITCH_STATES = {"switch_candidate", "switch_confirmed"}
WEAK_STATE = "weaken"
PREV_STRONG = {"strengthen", "switch_candidate", "switch_confirmed", "crowded"}

# 三大方向选取方式，供展示层说明“是否为相对排名兜底”。
BASIS_LABELS = {
    "status": "状态规则",
    "rs_rank": "相对排名兜底 · RS 最高且为正",
    "rs_improve": "相对排名兜底 · 近3日RS改善最大",
    "rs_weak_5d": "相对排名兜底 · 5日跌幅最大",
    "rs_weak": "相对排名兜底 · RS 最低",
}


def _rs(row: dict) -> float:
    value = row.get("rs")
    return -9.0 if value is None else float(value)


def _has_rs(row: dict) -> bool:
    return row.get("rs") is not None


def _rs_change(row: dict, days: int = 3) -> float | None:
    """近 days 日 RS 变化（首尾差值），数据不足返回 None。"""
    series = (row.get("extras") or {}).get("rs_series") or []
    if len(series) <= days:
        return None
    return float(series[-1]) - float(series[-1 - days])


def _ret_5d(row: dict) -> float | None:
    value = row.get("ret_5d")
    return None if value is None else float(value)


def choose_directions(
    metrics: dict[str, dict[str, Any]],
    statuses: dict[str, str],
    prev_themes: dict[str, dict],
) -> dict[str, Any]:
    """规则状态优先；对应状态未触发时按相对排名兜底，避免三张卡全空。"""
    directions: dict[str, Any] = {"strongest": None, "switching": None, "weakening": None}
    basis: dict[str, str] = {}

    strong = [tid for tid, state in statuses.items() if state in STRONG_STATES]
    if strong:
        directions["strongest"] = max(strong, key=lambda tid: _rs(metrics[tid]))
        basis["strongest"] = "status"

    switch_ids = [tid for tid, state in statuses.items() if state in SWITCH_STATES]
    if switch_ids:
        directions["switching"] = max(switch_ids, key=lambda tid: _rs(metrics[tid]))
        basis["switching"] = "status"
    else:
        newly = [
            tid for tid, state in statuses.items()
            if state == "strengthen"
            and tid != directions["strongest"]
            and prev_themes.get(tid, {}).get("status") not in PREV_STRONG
        ]
        if newly:
            directions["switching"] = max(newly, key=lambda tid: _rs(metrics[tid]))
            basis["switching"] = "status"

    weak = [tid for tid, state in statuses.items() if state == WEAK_STATE]
    if weak:
        directions["weakening"] = min(weak, key=lambda tid: _rs(metrics[tid]))
        basis["weakening"] = "status"

    used = {tid for tid in directions.values() if tid}

    if directions["strongest"] is None:
        pool = [
            tid for tid, row in metrics.items()
            if tid not in used and _has_rs(row) and float(row["rs"]) > 0
            and statuses.get(tid) != WEAK_STATE
        ]
        if pool:
            directions["strongest"] = max(pool, key=lambda tid: float(metrics[tid]["rs"]))
            basis["strongest"] = "rs_rank"
            used.add(directions["strongest"])

    if directions["switching"] is None:
        pool = [
            (tid, _rs_change(row)) for tid, row in metrics.items()
            if tid not in used and statuses.get(tid) != WEAK_STATE
        ]
        pool = [(tid, change) for tid, change in pool if change is not None]
        if pool:
            directions["switching"] = max(pool, key=lambda item: item[1])[0]
            basis["switching"] = "rs_improve"
            used.add(directions["switching"])

    if directions["weakening"] is None:
        pool = [tid for tid, row in metrics.items() if tid not in used and _has_rs(row)]
        decliners = [
            tid for tid in pool
            if _ret_5d(metrics[tid]) is not None and _ret_5d(metrics[tid]) < 0
        ]
        if decliners:
            directions["weakening"] = min(
                decliners, key=lambda tid: (_ret_5d(metrics[tid]), _rs(metrics[tid]))
            )
            basis["weakening"] = "rs_weak_5d"
        elif pool:
            directions["weakening"] = min(pool, key=lambda tid: _rs(metrics[tid]))
            basis["weakening"] = "rs_weak"

    directions["basis"] = basis
    return directions


def conclusion(row: dict[str, Any], status: str) -> str:
    parts = []
    if row.get("rs") is not None:
        parts.append(f"RS {row['rs'] * 100:+.1f}%")
    if row.get("volume_ratio") is not None:
        parts.append(f"量能 {row['volume_ratio']:.2f}倍")
    if row.get("breadth") is not None:
        parts.append(f"龙头宽度 {row['breadth']:.0%}")
    parts.append(f"近5日催化 {row['catalyst_count']}条")
    if row.get("limit_up_count") is not None:
        parts.append(f"涨停 {row['limit_up_count']}家/{row.get('limit_up_trend') or '—'}")
    return "，".join(parts) + f"；状态{status_label(status)}。"


CHANGE_LABELS = {
    "switch_confirmed": "切换确认",
    "switch_candidate": "切换候选",
    "strengthen": "新强化",
    "weaken": "新走弱",
    "crowded": "转入拥挤",
    "observe": "回归观察",
}


def build_radar(
    metrics: dict[str, dict[str, Any]],
    statuses: dict[str, str],
    prev_themes: dict[str, dict],
    switch_meta: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not prev_themes:
        return []
    rows: list[dict[str, Any]] = []
    for theme_id, state in statuses.items():
        prev = prev_themes.get(theme_id, {}).get("status")
        if prev is None or prev == state:
            continue
        detail = conclusion(metrics[theme_id], state)
        if switch_meta and theme_id == switch_meta.get("new"):
            old_name = metrics[switch_meta["old"]]["name"]
            confirm_days = int(switch_meta.get("confirm_days", 2))
            verdict = (
                f"连续{confirm_days}日成立，已确认"
                if switch_meta.get("confirmed")
                else "首日成立，待确认"
            )
            detail = f"{metrics[theme_id]['name']}强化 + {old_name}走弱，{verdict}。"
        rows.append(
            {
                "theme_id": theme_id,
                "name": metrics[theme_id]["name"],
                "prev": prev,
                "curr": state,
                "change_type": CHANGE_LABELS.get(state, "状态变化"),
                "detail": detail,
            }
        )
    priority = {
        "switch_confirmed": 0, "switch_candidate": 1, "strengthen": 2,
        "weaken": 3, "crowded": 4, "observe": 5,
    }
    rows.sort(key=lambda row: (priority.get(row["curr"], 9), row["name"]))
    return rows
