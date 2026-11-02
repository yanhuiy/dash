"""每日盘后运行入口。

用法：
    python run_daily.py                    # 定时任务用，非交易日自动跳过
    python run_daily.py --date 2026-10-23  # 手动补跑指定交易日
    python run_daily.py --intraday         # 盘前/盘中按当日生成（定时刷新用）
    python run_daily.py --force            # 非交易日强制用最新行情生成
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from mainline.directions import BASIS_LABELS
from mainline.pipeline import run


def main() -> int:
    parser = argparse.ArgumentParser(description="市场主线切换盘后看板 - 每日任务")
    parser.add_argument("--date", help="目标交易日，格式 YYYY-MM-DD")
    parser.add_argument("--intraday", action="store_true", help="盘前/盘中按当日生成，不覆盖上一交易日快照")
    parser.add_argument("--force", action="store_true", help="非交易日也运行")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 摘要")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else None
    try:
        result = run(target_date=target, force=args.force, keep_target=args.intraday)
    except Exception as exc:
        print(f"运行失败：{type(exc).__name__}: {exc}")
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    if result.get("skipped"):
        print(f"跳过：{result.get('date')} 非交易日")
        return 0

    print(f"快照日期：{result['date']}  生成时间：{result['generated_at']}")
    print("主线状态：")
    for item in result["themes"]:
        rs = "—" if item["rs"] is None else f"{item['rs'] * 100:+.1f}%"
        print(f"  {item['name']:<12} {item['status']:<16} RS {rs:>7}  {item['note']}")
    directions = result["directions"]
    names = {item["id"]: item["name"] for item in result["themes"]}
    basis_map = directions.get("basis") or {}
    print("三大方向：")
    for key, label in (("strongest", "当前最强"), ("switching", "潜在切换"), ("weakening", "走弱/风险")):
        theme_id = directions.get(key)
        if theme_id is None:
            print(f"  {label}：—")
            continue
        basis = BASIS_LABELS.get(basis_map.get(key, ""), "—")
        print(f"  {label}：{names.get(theme_id, theme_id)}（{basis}）")
    counts = result["news_count"]
    print(f"条目：政策 {counts['policy']}，研报 {counts['research']}，快讯 {counts['news']}")
    if result["failed_sources"]:
        print("缺失/降级信源：")
        for failure in result["failed_sources"]:
            print(f"  - {failure}")
    print("看板启动：streamlit run app.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
