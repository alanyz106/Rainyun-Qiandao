"""共享 CV 图像处理工具函数。

全部为纯函数，无状态，不依赖项目其他模块。
供 _glyph / _scoring / _search / _tencent / _twocaptcha 等模块复用。
"""

import logging
import math
import re

logger = logging.getLogger(__name__)


# ==========================================
# 几何 / 字符串 工具
# ==========================================

def distance(point_a, point_b):
    """两点之间的欧几里得距离。"""
    return math.dist(point_a, point_b)


def make_safe_name(raw_name):
    """将字符串转为安全的文件名片段。"""
    safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", raw_name or "unknown")
    return safe_name.strip("._") or "unknown"


def normalize_ocr_char(text):
    """规范化 OCR 输出为单个有效字符（数字/字母/中文），否则返回空串。"""
    text = text.strip() if text else ""
    if len(text) != 1:
        return ""

    ch = text[0]
    if ch.isdigit() or ("A" <= ch <= "Z") or ("a" <= ch <= "z"):
        return ch
    if "\u4e00" <= ch <= "\u9fff":
        return ch
    return ""


# ==========================================
# 前景提取 / 形状分析
# ==========================================

def measure_foreground_shape(image):
    """分析图像前景的形状指标（bbox、孔洞、暗像素比、边缘比、标准差等）。

    :return: dict 包含 has_foreground, bbox, bbox_area, holes,
             dark_ratio, edge_ratio, std
    """
    import cv2
    import numpy as np

    if image is None:
        return {
            "has_foreground": False, "bbox": (0, 0), "bbox_area": 0,
            "holes": 0, "dark_ratio": 0.0, "edge_ratio": 0.0, "std": 0.0,
        }

    if len(image.shape) == 3:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    else:
        gray = image.copy()

    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(
        blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
    )
    coords = cv2.findNonZero(binary)
    if coords is None:
        return {
            "has_foreground": False, "bbox": (0, 0), "bbox_area": 0,
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


def is_meaningful_candidate_crop(image):
    """判断候选区域是否有实际前景内容（排除空白/纯噪声）。"""
    metrics = measure_foreground_shape(image)
    if not metrics["has_foreground"]:
        return False
    if metrics["edge_ratio"] < 0.02 and metrics["std"] < 10 and metrics["dark_ratio"] < 0.02:
        return False
    return True


# ==========================================
# 二值 mask 提取
# ==========================================

def extract_binary_mask(image, crop_foreground=False, padding=2):
    """OTSU 二值化提取前景 mask。

    :param crop_foreground: 是否裁剪到前景包围盒
    :param padding: 裁剪时的边距
    :return: numpy.uint8 mask (0/255) 或 None
    """
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
        blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU,
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


def extract_dark_foreground_mask(image, crop_foreground=False, padding=2,
                                  dark_threshold=80, sat_threshold=120):
    """提取图像中的"深色（黑色/近黑）前景" mask（HSV 方案）。

    适用场景：在彩色背景里找黑色线条图标（腾讯点选验证码的核心场景）。
    HSV 黑色判定 + 形态学闭运算，精准定位前景，排除彩色背景噪声。

    :param dark_threshold: V 通道阈值（< 则视为深色）
    :param sat_threshold: S 通道阈值（< 则视为近灰/黑）
    :return: numpy.uint8 mask (0/255) 或 None
    """
    import cv2
    import numpy as np

    if image is None:
        return None

    if len(image.shape) == 3:
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        v_channel = hsv[:, :, 2]
        s_channel = hsv[:, :, 1]
    else:
        v_channel = image
        s_channel = np.zeros_like(image)

    dark_mask = ((v_channel < dark_threshold) & (s_channel < sat_threshold)).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    dark_mask = cv2.morphologyEx(dark_mask, cv2.MORPH_CLOSE, kernel)

    if crop_foreground:
        coords = cv2.findNonZero(dark_mask)
        if coords is None:
            return None
        x, y, w, h = cv2.boundingRect(coords)
        x = max(0, x - padding)
        y = max(0, y - padding)
        w = min(dark_mask.shape[1] - x, w + padding * 2)
        h = min(dark_mask.shape[0] - y, h + padding * 2)
        if w <= 0 or h <= 0:
            return None
        dark_mask = dark_mask[y:y + h, x:x + w]

    return dark_mask if dark_mask.size > 0 else None


# ==========================================
# 形状匹配 / 评分
# ==========================================

def compute_binary_shape_score_images(sprite_img, spec_img):
    """基于二值化 mask 的 IoU + 轮廓形状匹配评分。

    :param sprite_img: 目标图案（BGR 或灰度）
    :param spec_img: 候选区域（BGR 或灰度）
    :return: 0.0 ~ 1.0 的相似度分数
    """
    import cv2
    import numpy as np

    if sprite_img is None or spec_img is None:
        return 0.0

    if len(sprite_img.shape) == 3:
        sprite_img = cv2.cvtColor(sprite_img, cv2.COLOR_BGR2GRAY)
    if len(spec_img.shape) == 3:
        spec_img = cv2.cvtColor(spec_img, cv2.COLOR_BGR2GRAY)

    def _normalize_mask(img):
        blurred = cv2.GaussianBlur(img, (3, 3), 0)
        _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
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

    sprite_mask = _normalize_mask(sprite_img)
    spec_mask = _normalize_mask(spec_img)
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


def compute_binary_shape_score(sprite_path, spec_path):
    """从文件路径读取图片后计算二值形状匹配分。"""
    import cv2

    sprite_img = cv2.imread(sprite_path, cv2.IMREAD_GRAYSCALE)
    spec_img = cv2.imread(spec_path, cv2.IMREAD_GRAYSCALE)
    return compute_binary_shape_score_images(sprite_img, spec_img)


def compute_glyph_structure_factor(sprite_metrics, spec_metrics):
    """计算字形目标的结构相似因子（宽高比 + 孔洞数）。

    :param sprite_metrics: measure_foreground_shape 的输出
    :param spec_metrics: measure_foreground_shape 的输出
    :return: 0.22 ~ 1.0 的结构相似因子
    """
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
