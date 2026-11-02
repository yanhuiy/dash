"""每日 18:00 定时任务（APScheduler）。

交易日判断与失败降级由 pipeline 内部处理：非交易日自动跳过。
启动后保持前台运行；如需后台常驻，可使用系统计划任务调用 run_daily.py。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler

from mainline.config import load_config
from mainline.logging_setup import setup_logging
from mainline.pipeline import run


def main() -> None:
    config = load_config()
    logger = setup_logging(config)
    hour, minute = str(config.get("run_time", "18:00")).split(":")
    scheduler = BlockingScheduler(timezone=str(config.get("timezone", "Asia/Shanghai")))

    def job() -> None:
        try:
            result = run(config=config, logger=logger)
            logger.info("定时运行结果：%s", "跳过（非交易日）" if result.get("skipped") else result.get("date"))
        except Exception:
            logger.exception("定时运行失败")

    scheduler.add_job(job, "cron", day_of_week="mon-fri", hour=int(hour), minute=int(minute), id="daily")
    logger.info("定时任务已启动：每个交易日 %s 运行（时区 %s）", config["run_time"], config["timezone"])
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger("mainline").info("定时任务已停止")


if __name__ == "__main__":
    main()
