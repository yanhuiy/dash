"""主线状态判定与切换确认。"""

from __future__ import annotations

from typing import Any


def _strong_ok(row: dict, thr: dict, aux_enabled: bool, aux_min: int) -> bool:
    if row["rs"] is None or row["breadth"] is None or row["volume_ratio"] is None:
        return False
    base = (
        row.get("rs_rising")
        and row["volume_ratio"] > float(thr["volume_ratio_strong"])
        and row["breadth"] > float(thr["breadth_strong"])
        and int(row["catalyst_count"]) >= 1
    )
    if not base:
        return False
    if aux_enabled:
        return bool(row.get("limit_up_count")) and row.get("limit_up_trend") == "递增" and int(
            row["limit_up_count"]
        ) >= aux_min
    return True


def _weak_ok(row: dict, thr: dict) -> bool:
    if row["rs"] is None or row["breadth"] is None or row["volume_ratio"] is None:
        return False
    below = int(row["extras"].get("leaders_below_ma20", 0))
    return bool(
        row["extras"].get("rs_falling")
        and row["breadth"] < float(thr["breadth_weak"])
        and below >= int(thr.get("leaders_below_ma20_min", 2))
        and row["volume_ratio"] < float(thr["volume_ratio_weak"])
    )


def _crowded_ok(row: dict, thr: dict) -> bool:
    first = (
        row["ret_5d"] is not None
        and row["volume_ratio"] is not None
        and row["ret_5d"] > float(thr["crowded_5d_return"])
        and row["volume_ratio"] > float(thr["crowded_volume_ratio"])
    )
    ratio = row["extras"].get("leader_ret5_high_ratio")
    second = ratio is not None and ratio >= float(thr["crowded_leader_ratio"])
    return bool(first or second)


def _reason(row: dict, status: str, aux_enabled: bool, thr: dict) -> str:
    if status == "strengthen":
        parts = [
            f"RS连续{int(thr.get('rs_trend_days', 3))}日上升",
            f"量能{row['volume_ratio']:.2f}倍",
            f"宽度{row['breadth']:.0%}",
            f"催化{row['catalyst_count']}条",
        ]
        if aux_enabled:
            parts.append(f"涨停{row['limit_up_count']}家/{row['limit_up_trend']}")
        return "｜".join(parts)
    if status == "weaken":
        parts = [
            f"RS连续{int(thr.get('rs_trend_days', 3))}日下降",
            f"宽度{row['breadth']:.0%}",
            f"{row['extras'].get('leaders_below_ma20', 0)}只龙头跌破20日线",
            f"量能{row['volume_ratio']:.2f}倍",
        ]
        return "｜".join(parts)
    if status == "crowded":
        parts = []
        if row["ret_5d"] is not None:
            parts.append(f"5日涨幅{row['ret_5d']:+.1%}")
        if row["volume_ratio"] is not None:
            parts.append(f"量能{row['volume_ratio']:.2f}倍")
        ratio = row["extras"].get("leader_ret5_high_ratio")
        if ratio is not None and ratio >= float(thr.get("crowded_leader_ratio", 0.6)):
            high_ret = float(thr.get("crowded_leader_5d_return", 0.20))
            parts.append(f"{ratio:.0%}龙头5日涨幅>{high_ret:.0%}")
        return "｜".join(parts) if parts else "短期涨幅或成交过热"
    return "未满足强化/走弱/拥挤条件"


def evaluate_base(
    row: dict[str, Any],
    thresholds: dict,
    aux_enabled: bool,
    aux_min: int,
) -> tuple[str, str]:
    if _strong_ok(row, thresholds, aux_enabled, aux_min):
        return "strengthen", _reason(row, "strengthen", aux_enabled, thresholds)
    if _weak_ok(row, thresholds):
        return "weaken", _reason(row, "weaken", aux_enabled, thresholds)
    if _crowded_ok(row, thresholds):
        return "crowded", _reason(row, "crowded", aux_enabled, thresholds)
    return "observe", _reason(row, "observe", aux_enabled, thresholds)


def _held_for(
    theme_id: str,
    history: list[tuple[str, dict[str, dict]]],
    allowed: set[str],
    days: int,
) -> bool:
    """检查该主线在最近 days 个历史快照中是否连续处于 allowed 状态。"""
    if days <= 0:
        return True
    if len(history) < days:
        return False
    for _, snapshot in history[-days:]:
        row = snapshot.get(theme_id)
        if row is None or row.get("status") not in allowed:
            return False
    return True


def apply_switch(
    base_states: dict[str, str],
    metrics: dict[str, dict],
    prev_themes: dict[str, dict],
    confirm_days: int = 2,
    history: list[tuple[str, dict[str, dict]]] | None = None,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """新主线强化 + 旧主线走弱同时成立时，标记切换候选/确认。

    confirm_days 为确认所需的连续天数（含当日），历史不足则保持候选。
    """
    strong_ids = [tid for tid, state in base_states.items() if state == "strengthen"]
    weak_ids = [tid for tid, state in base_states.items() if state == "weaken"]
    statuses = dict(base_states)
    if not strong_ids or not weak_ids:
        return statuses, None

    new_id = max(strong_ids, key=lambda tid: metrics[tid].get("rs") or -9)
    old_id = min(weak_ids, key=lambda tid: metrics[tid].get("rs") or 9)
    required_prior = max(int(confirm_days) - 1, 0)
    if history is None:
        history = [("", prev_themes)] if prev_themes else []
    confirmed = _held_for(
        new_id,
        history,
        {"strengthen", "switch_candidate", "switch_confirmed"},
        required_prior,
    ) and _held_for(old_id, history, {"weaken"}, required_prior)

    statuses[new_id] = "switch_confirmed" if confirmed else "switch_candidate"
    meta = {
        "new": new_id,
        "old": old_id,
        "confirmed": confirmed,
        "confirm_days": int(confirm_days),
        "prev_new_status": prev_themes.get(new_id, {}).get("status"),
        "prev_old_status": prev_themes.get(old_id, {}).get("status"),
    }
    return statuses, meta
