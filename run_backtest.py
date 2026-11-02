"""主线参数回测入口。

用法：
    python run_backtest.py                          # 近 3 年，优先用缓存
    python run_backtest.py --refresh                # 强制重新下载行情
    python run_backtest.py --start 2023-01-01 --end 2026-11-02
    python run_backtest.py --workers 1              # 网络不稳时串行下载
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from mainline.backtest import run


def main() -> int:
    parser = argparse.ArgumentParser(description="市场主线切换参数回测")
    parser.add_argument("--start", help="回测起始日 YYYY-MM-DD，默认近 3 年")
    parser.add_argument("--end", help="回测结束日 YYYY-MM-DD，默认今天")
    parser.add_argument("--refresh", action="store_true", help="忽略缓存重新下载行情")
    parser.add_argument("--workers", type=int, default=1, help="行情并发下载数（akshare 建议串行）")
    parser.add_argument("--train-ratio", type=float, default=0.7, help="训练集时间占比")
    parser.add_argument("--min-signals", type=int, default=30, help="训练集最少信号数")
    parser.add_argument("--out", default="reports", help="报告输出目录")
    parser.add_argument("--cache", default="data/backtest_cache", help="行情缓存目录")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 摘要")
    args = parser.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    try:
        result = run(
            start=start,
            end=end,
            refresh=args.refresh,
            workers=args.workers,
            train_ratio=args.train_ratio,
            min_signals=args.min_signals,
            out_dir=args.out,
            cache_dir=args.cache,
        )
    except Exception as exc:
        print(f"回测失败：{type(exc).__name__}: {exc}")
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    current = result["current_train"]
    print("\n当前参数训练段：")
    print(
        f"  强化 {current['strong_total']} 次（10日胜率 {current['strong_hit_10']}，"
        f"超额 {current['strong_ex_10']}）"
    )
    print(
        f"  走弱 {current['weak_total']} 次（10日胜率 {current['weak_hit_10']}，"
        f"超额 {current['weak_ex_10']}）"
    )
    print(f"报告：{result['files']['md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
