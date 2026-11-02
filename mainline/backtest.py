"""主线状态参数回测。

流程：
1. 拉取并缓存基准、龙头、指数载体的全历史日线；
2. 对每个交易日、每条主线用生产指标函数重算 RS / 量能 / 宽度等；
3. 网格搜索强化、走弱阈值与 RS 趋势定义，统计信号后的远期超额收益；
4. 按时间切分训练/测试段，输出推荐参数与报告。

限制：历史新闻催化不可得，回测中 catalyst_count 固定为 1；
涨停辅助默认关闭；使用当前主线成分股，存在幸存者偏差。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Callable

import numpy as np
import pandas as pd

from .collectors import market
from .config import Theme, load_config, load_themes
from .indicators import compute_theme_metrics
from .status import evaluate_base

HORIZONS = (5, 10, 20)
TREND_DAYS = (2, 3, 4)
TREND_MODES = ("consecutive", "net")
CONFIRM_DAYS = (1, 2, 3)

STRONG_VOLUMES = (1.2, 1.3, 1.5)
STRONG_BREADTHS = (0.4, 0.6, 0.8)
WEAK_VOLUMES = (0.8, 1.0, 1.2)
WEAK_BREADTHS = (0.4,)


@dataclass(frozen=True)
class Params:
    trend_mode: str
    rs_trend_days: int
    volume_ratio_strong: float
    breadth_strong: float
    volume_ratio_weak: float
    breadth_weak: float
    confirm_days: int = 2

    def label(self) -> str:
        return (
            f"{self.trend_mode}/RS{self.rs_trend_days}d/"
            f"强量{self.volume_ratio_strong}/强宽{self.breadth_strong}/"
            f"弱量{self.volume_ratio_weak}/弱宽{self.breadth_weak}/确认{self.confirm_days}"
        )


@dataclass
class Bundle:
    baseline: pd.DataFrame
    carriers: dict[str, pd.DataFrame]
    leaders: dict[str, pd.DataFrame]
    failures: list[str]


def _fresh(df: pd.DataFrame, required: date) -> bool:
    if df is None or df.empty or "date" not in df.columns:
        return False
    last = max(df["date"])
    return last >= required - timedelta(days=4)


def _cache_path(cache_dir: Path, kind: str, code: str) -> Path:
    return cache_dir / f"{kind}_{code}.pkl"


def _get_series(
    kind: str,
    code: str,
    fetcher: Callable[[str], pd.DataFrame],
    cache_dir: Path,
    required: date,
    refresh: bool,
) -> tuple[str, pd.DataFrame | None, str | None]:
    path = _cache_path(cache_dir, kind, code)
    cached: pd.DataFrame | None = None
    if path.exists():
        try:
            cached = pd.read_pickle(path)
        except Exception:
            cached = None
    if cached is not None and not refresh and _fresh(cached, required):
        return code, cached, None
    try:
        frame = fetcher(code)
        if frame is None or frame.empty:
            raise ValueError("空数据")
        frame.to_pickle(path)
        return code, frame, None
    except Exception as exc:
        if cached is not None and not cached.empty:
            return code, cached, (
                f"{kind} {code}: {type(exc).__name__}，使用缓存（{max(cached['date'])}）"
            )
        return code, None, f"{kind} {code}: {type(exc).__name__}"


def _fetch_many(
    codes: list[str],
    kind: str,
    fetcher: Callable[[str], pd.DataFrame],
    cache_dir: Path,
    required: date,
    refresh: bool,
    workers: int,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    out: dict[str, pd.DataFrame] = {}
    failures: list[str] = []
    if not codes:
        return out, failures
    if workers <= 1:
        for code in codes:
            result = _get_series(kind, code, fetcher, cache_dir, required, refresh)
            _, frame, error = result
            if frame is not None and not frame.empty:
                out[code] = frame
            if error:
                failures.append(error)
            if len(out) % 5 == 0 or len(out) + len(failures) == len(codes):
                print(f"  {kind} 行情 {len(out) + len(failures)}/{len(codes)}", flush=True)
        return out, failures
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(_get_series, kind, code, fetcher, cache_dir, required, refresh): code
            for code in codes
        }
        done = 0
        for future in as_completed(futures):
            code, frame, error = future.result()
            done += 1
            if frame is not None and not frame.empty:
                out[code] = frame
            if error:
                failures.append(error)
            if done % 5 == 0 or done == len(codes):
                print(f"  {kind} 行情 {done}/{len(codes)}", flush=True)
    return out, failures


def build_basket_full(members: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """用全历史构建龙头等权组合；水平基准不影响收益率、RS 与量能倍数。"""
    merged: pd.DataFrame | None = None
    for code, frame in members.items():
        if frame is None or frame.empty:
            continue
        part = frame[["date", "close", "amount"]].rename(
            columns={"close": f"c_{code}", "amount": f"a_{code}"}
        )
        merged = part if merged is None else merged.merge(part, on="date", how="inner")
    if merged is None or merged.empty:
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "amount"])
    merged = merged.sort_values("date").reset_index(drop=True)
    close_cols = [col for col in merged.columns if col.startswith("c_")]
    amount_cols = [col for col in merged.columns if col.startswith("a_")]
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


def load_bundle(
    config: dict[str, Any],
    themes: list[Theme],
    cache_dir: Path,
    required: date,
    refresh: bool,
    workers: int,
) -> Bundle:
    cache_dir.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    baseline_code = str(config["baseline_index"])
    _, baseline, error = _get_series(
        "index", baseline_code, market.fetch_index_daily, cache_dir, required, refresh
    )
    if error:
        failures.append(error)
    if baseline is None or baseline.empty:
        raise RuntimeError("基准指数行情缺失，无法回测")

    leader_codes = sorted({code for theme in themes for code in theme.leader_codes})
    leaders, leader_failures = _fetch_many(
        leader_codes, "stock", market.fetch_stock_daily, cache_dir, required, refresh, workers
    )
    failures.extend(leader_failures)

    index_codes = sorted(
        {
            str(theme.index_code)
            for theme in themes
            if theme.carrier_type == "index" and theme.index_code
        }
    )
    index_frames, index_failures = _fetch_many(
        index_codes, "index", market.fetch_index_daily, cache_dir, required, refresh, workers
    )
    failures.extend(index_failures)

    carriers: dict[str, pd.DataFrame] = {}
    for theme in themes:
        if theme.carrier_type == "index":
            frame = index_frames.get(str(theme.index_code))
            if frame is None or frame.empty:
                failures.append(f"载体 {theme.id}: 指数行情缺失")
                continue
            carriers[theme.id] = frame
        else:
            members = {code: leaders[code] for code in theme.leader_codes if code in leaders}
            missing = len(theme.leaders) - len(members)
            if missing:
                failures.append(f"载体 {theme.id}: 组合成分缺失 {missing} 只")
            if not members:
                failures.append(f"载体 {theme.id}: 龙头行情全部缺失")
                continue
            carriers[theme.id] = build_basket_full(members)
    return Bundle(baseline=baseline, carriers=carriers, leaders=leaders, failures=failures)


def _forward_returns(close_by_date: pd.Series, dates: list[date], horizon: int) -> np.ndarray:
    """信号日收盘后，下一交易日收盘进入，持有 horizon 个交易日。"""
    series = close_by_date.reindex(dates).ffill()
    values = np.full(len(dates), np.nan)
    for i in range(len(dates) - horizon - 1):
        entry = series.iloc[i + 1]
        exit_ = series.iloc[i + 1 + horizon]
        if pd.notna(entry) and pd.notna(exit_) and entry:
            values[i] = float(exit_ / entry - 1)
    return values


def prepare_rows(
    themes: list[Theme],
    bundle: Bundle,
    start: date,
    end: date,
    max_trend_days: int = 6,
    leader_high_return: float = 0.20,
) -> tuple[pd.DataFrame, list[date], list[str]]:
    base = bundle.baseline.drop_duplicates("date").sort_values("date")
    dates = [day for day in base["date"].tolist() if start <= day <= end]
    if not dates:
        raise RuntimeError("所选区间没有基准行情")
    bench_close = base.set_index("date")["close"]
    bench_fwd = {
        horizon: _forward_returns(bench_close, dates, horizon) for horizon in HORIZONS
    }

    failures: list[str] = []
    rows: list[dict[str, Any]] = []
    for theme in themes:
        carrier = bundle.carriers.get(theme.id)
        if carrier is None or carrier.empty:
            failures.append(f"主线 {theme.id}: 无载体行情")
            continue
        carrier = carrier.drop_duplicates("date").sort_values("date")
        carrier_dates = set(carrier["date"].tolist())
        carrier_close = carrier.set_index("date")["close"]
        car_fwd = {
            horizon: _forward_returns(carrier_close, dates, horizon) for horizon in HORIZONS
        }
        for i, day in enumerate(dates):
            if day not in carrier_dates:
                continue
            metrics = compute_theme_metrics(
                theme=theme,
                carrier=carrier,
                leader_frames=bundle.leaders,
                baseline=bundle.baseline,
                target_date=day,
                catalyst_count=1,
                limit_up=None,
                market_note="",
                rs_trend_days=max_trend_days,
                leader_high_return=leader_high_return,
            )
            if metrics["rs"] is None:
                continue
            extras = metrics["extras"]
            row: dict[str, Any] = {
                "date": day,
                "theme_id": theme.id,
                "name": theme.name,
                "tier": theme.tier,
                "rs": metrics["rs"],
                "rs_series": list(extras.get("rs_series") or []),
                "volume_ratio": metrics["volume_ratio"],
                "breadth": metrics["breadth"],
                "leaders_below_ma20": int(extras.get("leaders_below_ma20", 0)),
                "leaders_covered": int(extras.get("leaders_covered", 0)),
                "leader_ret5_high_ratio": extras.get("leader_ret5_high_ratio"),
                "ret_5d": metrics["ret_5d"],
                "trend_above_ma20": bool(metrics["trend_above_ma20"]),
                "catalyst_count": 1,
            }
            for horizon in HORIZONS:
                row[f"fwd_ret_{horizon}"] = car_fwd[horizon][i]
                row[f"fwd_ex_{horizon}"] = car_fwd[horizon][i] - bench_fwd[horizon][i]
            rows.append(row)
        print(f"  指标完成 {theme.name}（{len(rows)} 行）", flush=True)
    return pd.DataFrame(rows), dates, failures


def _trend_flag(series: list[float], days: int, mode: str, up: bool) -> bool:
    if not isinstance(series, (list, tuple)):
        return False
    need = days + 1 if mode == "net" else days
    if len(series) < need:
        return False
    if mode == "net":
        latest, base = series[-1], series[-days - 1]
        if latest is None or base is None:
            return False
        return latest > base if up else latest < base
    window = [value for value in series[-days:]]
    if any(value is None or pd.isna(value) for value in window):
        return False
    if up:
        return all(window[i] > window[i - 1] for i in range(1, days))
    return all(window[i] < window[i - 1] for i in range(1, days))


def add_trend_columns(rows: pd.DataFrame) -> pd.DataFrame:
    rows = rows.copy()
    for mode in TREND_MODES:
        for days in TREND_DAYS:
            rows[f"rise_{mode}_{days}"] = [
                _trend_flag(series, days, mode, True) for series in rows["rs_series"]
            ]
            rows[f"fall_{mode}_{days}"] = [
                _trend_flag(series, days, mode, False) for series in rows["rs_series"]
            ]
    return rows


def thresholds_for(params: Params, base: dict[str, Any]) -> dict[str, Any]:
    thresholds = dict(base)
    thresholds.update(
        {
            "rs_trend_days": params.rs_trend_days,
            "volume_ratio_strong": params.volume_ratio_strong,
            "breadth_strong": params.breadth_strong,
            "volume_ratio_weak": params.volume_ratio_weak,
            "breadth_weak": params.breadth_weak,
        }
    )
    return thresholds


def compute_statuses(rows: pd.DataFrame, params: Params, base_thresholds: dict[str, Any]) -> np.ndarray:
    """向量化计算基础状态；与 status.evaluate_base 的优先级保持一致。"""
    statuses = np.full(len(rows), "observe", dtype=object)
    volume = pd.to_numeric(rows["volume_ratio"], errors="coerce").to_numpy(dtype=float)
    breadth = pd.to_numeric(rows["breadth"], errors="coerce").to_numpy(dtype=float)
    below = pd.to_numeric(rows["leaders_below_ma20"], errors="coerce").fillna(0).to_numpy(dtype=float)
    ret_5d = pd.to_numeric(rows["ret_5d"], errors="coerce").to_numpy(dtype=float)
    leader_ratio = pd.to_numeric(rows["leader_ret5_high_ratio"], errors="coerce").to_numpy(dtype=float)
    rising = rows[f"rise_{params.trend_mode}_{params.rs_trend_days}"].to_numpy(dtype=bool)
    falling = rows[f"fall_{params.trend_mode}_{params.rs_trend_days}"].to_numpy(dtype=bool)

    crowded_first = (
        (ret_5d > float(base_thresholds["crowded_5d_return"]))
        & (volume > float(base_thresholds["crowded_volume_ratio"]))
    )
    crowded_second = leader_ratio >= float(base_thresholds["crowded_leader_ratio"])
    crowded = crowded_first | crowded_second
    weak = (
        falling
        & (breadth < params.breadth_weak)
        & (below >= int(base_thresholds.get("leaders_below_ma20_min", 2)))
        & (volume < params.volume_ratio_weak)
    )
    strong = (
        rising
        & (volume > params.volume_ratio_strong)
        & (breadth > params.breadth_strong)
    )

    statuses[crowded] = "crowded"
    statuses[weak] = "weaken"
    statuses[strong] = "strengthen"
    return statuses


def verify_statuses(
    rows: pd.DataFrame, statuses: np.ndarray, params: Params, base_thresholds: dict[str, Any]
) -> int:
    """用生产函数抽查一致性，返回不一致条数。"""
    mismatches = 0
    sample = rows.head(400)
    for index, row in sample.iterrows():
        extras = {
            "rs_rising": bool(row[f"rise_{params.trend_mode}_{params.rs_trend_days}"]),
            "rs_falling": bool(row[f"fall_{params.trend_mode}_{params.rs_trend_days}"]),
            "leaders_below_ma20": row["leaders_below_ma20"],
            "leader_ret5_high_ratio": row["leader_ret5_high_ratio"],
        }
        payload = {
            "rs": row["rs"],
            "rs_rising": bool(row[f"rise_{params.trend_mode}_{params.rs_trend_days}"]),
            "breadth": row["breadth"],
            "volume_ratio": row["volume_ratio"],
            "catalyst_count": 1,
            "ret_5d": row["ret_5d"],
            "extras": extras,
        }
        state, _ = evaluate_base(
            payload, thresholds_for(params, base_thresholds), False, 3
        )
        if state != statuses[rows.index.get_loc(index)]:
            mismatches += 1
    return mismatches


def _aggregate(sub: pd.DataFrame, horizon: int, weak: bool = False) -> dict[str, float]:
    values = sub[f"fwd_ex_{horizon}"].dropna()
    if values.empty:
        return {"n": 0, "mean_ex": np.nan, "hit": np.nan}
    return {
        "n": int(len(values)),
        "mean_ex": float(values.mean()),
        "hit": float((values < 0).mean() if weak else (values > 0).mean()),
    }


def _period_metrics(rows: pd.DataFrame, statuses: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    strong = rows[(statuses == "strengthen") & mask]
    weak = rows[(statuses == "weaken") & mask]
    out: dict[str, float] = {
        "strong_total": int(len(strong)),
        "weak_total": int(len(weak)),
        "signal_rate": float(
            ((statuses == "strengthen") | (statuses == "weaken"))[mask].mean()
        )
        if mask.any()
        else np.nan,
    }
    for horizon in HORIZONS:
        strong_agg = _aggregate(strong, horizon)
        weak_agg = _aggregate(weak, horizon, weak=True)
        out[f"strong_n_{horizon}"] = strong_agg["n"]
        out[f"strong_ex_{horizon}"] = strong_agg["mean_ex"]
        out[f"strong_hit_{horizon}"] = strong_agg["hit"]
        out[f"weak_n_{horizon}"] = weak_agg["n"]
        out[f"weak_ex_{horizon}"] = weak_agg["mean_ex"]
        out[f"weak_hit_{horizon}"] = weak_agg["hit"]
        out[f"edge_{horizon}"] = strong_agg["mean_ex"] - weak_agg["mean_ex"]
    return out


def _records_by_date(rows: pd.DataFrame) -> dict[date, dict[str, dict[str, Any]]]:
    out: dict[date, dict[str, dict[str, Any]]] = {}
    for row in rows.to_dict("records"):
        out.setdefault(row["date"], {})[row["theme_id"]] = row
    return out


def switch_stats(
    rows: pd.DataFrame,
    statuses: np.ndarray,
    dates: list[date],
    records_by_date: dict[date, dict[str, dict[str, Any]]],
    confirm_days: int,
    mask: np.ndarray | None = None,
) -> dict[str, float]:
    """统计“新主线强化 + 旧主线走弱”的切换信号：做多新主线、做空旧主线的远期收益。"""
    states_by_date: dict[date, dict[str, str]] = {}
    for day, group in rows.groupby("date", sort=False):
        indices = group.index.to_numpy()
        states_by_date[day] = {
            rows.at[index, "theme_id"]: statuses[rows.index.get_loc(index)]
            for index in indices
        }

    spreads: dict[int, list[float]] = {horizon: [] for horizon in HORIZONS}
    candidates = 0
    confirmed = 0
    date_positions = {day: i for i, day in enumerate(dates)}
    for day in dates:
        states = states_by_date.get(day, {})
        strong_ids = [tid for tid, state in states.items() if state == "strengthen"]
        weak_ids = [tid for tid, state in states.items() if state == "weaken"]
        if not strong_ids or not weak_ids:
            continue
        new_id = max(strong_ids, key=lambda tid: records_by_date[day][tid]["rs"] or -9)
        old_id = min(weak_ids, key=lambda tid: records_by_date[day][tid]["rs"] or 9)
        if mask is not None and not mask[date_positions[day]]:
            continue
        candidates += 1
        prior = confirm_days - 1
        if prior > 0:
            pos = date_positions[day]
            if pos < prior:
                continue
            prior_days = dates[pos - prior : pos]
            held = all(
                states_by_date.get(prev_day, {}).get(new_id) == "strengthen"
                and states_by_date.get(prev_day, {}).get(old_id) == "weaken"
                for prev_day in prior_days
            )
            if not held:
                continue
        confirmed += 1
        for horizon in HORIZONS:
            new_ret = records_by_date[day][new_id].get(f"fwd_ret_{horizon}")
            old_ret = records_by_date[day][old_id].get(f"fwd_ret_{horizon}")
            if new_ret is not None and old_ret is not None and not pd.isna(new_ret) and not pd.isna(old_ret):
                spreads[horizon].append(float(new_ret - old_ret))

    out: dict[str, float] = {"switch_candidates": candidates, "switch_confirmed": confirmed}
    for horizon in HORIZONS:
        values = spreads[horizon]
        out[f"switch_n_{horizon}"] = len(values)
        out[f"switch_mean_{horizon}"] = float(mean(values)) if values else np.nan
        out[f"switch_hit_{horizon}"] = (
            float(np.mean([value > 0 for value in values])) if values else np.nan
        )
    return out


def _grid() -> list[Params]:
    out: list[Params] = []
    for mode in TREND_MODES:
        for trend_days in TREND_DAYS:
            for strong_volume in STRONG_VOLUMES:
                for strong_breadth in STRONG_BREADTHS:
                    for weak_volume in WEAK_VOLUMES:
                        for weak_breadth in WEAK_BREADTHS:
                            out.append(
                                Params(
                                    trend_mode=mode,
                                    rs_trend_days=trend_days,
                                    volume_ratio_strong=strong_volume,
                                    breadth_strong=strong_breadth,
                                    volume_ratio_weak=weak_volume,
                                    breadth_weak=weak_breadth,
                                )
                            )
    return out


def _current_params(config: dict[str, Any]) -> Params:
    thresholds = config["thresholds"]
    return Params(
        trend_mode="consecutive",
        rs_trend_days=int(thresholds.get("rs_trend_days", 3)),
        volume_ratio_strong=float(thresholds["volume_ratio_strong"]),
        breadth_strong=float(thresholds["breadth_strong"]),
        volume_ratio_weak=float(thresholds["volume_ratio_weak"]),
        breadth_weak=float(thresholds["breadth_weak"]),
        confirm_days=int(thresholds.get("confirm_days", 2)),
    )


def run(
    start: date | None = None,
    end: date | None = None,
    refresh: bool = False,
    workers: int = 1,
    train_ratio: float = 0.7,
    min_signals: int = 30,
    out_dir: str | Path = "reports",
    cache_dir: str | Path = "data/backtest_cache",
) -> dict[str, Any]:
    config = load_config()
    themes = load_themes(config)
    end = end or date.today()
    start = start or (end - timedelta(days=365 * 3))
    root = Path(config["_root"])

    print(f"回测区间：{start} ~ {end}，主线 {len(themes)} 条", flush=True)
    bundle = load_bundle(
        config, themes, root / cache_dir, date.today(), refresh, workers
    )
    if bundle.failures:
        print(f"行情降级 {len(bundle.failures)} 项", flush=True)

    rows, dates, row_failures = prepare_rows(
        themes,
        bundle,
        start,
        end,
        max_trend_days=max(TREND_DAYS) + 1,
        leader_high_return=float(config["thresholds"].get("crowded_leader_5d_return", 0.20)),
    )
    failures = list(dict.fromkeys(bundle.failures + row_failures))
    if rows.empty:
        raise RuntimeError("没有可回测的指标行")

    rows = add_trend_columns(rows)
    dates = sorted(rows["date"].unique())
    split_index = max(1, min(len(dates) - 1, int(len(dates) * train_ratio)))
    split_date = dates[split_index - 1]
    train_mask = (rows["date"] <= split_date).to_numpy()
    test_mask = ~train_mask
    records = _records_by_date(rows)
    base_thresholds = config["thresholds"]

    print(
        f"样本：{len(rows)} 行 / {len(dates)} 个交易日，"
        f"训练截至 {split_date}（{train_mask.sum()} 行），测试 {test_mask.sum()} 行",
        flush=True,
    )

    results: list[dict[str, Any]] = []
    combos = _grid()
    for index, params in enumerate(combos, start=1):
        statuses = compute_statuses(rows, params, base_thresholds)
        train_metrics = _period_metrics(rows, statuses, train_mask)
        test_metrics = _period_metrics(rows, statuses, test_mask)
        for confirm_days in CONFIRM_DAYS:
            switch_train = switch_stats(
                rows, statuses, dates, records, confirm_days, train_mask
            )
            switch_test = switch_stats(
                rows, statuses, dates, records, confirm_days, test_mask
            )
            item: dict[str, Any] = {
                "trend_mode": params.trend_mode,
                "rs_trend_days": params.rs_trend_days,
                "volume_ratio_strong": params.volume_ratio_strong,
                "breadth_strong": params.breadth_strong,
                "volume_ratio_weak": params.volume_ratio_weak,
                "breadth_weak": params.breadth_weak,
                "confirm_days": confirm_days,
            }
            item.update({f"train_{key}": value for key, value in train_metrics.items()})
            item.update({f"test_{key}": value for key, value in test_metrics.items()})
            item.update({f"train_sw_{key}": value for key, value in switch_train.items()})
            item.update({f"test_sw_{key}": value for key, value in switch_test.items()})
            results.append(item)
        if index % 20 == 0 or index == len(combos):
            print(f"  参数组合 {index}/{len(combos)}", flush=True)

    combos_df = pd.DataFrame(results)
    combos_df["train_score"] = combos_df["train_edge_10"]
    combos_df.loc[
        (combos_df["train_strong_n_10"] < min_signals)
        | (combos_df["train_weak_n_10"] < min_signals),
        "train_score",
    ] = np.nan
    combos_df["test_score"] = combos_df["test_edge_10"]
    combos_df.loc[
        (combos_df["test_strong_n_10"] < max(min_signals // 2, 5))
        | (combos_df["test_weak_n_10"] < max(min_signals // 2, 5)),
        "test_score",
    ] = np.nan

    current = _current_params(config)
    current_statuses = compute_statuses(rows, current, base_thresholds)
    current_train = _period_metrics(rows, current_statuses, train_mask)
    current_test = _period_metrics(rows, current_statuses, test_mask)
    mismatches = verify_statuses(rows, current_statuses, current, base_thresholds)

    ranked = combos_df.dropna(subset=["train_score"]).sort_values(
        ["train_score", "train_strong_hit_10", "train_weak_hit_10"],
        ascending=[False, False, True],
    )
    robust = ranked[ranked["test_score"].notna()].sort_values(
        ["test_score", "test_strong_hit_10"], ascending=[False, False]
    )
    top_train = ranked.head(10)
    top_with_test = top_train[top_train["test_score"].notna()]
    recommended = (
        top_with_test.sort_values("test_score", ascending=False).iloc[0].to_dict()
        if not top_with_test.empty
        else (ranked.iloc[0].to_dict() if not ranked.empty else None)
    )

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_path / f"backtest_{stamp}.csv"
    json_path = out_path / f"backtest_{stamp}.json"
    md_path = out_path / f"backtest_{stamp}.md"
    combos_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    summary = {
        "start": str(start),
        "end": str(dates[-1]),
        "dates": len(dates),
        "rows": len(rows),
        "split_date": str(split_date),
        "themes": len(themes),
        "failures": failures,
        "current": current.__dict__,
        "current_train": current_train,
        "current_test": current_test,
        "verify_mismatches": mismatches,
        "top_train": ranked.head(10).to_dict("records"),
        "recommended": recommended,
        "files": {"csv": str(csv_path), "json": str(json_path), "md": str(md_path)},
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    _write_markdown(
        md_path, summary, combos_df, ranked, robust, current, min_signals, failures, themes
    )

    print("\n回测完成")
    print(f"当前参数：强化 {current_train['strong_total']} 次，走弱 {current_train['weak_total']} 次")
    if recommended:
        print("推荐参数：", _params_label(recommended))
        print(
            "训练 edge10：%.2f%%，测试 edge10：%.2f%%"
            % (
                (recommended.get("train_edge_10") or 0) * 100,
                (recommended.get("test_edge_10") or 0) * 100,
            )
        )
    print(f"报告：{md_path}")
    return summary


def _params_label(row: dict[str, Any]) -> str:
    return (
        f"{row['trend_mode']}/RS{int(row['rs_trend_days'])}d/"
        f"强量{row['volume_ratio_strong']}/强宽{row['breadth_strong']}/"
        f"弱量{row['volume_ratio_weak']}/弱宽{row['breadth_weak']}/"
        f"确认{int(row['confirm_days'])}"
    )


def _fmt_pct(value: Any) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "—"
    return f"{float(value) * 100:+.2f}%"


def _write_markdown(
    path: Path,
    summary: dict[str, Any],
    combos: pd.DataFrame,
    ranked: pd.DataFrame,
    robust: pd.DataFrame,
    current: Params,
    min_signals: int,
    failures: list[str],
    themes: list[Theme],
) -> None:
    lines: list[str] = []
    lines.append("# 主线切换参数回测报告\n")
    lines.append(
        f"- 区间：{summary['start']} ~ {summary['end']}（{summary['dates']} 个交易日，"
        f"{summary['rows']} 行）\n"
        f"- 训练/测试切分：{summary['split_date']}\n"
        f"- 主线：{summary['themes']} 条；参数组合：{len(combos)}（含确认天数变体）\n"
    )
    lines.append(
        "口径：信号日收盘后、下一交易日收盘进入；持有 5/10/20 日；"
        "催化数固定为 1（历史新闻不可得）；涨停辅助关闭；使用当前成分股，存在幸存者偏差。\n"
    )
    if failures:
        lines.append("行情降级：\n")
        for item in failures:
            lines.append(f"- {item}\n")
        lines.append("")

    lines.append("## 当前参数表现\n")
    lines.append(f"当前：`{current.label()}`\n")
    lines.append("| 分段 | 强化次数 | 强化5日胜率 | 强化10日超额 | 走弱次数 | 走弱5日胜率 | 走弱10日超额 | 10日edge |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for name, metrics in (("训练", summary["current_train"]), ("测试", summary["current_test"])):
        lines.append(
            f"| {name} | {metrics['strong_total']} | {_fmt_pct(metrics['strong_hit_5'])} | "
            f"{_fmt_pct(metrics['strong_ex_10'])} | {metrics['weak_total']} | "
            f"{_fmt_pct(metrics['weak_hit_5'])} | {_fmt_pct(metrics['weak_ex_10'])} | "
            f"{_fmt_pct(metrics['edge_10'])} |"
        )
    lines.append(f"\n生产函数一致性抽查不一致：{summary['verify_mismatches']} 条\n")

    lines.append(f"## 训练集 Top 10（要求强化/走弱 10 日样本各 ≥{min_signals}）\n")
    lines.append(
        "| 参数 | 强化n | 强化胜率 | 强化超额10 | 走弱n | 走弱胜率 | 走弱超额10 | edge10 | 测试edge10 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for record in ranked.head(10).to_dict("records"):
        lines.append(
            f"| {_params_label(record)} | {int(record['train_strong_n_10'])} | "
            f"{_fmt_pct(record['train_strong_hit_10'])} | {_fmt_pct(record['train_strong_ex_10'])} | "
            f"{int(record['train_weak_n_10'])} | {_fmt_pct(record['train_weak_hit_10'])} | "
            f"{_fmt_pct(record['train_weak_ex_10'])} | {_fmt_pct(record['train_edge_10'])} | "
            f"{_fmt_pct(record.get('test_edge_10'))} |"
        )

    if not robust.empty:
        lines.append("\n## 测试集稳健候选（按测试 edge10 排序）\n")
        lines.append("| 参数 | 训练edge10 | 测试edge10 | 测试强化n | 测试走弱n | 测试切换n |")
        lines.append("|---|---|---|---|---|---|")
        for record in robust.head(5).to_dict("records"):
            lines.append(
                f"| {_params_label(record)} | {_fmt_pct(record['train_edge_10'])} | "
                f"{_fmt_pct(record['test_edge_10'])} | {int(record['test_strong_n_10'])} | "
                f"{int(record['test_weak_n_10'])} | {int(record['test_sw_switch_n_10'])} |"
            )

    if summary["recommended"]:
        rec = summary["recommended"]
        lines.append("\n## 推荐参数\n")
        lines.append(f"`{_params_label(rec)}`\n")
        lines.append(
            f"- 训练：强化 {int(rec['train_strong_total'])} 次 / 走弱 {int(rec['train_weak_total'])} 次，"
            f"edge10 {_fmt_pct(rec['train_edge_10'])}\n"
            f"- 测试：强化 {int(rec['test_strong_total'])} 次 / 走弱 {int(rec['test_weak_total'])} 次，"
            f"edge10 {_fmt_pct(rec['test_edge_10'])}\n"
            f"- 测试切换确认 {int(rec['test_sw_switch_confirmed'])} 次，"
            f"10日多空收益 {_fmt_pct(rec['test_sw_switch_mean_10'])}，"
            f"胜率 {_fmt_pct(rec['test_sw_switch_hit_10'])}\n"
        )

    lines.append("\n## 说明\n")
    lines.append(
        "- 强化/走弱样本按“主题×交易日”池化，远期区间重叠，统计显著性有限。\n"
        "- 回测不含交易成本、涨跌停无法成交、停牌与冲击成本。\n"
        "- 推荐参数只作为调参依据，正式落地前应再做滚动窗口或样本外验证。\n"
    )
    path.write_text("\n".join(lines), encoding="utf-8")
