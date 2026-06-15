import logging
import os
import threading
import time

from rainyun.config import import_selenium_modules

logger = logging.getLogger(__name__)


# ==========================================
# OCR 全局单例
# ==========================================
_ocr_model = None
_det_model = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def get_shared_ocr_models():
    global _ocr_model, _det_model
    if _ocr_model is None or _det_model is None:
        with _model_lock:
            if _ocr_model is None or _det_model is None:
                import ddddocr
                logger.info("正在加载OCR模型...")
                _ocr_model = ddddocr.DdddOcr(ocr=True, show_ad=False)
                _det_model = ddddocr.DdddOcr(det=True, show_ad=False)
    return _ocr_model, _det_model


# ==========================================
# 图片下载 + 样式解析工具函数
# ==========================================
def download_image(url, filename, user_agent=None):
    import requests

    os.makedirs("temp", exist_ok=True)

    headers = {}
    if user_agent:
        headers['User-Agent'] = user_agent

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            path = os.path.join("temp", filename)
            with open(path, "wb") as f:
                f.write(response.content)
            return True
        else:
            logger.error(f"下载图片失败！状态码: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"下载图片异常: {e}")
        return False


def get_url_from_style(style):
    import re
    return re.search(r'url\(["\']?(.*?)["\']?\)', style).group(1)


def get_width_from_style(style):
    import re
    return re.search(r'width:\s*([\d.]+)px', style).group(1)


def get_height_from_style(style):
    import re
    return re.search(r'height:\s*([\d.]+)px', style).group(1)


# ==========================================
# 验证码提供者基类
# ==========================================
class CaptchaProvider:
    """验证码提供者基类"""
    def solve(self, driver, timeout, retry_stats, logger_adapter):
        raise NotImplementedError


class TencentCaptchaProvider(CaptchaProvider):
    """腾讯滑块验证码处理"""

    def __init__(self, max_retries=-1):
        self.max_retries = max_retries

    def solve(self, driver, timeout, retry_stats, logger_adapter):
        modules = import_selenium_modules()
        WebDriverWait = modules['WebDriverWait']
        EC = modules['EC']
        By = modules['By']
        ActionChains = modules['ActionChains']
        TimeoutException = modules['TimeoutException']

        if retry_stats is None:
            retry_stats = {'count': 0}

        if self.max_retries >= 0 and retry_stats['count'] >= self.max_retries:
            logger_adapter.warning(
                f"本地方案已达到最大重试次数 {self.max_retries}，放弃并切换到备用方案"
            )
            return False

        try:
            wait = WebDriverWait(driver, min(timeout, 10))
            try:
                wait.until(EC.presence_of_element_located((By.ID, "slideBg")))
            except TimeoutException:
                logger_adapter.info("未检测到可处理验证码内容，跳过验证码处理")
                return

            import cv2

            ocr, det = get_shared_ocr_models()

            wait = WebDriverWait(driver, timeout)
            self._download_captcha_img(driver, timeout, logger_adapter)

            logger_adapter.info("开始处理验证码图片并识别")

            import cv2
            import numpy as np
            raw_sprite = cv2.imread("temp/sprite.jpg")
            if raw_sprite is not None:
                w_raw = raw_sprite.shape[1]
                for i in range(3):
                    temp = raw_sprite[:, w_raw // 3 * i: w_raw // 3 * (i + 1)]
                    cv2.imwrite(f"temp/sprite_{i + 1}.jpg", temp)

            captcha = cv2.imread("temp/captcha.jpg")
            with open("temp/captcha.jpg", 'rb') as f:
                captcha_b = f.read()

            with _inference_lock:
                bboxes = det.detection(captcha_b)

            spec_infos = []
            for i in range(len(bboxes)):
                x1, y1, x2, y2 = bboxes[i]
                spec = captcha[y1:y2, x1:x2]
                if not self._is_meaningful_candidate_crop(spec):
                    logger_adapter.info(f"候选框 {i + 1} 前景过弱，判定为空白/噪声，跳过")
                    continue
                spec_path = f"temp/spec_{i + 1}.jpg"
                cv2.imwrite(spec_path, spec)
                pos = f"{int((x1 + x2) / 2)},{int((y1 + y2) / 2)}"
                spec_infos.append({
                    "path": spec_path,
                    "pos": pos,
                    "index": i,
                    "bbox": (x1, y1, x2, y2),
                })

            best_assignment = None
            best_total_score = -1.0
            sprite_profiles = []

            if len(spec_infos) >= 3:
                import itertools
                score_matrix = []
                for j in range(3):
                    sprite_path = f"temp/sprite_{j + 1}.jpg"
                    sprite_profile = self._build_sprite_profile(sprite_path, ocr)
                    sprite_profiles.append(sprite_profile)
                    sprite_scores = []
                    for k, spec in enumerate(spec_infos):
                        score, is_semantic = self._compute_score(
                            sprite_path,
                            spec["path"],
                            ocr,
                            sprite_profile=sprite_profile,
                        )
                        sprite_scores.append(score)
                        logger_adapter.debug(f"目标 {j + 1} -> 候选 {k + 1}: 得分 {score:.2f} (语义匹配: {is_semantic})")
                    score_matrix.append(sprite_scores)

                all_spec_indices = list(range(len(spec_infos)))
                for perm in itertools.permutations(all_spec_indices, 3):
                    total_score = score_matrix[0][perm[0]] + score_matrix[1][perm[1]] + score_matrix[2][perm[2]]
                    if total_score > best_total_score:
                        best_total_score = total_score
                        best_assignment = perm

            MIN_ACCEPTABLE_TOTAL_SCORE = 2.0
            final_click_positions = []
            use_fallback = False
            assigned_scores = []

            if best_assignment is not None and best_total_score >= MIN_ACCEPTABLE_TOTAL_SCORE:
                assigned_scores = [score_matrix[j][best_assignment[j]] for j in range(3)]
                min_assigned_score = min(assigned_scores)
                glyph_low_confidence = False
                if sprite_profiles:
                    for j, score in enumerate(assigned_scores):
                        profile = sprite_profiles[j] if j < len(sprite_profiles) else None
                        if profile and profile.get("is_glyph") and score < 4.0:
                            glyph_low_confidence = True
                            logger_adapter.warning(
                                f"图案 {j + 1} 被识别为字形，但局部候选最高分仅 {score:.2f}，"
                                "怀疑正确字符未被候选框截到，降级使用全图搜索..."
                            )
                            break

                if min_assigned_score <= 0 or glyph_low_confidence:
                    logger_adapter.warning(
                        f"一阶段存在低可信目标（最低单项得分 {min_assigned_score:.2f}），"
                        "放弃直接提交，降级使用全图边缘模板匹配..."
                    )
                    use_fallback = True
                else:
                    logger_adapter.info(f"成功找到全局最优组合，验证码一阶段置信分: {best_total_score:.2f}")
                    for j in range(3):
                        sprite_path = f"temp/sprite_{j + 1}.jpg"
                        spec_idx = best_assignment[j]
                        spec_info = spec_infos[spec_idx]
                        positon = spec_info["pos"]
                        score = score_matrix[j][spec_idx]
                        profile = sprite_profiles[j] if j < len(sprite_profiles) else None
                        if profile and profile.get("is_glyph"):
                            logger_adapter.info(
                                f"--> 图案 {j + 1} 选择候选框 {spec_idx + 1} 位于 ({positon})，"
                                f"单项得分：{score:.2f}，字形目标使用候选框中心，跳过局部精修"
                            )
                        else:
                            refined_pos, refined_score = self._find_sprite_by_template(
                                sprite_path,
                                "temp/captcha.jpg",
                                search_box=spec_info["bbox"],
                                padding=12,
                                target_profile=profile,
                            )
                            if refined_pos:
                                positon = refined_pos
                                logger_adapter.info(
                                    f"--> 图案 {j + 1} 选择候选框 {spec_idx + 1}，候选框中心 ({spec_info['pos']}) -> "
                                    f"局部精修坐标 ({positon})，单项得分：{score:.2f}，精修边缘分：{refined_score:.2f}"
                                )
                            else:
                                logger_adapter.info(
                                    f"--> 图案 {j + 1} 选择候选框 {spec_idx + 1} 位于 ({positon})，"
                                    f"单项得分：{score:.2f}，局部精修失败，回退候选框中心"
                                )
                        final_click_positions.append(positon)
            else:
                score_info = f"{best_total_score:.2f}" if best_assignment is not None else "候选框不足3个"
                logger_adapter.warning(f"局部目标检测不佳（得分 {score_info} < {MIN_ACCEPTABLE_TOTAL_SCORE}），降级使用全图边缘模板匹配...")
                use_fallback = True

            if use_fallback:
                fallback_candidates = []
                for j in range(3):
                    sprite_path = f"temp/sprite_{j + 1}.jpg"
                    candidates = self._find_template_candidates(
                        sprite_path,
                        "temp/captcha.jpg",
                        top_k=5,
                        min_distance=24,
                        target_profile=sprite_profiles[j] if j < len(sprite_profiles) else None,
                    )
                    fallback_candidates.append(candidates)
                    if candidates:
                        top_candidate = candidates[0]
                        logger_adapter.info(
                            f"--> [全图匹配] 图案 {j + 1} 首选坐标 ({top_candidate['pos']})，"
                            f"候选数：{len(candidates)}，边缘响应分：{top_candidate['score']:.2f}"
                        )
                    else:
                        logger_adapter.info(f"--> [全图匹配] 图案 {j + 1} 未找到候选坐标")

                selected_candidates, fallback_total_score = self._select_best_candidate_combo(
                    fallback_candidates,
                    min_distance=24,
                )
                final_click_positions = [candidate["pos"] for candidate in selected_candidates]

                MIN_FALLBACK_TOTAL_SCORE = 0.75
                if fallback_total_score < MIN_FALLBACK_TOTAL_SCORE or len(final_click_positions) < 3:
                    logger_adapter.error(
                        f"全图匹配响应度过低 ({fallback_total_score:.2f} < {MIN_FALLBACK_TOTAL_SCORE:.2f})，放弃提交并刷新"
                    )
                    self._save_captcha_debug_bundle(
                        logger_adapter,
                        stage="fallback_low_score",
                        retry_count=retry_stats['count'],
                        extra={
                            "fallback_total_score": fallback_total_score,
                            "click_positions": final_click_positions,
                        },
                    )
                    final_click_positions = []

            if len(final_click_positions) == 3:
                for positon in final_click_positions:
                    slideBg = wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]')))
                    style = slideBg.get_attribute("style")
                    x, y = int(positon.split(",")[0]), int(positon.split(",")[1])
                    width_raw, height_raw = captcha.shape[1], captcha.shape[0]
                    width, height = float(get_width_from_style(style)), float(get_height_from_style(style))
                    x_offset, y_offset = float(-width / 2), float(-height / 2)
                    final_x, final_y = int(x_offset + x / width_raw * width), int(y_offset + y / height_raw * height)
                    ActionChains(driver).move_to_element_with_offset(slideBg, final_x, final_y).click().perform()
                    time.sleep(0.3)

                confirm = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="tcStatus"]/div[2]/div[2]/div/div')))
                logger_adapter.info("提交验证码")
                time.sleep(0.5)
                confirm.click()
                time.sleep(3)

                result_elem = wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="tcOperation"]')))
                if result_elem.get_attribute("class") == 'tc-opera pointer show-success':
                    logger_adapter.info("验证码通过 🎉")
                    return
                else:
                    logger_adapter.error(f"验证码提交后未通过，匹配坐标可能存在偏移。")
                    self._save_captcha_debug_bundle(
                        logger_adapter,
                        stage="submit_failed",
                        retry_count=retry_stats['count'],
                        extra={
                            "click_positions": final_click_positions,
                            "used_fallback": use_fallback,
                            "best_total_score": best_total_score,
                        },
                    )
                    retry_stats['count'] += 1
            else:
                retry_stats['count'] += 1

            reload_btn = wait.until(EC.element_to_be_clickable((By.XPATH, '//*[@id="reload"]')))
            time.sleep(1)
            reload_btn.click()
            time.sleep(3)
            logger_adapter.info(f"重新发起验证码挑战 (当前重试: {retry_stats['count']})")
            return self.solve(driver, timeout, retry_stats, logger_adapter)

        except TimeoutException:
            logger_adapter.error("获取验证码图片等元素超时")
        except Exception as e:
            logger_adapter.error(f"验证码执行流程中发生未知错误: {e}")
            import traceback
            logger_adapter.debug(traceback.format_exc())
            retry_stats['count'] += 1
            try:
                reload_btn = driver.find_element(By.XPATH, '//*[@id="reload"]')
                reload_btn.click()
                time.sleep(3)
                return self.solve(driver, timeout, retry_stats, logger_adapter)
            except:
                pass
        finally:
            logger_adapter.debug("验证码单次处理周期完毕")

    def _download_captcha_img(self, driver, timeout, logger_adapter):
        modules = import_selenium_modules()
        WebDriverWait = modules['WebDriverWait']
        EC = modules['EC']
        By = modules['By']

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

        slideBg = wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]')))
        img1_style = slideBg.get_attribute("style")
        img1_url = get_url_from_style(img1_style)
        logger_adapter.info("开始下载验证码图片(1): " + img1_url)
        download_image(img1_url, "captcha.jpg", user_agent=current_ua)

        sprite = wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="instruction"]/div/img')))
        img2_url = sprite.get_attribute("src")
        logger_adapter.info("开始下载验证码图片(2): " + img2_url)
        download_image(img2_url, "sprite.jpg", user_agent=current_ua)

    @staticmethod
    def _distance(point_a, point_b):
        import math
        return math.dist(point_a, point_b)

    @staticmethod
    def _compute_binary_shape_score_images(sprite_img, spec_img):
        import cv2
        import numpy as np

        if sprite_img is None or spec_img is None:
            return 0.0

        if len(sprite_img.shape) == 3:
            sprite_img = cv2.cvtColor(sprite_img, cv2.COLOR_BGR2GRAY)
        if len(spec_img.shape) == 3:
            spec_img = cv2.cvtColor(spec_img, cv2.COLOR_BGR2GRAY)

        def normalize_mask(img):
            blurred = cv2.GaussianBlur(img, (3, 3), 0)
            _, binary = cv2.threshold(
                blurred,
                0,
                255,
                cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
            )
            coords = cv2.findNonZero(binary)
            if coords is None:
                return None

            x, y, w, h = cv2.boundingRect(coords)
            crop = binary[y:y + h, x:x + w]
            if crop.size == 0:
                return None

            canvas_size = 64
            usable_size = canvas_size - 8
            scale = min(usable_size / max(w, 1), usable_size / max(h, 1))
            resized_w = max(1, int(round(w * scale)))
            resized_h = max(1, int(round(h * scale)))
            resized = cv2.resize(crop, (resized_w, resized_h), interpolation=cv2.INTER_AREA)

            canvas = np.zeros((canvas_size, canvas_size), dtype=np.uint8)
            offset_x = (canvas_size - resized_w) // 2
            offset_y = (canvas_size - resized_h) // 2
            canvas[offset_y:offset_y + resized_h, offset_x:offset_x + resized_w] = resized
            return canvas

        sprite_mask = normalize_mask(sprite_img)
        spec_mask = normalize_mask(spec_img)
        if sprite_mask is None or spec_mask is None:
            return 0.0

        intersection = np.logical_and(sprite_mask > 0, spec_mask > 0).sum()
        union = np.logical_or(sprite_mask > 0, spec_mask > 0).sum()
        iou_score = intersection / union if union else 0.0

        contours_1, _ = cv2.findContours(sprite_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours_2, _ = cv2.findContours(spec_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contour_score = 0.0
        if contours_1 and contours_2:
            c1 = max(contours_1, key=cv2.contourArea)
            c2 = max(contours_2, key=cv2.contourArea)
            try:
                shape_distance = cv2.matchShapes(c1, c2, cv2.CONTOURS_MATCH_I1, 0.0)
                contour_score = 1.0 / (1.0 + shape_distance * 8.0)
            except Exception:
                contour_score = 0.0

        return max(iou_score, contour_score, (iou_score + contour_score) / 2.0)

    def _compute_binary_shape_score(self, sprite_path, spec_path):
        import cv2
        sprite_img = cv2.imread(sprite_path, cv2.IMREAD_GRAYSCALE)
        spec_img = cv2.imread(spec_path, cv2.IMREAD_GRAYSCALE)
        return self._compute_binary_shape_score_images(sprite_img, spec_img)

    @staticmethod
    def _measure_foreground_shape(image):
        import cv2
        import numpy as np

        if image is None:
            return {
                "has_foreground": False,
                "bbox": (0, 0),
                "bbox_area": 0,
                "holes": 0,
                "dark_ratio": 0.0,
                "edge_ratio": 0.0,
                "std": 0.0,
            }

        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()

        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        _, binary = cv2.threshold(
            blurred,
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )
        coords = cv2.findNonZero(binary)
        if coords is None:
            return {
                "has_foreground": False,
                "bbox": (0, 0),
                "bbox_area": 0,
                "holes": 0,
                "dark_ratio": float((gray < 180).sum() / gray.size) if gray.size else 0.0,
                "edge_ratio": 0.0,
                "std": float(gray.std()) if gray.size else 0.0,
            }

        x, y, w, h = cv2.boundingRect(coords)
        bbox_area = w * h
        contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        holes = 0
        if hierarchy is not None:
            for contour_hierarchy in hierarchy[0]:
                if contour_hierarchy[3] != -1:
                    holes += 1

        edges = cv2.Canny(gray, 50, 150)
        return {
            "has_foreground": True,
            "bbox": (w, h),
            "bbox_area": bbox_area,
            "holes": holes,
            "dark_ratio": float((gray < 180).sum() / gray.size) if gray.size else 0.0,
            "edge_ratio": float((edges > 0).sum() / edges.size) if edges.size else 0.0,
            "std": float(gray.std()) if gray.size else 0.0,
        }

    def _is_meaningful_candidate_crop(self, image):
        metrics = self._measure_foreground_shape(image)
        if not metrics["has_foreground"]:
            return False

        if metrics["edge_ratio"] < 0.02 and metrics["std"] < 10 and metrics["dark_ratio"] < 0.02:
            return False

        return True

    @staticmethod
    def _normalize_ocr_char(text):
        text = text.strip() if text else ""
        if len(text) != 1:
            return ""

        ch = text[0]
        if ch.isdigit() or ('A' <= ch <= 'Z') or ('a' <= ch <= 'z'):
            return ch
        if '一' <= ch <= '鿿':
            return ch
        return ""

    def _classify_glyph_char(self, image, ocr):
        import cv2
        import numpy as np

        if image is None:
            return "", {}

        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()

        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        _, binary = cv2.threshold(
            blurred,
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )
        coords = cv2.findNonZero(binary)
        if coords is not None:
            x, y, w, h = cv2.boundingRect(coords)
            padding = 2
            x = max(0, x - padding)
            y = max(0, y - padding)
            w = min(gray.shape[1] - x, w + padding * 2)
            h = min(gray.shape[0] - y, h + padding * 2)
            gray = gray[y:y + h, x:x + w]
            binary = binary[y:y + h, x:x + w]

        variants = {
            "orig": gray,
            "th": binary,
            "inv": 255 - binary,
            "th_up2": cv2.resize(binary, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST),
            "inv_up2": cv2.resize(255 - binary, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST),
        }

        variant_texts = {}
        try:
            with _inference_lock:
                for name, variant in variants.items():
                    success, encoded = cv2.imencode('.png', variant)
                    if not success:
                        variant_texts[name] = ""
                        continue
                    variant_texts[name] = (ocr.classification(encoded.tobytes()) or "").strip()
        except Exception:
            return "", {}

        orig_char = self._normalize_ocr_char(variant_texts.get("orig"))
        th_char = self._normalize_ocr_char(variant_texts.get("th"))
        inv_char = self._normalize_ocr_char(variant_texts.get("inv"))
        th_up_char = self._normalize_ocr_char(variant_texts.get("th_up2"))
        inv_up_char = self._normalize_ocr_char(variant_texts.get("inv_up2"))

        if th_char and th_char == inv_char and th_char == th_up_char:
            return th_char, variant_texts
        if th_char and th_char == inv_char and th_char == inv_up_char:
            return th_char, variant_texts
        if orig_char and th_char and orig_char == th_char:
            return orig_char, variant_texts
        if orig_char and inv_char and orig_char == inv_char:
            return orig_char, variant_texts

        return "", variant_texts

    @staticmethod
    def _is_likely_glyph_text(text):
        return bool(TencentCaptchaProvider._normalize_ocr_char(text))

    def _build_sprite_profile(self, sprite_path, ocr):
        import cv2

        sprite_text = ""
        raw_texts = {}
        foreground_metrics = {}
        try:
            sprite_img = cv2.imread(sprite_path)
            foreground_metrics = self._measure_foreground_shape(sprite_img)
            sprite_text, raw_texts = self._classify_glyph_char(sprite_img, ocr)
        except Exception:
            sprite_text = ""
            raw_texts = {}
            foreground_metrics = {}

        bbox_w, bbox_h = foreground_metrics.get("bbox", (0, 0))
        bbox_area = foreground_metrics.get("bbox_area", 0)
        holes = foreground_metrics.get("holes", 0)
        size_likely_glyph = (
            bbox_w > 0
            and bbox_h > 0
            and bbox_w <= 36
            and bbox_h <= 40
            and bbox_area <= 1400
            and holes <= 2
        )
        return {
            "ocr_text": sprite_text,
            "is_glyph": size_likely_glyph,
            "raw_ocr": raw_texts,
            "foreground": foreground_metrics,
        }

    @staticmethod
    def _compute_glyph_structure_factor(sprite_metrics, spec_metrics):
        sprite_w, sprite_h = sprite_metrics.get("bbox", (0, 0)) if sprite_metrics else (0, 0)
        spec_w, spec_h = spec_metrics.get("bbox", (0, 0)) if spec_metrics else (0, 0)
        if sprite_w <= 0 or sprite_h <= 0 or spec_w <= 0 or spec_h <= 0:
            return 1.0

        sprite_aspect = sprite_w / max(sprite_h, 1)
        spec_aspect = spec_w / max(spec_h, 1)
        aspect_similarity = min(sprite_aspect, spec_aspect) / max(sprite_aspect, spec_aspect)

        hole_gap = abs((sprite_metrics or {}).get("holes", 0) - (spec_metrics or {}).get("holes", 0))
        if hole_gap == 0:
            hole_factor = 1.0
        elif hole_gap == 1:
            hole_factor = 0.72
        elif hole_gap == 2:
            hole_factor = 0.45
        else:
            hole_factor = 0.22

        return max(0.22, hole_factor * (0.7 + 0.3 * aspect_similarity))

    @staticmethod
    def _extract_binary_mask(image, crop_foreground=False, padding=2):
        import cv2
        import numpy as np

        if image is None:
            return None

        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        _, binary = cv2.threshold(
            blurred,
            0,
            255,
            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
        )

        if crop_foreground:
            coords = cv2.findNonZero(binary)
            if coords is None:
                return None
            x, y, w, h = cv2.boundingRect(coords)
            x = max(0, x - padding)
            y = max(0, y - padding)
            w = min(binary.shape[1] - x, w + padding * 2)
            h = min(binary.shape[0] - y, h + padding * 2)
            binary = binary[y:y + h, x:x + w]

        return binary if binary.size > 0 else None

    @staticmethod
    def _make_safe_name(raw_name):
        import re
        safe_name = re.sub(r'[^0-9A-Za-z._-]+', '_', raw_name or "unknown")
        return safe_name.strip("._") or "unknown"

    def _save_captcha_debug_bundle(self, logger_adapter, stage, retry_count, extra=None):
        import json
        import shutil
        from datetime import datetime

        from rainyun.config import now_local

        account_prefix = self._make_safe_name(getattr(logger_adapter, "extra", {}).get("prefix", "unknown"))
        bundle_name = f"{now_local().strftime('%Y%m%d_%H%M%S_%f')[:-3]}_{stage}_r{retry_count}"
        bundle_dir = os.path.join("logs", "captcha_debug", account_prefix, bundle_name)
        os.makedirs(bundle_dir, exist_ok=True)

        temp_dir = "temp"
        copied_files = []
        if os.path.isdir(temp_dir):
            for filename in sorted(os.listdir(temp_dir)):
                if not (
                    filename in {"captcha.jpg", "sprite.jpg"}
                    or filename.startswith("sprite_")
                    or filename.startswith("spec_")
                ):
                    continue
                source_path = os.path.join(temp_dir, filename)
                if not os.path.isfile(source_path):
                    continue
                shutil.copy2(source_path, os.path.join(bundle_dir, filename))
                copied_files.append(filename)

        metadata = {
            "stage": stage,
            "retry_count": retry_count,
            "account_prefix": getattr(logger_adapter, "extra", {}).get("prefix", "unknown"),
            "captured_at": now_local().isoformat(timespec="seconds"),
            "copied_files": copied_files,
            "extra": extra or {},
        }
        metadata_path = os.path.join(bundle_dir, "metadata.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        logger_adapter.info(f"已保存验证码调试样本到 {bundle_dir}")

    def _dedupe_candidates(self, candidates, min_distance=24, top_k=5):
        deduped_candidates = []
        for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
            if any(
                self._distance(candidate["coords"], existing["coords"]) < min_distance
                for existing in deduped_candidates
            ):
                continue
            deduped_candidates.append(candidate)
            if len(deduped_candidates) >= top_k:
                break
        return deduped_candidates

    def _find_glyph_candidates(self, sprite_path, captcha_path, search_box=None, top_k=5, min_distance=24, padding=0):
        import cv2
        import numpy as np

        sprite_img = cv2.imread(sprite_path)
        captcha_img = cv2.imread(captcha_path)
        if sprite_img is None or captcha_img is None:
            return []

        sprite_mask = self._extract_binary_mask(sprite_img, crop_foreground=True, padding=2)
        if sprite_mask is None:
            return []

        origin_x, origin_y = 0, 0
        if search_box is not None:
            x1, y1, x2, y2 = search_box
            x1 = max(0, x1 - padding)
            y1 = max(0, y1 - padding)
            x2 = min(captcha_img.shape[1], x2 + padding)
            y2 = min(captcha_img.shape[0], y2 + padding)
            captcha_view = captcha_img[y1:y2, x1:x2]
            origin_x, origin_y = x1, y1
        else:
            captcha_view = captcha_img

        captcha_mask = self._extract_binary_mask(captcha_view, crop_foreground=False, padding=0)
        if captcha_mask is None:
            return []

        if (
            captcha_mask.shape[0] < sprite_mask.shape[0]
            or captcha_mask.shape[1] < sprite_mask.shape[1]
        ):
            return []

        candidates = []
        h_s, w_s = sprite_mask.shape
        for angle in [-12, 0, 12]:
            if angle != 0:
                matrix = cv2.getRotationMatrix2D((w_s // 2, h_s // 2), angle, 1.0)
                rotated_mask = cv2.warpAffine(
                    sprite_mask,
                    matrix,
                    (w_s, h_s),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
            else:
                rotated_mask = sprite_mask

            if (
                captcha_mask.shape[0] < rotated_mask.shape[0]
                or captcha_mask.shape[1] < rotated_mask.shape[1]
            ):
                continue

            res = cv2.matchTemplate(captcha_mask, rotated_mask, cv2.TM_CCOEFF_NORMED)
            res_work = res.copy()
            for _ in range(top_k):
                _, max_val, _, max_loc = cv2.minMaxLoc(res_work)
                if max_val <= 0:
                    break

                center_x = origin_x + max_loc[0] + rotated_mask.shape[1] // 2
                center_y = origin_y + max_loc[1] + rotated_mask.shape[0] // 2
                candidates.append({
                    "pos": f"{center_x},{center_y}",
                    "coords": (center_x, center_y),
                    "score": float(max_val),
                    "angle": angle,
                })

                left = max(0, max_loc[0] - min_distance)
                top = max(0, max_loc[1] - min_distance)
                right = min(res_work.shape[1], max_loc[0] + rotated_mask.shape[1] + min_distance)
                bottom = min(res_work.shape[0], max_loc[1] + rotated_mask.shape[0] + min_distance)
                res_work[top:bottom, left:right] = -1.0

        return self._dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)

    def _find_component_candidates(self, sprite_path, captcha_path, search_box=None, top_k=5, min_distance=24, padding=0, target_profile=None):
        import cv2
        import numpy as np

        ocr, _ = get_shared_ocr_models()
        sprite_img = cv2.imread(sprite_path)
        captcha_img = cv2.imread(captcha_path)
        if sprite_img is None or captcha_img is None:
            return []

        gray_sprite = cv2.cvtColor(sprite_img, cv2.COLOR_BGR2GRAY)
        _, sprite_binary = cv2.threshold(gray_sprite, 240, 255, cv2.THRESH_BINARY_INV)
        sprite_coords = cv2.findNonZero(sprite_binary)
        if sprite_coords is not None:
            _, _, sprite_w, sprite_h = cv2.boundingRect(sprite_coords)
        else:
            sprite_h, sprite_w = sprite_img.shape[:2]

        sprite_foreground = (target_profile or {}).get("foreground", {})
        if target_profile and target_profile.get("is_glyph"):
            sprite_w, sprite_h = sprite_foreground.get("bbox", (sprite_w, sprite_h))
            bbox_area = max(1, sprite_foreground.get("bbox_area", sprite_w * sprite_h))
            min_bbox_area = max(180, int(bbox_area * 0.18))
            max_bbox_area = max(min_bbox_area + 1, int(bbox_area * 6.0))
            crop_padding = 4 if search_box is None else 2
            thresholds = [24, 32, 40, 48, 60, 72, 96]
        else:
            bbox_area = max(1, sprite_w * sprite_h)
            min_bbox_area = max(180, int(bbox_area * 0.2))
            max_bbox_area = max(min_bbox_area + 1, int(bbox_area * 6.0))
            crop_padding = 4 if search_box is None else 2
            thresholds = [96]

        origin_x, origin_y = 0, 0
        if search_box is not None:
            x1, y1, x2, y2 = search_box
            x1 = max(0, x1 - padding)
            y1 = max(0, y1 - padding)
            x2 = min(captcha_img.shape[1], x2 + padding)
            y2 = min(captcha_img.shape[0], y2 + padding)
            captcha_view = captcha_img[y1:y2, x1:x2]
            origin_x, origin_y = x1, y1
        else:
            captcha_view = captcha_img

        if captcha_view.size == 0:
            return []

        gray_view = cv2.cvtColor(captcha_view, cv2.COLOR_BGR2GRAY)

        candidates = []
        for threshold in thresholds:
            _, dark_mask = cv2.threshold(gray_view, threshold, 255, cv2.THRESH_BINARY_INV)
            dark_mask = cv2.medianBlur(dark_mask, 3)

            num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(dark_mask, 8)
            for i in range(1, num_labels):
                x, y, w, h, area = stats[i]
                current_bbox_area = w * h
                if area < 80 or w < 18 or h < 18:
                    continue
                if current_bbox_area < min_bbox_area or current_bbox_area > max_bbox_area:
                    continue

                left = max(0, x - crop_padding)
                top = max(0, y - crop_padding)
                right = min(captcha_view.shape[1], x + w + crop_padding)
                bottom = min(captcha_view.shape[0], y + h + crop_padding)
                component_crop = captcha_view[top:bottom, left:right]
                if component_crop.size == 0:
                    continue

                score, is_semantic = self._compute_score_from_images(
                    sprite_img,
                    component_crop,
                    ocr,
                    sprite_profile=target_profile,
                )
                if score <= 0:
                    continue

                component_metrics = self._measure_foreground_shape(component_crop)
                compare_w, compare_h = component_metrics.get("bbox", (w, h))
                compare_area = max(1, component_metrics.get("bbox_area", current_bbox_area))
                width_similarity = min(compare_w, sprite_w) / max(compare_w, sprite_w)
                height_similarity = min(compare_h, sprite_h) / max(compare_h, sprite_h)
                area_similarity = min(compare_area, bbox_area) / max(compare_area, bbox_area)
                if not is_semantic:
                    if target_profile and target_profile.get("is_glyph"):
                        size_factor = max(
                            0.65,
                            0.4 * ((width_similarity + height_similarity) / 2.0)
                            + 0.6 * (area_similarity ** 0.25),
                        )
                    else:
                        size_factor = max(0.35, 0.6 * ((width_similarity + height_similarity) / 2.0) + 0.4 * area_similarity)
                    score *= size_factor

                center_x = origin_x + x + w // 2
                center_y = origin_y + y + h // 2
                candidates.append({
                    "pos": f"{center_x},{center_y}",
                    "coords": (center_x, center_y),
                    "score": float(score),
                    "source": "component",
                    "semantic": is_semantic,
                })

        return self._dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)

    def _find_edge_template_candidates(self, sprite_path, captcha_path, search_box=None, top_k=5, min_distance=24, padding=0):
        import cv2
        import numpy as np

        sprite_img = cv2.imread(sprite_path)
        captcha_img = cv2.imread(captcha_path)
        if sprite_img is None or captcha_img is None:
            return []

        gray_sprite = cv2.cvtColor(sprite_img, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray_sprite, 240, 255, cv2.THRESH_BINARY_INV)
        coords = cv2.findNonZero(binary)
        if coords is not None:
            x, y, w, h = cv2.boundingRect(coords)
            x = max(0, x - 2)
            y = max(0, y - 2)
            w = min(sprite_img.shape[1] - x, w + 4)
            h = min(sprite_img.shape[0] - y, h + 4)
            sprite_icon = sprite_img[y:y+h, x:x+w]
        else:
            sprite_icon = sprite_img

        sprite_gray = cv2.cvtColor(sprite_icon, cv2.COLOR_BGR2GRAY)

        origin_x, origin_y = 0, 0
        if search_box is not None:
            x1, y1, x2, y2 = search_box
            x1 = max(0, x1 - padding)
            y1 = max(0, y1 - padding)
            x2 = min(captcha_img.shape[1], x2 + padding)
            y2 = min(captcha_img.shape[0], y2 + padding)
            captcha_view = captcha_img[y1:y2, x1:x2]
            origin_x, origin_y = x1, y1
        else:
            captcha_view = captcha_img

        if captcha_view.size == 0:
            return []

        captcha_gray = cv2.cvtColor(captcha_view, cv2.COLOR_BGR2GRAY)

        sprite_canny = cv2.Canny(sprite_gray, 50, 150)
        captcha_canny = cv2.Canny(captcha_gray, 50, 150)

        if (
            captcha_canny.shape[0] < sprite_canny.shape[0]
            or captcha_canny.shape[1] < sprite_canny.shape[1]
        ):
            return []

        h_s, w_s = sprite_canny.shape
        candidates = []

        for angle in [-15, 0, 15]:
            if angle != 0:
                M = cv2.getRotationMatrix2D((w_s//2, h_s//2), angle, 1.0)
                rotated_canny = cv2.warpAffine(sprite_canny, M, (w_s, h_s), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            else:
                rotated_canny = sprite_canny

            if (
                captcha_canny.shape[0] < rotated_canny.shape[0]
                or captcha_canny.shape[1] < rotated_canny.shape[1]
            ):
                continue

            res = cv2.matchTemplate(captcha_canny, rotated_canny, cv2.TM_CCOEFF_NORMED)
            res_work = res.copy()

            for _ in range(top_k):
                _, max_val, _, max_loc = cv2.minMaxLoc(res_work)
                if max_val <= 0:
                    break

                center_x = origin_x + max_loc[0] + rotated_canny.shape[1] // 2
                center_y = origin_y + max_loc[1] + rotated_canny.shape[0] // 2
                candidates.append({
                    "pos": f"{center_x},{center_y}",
                    "coords": (center_x, center_y),
                    "score": float(max_val),
                    "angle": angle,
                })

                left = max(0, max_loc[0] - min_distance)
                top = max(0, max_loc[1] - min_distance)
                right = min(res_work.shape[1], max_loc[0] + rotated_canny.shape[1] + min_distance)
                bottom = min(res_work.shape[0], max_loc[1] + rotated_canny.shape[0] + min_distance)
                res_work[top:bottom, left:right] = -1.0

        return self._dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)

    def _find_template_candidates(self, sprite_path, captcha_path, search_box=None, top_k=5, min_distance=24, padding=0, target_profile=None):
        candidates = self._find_component_candidates(
            sprite_path,
            captcha_path,
            search_box=search_box,
            top_k=top_k,
            min_distance=min_distance,
            padding=padding,
            target_profile=target_profile,
        )

        if target_profile and target_profile.get("is_glyph"):
            candidates.extend(
                self._find_glyph_candidates(
                    sprite_path,
                    captcha_path,
                    search_box=search_box,
                    top_k=top_k,
                    min_distance=min_distance,
                    padding=padding,
                )
            )
        else:
            candidates.extend(
                self._find_edge_template_candidates(
                    sprite_path,
                    captcha_path,
                    search_box=search_box,
                    top_k=top_k,
                    min_distance=min_distance,
                    padding=padding,
                )
            )

        return self._dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)

    def _find_sprite_by_template(self, sprite_path, captcha_path, search_box=None, padding=0, target_profile=None):
        candidates = self._find_template_candidates(
            sprite_path,
            captcha_path,
            search_box=search_box,
            top_k=1,
            min_distance=24,
            padding=padding,
            target_profile=target_profile,
        )
        if not candidates:
            return None, 0.0
        return candidates[0]["pos"], candidates[0]["score"]

    @staticmethod
    def _select_best_candidate_combo(candidate_groups, min_distance=24):
        import itertools

        if not candidate_groups or any(not candidates for candidates in candidate_groups):
            return [], 0.0

        best_combo = None
        best_total_score = -1.0

        for combo in itertools.product(*candidate_groups):
            coords = [candidate["coords"] for candidate in combo]
            has_overlap = False
            for i in range(len(coords)):
                for j in range(i + 1, len(coords)):
                    if TencentCaptchaProvider._distance(coords[i], coords[j]) < min_distance:
                        has_overlap = True
                        break
                if has_overlap:
                    break
            if has_overlap:
                continue

            total_score = sum(candidate["score"] for candidate in combo)
            if total_score > best_total_score:
                best_total_score = total_score
                best_combo = combo

        if best_combo is None:
            return [], 0.0

        return list(best_combo), best_total_score

    def _compute_score_from_images(self, sprite_img, spec_img, ocr, sprite_profile=None):
        import cv2
        import numpy as np

        shape_score = self._compute_binary_shape_score_images(sprite_img, spec_img)
        sprite_foreground = (sprite_profile or {}).get("foreground", {})
        spec_foreground = self._measure_foreground_shape(spec_img)
        sprite_char = ""
        if sprite_profile:
            sprite_char = (sprite_profile.get("ocr_text") or "").strip()
        is_glyph_target = sprite_profile.get("is_glyph", False) if sprite_profile else False
        spec_char = ""
        glyph_structure_factor = 1.0
        if is_glyph_target:
            glyph_structure_factor = self._compute_glyph_structure_factor(
                sprite_foreground,
                spec_foreground,
            )
            shape_score *= glyph_structure_factor

        try:
            if not sprite_char:
                sprite_char, _ = self._classify_glyph_char(sprite_img, ocr)
                is_glyph_target = bool(sprite_char)
            if is_glyph_target:
                spec_char, _ = self._classify_glyph_char(spec_img, ocr)

            if is_glyph_target:
                if len(sprite_char) > 0 and len(spec_char) > 0 and sprite_char == spec_char:
                    threshold = 0.45 if sprite_char in ["0", "1"] else 0.35
                    if shape_score >= threshold:
                        return 75.0 + shape_score * 25.0, True
                    return 60.0 + shape_score * 10.0, True
                if len(sprite_char) > 0 and len(spec_char) > 0 and sprite_char != spec_char:
                    return shape_score * 1.5, False
        except Exception:
            pass

        if is_glyph_target:
            if shape_score >= 0.75:
                return shape_score * 28.0, False
            if shape_score >= 0.55:
                return shape_score * 16.0, False
            return shape_score * 4.0, False

        if shape_score >= 0.55:
            return shape_score * 20.0, False

        if sprite_img is None or spec_img is None:
            return 0.0, False

        img1 = cv2.cvtColor(sprite_img, cv2.COLOR_BGR2GRAY) if len(sprite_img.shape) == 3 else sprite_img
        img2 = cv2.cvtColor(spec_img, cv2.COLOR_BGR2GRAY) if len(spec_img.shape) == 3 else spec_img

        if img1 is None or img2 is None:
            return 0.0, False

        sift = cv2.SIFT_create(nfeatures=500, contrastThreshold=0.02, edgeThreshold=15)
        kp1, des1 = sift.detectAndCompute(img1, None)
        kp2, des2 = sift.detectAndCompute(img2, None)

        if des1 is None or des2 is None or len(kp1) < 4 or len(kp2) < 4:
            return 0.0, False

        bf = cv2.BFMatcher()
        matches = bf.knnMatch(des1, des2, k=2)

        good = []
        for m_n in matches:
            if len(m_n) == 2:
                m, n = m_n
                if m.distance < 0.8 * n.distance:
                    good.append(m)

        if len(good) >= 4:
            src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

            try:
                M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                if mask is not None:
                    inliers = np.sum(mask)
                    return float(inliers), False
            except Exception:
                pass

        if len(des1) > 0:
            return max(len(good) / len(des1), shape_score * 8.0), False

        return shape_score * 5.0, False

    def _compute_score(self, sprite_path, spec_path, ocr, sprite_profile=None):
        import cv2
        sprite_img = cv2.imread(sprite_path)
        spec_img = cv2.imread(spec_path)
        return self._compute_score_from_images(sprite_img, spec_img, ocr, sprite_profile=sprite_profile)


class TwoCaptchaProvider(CaptchaProvider):
    """使用 2captcha API 破解腾讯点选验证码"""

    API_BASE = "https://2captcha.com"

    def __init__(self, max_retries=5, global_timeout=300):
        self.api_key = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
        self.max_retries = int(os.getenv("TWOCAPTCHA_MAX_RETRIES", max_retries))
        self.global_timeout = int(os.getenv("TWOCAPTCHA_GLOBAL_TIMEOUT", global_timeout))

    def solve(self, driver, timeout, retry_stats, logger_adapter):
        modules = import_selenium_modules()
        WebDriverWait = modules['WebDriverWait']
        EC = modules['EC']
        By = modules['By']
        ActionChains = modules['ActionChains']
        TimeoutException = modules['TimeoutException']

        if retry_stats is None:
            retry_stats = {'count': 0}

        if self.max_retries >= 0 and retry_stats['count'] >= self.max_retries:
            logger_adapter.warning(f"2captcha 已达到最大重试次数 {self.max_retries}，放弃")
            return False

        _start_time = retry_stats.get('_twocaptcha_start_time')
        if _start_time is None:
            _start_time = time.time()
            retry_stats['_twocaptcha_start_time'] = _start_time
        elif self.global_timeout > 0 and (time.time() - _start_time) > self.global_timeout:
            logger_adapter.warning(f"2captcha 全局超时 ({time.time() - _start_time:.0f}s)，放弃")
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
                retry_stats['count'] += 1
                time.sleep(3)
                return self.solve(driver, timeout, retry_stats, logger_adapter)

            captcha_coords = []
            for x, y in click_coords:
                # sprite现在在captcha下方，所以y值在captcha高度范围内的是有效坐标
                if y < captcha_height:
                    captcha_coords.append((x, y))
                else:
                    logger_adapter.debug(f"忽略在图案提示区域的点击: ({x}, {y})")

            if len(captcha_coords) < 3:
                logger_adapter.error(f"有效点击坐标不足3个 (仅 {len(captcha_coords)} 个)，刷新重试")
                retry_stats['count'] += 1
                time.sleep(3)
                return self.solve(driver, timeout, retry_stats, logger_adapter)

            final_coords = captcha_coords[:3]

            slideBg = wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]')))
            style = slideBg.get_attribute("style")
            captcha_img = cv2.imread("temp/captcha.jpg")
            width_raw, height_raw = captcha_img.shape[1], captcha_img.shape[0]

            import re
            width = float(re.search(r'width:\s*([\d.]+)px', style).group(1))
            height = float(re.search(r'height:\s*([\d.]+)px', style).group(1))
            x_offset, y_offset = float(-width / 2), float(-height / 2)

            for x, y in final_coords:
                final_x = int(x_offset + x / width_raw * width)
                final_y = int(y_offset + y / height_raw * height)
                ActionChains(driver).move_to_element_with_offset(slideBg, final_x, final_y).click().perform()
                time.sleep(0.3)

            confirm = wait.until(EC.element_to_be_clickable(
                (By.XPATH, '//*[@id="tcStatus"]/div[2]/div[2]/div/div')))
            logger_adapter.info("提交验证码")
            time.sleep(0.5)
            confirm.click()
            time.sleep(3)

            result_elem = wait.until(EC.visibility_of_element_located(
                (By.XPATH, '//*[@id="tcOperation"]')))
            if result_elem.get_attribute("class") == 'tc-opera pointer show-success':
                logger_adapter.info("验证码通过 🎉")
                return
            else:
                logger_adapter.error("2captcha 验证码提交后未通过")
                retry_stats['count'] += 1
                time.sleep(3)
                return self.solve(driver, timeout, retry_stats, logger_adapter)

        except TimeoutException:
            logger_adapter.error("获取验证码元素超时")
        except Exception as e:
            logger_adapter.error(f"2captcha 执行流程中发生错误: {e}")
            import traceback
            logger_adapter.debug(traceback.format_exc())
            retry_stats['count'] += 1
            try:
                reload_btn = driver.find_element(By.XPATH, '//*[@id="reload"]')
                reload_btn.click()
                time.sleep(3)
                return self.solve(driver, timeout, retry_stats, logger_adapter)
            except Exception:
                pass
        finally:
            logger_adapter.debug("2captcha 单次处理周期完毕")

    def _download_captcha_img(self, driver, timeout, logger_adapter):
        modules = import_selenium_modules()
        WebDriverWait = modules['WebDriverWait']
        EC = modules['EC']
        By = modules['By']

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

        slideBg = wait.until(EC.visibility_of_element_located(
            (By.XPATH, '//*[@id="slideBg"]')))
        img1_style = slideBg.get_attribute("style")

        import re
        img1_url = re.search(r'url\(["\']?(.*?)["\']?\)', img1_style).group(1)
        logger_adapter.info("开始下载验证码图片(1): " + img1_url)
        download_image(img1_url, "captcha.jpg", user_agent=current_ua)

        sprite = wait.until(EC.visibility_of_element_located(
            (By.XPATH, '//*[@id="instruction"]/div/img')))
        img2_url = sprite.get_attribute("src")
        logger_adapter.info("开始下载验证码图片(2): " + img2_url)
        download_image(img2_url, "sprite.jpg", user_agent=current_ua)

    @staticmethod
    def _build_combined_image(logger_adapter):
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

        sprite_canvas = np.full((sprite_strip.shape[0], captcha.shape[1], 3),
                                255, dtype=np.uint8)
        x_offset = (captcha.shape[1] - sprite_strip.shape[1]) // 2
        sprite_canvas[:, x_offset:x_offset + sprite_strip.shape[1]] = sprite_strip

        # 把sprite放在captcha下方，这样2captcha返回的坐标都在captcha区域内
        # 不需要过滤坐标，直接使用所有返回的坐标
        combined = np.vstack([captcha, line, sprite_canvas])
        logger_adapter.debug(f"组合图片尺寸: {combined.shape[1]}x{combined.shape[0]}")
        return combined

    def _submit_to_2captcha(self, image_path, logger_adapter, timeout):
        import requests
        import base64

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
            resp = requests.post(f"{self.API_BASE}/in.php",
                                 data=submit_payload, timeout=30)
            result = resp.text.strip()
            if not result.startswith("OK|"):
                logger_adapter.error(f"2captcha 提交失败: {result}")
                return None

            captcha_id = result[3:]
            logger_adapter.info(f"2captcha 任务已提交, ID: {captcha_id}")
        except requests.RequestException as e:
            logger_adapter.error(f"2captcha 提交请求失败: {e}")
            return None

        poll_params = {
            "key": api_key,
            "action": "get",
            "id": captcha_id,
        }
        max_wait = min(timeout + 30, 150)
        start_time = time.time()
        poll_interval = 5

        while time.time() - start_time < max_wait:
            try:
                resp = requests.get(f"{self.API_BASE}/res.php",
                                    params=poll_params, timeout=10)
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
        # 去掉可能的 "coordinates:" 前缀
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


class CompositeCaptchaProvider(CaptchaProvider):
    """复合验证码方案：先尝试主方案，失败后回退到备用方案"""

    def __init__(self, primary: CaptchaProvider, fallback: CaptchaProvider):
        self.primary = primary
        self.fallback = fallback

    def solve(self, driver, timeout, retry_stats, logger_adapter):
        result = self.primary.solve(driver, timeout, retry_stats, logger_adapter)

        if result is False:
            logger_adapter.warning(
                "本地方案已耗尽重试次数，切换到 2captcha 备用方案..."
            )
            self.fallback.solve(driver, timeout, retry_stats, logger_adapter)


class CaptchaFactory:
    """验证码工厂类"""
    @classmethod
    def create_provider(cls, captcha_type: str = "tencent") -> CaptchaProvider:
        if captcha_type == "tencent":
            return TencentCaptchaProvider()
        if captcha_type == "twocaptcha":
            return TwoCaptchaProvider()
        raise ValueError(f"Unknown captcha type: {captcha_type}")


def get_captcha_provider():
    """根据环境变量自动选择验证码破解方案"""
    twocaptcha_key = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
    if twocaptcha_key:
        return CompositeCaptchaProvider(
            TencentCaptchaProvider(max_retries=3),
            TwoCaptchaProvider(),
        )
    return TencentCaptchaProvider(max_retries=2)