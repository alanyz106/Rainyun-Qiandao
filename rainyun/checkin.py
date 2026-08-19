import logging
import os
import random
import time
from datetime import timedelta

import schedule

from rainyun import config as rconfig
from rainyun.account import load_cookies, parse_accounts, save_cookies
from rainyun.browser import (
    check_rainyun_blocked,
    generate_fingerprint_script,
    get_freeproxy_ip,
    get_proxy_ip,
    init_selenium,
    validate_proxy,
)
from rainyun.captcha import get_captcha_provider
from rainyun.config import _IN_ACTIONS, import_selenium_modules, now_local, unload_selenium_modules
from rainyun.config import logger as config_logger
from rainyun.report import generate_html_report, generate_markdown_report, generate_summary_report, save_daily_record, save_screenshot

logger = logging.getLogger(__name__)


def _is_ssl_blocked_text(text):
    """检测页面文字是否为 SSL/证书警告页（代理 MITM 或连接不安全）。
    headless Chrome 拒绝 MITM 代理的伪造证书时会显示 "Your connection is not private"。
    """
    if not text:
        return False
    text_lower = text.lower()
    ssl_keywords = (
        "your connection is not private",   # Chrome NET::ERR_CERT_AUTHORITY_INVALID 等
        "your connection is not secure",    # Firefox
        "net::err_cert",                    # Chrome 各类证书错误
        "net::err_ssl",                     # Chrome SSL 协议错误
        "potential security risk ahead",    # Firefox 警告页
        "ssl_error",                        # 通用 SSL 错误
    )
    return any(kw in text_lower for kw in ssl_keywords)


def dismiss_modal_confirm(driver, timeout):
    modules = import_selenium_modules()
    WebDriverWait = modules['WebDriverWait']
    EC = modules['EC']
    By = modules['By']
    TimeoutException = modules['TimeoutException']

    wait = WebDriverWait(driver, min(timeout, 5))
    try:
        confirm = wait.until(
            EC.element_to_be_clickable(
                (By.XPATH, "//footer[contains(@id,'modal') and contains(@id,'footer')]//button[contains(normalize-space(.), '确认')]")
            )
        )
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", confirm)
        except Exception:
            pass
        time.sleep(0.2)
        confirm.click()
        logger.info("已关闭弹窗：确认")
        time.sleep(0.5)
        return True
    except TimeoutException:
        return False
    except Exception:
        try:
            confirm = driver.find_element(By.XPATH, "//button[contains(normalize-space(.), '确认') and contains(@class,'btn')]")
            driver.execute_script("arguments[0].click();", confirm)
            logger.info("已关闭弹窗：确认")
            time.sleep(0.5)
            return True
        except Exception:
            return False


def wait_captcha_or_modal(driver, timeout):
    modules = import_selenium_modules()
    WebDriverWait = modules['WebDriverWait']
    EC = modules['EC']
    By = modules['By']
    TimeoutException = modules['TimeoutException']

    def find_visible_tcaptcha_iframe():
        try:
            iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[id^='tcaptcha_iframe']")
        except Exception:
            return None
        for fr in iframes:
            try:
                if fr.is_displayed() and fr.size.get("width", 0) > 0 and fr.size.get("height", 0) > 0:
                    return fr
            except Exception:
                continue
        return None

    end_time = time.time() + min(timeout, 20)
    while time.time() < end_time:
        if dismiss_modal_confirm(driver, timeout):
            return "modal"
        try:
            iframe = find_visible_tcaptcha_iframe()
            if iframe:
                return "captcha"
        except Exception:
            pass
        time.sleep(0.3)
    return "none"


def run_checkin(account_user=None, account_pwd=None, reuse_proxy=None):
    """执行单个账号的签到任务"""
    modules = import_selenium_modules()
    webdriver = modules['webdriver']
    ActionChains = modules['ActionChains']
    By = modules['By']
    EC = modules['EC']
    WebDriverWait = modules['WebDriverWait']
    TimeoutException = modules['TimeoutException']
    WebDriverException = modules['WebDriverException']
    import subprocess

    current_user = account_user or rconfig.user
    current_pwd = account_pwd or rconfig.pwd
    driver = None
    retry_stats = {'count': 0}

    masked_user = f"{current_user[:3]}***{current_user[-3:] if len(current_user) > 6 else current_user}"

    class PrefixAdapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):
            return '[%s] %s' % (self.extra['prefix'], msg), kwargs

    logger_adapter = PrefixAdapter(logger, {'prefix': masked_user})

    proxy = None
    proxy_failed = False
    try:
        logger_adapter.info(f"开始执行签到任务...")

        # 获取代理IP（每个账号单独获取）
        proxy_api_url = os.getenv("PROXY_API_URL", "").strip()
        if proxy_api_url:
            # 优先使用配置的代理接口（付费/自建）
            proxy = get_proxy_ip()
            if proxy:
                # 验证代理可用性
                if validate_proxy(proxy):
                    logger_adapter.info(f"代理 {proxy} 验证通过，将使用此代理")
                else:
                    logger_adapter.warning(f"代理 {proxy} 验证失败，将使用本地IP继续")
                    proxy = None
            else:
                logger_adapter.warning("获取代理失败，将使用本地IP继续")
        elif _IN_ACTIONS or check_rainyun_blocked():
            # 海外 IP 会被雨云拒绝连接（浏览器显示 This site can't be reached），
            # 自动抓取国内免费代理绕过拦截，覆盖 GitHub Actions、海外 VPS、Docker 等环境。
            # 重试时优先复用上次的代理：换 IP 会导致服务器 Cookie 失效，
            # 进而被迫走密码登录，而慢代理下密码登录容易超时失败。
            if reuse_proxy:
                if validate_proxy(reuse_proxy):
                    proxy = reuse_proxy
                    logger_adapter.info(f"复用上次代理: {proxy}（避免换 IP 导致 Cookie 失效）")
                else:
                    logger_adapter.warning(f"上次代理 {reuse_proxy} 已失效，重新抓取国内代理")
                    proxy = get_freeproxy_ip()
            else:
                proxy = get_freeproxy_ip()
            if proxy:
                logger_adapter.info(f"国内代理 {proxy} 已就绪，用于绕过海外 IP 拦截")
            else:
                logger_adapter.warning("未获取到可用国内代理，直连可能被拒绝连接")

        logger_adapter.info("初始化 Selenium（账号专属配置）")
        driver = init_selenium(current_user, proxy=proxy)
        from rainyun.config import apply_browser_timezone
        apply_browser_timezone(driver)

        with open("stealth.min.js", mode="r") as f:
            js = f.read()
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": js
        })

        fingerprint_js = generate_fingerprint_script(current_user)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": fingerprint_js
        })
        logger_adapter.info("已注入浏览器指纹脚本（账号专属指纹）")

        timeout = rconfig.timeout
        wait = WebDriverWait(driver, timeout)

        # 加载 Cookie 并直接跳转积分页
        # 慢代理或断连会导致 driver.get() 抛 WebDriverException（ERR_PROXY_CONNECTION_FAILED 等），
        # 需要捕获并标记为代理失败，让重试机制换新代理而非复用旧代理。
        proxy_failed = False
        try:
            load_cookies(driver, current_user)
            logger_adapter.info("正在跳转积分页...")
            driver.get("https://app.rainyun.com/account/reward/earn")
            time.sleep(3)
        except WebDriverException as e:
            error_msg = str(e)
            if any(kw in error_msg for kw in (
                "ERR_PROXY", "ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED",
                "ERR_TIMED_OUT", "ERR_CONNECTION", "ERR_CERT", "ERR_SSL",
                "Timed out receiving message from renderer"
            )):
                logger_adapter.error(f"代理连接失败，页面无法加载: {error_msg[:200]}")
                screenshot_path = save_screenshot(driver, current_user, status="failure")
                return {
                    'status': False, 'msg': '代理连接失败，页面无法加载', 'points': 0,
                    'username': masked_user,
                    'retries': retry_stats['count'], 'screenshot': screenshot_path,
                    'proxy': proxy, 'proxy_failed': True
                }
            raise

        if "/auth/login" in driver.current_url:
            logger_adapter.info("Cookie 已失效，使用账号密码登录")

            try:
                username = wait.until(EC.visibility_of_element_located((By.NAME, 'login-field')))
                password = wait.until(EC.visibility_of_element_located((By.NAME, 'login-password')))
                login_button = wait.until(EC.visibility_of_element_located((By.XPATH,
                    '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button')))
                username.send_keys(current_user)
                password.send_keys(current_pwd)

                # 关键：取消勾选 "7天免登录"（默认勾选会触发简化验证流程，导致验证码点选图片不下发）
                try:
                    remember_me = driver.find_element(By.ID, 'remember-me')
                    if remember_me.is_selected():
                        driver.execute_script("arguments[0].click();", remember_me)
                        logger_adapter.info("已取消勾选'7天免登录'，确保验证码图片正常下发")
                except Exception as e:
                    logger_adapter.debug(f"取消免登录勾选失败(忽略): {e}")

                login_button.click()

                # 登录页验证码为"软拦截"：账号密码正确时，即使不破解，
                # 腾讯验证码超时后也会自动放行、表单提交成功并跳转。
                # 在 GH headless 下 #slideBg 不可见，强行破解会让本地 OCR 白等 max(timeout,60)s 超时，
                # 因此这里不再破解登录页验证码，点登录后直接轮询等待跳转到登录成功态。
                logger_adapter.info("已点击登录，等待验证码软放行与页面跳转...")
                logged_in = False
                login_wait_end = time.time() + min(timeout, 30)
                while time.time() < login_wait_end:
                    try:
                        current = driver.current_url
                        if "/dashboard" in current or "/account" in current:
                            logged_in = True
                            break
                    except Exception:
                        pass
                    dismiss_modal_confirm(driver, 5)
                    time.sleep(2)
            except TimeoutException:
                logger_adapter.error("页面加载超时")
                screenshot_path = save_screenshot(driver, current_user, status="failure")
                return {
                    'status': False, 'msg': '页面加载超时', 'points': 0,
                    'username': masked_user,
                    'retries': retry_stats['count'], 'screenshot': screenshot_path,
                    'proxy': proxy, 'proxy_failed': proxy_failed
                }

            time.sleep(2)
            driver.switch_to.default_content()
            dismiss_modal_confirm(driver, timeout)

            if "/dashboard" in driver.current_url or "/account" in driver.current_url:
                logger_adapter.info("登录成功！")
                save_cookies(driver, current_user)
                driver.get("https://app.rainyun.com/account/reward/earn")
                time.sleep(2)
            else:
                logger_adapter.error(f"登录失败，当前页面: {driver.current_url}")
                screenshot_path = save_screenshot(driver, current_user, status="failure")
                return {
                    'status': False, 'msg': '登录失败', 'points': 0,
                    'username': masked_user,
                    'retries': retry_stats['count'], 'screenshot': screenshot_path,
                    'proxy': proxy, 'proxy_failed': proxy_failed
                }
        else:
            logger_adapter.info("Cookie 有效，免密登录成功！🎉")

        if "/account/reward/earn" not in driver.current_url:
            driver.get("https://app.rainyun.com/account/reward/earn")

        driver.implicitly_wait(5)
        time.sleep(1)
        dismiss_modal_confirm(driver, timeout)
        dismiss_modal_confirm(driver, timeout)

        checkin_xpath = '//div[contains(@class, "card-header")]//span[contains(text(), "每日签到")]/ancestor::div[contains(@class, "card-header")]//*[contains(@class, "badge")]'
        checkin_xpath_fallback = '//div[contains(@class, "card")]//span[contains(text(), "每日签到")]/../..//*[contains(@class, "badge")]'
        checkin_xpath_link = '//span[contains(text(), "每日签到")]/..//a[contains(@class, "badge") or contains(text(), "领取奖励")]'

        earn = None
        for xpath in [checkin_xpath, checkin_xpath_fallback, checkin_xpath_link]:
            try:
                earn = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, xpath))
                )
                break
            except TimeoutException:
                continue

        if earn is None:
            logger_adapter.error("无法找到每日签到按钮")
            body_text = ""
            try:
                body_text = driver.find_element(By.TAG_NAME, "body").text[:500]
                logger_adapter.error(f"页面可见文字（前500字）: {body_text}")
            except Exception:
                pass
            # 识别代理 SSL 中间人：headless Chrome 拒绝 MITM 证书 → 显示 "Your connection is not private"。
            # 此类失败必须标记 proxy_failed，否则重试会复用同一个坏代理 → 重复失败。
            ssl_blocked = _is_ssl_blocked_text(body_text)
            if ssl_blocked:
                proxy_failed = True
                logger_adapter.error("检测到代理 SSL 拦截（MITM），标记 proxy_failed 以便重试换代理")
            # 代理已配置但页面几乎空白：免费代理时通时断，验证阶段通过不代表浏览器会话可用，
            # 常见表现为 ERR_PROXY_CONNECTION_FAILED 后页面 JS 未渲染、body 无文字。
            # 有代理 + 页面空白 → 判定代理失效，标记 proxy_failed 让重试换新代理。
            if proxy and len(body_text.strip()) < 20:
                proxy_failed = True
                logger_adapter.error(
                    "检测到代理已配置但页面空白（body 无文字），疑似代理失效，标记 proxy_failed 以便重试换代理"
                )
            screenshot_path = save_screenshot(driver, current_user, status="failure")
            return {
                'status': False,
                'msg': '代理 SSL 拦截：页面无法加载' if ssl_blocked else '签到按钮未找到',
                'points': 0,
                'username': masked_user,
                'retries': retry_stats['count'], 'screenshot': screenshot_path,
                'proxy': proxy, 'proxy_failed': ssl_blocked or proxy_failed
            }

        btn_text = earn.text.strip()

        # 签到前先读取积分
        points_before = 0
        try:
            points_raw_before = driver.find_element(By.XPATH,
                                                     '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3').get_attribute(
                "textContent")
            import re
            points_before = int(''.join(re.findall(r'\d+', points_raw_before)))
            logger_adapter.info(f"签到前积分: {points_before}")
        except Exception as e:
            logger_adapter.warning(f"读取签到前积分失败: {e}")

        if "领取奖励" in btn_text:
            logger_adapter.info("开始点击领取奖励...")
            earn.click()
            captcha_timeout = max(timeout, 20)
            state = wait_captcha_or_modal(driver, captcha_timeout)
            if state == "captcha":
                logger_adapter.info("处理验证码")
                try:
                    captcha_iframe = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "iframe[id^='tcaptcha_iframe']")))
                    driver.switch_to.frame(captcha_iframe)
                    captcha_provider = get_captcha_provider()
                    captcha_provider.solve(driver, timeout, retry_stats, logger_adapter)
                finally:
                    driver.switch_to.default_content()
                driver.implicitly_wait(5)
            else:
                logger_adapter.info("未触发验证码")

            time.sleep(3)
            retry_check = 0
            checkin_success = False
            while retry_check < 5 and not checkin_success:
                earn_verify = None
                for v_xpath in [checkin_xpath, checkin_xpath_fallback, checkin_xpath_link]:
                    try:
                        earn_verify = WebDriverWait(driver, 3).until(
                            EC.presence_of_element_located((By.XPATH, v_xpath))
                        )
                        break
                    except TimeoutException:
                        continue
                if earn_verify is not None:
                    btn_after = earn_verify.text.strip()
                    if "已完成" in btn_after:
                        logger_adapter.info(f"签到验证通过，按钮文字变为: [{btn_after}]")
                        checkin_success = True
                        break

                if not checkin_success:
                    try:
                        body_text = driver.find_element(By.TAG_NAME, "body").text
                        if "已完成" in body_text:
                            logger_adapter.info("页面检测到'已完成'，签到验证通过")
                            checkin_success = True
                            break
                    except Exception:
                        pass

                if not checkin_success:
                    logger_adapter.warning(f"签到后按钮文字仍为 [领取奖励]，等待重试 ({retry_check+1}/5)")
                    time.sleep(3)
                    retry_check += 1

            if not checkin_success:
                logger_adapter.error("签到验证失败：点击领取奖励后按钮状态未改变")
                screenshot_path = save_screenshot(driver, current_user, status="failure")
                return {
                    'status': False, 'msg': '签到失败：验证码未正确处理或签到未生效', 'points': 0,
                    'username': masked_user,
                    'retries': retry_stats['count'], 'screenshot': screenshot_path,
                    'proxy': proxy, 'proxy_failed': proxy_failed
                }
        else:
            logger_adapter.info(f"今日已签到（按钮显示: {btn_text}）")

        points_raw = driver.find_element(By.XPATH,
                                         '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3').get_attribute(
            "textContent")
        import re
        current_points = int(''.join(re.findall(r'\d+', points_raw)))
        if not os.getenv('CI'):
            logger_adapter.info(f"当前剩余积分: {current_points} | 约为 {current_points / 2000:.2f} 元")

        # 判断是"今日已签到"还是"签到成功"
        is_already_checked_in = "领取奖励" not in btn_text
        if is_already_checked_in:
            logger_adapter.info("今日已签到，无需重复签到")
            screenshot_path = save_screenshot(driver, current_user, status="success")
            return {
                'status': True,
                'msg': '今日已签到',
                'points': current_points,
                'username': masked_user,
                'retries': retry_stats['count'],
                'screenshot': screenshot_path,
                'proxy': proxy, 'proxy_failed': proxy_failed
            }

        # 签到后积分验证
        if points_before > 0 and current_points <= points_before:
            logger_adapter.warning(f"签到后积分未增加（签到前: {points_before}, 签到后: {current_points}），签到可能失败")
            screenshot_path = save_screenshot(driver, current_user, status="failure")
            return {
                'status': False,
                'msg': '签到失败：积分未增加',
                'points': current_points,
                'username': masked_user,
                'retries': retry_stats['count'],
                'screenshot': screenshot_path,
                'proxy': proxy, 'proxy_failed': proxy_failed
            }

        logger_adapter.info(f"签到成功，积分从 {points_before} 增加到 {current_points}")
        screenshot_path = save_screenshot(driver, current_user, status="success")
        return {
            'status': True,
            'msg': '签到成功',
            'points': current_points,
            'username': masked_user,
            'retries': retry_stats['count'],
            'screenshot': screenshot_path,
            'proxy': proxy, 'proxy_failed': proxy_failed
        }

    except Exception as e:
        error_msg = str(e)
        # 判断异常是否由代理引起（ERR_PROXY_CONNECTION_FAILED、renderer 超时等）
        is_proxy_error = any(kw in error_msg for kw in (
            "ERR_PROXY", "ERR_INTERNET_DISCONNECTED", "ERR_NAME_NOT_RESOLVED",
            "ERR_TIMED_OUT", "ERR_CONNECTION", "ERR_CERT", "ERR_SSL",
            "Timed out receiving message from renderer"
        ))
        # 代理环境下，元素找不到通常是代理过慢导致页面 JS 未完整渲染
        if proxy and not is_proxy_error and "no such element" in error_msg.lower():
            is_proxy_error = True
        logger_adapter.error(f"签到任务执行失败: {e}")
        import traceback
        logger_adapter.error(f"详细错误信息: {traceback.format_exc()}")
        screenshot_path = None
        if driver is not None:
            screenshot_path = save_screenshot(driver, current_user, status="failure")
        return {
            'status': False,
            'msg': f'执行异常: {str(e)[:50]}...',
            'points': 0,
            'username': masked_user,
            'retries': retry_stats['count'],
            'screenshot': screenshot_path,
            'proxy': proxy, 'proxy_failed': is_proxy_error
        }
    finally:
        if driver is not None:
            try:
                logger_adapter.info("正在关闭 WebDriver...")

                try:
                    driver.quit()
                    logger_adapter.info("WebDriver 已安全关闭")
                except Exception as e:
                    logger_adapter.error(f"关闭 WebDriver 时出错: {e}")

                time.sleep(1)

                try:
                    if hasattr(driver, 'service') and driver.service.process:
                        process = driver.service.process
                        pid = process.pid

                        if os.name == 'posix' and pid:
                            try:
                                logger_adapter.info(f"正在清理 PID {pid} 的衍生进程...")
                                subprocess.run(['pkill', '-9', '-P', str(pid)],
                                             stderr=subprocess.DEVNULL)
                            except Exception:
                                pass

                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                            logger_adapter.info(f"已终止 ChromeDriver 进程 (PID: {pid})")
                except Exception as e:
                    logger_adapter.debug(f"清理 ChromeDriver 进程时出错: {e}")

            except Exception as e:
                logger_adapter.error(f"WebDriver 清理过程出现异常: {e}")

        try:
            unload_selenium_modules()
            logger.debug("已卸载Selenium模块")
        except:
            pass


def run_all_accounts():
    """执行所有账号的签到任务"""
    import concurrent.futures

    max_retries = int(os.getenv("CHECKIN_MAX_RETRIES", "2"))
    max_workers = int(os.getenv("MAX_WORKERS", "3"))
    stagger_delay = int(os.getenv("MAX_DELAY", "15"))

    accounts = parse_accounts()
    results = {}

    for i, (username, password) in enumerate(accounts):
        results[username] = {
            'password': password,
            'result': None,
            'retry_count': 0,
            'index': i + 1
        }

    pending_accounts = list(accounts)
    current_attempt = 0

    while pending_accounts and current_attempt <= max_retries:
        if current_attempt == 0:
            logger.info(f"========== 开始执行签到任务（共 {len(pending_accounts)} 个账号，并发数: {max_workers}） ==========")
        else:
            logger.info(f"========== 第 {current_attempt} 次重试（共 {len(pending_accounts)} 个失败账号） ==========")

        failed_accounts = []
        future_to_account = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            for i, (username, password) in enumerate(pending_accounts):
                if i > 0 and stagger_delay > 0:
                    lower_bound = 5
                    upper_bound = max(5, stagger_delay)
                    actual_delay = random.randint(lower_bound, upper_bound)
                    logger.info(f"随机等待 {actual_delay} 秒后启动下一个账号任务...")
                    time.sleep(actual_delay)

                account_idx = results[username]['index']
                retry_info = f"（第 {results[username]['retry_count'] + 1} 次尝试）" if results[username]['retry_count'] > 0 else ""
                logger.info(f"========== 启动账号 {account_idx}/{len(accounts)} {retry_info} ==========")

                # 重试时复用上次代理，避免换 IP 导致 Cookie 失效。
                # 但如果上次失败是代理问题（proxy_failed），则不复用——慢代理通过 validate_proxy
                # 却无法支撑浏览器会话，复用只会重复同样的失败。
                reuse_proxy = None
                if results[username]['result'] and results[username]['result'].get('proxy'):
                    if not results[username]['result'].get('proxy_failed'):
                        reuse_proxy = results[username]['result']['proxy']
                    else:
                        logger.info(f"上次失败由代理引起，不复用旧代理，重新抓取")

                future = executor.submit(run_checkin, username, password, reuse_proxy)
                future_to_account[future] = username

            for future in concurrent.futures.as_completed(future_to_account):
                username = future_to_account[future]
                account_idx = results[username]['index']

                try:
                    result = future.result()
                    results[username]['result'] = result

                    if result['status']:
                        logger.info(f"✅ 账号 {account_idx} 签到成功")
                    else:
                        logger.error(f"❌ 账号 {account_idx} 签到失败: {result['msg']}")
                        results[username]['retry_count'] += 1
                        if results[username]['retry_count'] <= max_retries:
                            failed_accounts.append((username, results[username]['password']))
                except Exception as e:
                    logger.error(f"❌ 账号 {account_idx} 执行异常: {e}")
                    results[username]['retry_count'] += 1
                    if results[username]['retry_count'] <= max_retries:
                        failed_accounts.append((username, results[username]['password']))

        pending_accounts = failed_accounts
        current_attempt += 1

        if pending_accounts:
            retry_wait = 60
            logger.info(f"等待 {retry_wait} 秒后开始重试 {len(pending_accounts)} 个失败账号...")
            time.sleep(retry_wait)

    final_results = [results[username]['result'] for username, _ in accounts]
    success_count = len([r for r in final_results if r and r['status'] and r.get('msg') != '今日已签到'])
    already_checked_in_count = len([r for r in final_results if r and r['status'] and r.get('msg') == '今日已签到'])

    retry_accounts = [(username, results[username]['retry_count']) for username, _ in accounts if results[username]['retry_count'] > 0]
    if retry_accounts:
        logger.info(f"重试统计: {len(retry_accounts)} 个账号进行了重试")
        for username, count in retry_accounts:
            masked_user = f"{username[:3]}***{username[-3:] if len(username) > 6 else username}"
            final_status = "成功" if results[username]['result'] and results[username]['result']['status'] else "失败"
            logger.info(f"  - {masked_user}: 重试 {count} 次, 最终{final_status}")

    if accounts:
        from rainyun.notifications import DingTalkProvider, EmailProvider, NotificationManager, PushPlusProvider, WXPusherProvider

        notification_manager = NotificationManager()

        push_token = os.getenv("PUSHPLUS_TOKEN")
        if push_token:
            logger.info("Configuring PushPlus provider...")
            notification_manager.add_provider(PushPlusProvider(push_token))

        wx_app_token = os.getenv("WXPUSHER_APP_TOKEN")
        wx_uids = os.getenv("WXPUSHER_UIDS")
        wx_topics = os.getenv("WXPUSHER_TOPIC_IDS")
        if wx_app_token and (wx_uids or wx_topics):
            logger.info("Configuring WXPusher provider...")
            notification_manager.add_provider(WXPusherProvider(wx_app_token, wx_uids, wx_topics))

        dingtalk_token = os.getenv("DINGTALK_ACCESS_TOKEN")
        dingtalk_secret = os.getenv("DINGTALK_SECRET")
        if dingtalk_token:
            logger.info("Configuring DingTalk provider...")
            notification_manager.add_provider(DingTalkProvider(dingtalk_token, dingtalk_secret))

        smtp_host = os.getenv("SMTP_HOST")
        smtp_port = os.getenv("SMTP_PORT")
        smtp_user = os.getenv("SMTP_USER")
        smtp_pass = os.getenv("SMTP_PASS")
        smtp_to = os.getenv("SMTP_TO")

        if smtp_host and smtp_port and smtp_user and smtp_pass:
            if not smtp_to and accounts:
                first_account = accounts[0][0]
                if '@' in first_account:
                    smtp_to = first_account
                    logger.info(f"配置提示: 未填写 SMTP_TO，将使用第一个雨云账号 ({smtp_to}) 作为收件人")

            if smtp_to:
                logger.info("Configuring Email provider...")
                notification_manager.add_provider(EmailProvider(smtp_host, smtp_port, smtp_user, smtp_pass, smtp_to))

        if notification_manager.providers:
            logger.info("正在生成详细推送报告...")

            # 先把今日结果写入 stats/，这样月统计能包含今天
            save_daily_record(final_results)

            screenshot_mode = os.getenv("SCREENSHOT_MODE", "failed_only").strip().lower()
            if screenshot_mode not in ('all', 'failed_only', 'none'):
                logger.warning(f"无效的 SCREENSHOT_MODE '{screenshot_mode}'，使用默认值 'failed_only'")
                screenshot_mode = 'failed_only'
            logger.info(f"截图策略: {screenshot_mode}")

            context = {
                'html_email':        generate_html_report(final_results, screenshot_mode='all'),
                'html_full':         generate_html_report(final_results, screenshot_mode=screenshot_mode),
                'html_lite':         generate_html_report(final_results, screenshot_mode='none'),
                'markdown_full':     generate_markdown_report(final_results, compact=False),
                'markdown_lite':     generate_markdown_report(final_results, compact=True),
                'summary_html':      generate_summary_report(final_results, fmt='html'),
                'summary_markdown':  generate_summary_report(final_results, fmt='markdown'),
            }

            for key, content in context.items():
                byte_size = len(content.encode('utf-8'))
                logger.info(f"内容版本 {key}: {byte_size} bytes ({byte_size/1024:.1f} KB)")

            title = f"雨云签到: {success_count}成功 {already_checked_in_count}已签到 {len(accounts)-success_count-already_checked_in_count}失败"
            notification_manager.send_all(title, context)

    logger.info("任务完成，执行最终清理...")
    from rainyun.config import cleanup_zombie_processes
    cleanup_zombie_processes()

    return success_count > 0 or already_checked_in_count > 0


def scheduled_checkin():
    """定时任务包装器"""
    logger.info(f"定时任务触发 - {now_local().strftime('%Y-%m-%d %H:%M:%S')}")
    success = run_all_accounts()

    if success:
        logger.info("定时签到任务执行成功！")
    else:
        logger.error("定时签到任务执行失败！")

    logger.info("定时任务完成，查看下次执行安排...")
    time.sleep(1)

    schedule_time = os.getenv("SCHEDULE_TIME", "08:00")
    current_time = now_local()
    next_run = current_time.replace(
        hour=int(schedule_time.split(':')[0]),
        minute=int(schedule_time.split(':')[1]),
        second=0,
        microsecond=0
    )

    if next_run <= current_time:
        next_run += timedelta(days=1)

    logger.info(f"✅ 程序继续运行，下次执行时间: {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
    time_diff = next_run - current_time
    hours, remainder = divmod(time_diff.total_seconds(), 3600)
    minutes, _ = divmod(remainder, 60)
    logger.info(f"距离下次执行还有: {int(hours)}小时{int(minutes)}分钟")

    return success
