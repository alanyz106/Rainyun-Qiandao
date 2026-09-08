import logging
import logging.handlers
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# GitHub Actions 环境检测（Actions 海外 IP 会被雨云拒绝连接，需自动走国内代理）
_IN_ACTIONS = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

DEFAULT_TIMEZONE = "Asia/Shanghai"

# 运行态配置（由入口点在 main 中设置）
timeout = 30
max_delay = 5
debug = False
linux = True
user = ""
pwd = ""


def get_app_timezone_name():
    return (os.getenv("TZ", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE).strip()


def get_app_timezone():
    tz_name = get_app_timezone_name()
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        logger.warning(f"未找到时区 '{tz_name}'，回退为 {DEFAULT_TIMEZONE}")
        return timezone(timedelta(hours=8), name=DEFAULT_TIMEZONE)


APP_TIMEZONE = get_app_timezone()


def now_local():
    return datetime.now(APP_TIMEZONE)


def configure_process_timezone():
    tz_name = get_app_timezone_name()
    os.environ["TZ"] = tz_name
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except Exception as exc:
            logger.warning(f"设置进程时区失败: {exc}")


def apply_browser_timezone(driver):
    tz_name = get_app_timezone_name()
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
            "timezoneId": tz_name
        })
        logger.info(f"浏览器时区已设置为: {tz_name}")
    except Exception as exc:
        logger.warning(f"设置浏览器时区失败: {exc}")


def setup_logging():
    configure_process_timezone()

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, "rainyun.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    cleanup_old_logs(log_dir, days=7)
    cleanup_old_logs(log_dir, days=7)

    return root_logger


def cleanup_old_logs(log_dir, days=7):
    try:
        now = time.time()
        cutoff = now - (days * 86400)

        for filename in os.listdir(log_dir):
            file_path = os.path.join(log_dir, filename)
            if os.path.isfile(file_path) and filename.startswith('rainyun.log.'):
                file_time = os.path.getmtime(file_path)
                if file_time < cutoff:
                    os.remove(file_path)
                    logging.info(f"已删除过期日志文件: {filename}")
    except Exception as e:
        logging.error(f"清理旧日志文件时出错: {e}")


def cleanup_logs_on_startup():
    log_dir = "logs"
    if not os.path.exists(log_dir):
        return

    try:
        log_files = [f for f in os.listdir(log_dir) if f.startswith('rainyun.log.')]
        total_size = sum(os.path.getsize(os.path.join(log_dir, f)) for f in log_files if os.path.isfile(os.path.join(log_dir, f)))

        if log_files:
            logging.info(f"检测到 {len(log_files)} 个历史日志文件，总大小约 {total_size / 1024 / 1024:.2f} MB")

            if len(log_files) > 10:
                logging.info("历史日志文件过多，执行清理...")
                cleanup_old_logs(log_dir, days=7)

                remaining_files = [f for f in os.listdir(log_dir) if f.startswith('rainyun.log.')]
                remaining_size = sum(os.path.getsize(os.path.join(log_dir, f)) for f in remaining_files if os.path.isfile(os.path.join(log_dir, f)))
                logging.info(f"清理完成，剩余 {len(remaining_files)} 个日志文件，总大小约 {remaining_size / 1024 / 1024:.2f} MB")
    except Exception as e:
        logging.error(f"启动时日志清理出错: {e}")


# ==========================================
# Selenium 模块懒加载管理
# ==========================================
selenium_modules = None


def import_selenium_modules():
    global selenium_modules
    if selenium_modules is None:
        from selenium import webdriver
        from selenium.webdriver import ActionChains
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.webdriver import WebDriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.wait import WebDriverWait
        from selenium.common import TimeoutException, WebDriverException

        selenium_modules = {
            'webdriver': webdriver,
            'ActionChains': ActionChains,
            'Options': Options,
            'Service': Service,
            'WebDriver': WebDriver,
            'By': By,
            'EC': EC,
            'WebDriverWait': WebDriverWait,
            'TimeoutException': TimeoutException,
            'WebDriverException': WebDriverException
        }
    return selenium_modules


def unload_selenium_modules():
    global selenium_modules
    if selenium_modules is not None:
        modules_to_remove = [
            'selenium',
            'selenium.webdriver',
            'selenium.webdriver.chrome',
            'selenium.webdriver.chrome.options',
            'selenium.webdriver.chrome.service',
            'selenium.webdriver.chrome.webdriver',
            'selenium.webdriver.common',
            'selenium.webdriver.common.by',
            'selenium.webdriver.support',
            'selenium.webdriver.support.expected_conditions',
            'selenium.webdriver.support.wait',
            'selenium.common'
        ]

        for module in modules_to_remove:
            if module in sys.modules:
                del sys.modules[module]

        selenium_modules = None


# ==========================================
# 进程清理
# ==========================================
def setup_sigchld_handler():
    import signal

    def sigchld_handler(signum, frame):
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break
            except Exception:
                break

    if os.name == 'posix':
        signal.signal(signal.SIGCHLD, sigchld_handler)
        logging.info("已设置子进程自动回收机制，防止僵尸进程累积")


def cleanup_zombie_processes():
    """回收本进程的子进程残留。

    历史坑（2026-09-08）：原实现末尾有
        subprocess.run(['pkill', '-9', '-f', 'chrome.*--type='], timeout=5)
    该正则会匹配 pkill 自身的命令行（re.search('chrome.*--type=',
    "pkill -9 -f 'chrome.*--type='") 为 True），且 pkill -f 会杀掉同父 shell 下
    任何命令行含该串的进程。在 GitHub Actions 上导致「执行签到」step 挂死
    40+ 分钟且 gh run cancel 无效（进程已不响应信号）。

    现改为：只对【本进程已知的子进程】做非阻塞回收 + 兜底 terminate，
    绝不使用 pkill -f 之类会误伤无关进程（甚至自身进程链）的模糊匹配。
    """

    def _reap_own_children():
        """非阻塞回收本进程已退出的子进程。"""
        reaped = 0
        try:
            while True:
                pid, _status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
                reaped += 1
        except (ChildProcessError, OSError):
            # 没有子进程可回收，属正常情况
            pass
        return reaped

    try:
        reaped = _reap_own_children()
        if reaped:
            logger.info(f"已回收 {reaped} 个已退出的子进程")
    except Exception as e:
        logger.debug(f"回收子进程时出现异常（可忽略）: {e}")