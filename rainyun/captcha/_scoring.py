"""模式匹配评分。

将目标图案与候选区域进行评分（形状匹配 + OCR 语义匹配 + SIFT 特征匹配）。
依赖 _cv_utils + _glyph + _ocr。
"""

import logging

from rainyun.captcha._cv_utils import (
    compute_binary_shape_score_images,
    compute_glyph_structure_factor,
    measure_foreground_shape,
)
from rainyun.captcha._glyph import classify_glyph_char

logger = logging.getLogger(__name__)


def compute_score_from_images(sprite_img, spec_img, ocr, sprite_profile=None):
    """计算目标图案与候选区域的综合匹配分。

    流程：
    1. 二值形状分（IoU + 轮廓匹配）
    2. 字形目标 → OCR 语义匹配 + 结构因子
    3. 非字形目标 → SIFT 特征匹配兜底

    :return: (score: float, is_semantic: bool)
    """
    import cv2
    import numpy as np

    shape_score = compute_binary_shape_score_images(sprite_img, spec_img)
    sprite_foreground = (sprite_profile or {}).get("foreground", {})
    spec_foreground = measure_foreground_shape(spec_img)
    sprite_char = ""
    if sprite_profile:
        sprite_char = (sprite_profile.get("ocr_text") or "").strip()
    is_glyph_target = sprite_profile.get("is_glyph", False) if sprite_profile else False
    spec_char = ""
    glyph_structure_factor = 1.0
    if is_glyph_target:
        glyph_structure_factor = compute_glyph_structure_factor(sprite_foreground, spec_foreground)
        shape_score *= glyph_structure_factor

    try:
        if not sprite_char:
            sprite_char, _ = classify_glyph_char(sprite_img, ocr)
            is_glyph_target = bool(sprite_char)
        if is_glyph_target:
            spec_char, _ = classify_glyph_char(spec_img, ocr)

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


def compute_score(sprite_path, spec_path, ocr, sprite_profile=None):
    """从文件路径读取图片后计算匹配分。"""
    import cv2

    sprite_img = cv2.imread(sprite_path)
    spec_img = cv2.imread(spec_path)
    return compute_score_from_images(sprite_img, spec_img, ocr, sprite_profile=sprite_profile)
