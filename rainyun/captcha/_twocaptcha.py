"""2captcha API 验证码方案。

TwoCaptchaProvider 将验证码图片提交到 2captcha 在线服务进行点选坐标识别。
当本地 CV 方案失败时作为备用方案使用。
"""

import base64
import logging
import os
import re
import time

from rainyun.captcha._archive import save_captcha_archive_bundle
from rainyun.captcha._cv_utils import make_safe_name
from rainyun.captcha._image_utils import download_image
from rainyun.config import import_selenium_modules

logger = logging.getLogger(__name__)


class TwoCaptchaProvider:
    """使用 2captcha API 破解腾讯点选验证码。"""

    API_BASE = "https://2captcha.com"

    def __init__(self, max_retries=5, global_timeout=300):
        self.api_key = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
        self.max_retries = int(os.getenv("TWOCAPTCHA_MAX_RETRIES", max_retries))
        self.global_timeout = int(os.getenv("TWOCAPTCHA_GLOBAL_TIMEOUT", global_timeout))

    def solve(self, driver, timeout, retry_stats, logger_adapter):
        """提交验证码到 2captcha 进行点选坐标识别。

        :return: None 表示通过，False 表示放弃
        """
        modules = import_selenium_modules()
        WebDriverWait = modules["WebDriverWait"]
        EC = modules["EC"]
        By = modules["By"]
        ActionChains = modules["ActionChains"]
        TimeoutException = modules["TimeoutException"]

        if retry_stats is None:
            retry_stats = {"count": 0}

        attempt_index = retry_stats["count"]

        if self.max_retries >= 0 and retry_stats["count"] >= self.max_retries:
            logger_adapter.warning(
                f"2captcha 已达到最大重试次数 {self.max_retries}，放弃"
            )
            return False

        _start_time = retry_stats.get("_twocaptcha_start_time")
        if _start_time is None:
            _start_time = time.time()
            retry_stats["_twocaptcha_start_time"] = _start_time
        elif self.global_timeout > 0 and (time.time() - _start_time) > self.global_timeout:
            logger_adapter.warning(
                f"2captcha 全局超时 ({time.time() - _start_time:.0f}s)，放弃"
            )
            return False

        try:
            wait = WebDriverWait(driver, min(timeout, 10))
            try:
                wait.until(EC.presence_of_element_located((By.ID, "slideBg")))
            except TimeoutException:
                logger_adapter.info("未检测到可处理验证码内容，跳过验证码处理")
                return

            wait = WebDriverWait(driver, timeout)
            self._download_captcha_img(driver, timeout, logger_adapter)

            import cv2

            raw_sprite = cv2.imread("temp/sprite.jpg")
            if raw_sprite is not None:
                w_raw = raw_sprite.shape[1]
                for i in range(3):
                    temp = raw_sprite[:, w_raw // 3 * i: w_raw // 3 * (i + 1)]
                    cv2.imwrite(f"temp/sprite_{i + 1}.jpg", temp)

            combined = self._build_combined_image(logger_adapter)
            combined_path = "temp/combined_captcha.jpg"
            cv2.imwrite(combined_path, combined)
            captcha_height = cv2.imread("temp/captcha.jpg").shape[0]

            click_coords = self._submit_to_2captcha(combined_path, logger_adapter, timeout)
            if not click_coords:
                logger_adapter.error("2captcha 未能返回有效坐标")
                retry_stats["count"] += 1
                time.sleep(3)
                save_captcha_archive_bundle(
                    logger_adapter, attempt_index, "retry", {"reason": "no_coords"}
                )
                return self.solve(driver, timeout, retry_stats, logger_adapter)

            captcha_coords = []
            for x, y in click_coords:
                if y < captcha_height:
                    captcha_coords.append((x, y))
                else:
                    logger_adapter.debug(f"忽略在图案提示区域的点击: ({x}, {y})")

            if len(captcha_coords) < 3:
                logger_adapter.error(
                    f"有效点击坐标不足3个 (仅 {len(captcha_coords)} 个)，刷新重试"
                )
                retry_stats["count"] += 1
                time.sleep(3)
                save_captcha_archive_bundle(
                    logger_adapter, attempt_index, "retry",
                    {"reason": "insufficient_coords", "n": len(captcha_coords)},
                )
                return self.solve(driver, timeout, retry_stats, logger_adapter)

            final_coords = captcha_coords[:3]

            slideBg = wait.until(
                EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]'))
            )
            style = slideBg.get_attribute("style")
            captcha_img = cv2.imread("temp/captcha.jpg")
            width_raw, height_raw = captcha_img.shape[1], captcha_img.shape[0]

            width = float(re.search(r"width:\s*([\d.]+)px", style).group(1))
            height = float(re.search(r"height:\s*([\d.]+)px", style).group(1))
            x_offset, y_offset = float(-width / 2), float(-height / 2)

            for x, y in final_coords:
                final_x = int(x_offset + x / width_raw * width)
                final_y = int(y_offset + y / height_raw * height)
                ActionChains(driver).move_to_element_with_offset(
                    slideBg, final_x, final_y
                ).click().perform()
                time.sleep(0.3)

            confirm = wait.until(
                EC.element_to_be_clickable(
                    (By.XPATH, '//*[@id="tcStatus"]/div[2]/div[2]/div/div')
                )
            )
            logger_adapter.info("提交验证码")
            time.sleep(0.5)
            confirm.click()
            time.sleep(3)

            result_elem = wait.until(
                EC.visibility_of_element_located((By.XPATH, '//*[@id="tcOperation"]'))
            )
            if result_elem.get_attribute("class") == "tc-opera pointer show-success":
                logger_adapter.info("验证码通过 🎉")
                save_captcha_archive_bundle(logger_adapter, attempt_index, "pass", {
                    "click_coords": final_coords,
                })
                return
            else:
                logger_adapter.error("2captcha 验证码提交后未通过")
                retry_stats["count"] += 1
                time.sleep(3)
                save_captcha_archive_bundle(
                    logger_adapter, attempt_index, "retry", {"reason": "submit_failed"}
                )
                return self.solve(driver, timeout, retry_stats, logger_adapter)

        except TimeoutException:
            logger_adapter.error("获取验证码元素超时")
            save_captcha_archive_bundle(
                logger_adapter, attempt_index, "error", {"reason": "timeout"}
            )
        except Exception as e:
            logger_adapter.error(f"2captcha 执行流程中发生错误: {e}")
            import traceback

            logger_adapter.debug(traceback.format_exc())
            retry_stats["count"] += 1
            save_captcha_archive_bundle(
                logger_adapter, attempt_index, "retry",
                {"reason": "exception", "error": str(e)[:200]},
            )
            try:
                reload_btn = driver.find_element(By.XPATH, '//*[@id="reload"]')
                reload_btn.click()
                time.sleep(3)
                return self.solve(driver, timeout, retry_stats, logger_adapter)
            except Exception:
                pass
        finally:
            logger_adapter.debug("2captcha 单次处理周期完毕")

    # ==========================================
    # 图片下载（URL 优先 + 元素截图兜底）
    # ==========================================

    def _download_captcha_img(self, driver, timeout, logger_adapter):
        """下载 captcha 大图 + sprite 提示条。

        优先从 URL 下载，失败时回退到 Selenium 元素截图。
        """
        modules = import_selenium_modules()
        WebDriverWait = modules["WebDriverWait"]
        EC = modules["EC"]
        By = modules["By"]

        wait = WebDriverWait(driver, timeout)
        if os.path.exists("temp"):
            for filename in os.listdir("temp"):
                file_path = os.path.join("temp", filename)
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.remove(file_path)

        try:
            current_ua = driver.execute_script("return navigator.userAgent;")
            logger_adapter.debug(f"下载图片使用 UA: {current_ua[:50]}...")
        except Exception:
            current_ua = None

        url_ok = True
        try:
            slideBg = wait.until(
                EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]'))
            )
            img1_style = slideBg.get_attribute("style")
            img1_url = re.search(r'url\(["\']?(.*?)["\']?\)', img1_style).group(1)
            logger_adapter.info("开始下载验证码图片(1): " + img1_url)
            download_image(img1_url, "captcha.jpg", user_agent=current_ua)

            sprite = wait.until(
                EC.visibility_of_element_located(
                    (By.XPATH, '//*[@id="instruction"]/div/img')
                )
            )
            img2_url = sprite.get_attribute("src")
            logger_adapter.info("开始下载验证码图片(2): " + img2_url)
            download_image(img2_url, "sprite.jpg", user_agent=current_ua)
        except Exception as e:
            logger_adapter.warning(f"URL 下载验证码图片失败(改用元素截图兜底): {e}")
            url_ok = False

        if not url_ok or not self._img_file_valid("temp/captcha.jpg") or not self._img_file_valid("temp/sprite.jpg"):
            logger_adapter.info("验证码图片不可用，使用元素截图兜底")
            self._screenshot_captcha_elements(driver, logger_adapter)

    @staticmethod
    def _img_file_valid(path, min_size=1000):
        """检查图片文件是否有效（存在且大于 min_size 字节）。"""
        try:
            return os.path.isfile(path) and os.path.getsize(path) >= min_size
        except Exception:
            return False

    def _screenshot_captcha_elements(self, driver, logger_adapter):
        """使用 Selenium 元素截图作为兜底方案。"""
        modules = import_selenium_modules()
        WebDriverWait = modules["WebDriverWait"]
        EC = modules["EC"]
        By = modules["By"]

        try:
            slide_bg = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="slideBg"]'))
            )
            slide_bg.screenshot("temp/captcha.jpg")
            logger_adapter.info("元素截图: slideBg -> temp/captcha.jpg")
        except Exception as e:
            logger_adapter.warning(f"slideBg 截图失败: {e}")

        try:
            sprite = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located(
                    (By.XPATH, '//*[@id="instruction"]/div/img')
                )
            )
            sprite.screenshot("temp/sprite.jpg")
            logger_adapter.info("元素截图: instruction img -> temp/sprite.jpg")
        except Exception as e:
            logger_adapter.warning(f"instruction img 截图失败: {e}")

    # ==========================================
    # 2captcha API 交互
    # ==========================================

    @staticmethod
    def _build_combined_image(logger_adapter):
        """将 captcha 大图和 3 个 sprite 切片拼接为一张组合图提交给 2captcha。"""
        import cv2
        import numpy as np

        captcha = cv2.imread("temp/captcha.jpg")
        if captcha is None:
            logger_adapter.error("无法读取 captcha.jpg")
            raise FileNotFoundError("captcha.jpg not found")

        sprite_parts = []
        for i in range(3):
            sprite = cv2.imread(f"temp/sprite_{i + 1}.jpg")
            if sprite is not None:
                target_h = int(captcha.shape[1] * 0.08)
                scale = target_h / sprite.shape[0]
                new_w = int(sprite.shape[1] * scale)
                sprite = cv2.resize(sprite, (new_w, target_h), interpolation=cv2.INTER_AREA)
                sprite_parts.append(sprite)

        if not sprite_parts:
            logger_adapter.warning("未能读取sprite图片，直接使用captcha.jpg")
            return captcha

        sprite_strip = np.hstack(sprite_parts)

        line_h = 4
        line = np.full((line_h, captcha.shape[1], 3), (200, 200, 200), dtype=np.uint8)

        sprite_canvas = np.full(
            (sprite_strip.shape[0], captcha.shape[1], 3), 255, dtype=np.uint8,
        )
        x_offset = (captcha.shape[1] - sprite_strip.shape[1]) // 2
        sprite_canvas[:, x_offset:x_offset + sprite_strip.shape[1]] = sprite_strip

        combined = np.vstack([captcha, line, sprite_canvas])
        logger_adapter.debug(f"组合图片尺寸: {combined.shape[1]}x{combined.shape[0]}")
        return combined

    def _submit_to_2captcha(self, image_path, logger_adapter, timeout):
        """提交图片到 2captcha 并轮询获取点选坐标。

        :return: list of (x, y) tuples 或 None
        """
        import requests

        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode("utf-8")

        api_key = self.api_key
        if not api_key:
            logger_adapter.error("TWOCAPTCHA_API_KEY 未配置")
            return None

        submit_payload = {
            "key": api_key,
            "method": "base64",
            "body": img_b64,
            "coordinatescaptcha": 1,
            "lang": "en",
        }
        try:
            logger_adapter.info("正在提交验证码到 2captcha...")
            resp = requests.post(
                f"{self.API_BASE}/in.php", data=submit_payload, timeout=30,
            )
            result = resp.text.strip()
            if not result.startswith("OK|"):
                logger_adapter.error(f"2captcha 提交失败: {result}")
                return None

            captcha_id = result[3:]
            logger_adapter.info(f"2captcha 任务已提交, ID: {captcha_id}")
        except requests.RequestException as e:
            logger_adapter.error(f"2captcha 提交请求失败: {e}")
            return None

        poll_params = {"key": api_key, "action": "get", "id": captcha_id}
        max_wait = min(timeout + 30, 150)
        start_time = time.time()
        poll_interval = 5

        while time.time() - start_time < max_wait:
            try:
                resp = requests.get(
                    f"{self.API_BASE}/res.php", params=poll_params, timeout=10,
                )
                text = resp.text.strip()
                if text == "CAPCHA_NOT_READY":
                    time.sleep(poll_interval)
                    continue
                if text.startswith("OK|"):
                    coords_str = text[3:]
                    logger_adapter.info(f"2captcha 返回坐标: {coords_str}")
                    return self._parse_coordinates(coords_str)
                else:
                    logger_adapter.error(f"2captcha 返回错误: {text}")
                    return None
            except requests.RequestException as e:
                logger_adapter.error(f"2captcha 轮询请求失败: {e}")
                time.sleep(poll_interval)
                continue

        logger_adapter.error("2captcha 验证超时")
        return None

    @staticmethod
    def _parse_coordinates(coords_str):
        """解析 2captcha 返回的坐标字符串。

        支持格式：
        - "x=100,y=200;x=150,y=250;..."
        - "coordinates:100,200;150,250;..."
        - "100,200;150,250;..."
        """
        if coords_str.startswith("coordinates:"):
            coords_str = coords_str[len("coordinates:"):]

        coords = []
        for part in coords_str.split(";"):
            part = part.strip()
            if not part:
                continue
            try:
                if part.startswith("x=") or part.startswith("X="):
                    kv = part.replace("X=", "x=").replace("Y=", "y=").split(",")
                    x = int(kv[0].split("=")[1].strip())
                    y = int(kv[1].split("=")[1].strip())
                else:
                    xy = part.split(",")
                    x = int(xy[0].strip())
                    y = int(xy[1].strip())
                coords.append((x, y))
            except (ValueError, IndexError) as e:
                log = logging.getLogger(__name__)
                log.debug(f"解析坐标失败: {part} - {e}")
                continue
        return coords
