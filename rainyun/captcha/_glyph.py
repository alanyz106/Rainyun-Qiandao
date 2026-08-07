"""字形（数字/字母/中文）OCR 分类。

将 ddddocr 应用于目标图案和候选区域，判断是否为字形目标。
依赖 _ocr（模型）+ _cv_utils（前景分析）。
"""

import logging

from rainyun.captcha._cv_utils import normalize_ocr_char, measure_foreground_shape

logger = logging.getLogger(__name__)


def is_likely_glyph_text(text):
    """检查 OCR 结果是否为有效单字符（数字/字母/中文）。"""
    return bool(normalize_ocr_char(text))


def classify_glyph_char(image, ocr):
    """使用多种二值化变体尝试 OCR 分类单个字形字符。

    :param image: BGR 或灰度 numpy 数组
    :param ocr: ddddocr OCR 模型
    :return: (char, variant_texts_dict)
    """
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
        blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
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

    from rainyun.captcha._ocr import get_inference_lock

    variant_texts = {}
    try:
        with get_inference_lock():
            for name, variant in variants.items():
                success, encoded = cv2.imencode(".png", variant)
                if not success:
                    variant_texts[name] = ""
                    continue
                variant_texts[name] = (ocr.classification(encoded.tobytes()) or "").strip()
    except Exception:
        return "", {}

    orig_char = normalize_ocr_char(variant_texts.get("orig"))
    th_char = normalize_ocr_char(variant_texts.get("th"))
    inv_char = normalize_ocr_char(variant_texts.get("inv"))
    th_up_char = normalize_ocr_char(variant_texts.get("th_up2"))
    inv_up_char = normalize_ocr_char(variant_texts.get("inv_up2"))

    # 多数表决：选最可靠的结果
    if th_char and th_char == inv_char and th_char == th_up_char:
        return th_char, variant_texts
    if th_char and th_char == inv_char and th_char == inv_up_char:
        return th_char, variant_texts
    if orig_char and th_char and orig_char == th_char:
        return orig_char, variant_texts
    if orig_char and inv_char and orig_char == inv_char:
        return orig_char, variant_texts

    return "", variant_texts


def build_sprite_profile(sprite_path, ocr):
    """构建目标图案的配置文件。

    包含 OCR 文本、是否为字形目标、前景形状指标。

    :return: dict {ocr_text, is_glyph, raw_ocr, foreground}
    """
    import cv2

    sprite_text = ""
    raw_texts = {}
    foreground_metrics = {}
    try:
        sprite_img = cv2.imread(sprite_path)
        foreground_metrics = measure_foreground_shape(sprite_img)
        sprite_text, raw_texts = classify_glyph_char(sprite_img, ocr)
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
