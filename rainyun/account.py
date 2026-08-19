import logging
import os
import time

logger = logging.getLogger(__name__)


def parse_accounts():
    usernames = os.getenv("RAINYUN_USERNAME", "").split("|")
    passwords = os.getenv("RAINYUN_PASSWORD", "").split("|")

    if len(usernames) != len(passwords):
        logger.warning("用户名和密码数量不匹配，只使用匹配的部分")
        min_len = min(len(usernames), len(passwords))
        usernames = usernames[:min_len]
        passwords = passwords[:min_len]

    accounts = [(u.strip(), p.strip()) for u, p in zip(usernames, passwords) if u.strip() and p.strip()]

    if not accounts:
        single_user = os.getenv("RAINYUN_USERNAME", "username")
        single_pwd = os.getenv("RAINYUN_PASSWORD", "password")
        accounts = [(single_user, single_pwd)]

    logger.info(f"检测到 {len(accounts)} 个账号")
    for i, (username, _) in enumerate(accounts, 1):
        masked_user = f"{username[:3]}***{username[-3:] if len(username) > 6 else username}"
        logger.info(f"账号 {i}: {masked_user}")

    return accounts


def save_cookies(driver, account_id):
    import json
    import hashlib

    if not account_id:
        return

    os.makedirs("temp/cookies", exist_ok=True)
    account_hash = hashlib.md5(account_id.encode()).hexdigest()[:16]
    cookie_path = os.path.join("temp", "cookies", f"{account_hash}.json")

    try:
        cookies = driver.get_cookies()
        with open(cookie_path, 'w', encoding='utf-8') as f:
            json.dump(cookies, f, ensure_ascii=False)
        logger.info(f"Cookie 已保存到本地")
    except Exception as e:
        logger.warning(f"保存 Cookie 失败: {e}")


def load_cookies(driver, account_id):
    import json
    import hashlib

    if not account_id:
        return False

    account_hash = hashlib.md5(account_id.encode()).hexdigest()[:16]
    cookie_path = os.path.join("temp", "cookies", f"{account_hash}.json")

    if not os.path.exists(cookie_path):
        logger.info("未找到本地 Cookie，将使用账号密码登录")
        return False

    try:
        with open(cookie_path, 'r', encoding='utf-8') as f:
            cookies = json.load(f)

        driver.get("https://app.rainyun.com/")
        time.sleep(1)

        for cookie in cookies:
            if 'expiry' in cookie:
                cookie['expiry'] = int(cookie['expiry'])
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass

        logger.info(f"已加载本地 Cookie")
        return True
    except Exception as e:
        error_msg = str(e)
        # 代理/网络类异常必须上抛（ERR_PROXY_CONNECTION_FAILED 等），
        # 让上层 checkin.py 的 WebDriverException 捕获分支标记 proxy_failed，
        # 重试时才能换新代理。若在此吞掉，坏代理会被复用导致重复失败。
        if any(kw in error_msg for kw in (
            "ERR_PROXY", "ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED",
            "ERR_TIMED_OUT", "ERR_CONNECTION", "ERR_CERT", "ERR_SSL",
            "Timed out receiving message from renderer",
        )):
            raise
        logger.warning(f"加载 Cookie 失败: {e}")
        return False
