"""候选搜索 — 模板匹配 + 连通区域 + 去重 + 组合选择。

在大图中搜索目标图案的匹配位置，支持：
- 字形候选（二值 mask 模板匹配 + 旋转容差）
- 连通区域候选（分量匹配 + 尺寸筛选）
- 边缘模板匹配（HSV 黑色前景 mask）

依赖 _cv_utils + _glyph + _scoring + _ocr。
"""

import itertools
import logging

from rainyun.captcha._cv_utils import (
    distance,
    extract_binary_mask,
    extract_dark_foreground_mask,
    measure_foreground_shape,
)

logger = logging.getLogger(__name__)


def dedupe_candidates(candidates, min_distance=24, top_k=5):
    """按分数排序候选，移除距离过近的重复项。"""
    deduped = []
    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        if any(
            distance(candidate["coords"], existing["coords"]) < min_distance
            for existing in deduped
        ):
            continue
        deduped.append(candidate)
        if len(deduped) >= top_k:
            break
    return deduped


def select_best_candidate_combo(candidate_groups, min_distance=24):
    """从三组候选中选出互不重叠的最优组合。

    :param candidate_groups: list of 3 lists，每组为候选坐标列表
    :return: (best_combo: list, best_total_score: float)
    """
    if not candidate_groups or any(not candidates for candidates in candidate_groups):
        return [], 0.0

    best_combo = None
    best_total_score = -1.0

    for combo in itertools.product(*candidate_groups):
        coords = [candidate["coords"] for candidate in combo]
        has_overlap = False
        for i in range(len(coords)):
            for j in range(i + 1, len(coords)):
                if distance(coords[i], coords[j]) < min_distance:
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


# ==========================================
# 字形候选（二值 mask 模板匹配）
# ==========================================

def find_glyph_candidates(sprite_path, captcha_path, search_box=None,
                          top_k=5, min_distance=24, padding=0):
    """使用 OTSU 二值 mask + 旋转模板匹配查找字形候选。"""
    import cv2
    import numpy as np

    sprite_img = cv2.imread(sprite_path)
    captcha_img = cv2.imread(captcha_path)
    if sprite_img is None or captcha_img is None:
        return []

    sprite_mask = extract_binary_mask(sprite_img, crop_foreground=True, padding=2)
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

    captcha_mask = extract_binary_mask(captcha_view, crop_foreground=False, padding=0)
    if captcha_mask is None:
        return []

    if captcha_mask.shape[0] < sprite_mask.shape[0] or captcha_mask.shape[1] < sprite_mask.shape[1]:
        return []

    candidates = []
    h_s, w_s = sprite_mask.shape
    for angle in [-12, 0, 12]:
        if angle != 0:
            matrix = cv2.getRotationMatrix2D((w_s // 2, h_s // 2), angle, 1.0)
            rotated_mask = cv2.warpAffine(
                sprite_mask, matrix, (w_s, h_s),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=0,
            )
        else:
            rotated_mask = sprite_mask

        if (captcha_mask.shape[0] < rotated_mask.shape[0]
                or captcha_mask.shape[1] < rotated_mask.shape[1]):
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

    return dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)


# ==========================================
# 连通区域候选
# ==========================================

def find_component_candidates(sprite_path, captcha_path, search_box=None,
                              top_k=5, min_distance=24, padding=0,
                              target_profile=None):
    """使用连通区域分析在大图中搜索与目标图案匹配的候选区域。"""
    import cv2
    import numpy as np

    from rainyun.captcha._ocr import get_shared_ocr_models
    from rainyun.captcha._scoring import compute_score_from_images

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

            score, is_semantic = compute_score_from_images(
                sprite_img, component_crop, ocr, sprite_profile=target_profile,
            )
            if score <= 0:
                continue

            component_metrics = measure_foreground_shape(component_crop)
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
                    size_factor = max(
                        0.35,
                        0.6 * ((width_similarity + height_similarity) / 2.0)
                        + 0.4 * area_similarity,
                    )
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

    return dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)


# ==========================================
# 边缘模板匹配候选（HSV 黑色前景）
# ==========================================

def find_edge_template_candidates(sprite_path, captcha_path, search_box=None,
                                   top_k=5, min_distance=24, padding=0):
    """使用 HSV 黑色前景 mask 做模板匹配（适合线条图标）。

    相比 Canny 边缘方案，HSV 黑色前景能精准定位图标，排除彩色背景噪声。
    """
    import cv2
    import numpy as np

    sprite_img = cv2.imread(sprite_path)
    captcha_img = cv2.imread(captcha_path)
    if sprite_img is None or captcha_img is None:
        return []

    sprite_mask = extract_dark_foreground_mask(sprite_img, crop_foreground=True, padding=2)
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

    if captcha_view.size == 0:
        return []

    captcha_mask = extract_dark_foreground_mask(captcha_view, crop_foreground=False)
    if captcha_mask is None:
        return []

    if (captcha_mask.shape[0] < sprite_mask.shape[0]
            or captcha_mask.shape[1] < sprite_mask.shape[1]):
        return []

    h_s, w_s = sprite_mask.shape
    candidates = []

    for angle in [-15, 0, 15]:
        if angle != 0:
            M = cv2.getRotationMatrix2D((w_s // 2, h_s // 2), angle, 1.0)
            rotated_mask = cv2.warpAffine(
                sprite_mask, M, (w_s, h_s),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=0,
            )
        else:
            rotated_mask = sprite_mask

        if (captcha_mask.shape[0] < rotated_mask.shape[0]
                or captcha_mask.shape[1] < rotated_mask.shape[1]):
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

    return dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)


# ==========================================
# 综合候选搜索
# ==========================================

def find_template_candidates(sprite_path, captcha_path, search_box=None,
                             top_k=5, min_distance=24, padding=0,
                             target_profile=None):
    """综合搜索候选坐标（连通区域 + 字形/边缘模板匹配）。"""
    candidates = find_component_candidates(
        sprite_path, captcha_path,
        search_box=search_box, top_k=top_k,
        min_distance=min_distance, padding=padding,
        target_profile=target_profile,
    )

    if target_profile and target_profile.get("is_glyph"):
        candidates.extend(
            find_glyph_candidates(
                sprite_path, captcha_path,
                search_box=search_box, top_k=top_k,
                min_distance=min_distance, padding=padding,
            )
        )
    else:
        candidates.extend(
            find_edge_template_candidates(
                sprite_path, captcha_path,
                search_box=search_box, top_k=top_k,
                min_distance=min_distance, padding=padding,
            )
        )

    return dedupe_candidates(candidates, min_distance=min_distance, top_k=top_k)


def find_sprite_by_template(sprite_path, captcha_path, search_box=None,
                            padding=0, target_profile=None):
    """在大图中定位目标图案的最佳匹配坐标（取 top1）。"""
    candidates = find_template_candidates(
        sprite_path, captcha_path,
        search_box=search_box, top_k=1, min_distance=24,
        padding=padding, target_profile=target_profile,
    )
    if not candidates:
        return None, 0.0
    return candidates[0]["pos"], candidates[0]["score"]
