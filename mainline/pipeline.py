"""每日盘后流水线：采集 → 指标 → 状态 → 三大方向 → 落库。"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import akshare as ak
import pandas as pd

from . import db
from .collectors import global_market, limitup, market, news
from .config import load_config, load_holdings, load_themes
from .directions import build_radar, choose_directions
from .indicators import compute_theme_metrics
from .logging_setup import now, setup_logging
from .status import apply_switch, evaluate_base
from .summarize import llm_enrich


def _trade_calendar() -> list[date]:
    try:
        df = ak.tool_trade_date_hist_sina()
        column = "trade_date" if "trade_date" in df.columns else df.columns[0]
        return sorted(pd.to_datetime(df[column]).dt.date.tolist())
    except Exception:
        return []


def _select_display(items: list[dict], category: str, limit: int) -> list[dict]:
    """按发布时间取最新条目，保持同一日期内的来源顺序。"""
    pool = [item for item in items if item["category"] == category]
    pool.sort(key=lambda item: item.get("published_at", ""), reverse=True)
    return pool[:limit]


def _catalyst_counts(
    items: list[dict], themes, window_dates: list[date], target: date
) -> tuple[dict[str, int], dict[str, int]]:
    window = set(window_dates)
    five_day: dict[str, set[str]] = {theme.id: set() for theme in themes}
    today: dict[str, set[str]] = {theme.id: set() for theme in themes}
    for item in items:
        if not item.get("matched_themes"):
            continue
        try:
            pub = date.fromisoformat(item["published_at"][:10])
        except ValueError:
            continue
        if pub in window:
            for theme_id in item["matched_themes"].split(","):
                five_day.get(theme_id, set()).add(item["event_key"])
        if pub == target:
            for theme_id in item["matched_themes"].split(","):
                today.get(theme_id, set()).add(item["event_key"])
    return (
        {tid: len(values) for tid, values in five_day.items()},
        {tid: len(values) for tid, values in today.items()},
    )


def run(
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    force: bool = False,
    keep_target: bool = False,
    logger=None,
) -> dict[str, Any]:
    """执行一次采集与落库。

    keep_target=True 时，即使基准指数尚未更新到当日（盘前/盘中），
    也按当日生成快照，避免把上一交易日的归档快照覆盖掉。
    """
    config = config or load_config()
    logger = logger or setup_logging(config)
    themes = load_themes(config)
    holdings = load_holdings(config)

    conn = db.connect(config["output"]["sqlite_path"])
    db.init_db(conn)
    started = now()
    requested = target_date

    calendar = _trade_calendar()
    calendar_set = set(calendar)
    if requested is None:
        if not force and calendar and started.date() not in calendar_set:
            logger.info("非交易日（%s），跳过运行", started.date())
            conn.close()
            return {"skipped": True, "date": str(started.date()), "reason": "非交易日"}
        target = started.date()
    else:
        target = requested

    failures: list[str] = []
    market_data = market.fetch_theme_market_data(config, themes, target)
    failures.extend(market_data["failures"])
    baseline = market_data["baseline"]

    if baseline is None or baseline.empty:
        failures.append("基准指数行情缺失，RS 无法计算")
        if requested is None:
            conn.close()
            raise RuntimeError("基准指数行情缺失，无法确定目标交易日")
        baseline_last = target
    else:
        baseline_last = baseline["date"].iloc[-1]
        if baseline_last < target:
            if requested is not None or keep_target:
                logger.info(
                    "基准指数最新日期 %s 未到目标 %s，按目标日期生成盘前/盘中快照",
                    baseline_last,
                    target,
                )
                failures.append(f"基准指数未更新到 {target}，最新 {baseline_last}")
            else:
                logger.warning("基准指数未更新到今日（最新 %s），快照回退到该日期", baseline_last)
                target = baseline_last
                failures.append(f"基准指数未更新到 {started.date()}，快照日期回退到 {baseline_last}")

    trade_days = [day for day in calendar if day <= target]
    if len(trade_days) < 5:
        if baseline is not None and not baseline.empty:
            trade_days = sorted(baseline["date"].tolist())
        else:
            trade_days = [target]
    trade_days = trade_days[-5:]
    window_dates = trade_days[-int(config.get("catalyst_window_days", 5)):]

    news_items, news_failures = news.collect_all(config, themes, target)
    failures.extend(news_failures)

    limit_rows_raw, theme_heat, lu_failures, lu_available = limitup.collect(
        config, themes, trade_days
    )
    failures.extend(lu_failures)
    if not lu_available:
        failures.append("涨停数据今日不可用，涨停热度仅展示为空")

    quotes, quote_failures = global_market.fetch_global_quotes(config, target)
    failures.extend(quote_failures)

    catalyst_5d, catalyst_today = _catalyst_counts(news_items, themes, window_dates, target)

    max_policy = int(config.get("display_max_policy", 3))
    max_research = int(config.get("display_max_research", 3))
    display_policy = _select_display(news_items, "policy", max_policy)
    display_research = _select_display(news_items, "research", max_research)
    news.enrich_policy_summaries(display_policy, max_items=max_policy)
    llm_enrich(display_policy + display_research, config, logger)

    thresholds = config["thresholds"]
    rs_trend_days = int(thresholds.get("rs_trend_days", 3))
    leader_high_return = float(thresholds.get("crowded_leader_5d_return", 0.20))
    confirm_days = int(thresholds.get("confirm_days", 2))
    aux_enabled = bool(config["limit_up"].get("enable_as_aux", False))
    aux_min = int(config["limit_up"].get("min_count_per_day", 3))

    metrics: dict[str, dict[str, Any]] = {}
    for theme in themes:
        heat = theme_heat.get(theme.id) if lu_available else None
        metrics[theme.id] = compute_theme_metrics(
            theme=theme,
            carrier=market_data["carriers"].get(theme.id),
            leader_frames=market_data["leaders"],
            baseline=baseline,
            target_date=target,
            catalyst_count=catalyst_5d.get(theme.id, 0),
            limit_up=heat,
            market_note=market_data["notes"].get(theme.id, ""),
            rs_trend_days=rs_trend_days,
            leader_high_return=leader_high_return,
        )

    base_states: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for theme_id, row in metrics.items():
        state, reason = evaluate_base(row, thresholds, aux_enabled, aux_min)
        base_states[theme_id] = state
        reasons[theme_id] = reason

    history = db.recent_themes(conn, target.isoformat(), max(confirm_days - 1, 1))
    prev_themes = history[-1][1] if history else {}
    statuses, switch_meta = apply_switch(
        base_states, metrics, prev_themes, confirm_days=confirm_days, history=history
    )
    for theme_id, row in metrics.items():
        row["status"] = statuses[theme_id]
        row["note"] = "；".join(filter(None, [row.get("note", ""), reasons.get(theme_id, "")]))

    directions = choose_directions(metrics, statuses, prev_themes)
    radar = build_radar(metrics, statuses, prev_themes, switch_meta)

    generated_at = now().isoformat(timespec="seconds")
    prev_date = db.previous_date(conn, target.isoformat())
    payload = {
        "date": target.isoformat(),
        "generated_at": generated_at,
        "prev_date": prev_date,
        "baseline_index": config["baseline_index"],
        "baseline_last_date": str(baseline_last),
        "failed_sources": failures,
        "switch": switch_meta,
        "directions": directions,
        "radar": radar,
        "catalyst_today": catalyst_today,
        "catalyst_5d": catalyst_5d,
        "limit_up_available": lu_available,
        "window_dates": [str(day) for day in window_dates],
        "display": {
            "policy": [item["url"] for item in display_policy],
            "research": [item["url"] for item in display_research],
        },
        "ai_enabled": bool(config["ai"].get("enabled")),
        "theme_count": len(themes),
    }

    theme_rows = []
    for theme in themes:
        row = metrics[theme.id]
        theme_rows.append(
            {
                "date": target.isoformat(),
                "theme_id": row["theme_id"],
                "name": row["name"],
                "tier": row["tier"],
                "carrier": row["carrier"],
                "carrier_type": row["carrier_type"],
                "rs": row["rs"],
                "rs_prev": row["rs_prev"],
                "rs_rising": int(bool(row["rs_rising"])),
                "trend_above_ma20": int(bool(row["trend_above_ma20"])),
                "trend_ma5_over_ma10": int(bool(row["trend_ma5_over_ma10"])),
                "trend_golden_cross": int(bool(row["trend_golden_cross"])),
                "volume_ratio": row["volume_ratio"],
                "breadth": row["breadth"],
                "breadth_covered": row["breadth_covered"],
                "catalyst_count": row["catalyst_count"],
                "limit_up_count": row["limit_up_count"],
                "limit_up_trend": row["limit_up_trend"],
                "ret_5d": row["ret_5d"],
                "status": row["status"],
                "note": row["note"],
                "extras_json": json.dumps(row["extras"], ensure_ascii=False),
            }
        )

    news_rows = [
        {
            "date": target.isoformat(),
            "category": item["category"],
            "title": item["title"],
            "source": item["source"],
            "published_at": item["published_at"],
            "summary": item.get("summary", ""),
            "matched_themes": item.get("matched_themes", ""),
            "sentiment": item.get("sentiment", "neutral"),
            "url": item["url"],
            "event_key": item.get("event_key", ""),
        }
        for item in news_items
    ]

    quote_rows = [
        {
            "date": target.isoformat(),
            **{
                key: quote.get(key)
                for key in ("symbol", "name", "category", "latest", "change_pct", "note", "quote_time")
            },
        }
        for quote in quotes
    ]

    limit_rows = [
        {
            "date": target.isoformat(),
            "sector": row["sector"],
            "count": row["count"],
            "lianban": row["lianban"],
            "first_board": row["first_board"],
            "series_json": json.dumps(row["series"], ensure_ascii=False),
            "trend": row["trend"],
            "in_theme_pool": int(bool(row["in_theme_pool"])),
            "theme_ids": ",".join(row["theme_ids"]),
            "note": row["note"],
        }
        for row in limit_rows_raw
    ]

    theme_by_id = {theme.id: theme for theme in themes}
    negative_events: dict[str, set[str]] = {theme.id: set() for theme in themes}
    for item in news_items:
        if item.get("sentiment") != "negative" or item["published_at"][:10] != target.isoformat():
            continue
        for theme_id in (item.get("matched_themes") or "").split(","):
            if theme_id:
                negative_events.setdefault(theme_id, set()).add(item.get("event_key", ""))

    holding_rows = []
    for holding in holdings:
        theme_id = holding["theme"]
        theme = theme_by_id.get(theme_id) if theme_id else None
        if theme is None:
            holding_rows.append(
                {
                    "date": target.isoformat(),
                    "code": holding["code"],
                    "name": holding["name"],
                    "theme_id": None,
                    "theme_name": None,
                    "theme_status": None,
                    "risk_note": holding["risk_note"] or "宽基/未映射，不参与主线计算",
                    "mapping_status": "宽基/未映射",
                }
            )
            continue
        risk_note = holding["risk_note"]
        if not risk_note:
            count = len(negative_events.get(theme.id, set()))
            risk_note = (
                f"当日匹配该主线的公开负面消息 {count} 条"
                if count
                else "未发现当日公开负面消息"
            )
        holding_rows.append(
            {
                "date": target.isoformat(),
                "code": holding["code"],
                "name": holding["name"],
                "theme_id": theme.id,
                "theme_name": theme.name,
                "theme_status": statuses.get(theme.id),
                "risk_note": risk_note,
                "mapping_status": theme.name,
            }
        )

    db.save_day(
        conn,
        target.isoformat(),
        generated_at,
        payload,
        theme_rows,
        news_rows,
        quote_rows,
        limit_rows,
        holding_rows,
    )
    conn.close()

    summary = {
        "date": target.isoformat(),
        "generated_at": generated_at,
        "themes": [
            {
                "id": theme.id,
                "name": theme.name,
                "status": statuses[theme.id],
                "rs": metrics[theme.id]["rs"],
                "note": metrics[theme.id]["note"],
            }
            for theme in themes
        ],
        "directions": directions,
        "failed_sources": failures,
        "news_count": {
            "policy": sum(1 for item in news_items if item["category"] == "policy"),
            "research": sum(1 for item in news_items if item["category"] == "research"),
            "news": sum(1 for item in news_items if item["category"] == "news"),
        },
        "limit_up_available": lu_available,
    }
    logger.info(
        "完成 %s：主线 %s 条，政策 %s，研报 %s，快讯 %s，失败源 %s",
        target,
        len(themes),
        summary["news_count"]["policy"],
        summary["news_count"]["research"],
        summary["news_count"]["news"],
        len(failures),
    )
    return summary
