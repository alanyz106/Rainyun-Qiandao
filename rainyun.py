#!/usr/bin/env python3
"""
Rainyun-Qiandao - 雨云每日签到
入口文件，初始化运行环境后调用 rainyun 包中的签到逻辑。

基于原项目重构：https://github.com/LeapYa/Rainyun-Qiandao
"""

import os
import time
from datetime import timedelta

import schedule

import rainyun.config as rconfig
from rainyun.config import (
    cleanup_logs_on_startup,
    cleanup_zombie_processes,
    now_local,
    setup_logging,
    setup_sigchld_handler,
)
from rainyun.checkin import run_all_accounts, scheduled_checkin

if __name__ == "__main__":
    # 配置参数
    rconfig.timeout = int(os.getenv("TIMEOUT", "15000")) // 1000
    rconfig.max_delay = int(os.getenv("MAX_DELAY", "5"))
    rconfig.debug = os.getenv("DEBUG", "false").lower() == "true"
    rconfig.linux = os.getenv("LINUX_MODE", "true").lower() == "true" or os.path.exists("/.dockerenv")

    rconfig.user = os.getenv("RAINYUN_USERNAME", "username").split("|")[0]
    rconfig.pwd = os.getenv("RAINYUN_PASSWORD", "password").split("|")[0]

    run_mode = os.getenv("RUN_MODE", "schedule")
    schedule_time = os.getenv("SCHEDULE_TIME", "08:00")

    logger = setup_logging()
    ver = "2.2-docker-notify-pp"
    logger.info("===================================================================")
    logger.info(f"🌧️ Rainyun-Qiandao v{ver} (Selenium)")
    logger.info("👨‍💻 Based on original project by: SerendipityR-2022")
    logger.info("🚀 Maintained & Extended by: LeapYa")
    logger.info("🔗 GitHub: https://github.com/LeapYa/Rainyun-Qiandao")
    logger.info("💡 开源不易，感谢原作者。请二、三次修改者能够保留源出处，谢谢！")
    logger.info("===================================================================")
    print("")
    logger.info("已启用日志轮转功能，将自动清理7天前的日志")
    if rconfig.debug:
        logger.info(f"当前配置: MAX_DELAY={rconfig.max_delay}分钟, TIMEOUT={rconfig.timeout}秒")

    cleanup_logs_on_startup()

    setup_sigchld_handler()

    logger.info("程序启动，检查系统中的僵尸进程...")
    cleanup_zombie_processes()

    if run_mode == "schedule":
        logger.info(f"启动定时模式，每天 {schedule_time} 自动执行签到")
        logger.info("程序将持续运行，按 Ctrl+C 退出")
        logger.info(f"当前应用时区: {rconfig.get_app_timezone_name()}")

        schedule.every().day.at(schedule_time).do(scheduled_checkin)

        tomorrow_schedule = now_local().replace(
            hour=int(schedule_time.split(':')[0]),
            minute=int(schedule_time.split(':')[1]),
            second=0, microsecond=0
        )
        if tomorrow_schedule <= now_local():
            tomorrow_schedule += timedelta(days=1)
        logger.info(f"每日执行时间: {tomorrow_schedule.strftime('%Y-%m-%d %H:%M:%S')}")

        logger.info("首次启动，将在1分钟后执行首次签到任务")
        first_run_time = now_local() + timedelta(minutes=1)
        logger.info(f"首次执行时间: {first_run_time.strftime('%Y-%m-%d %H:%M:%S')}")

        logger.info("调度器已启动，等待执行任务...")
        first_run_done = False

        try:
            while True:
                current_time = now_local()

                if not first_run_done and current_time >= first_run_time:
                    logger.info("执行首次签到任务（所有账号）")
                    success = run_all_accounts()
                    if success:
                        logger.info("首次签到任务执行成功！")
                    else:
                        logger.error("首次签到任务执行失败！")

                    logger.info("首次任务完成，查看下次执行安排...")
                    logger.info(f"✅ 程序将继续运行，下次执行时间: {tomorrow_schedule.strftime('%Y-%m-%d %H:%M:%S')}")
                    time_diff = tomorrow_schedule - now_local()
                    hours, remainder = divmod(time_diff.total_seconds(), 3600)
                    minutes, _ = divmod(remainder, 60)
                    logger.info(f"距离下次执行还有: {int(hours)}小时{int(minutes)}分钟")

                    first_run_done = True

                schedule.run_pending()
                time.sleep(30)

        except KeyboardInterrupt:
            logger.info("程序已停止")
    else:
        logger.info("运行模式: 单次执行（所有账号）")
        success = run_all_accounts()
        if success:
            logger.info("程序执行完成")
        else:
            logger.error("程序执行失败")
