"""腾讯滑块验证码 — 本地 CV 方案。

TencentCaptchaProvider 使用 ddddocr + OpenCV 在本地完成腾讯点选验证码的识别。
核心流程：下载图片 → OCR 检测候选框 → 多模态评分匹配 → 精修定位 → 点击提交。
"""

import itertools
import json
import logging
import os
import shutil
import time
from datetime import datetime

from rainyun.captcha._archive import save_captcha_archive_bundle
from rainyun.captcha._cv_utils import (
    compute_binary_shape_score,
    compute_binary_shape_score_images,
    compute_glyph_structure_factor,
    extract_binary_mask,
    extract_dark_foreground_mask,
    is_meaningful_candidate_crop,
    make_safe_name,
    measure_foreground_shape,
    normalize_ocr_char,
)
from rainyun.captcha._glyph import build_sprite_profile, classify_glyph_char, is_likely_glyph_text
from rainyun.captcha._image_utils import download_image, get_height_from_style, get_url_from_style, get_width_from_style
from rainyun.captcha._ocr import get_inference_lock, get_shared_ocr_models
from rainyun.captcha._scoring import compute_score, compute_score_from_images
from rainyun.captcha._search import (
    dedupe_candidates,
    find_edge_template_candidates,
    find_glyph_candidates,
    find_sprite_by_template,
    find_template_candidates,
    select_best_candidate_combo,
)
from rainyun.captcha._siamese import match_sprites_to_boxes, hungry_assign
from rainyun.config import import_selenium_modules, now_local

logger = logging.getLogger(__name__)


class TencentCaptchaProvider:
    """腾讯滑块验证码 — 本地 OpenCV + OCR 方案。"""

    def __init__(self, max_retries=-1):
        self.max_retries = max_retries

    # ==========================================
    # 主入口 solve()
    # ==========================================

    def solve(self, driver, timeout, retry_stats, logger_adapter):
        """尝试解决一次腾讯点选验证码。

        流程：
        1. 等待 slideBg 出现 → 如果没有，说明无需验证码，直接返回
        2. 下载大图和 sprite
        3. ddddocr det 提取候选框
        4. 多模态评分匹配（形状 + OCR 语义 + SIFT）
        5. 精修定位 → 点击 → 确认
        6. 失败则刷新重试，直到 max_retries 耗尽

        :return: None 表示通过，False 表示放弃（耗尽重试次数）
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
            import numpy as np

            ocr, det = get_shared_ocr_models()

            wait = WebDriverWait(driver, timeout)
            self._download_captcha_img(driver, timeout, logger_adapter)

            logger_adapter.info("开始处理验证码图片并识别")

            raw_sprite = cv2.imread("temp/sprite.jpg")
            if raw_sprite is not None:
                w_raw = raw_sprite.shape[1]
                for i in range(3):
                    temp = raw_sprite[:, w_raw // 3 * i: w_raw // 3 * (i + 1)]
                    cv2.imwrite(f"temp/sprite_{i + 1}.jpg", temp)

            captcha = cv2.imread("temp/captcha.jpg")
            with open("temp/captcha.jpg", "rb") as f:
                captcha_b = f.read()

            with get_inference_lock():
                bboxes = det.detection(captcha_b)

            spec_infos = []
            for i in range(len(bboxes)):
                x1, y1, x2, y2 = bboxes[i]
                spec = captcha[y1:y2, x1:x2]
                if not is_meaningful_candidate_crop(spec):
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
                score_matrix = []
                for j in range(3):
                    sprite_path = f"temp/sprite_{j + 1}.jpg"
                    sprite_profile = build_sprite_profile(sprite_path, ocr)
                    sprite_profiles.append(sprite_profile)
                    sprite_scores = []
                    for k, spec in enumerate(spec_infos):
                        score, is_semantic = compute_score(
                            sprite_path,
                            spec["path"],
                            ocr,
                            sprite_profile=sprite_profile,
                        )
                        sprite_scores.append(score)
                        logger_adapter.debug(
                            f"目标 {j + 1} -> 候选 {k + 1}: 得分 {score:.2f} (语义匹配: {is_semantic})"
                        )
                    score_matrix.append(sprite_scores)

                all_spec_indices = list(range(len(spec_infos)))
                for perm in itertools.permutations(all_spec_indices, 3):
                    total_score = (
                        score_matrix[0][perm[0]]
                        + score_matrix[1][perm[1]]
                        + score_matrix[2][perm[2]]
                    )
                    if total_score > best_total_score:
                        best_total_score = total_score
                        best_assignment = perm

            MIN_ACCEPTABLE_TOTAL_SCORE = 2.0
            final_click_positions = []
            use_fallback = False
            assigned_scores = []
            siamese_ok = False

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
                    logger_adapter.info(
                        f"成功找到全局最优组合，验证码一阶段置信分: {best_total_score:.2f}"
                    )
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
                            refined_pos, refined_score = find_sprite_by_template(
                                sprite_path,
                                "temp/captcha.jpg",
                                search_box=spec_info["bbox"],
                                padding=12,
                                target_profile=profile,
                            )
                            if refined_pos:
                                positon = refined_pos
                                logger_adapter.info(
                                    f"--> 图案 {j + 1} 选择候选框 {spec_idx + 1}，"
                                    f"候选框中心 ({spec_info['pos']}) -> "
                                    f"局部精修坐标 ({positon})，单项得分：{score:.2f}，"
                                    f"精修边缘分：{refined_score:.2f}"
                                )
                            else:
                                logger_adapter.info(
                                    f"--> 图案 {j + 1} 选择候选框 {spec_idx + 1} 位于 ({positon})，"
                                    f"单项得分：{score:.2f}，局部精修失败，回退候选框中心"
                                )
                        final_click_positions.append(positon)
            else:
                score_info = (
                    f"{best_total_score:.2f}" if best_assignment is not None
                    else "候选框不足3个"
                )
                logger_adapter.warning(
                    f"局部目标检测不佳（得分 {score_info} < {MIN_ACCEPTABLE_TOTAL_SCORE}），"
                    "降级使用全图边缘模板匹配..."
                )
                use_fallback = True

            if use_fallback:
                # ===== V1: Siamese within ddddocr candidate boxes =====
                siamese_ok = False
                SIAMESE_MIN_CONFIDENCE = 0.6

                if len(spec_infos) >= 3:
                    logger_adapter.info("一阶段未通过，尝试 Siamese 匹配（候选框内）...")
                    try:
                        sprite_imgs = [cv2.imread(f"temp/sprite_{j+1}.jpg") for j in range(3)]
                        bbox_list = [s["bbox"] for s in spec_infos]
                        siam_scores = match_sprites_to_boxes(captcha, bbox_list, sprite_imgs)
                        siam_assign, siam_total = hungry_assign(siam_scores)

                        if siam_assign and len(siam_assign) == 3:
                            siam_per_sprite = [siam_scores[j][siam_assign[j]] for j in range(3)]
                            siam_min = min(siam_per_sprite)
                            logger_adapter.info(
                                f"Siamese 分配: {siam_assign}  总分={siam_total:.4f}  "
                                f"单项={[f'{v:.3f}' for v in siam_per_sprite]}  "
                                f"最低={siam_min:.3f}"
                            )
                            if siam_min >= SIAMESE_MIN_CONFIDENCE:
                                for j in range(3):
                                    spec_idx = siam_assign[j]
                                    spec_info = spec_infos[spec_idx]
                                    cx = int((spec_info["bbox"][0] + spec_info["bbox"][2]) / 2)
                                    cy = int((spec_info["bbox"][1] + spec_info["bbox"][3]) / 2)
                                    final_click_positions.append(f"{cx},{cy}")
                                    logger_adapter.info(
                                        f"--> [Siamese] 图案 {j+1} → 候选框 {spec_idx+1}  "
                                        f"({cx},{cy})  相似度={siam_per_sprite[j]:.3f}"
                                    )
                                siamese_ok = True
                            else:
                                logger_adapter.warning(
                                    f"Siamese 最低置信度 {siam_min:.3f} < {SIAMESE_MIN_CONFIDENCE}，"
                                    f"尝试 mask 增强..."
                                )
                        else:
                            logger_adapter.warning("Siamese 分配失败")
                    except Exception as e:
                        logger_adapter.warning(f"Siamese 匹配异常: {e}")

                # ===== V2: mask + Siamese =====
                if not siamese_ok:
                    logger_adapter.info("尝试 mask 预处理 + Siamese...")
                    try:
                        masked_captcha = captcha.copy()
                        black_mask = (
                            (captcha[:, :, 0] <= 30)
                            & (captcha[:, :, 1] <= 30)
                            & (captcha[:, :, 2] <= 30)
                        )
                        masked_captcha[~black_mask] = 255

                        cv2.imwrite("temp/captcha_masked.jpg", masked_captcha)
                        with open("temp/captcha_masked.jpg", "rb") as f:
                            mask_bytes = f.read()
                        with get_inference_lock():
                            mask_boxes = det.detection(mask_bytes)

                        mask_specs = []
                        for x1, y1, x2, y2 in mask_boxes:
                            x1, y1 = int(max(0, x1)), int(max(0, y1))
                            x2, y2 = int(min(captcha.shape[1], x2)), int(min(captcha.shape[0], y2))
                            crop = masked_captcha[y1:y2, x1:x2]
                            if is_meaningful_candidate_crop(crop):
                                mask_specs.append((x1, y1, x2, y2))
                        logger_adapter.info(
                            f"mask 后 ddddocr 检测: {len(mask_specs)} 框 (原始: {len(spec_infos)})"
                        )

                        if len(mask_specs) >= 3:
                            sprite_imgs2 = [cv2.imread(f"temp/sprite_{j+1}.jpg") for j in range(3)]
                            mask_siam = match_sprites_to_boxes(captcha, mask_specs, sprite_imgs2)
                            mask_assign, mask_total = hungry_assign(mask_siam)

                            if mask_assign and len(mask_assign) == 3:
                                mask_per = [mask_siam[j][mask_assign[j]] for j in range(3)]
                                mask_min = min(mask_per)
                                logger_adapter.info(
                                    f"mask+Siamese 分配: {mask_assign}  总分={mask_total:.4f}  "
                                    f"最低={mask_min:.3f}"
                                )
                                if mask_min >= SIAMESE_MIN_CONFIDENCE:
                                    for j in range(3):
                                        spec_idx = mask_assign[j]
                                        x1, y1, x2, y2 = mask_specs[spec_idx]
                                        cx, cy = int((x1 + x2) / 2), int((y1 + y2) / 2)
                                        final_click_positions.append(f"{cx},{cy}")
                                    siamese_ok = True
                                    logger_adapter.info(
                                        f"✅ mask+Siamese 通过 (≥ {SIAMESE_MIN_CONFIDENCE})"
                                    )
                                else:
                                    logger_adapter.warning(
                                        f"mask+Siamese 最低 {mask_min:.3f} < {SIAMESE_MIN_CONFIDENCE}"
                                    )
                    except Exception as e:
                        logger_adapter.warning(f"mask+Siamese 异常: {e}")

                # ===== V3: 像素回退 =====
                if not siamese_ok:
                    logger_adapter.info("Siamese 方案均失败，降级使用全图像素模板匹配...")
                    fallback_candidates = []
                    for j in range(3):
                        sprite_path = f"temp/sprite_{j + 1}.jpg"
                        candidates = find_template_candidates(
                            sprite_path,
                            "temp/captcha.jpg",
                            top_k=5,
                            min_distance=24,
                            target_profile=(
                                sprite_profiles[j] if j < len(sprite_profiles) else None
                            ),
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

                    selected_candidates, fallback_total_score = select_best_candidate_combo(
                        fallback_candidates, min_distance=24,
                    )
                    final_click_positions = [
                        candidate["pos"] for candidate in selected_candidates
                    ]

                    MIN_FALLBACK_TOTAL_SCORE = 0.75
                    if fallback_total_score < MIN_FALLBACK_TOTAL_SCORE or len(final_click_positions) < 3:
                        logger_adapter.error(
                            f"全图匹配响应度过低 ({fallback_total_score:.2f} < "
                            f"{MIN_FALLBACK_TOTAL_SCORE:.2f})，放弃提交并刷新"
                        )
                        self._save_captcha_debug_bundle(
                            logger_adapter,
                            stage="fallback_low_score",
                            retry_count=retry_stats["count"],
                            extra={
                                "fallback_total_score": fallback_total_score,
                                "click_positions": final_click_positions,
                            },
                        )
                        final_click_positions = []

            if len(final_click_positions) == 3:
                for positon in final_click_positions:
                    slideBg = wait.until(
                        EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]'))
                    )
                    style = slideBg.get_attribute("style")
                    x, y = int(positon.split(",")[0]), int(positon.split(",")[1])
                    width_raw, height_raw = captcha.shape[1], captcha.shape[0]
                    width = float(get_width_from_style(style))
                    height = float(get_height_from_style(style))
                    x_offset, y_offset = float(-width / 2), float(-height / 2)
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
                        "best_total_score": best_total_score,
                        "use_fallback": use_fallback,
                        "siamese_ok": siamese_ok,
                        "click_positions": final_click_positions,
                    })
                    return
                else:
                    logger_adapter.error("验证码提交后未通过，匹配坐标可能存在偏移。")
                    self._save_captcha_debug_bundle(
                        logger_adapter,
                        stage="submit_failed",
                        retry_count=retry_stats["count"],
                        extra={
                            "click_positions": final_click_positions,
                            "used_fallback": use_fallback,
                            "best_total_score": best_total_score,
                        },
                    )
                    retry_stats["count"] += 1
            else:
                retry_stats["count"] += 1

            reload_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//*[@id="reload"]'))
            )
            time.sleep(1)
            reload_btn.click()
            time.sleep(3)
            logger_adapter.info(
                f"重新发起验证码挑战 (当前重试: {retry_stats['count']})"
            )
            attempt_extra = {
                "best_total_score": best_total_score,
                "use_fallback": use_fallback,
            }
            if use_fallback:
                attempt_extra["fallback_total_score"] = fallback_total_score
                attempt_extra["click_positions"] = final_click_positions
            else:
                attempt_extra["assigned_scores"] = assigned_scores
            save_captcha_archive_bundle(logger_adapter, attempt_index, "retry", attempt_extra)
            return self.solve(driver, timeout, retry_stats, logger_adapter)

        except TimeoutException:
            logger_adapter.error("获取验证码图片等元素超时")
            save_captcha_archive_bundle(logger_adapter, attempt_index, "error", {"reason": "timeout"})
        except Exception as e:
            logger_adapter.error(f"验证码执行流程中发生未知错误: {e}")
            save_captcha_archive_bundle(
                logger_adapter, attempt_index, "error",
                {"reason": "exception", "error": str(e)[:200]},
            )
            import traceback

            logger_adapter.debug(traceback.format_exc())
            retry_stats["count"] += 1
            try:
                reload_btn = driver.find_element(By.XPATH, '//*[@id="reload"]')
                reload_btn.click()
                time.sleep(3)
                return self.solve(driver, timeout, retry_stats, logger_adapter)
            except Exception:
                pass
        finally:
            logger_adapter.debug("验证码单次处理周期完毕")

    # ==========================================
    # 图片下载
    # ==========================================

    def _download_captcha_img(self, driver, timeout, logger_adapter):
        """从页面下载 captcha 大图 + sprite 提示条到 temp/ 目录。"""
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

        slideBg = wait.until(
            EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]'))
        )
        img1_style = slideBg.get_attribute("style")
        img1_url = get_url_from_style(img1_style)
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

    # ==========================================
    # 调试样本保存
    # ==========================================

    def _save_captcha_debug_bundle(self, logger_adapter, stage, retry_count, extra=None):
        """仅在关键失败分支触发，保存完整 debug 样本到 logs/captcha_debug/。"""
        account_prefix = make_safe_name(
            getattr(logger_adapter, "extra", {}).get("prefix", "unknown")
        )
        bundle_name = (
            f"{now_local().strftime('%Y%m%d_%H%M%S_%f')[:-3]}_{stage}_r{retry_count}"
        )
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

    # ==========================================
    # 兼容性桥接（旧代码直接调用实例方法/静态方法）
    # ==========================================

    # 保留这些方法作为对底层工具函数的委托，确保 script/captcha_compare.py
    # 等外部调用 `provider._find_edge_template_candidates(...)` 仍然可用。

    @staticmethod
    def _make_safe_name(raw_name):
        return make_safe_name(raw_name)

    @staticmethod
    def _distance(point_a, point_b):
        from rainyun.captcha._cv_utils import distance
        return distance(point_a, point_b)

    @staticmethod
    def _compute_binary_shape_score_images(sprite_img, spec_img):
        return compute_binary_shape_score_images(sprite_img, spec_img)

    @staticmethod
    def _measure_foreground_shape(image):
        return measure_foreground_shape(image)

    @staticmethod
    def _normalize_ocr_char(text):
        return normalize_ocr_char(text)

    @staticmethod
    def _is_likely_glyph_text(text):
        return is_likely_glyph_text(text)

    @staticmethod
    def _compute_glyph_structure_factor(sprite_metrics, spec_metrics):
        return compute_glyph_structure_factor(sprite_metrics, spec_metrics)

    @staticmethod
    def _extract_binary_mask(image, crop_foreground=False, padding=2):
        return extract_binary_mask(image, crop_foreground, padding)

    @staticmethod
    def _extract_dark_foreground_mask(image, crop_foreground=False, padding=2,
                                       dark_threshold=80, sat_threshold=120):
        return extract_dark_foreground_mask(
            image, crop_foreground, padding, dark_threshold, sat_threshold,
        )

    @staticmethod
    def _select_best_candidate_combo(candidate_groups, min_distance=24):
        return select_best_candidate_combo(candidate_groups, min_distance)

    def _classify_glyph_char(self, image, ocr):
        return classify_glyph_char(image, ocr)

    def _build_sprite_profile(self, sprite_path, ocr):
        return build_sprite_profile(sprite_path, ocr)

    def _is_meaningful_candidate_crop(self, image):
        return is_meaningful_candidate_crop(image)

    def _compute_binary_shape_score(self, sprite_path, spec_path):
        return compute_binary_shape_score(sprite_path, spec_path)

    def _compute_score_from_images(self, sprite_img, spec_img, ocr, sprite_profile=None):
        return compute_score_from_images(sprite_img, spec_img, ocr, sprite_profile)

    def _compute_score(self, sprite_path, spec_path, ocr, sprite_profile=None):
        return compute_score(sprite_path, spec_path, ocr, sprite_profile)

    def _dedupe_candidates(self, candidates, min_distance=24, top_k=5):
        return dedupe_candidates(candidates, min_distance, top_k)

    def _find_glyph_candidates(self, sprite_path, captcha_path, search_box=None,
                                top_k=5, min_distance=24, padding=0):
        return find_glyph_candidates(
            sprite_path, captcha_path, search_box, top_k, min_distance, padding,
        )

    def _find_component_candidates(self, sprite_path, captcha_path, search_box=None,
                                    top_k=5, min_distance=24, padding=0,
                                    target_profile=None):
        return find_component_candidates(
            sprite_path, captcha_path, search_box, top_k, min_distance, padding, target_profile,
        )

    def _find_edge_template_candidates(self, sprite_path, captcha_path, search_box=None,
                                        top_k=5, min_distance=24, padding=0):
        return find_edge_template_candidates(
            sprite_path, captcha_path, search_box, top_k, min_distance, padding,
        )

    def _find_template_candidates(self, sprite_path, captcha_path, search_box=None,
                                   top_k=5, min_distance=24, padding=0,
                                   target_profile=None):
        return find_template_candidates(
            sprite_path, captcha_path, search_box, top_k, min_distance, padding, target_profile,
        )

    def _find_sprite_by_template(self, sprite_path, captcha_path, search_box=None,
                                  padding=0, target_profile=None):
        return find_sprite_by_template(
            sprite_path, captcha_path, search_box, padding, target_profile,
        )
