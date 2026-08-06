"""
captcha_compare.py — 对比"黑色前景 mask" vs "Canny 边缘" 模板匹配的响应分

用法：python script/captcha_compare.py [captcha_path] [sprite_path]
默认读取 temp/captcha.jpg 和 temp/sprite.jpg（需先触发一次验证码流程生成）。
"""
import os
import sys

import cv2

# 确保能找到 rainyun 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rainyun.captcha import TencentCaptchaProvider


def old_edge_template(captcha_path, sprite_path, top_k=3, min_distance=24):
    """旧版 Canny 边缘模板匹配（保留原 _find_edge_template_candidates 的逻辑，仅取 top1 最高分）。"""
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
        sprite_icon = sprite_img[y:y + h, x:x + w]
    else:
        sprite_icon = sprite_img
    sprite_gray = cv2.cvtColor(sprite_icon, cv2.COLOR_BGR2GRAY)

    captcha_gray = cv2.cvtColor(captcha_img, cv2.COLOR_BGR2GRAY)
    sprite_canny = cv2.Canny(sprite_gray, 50, 150)
    captcha_canny = cv2.Canny(captcha_gray, 50, 150)

    if captcha_canny.shape[0] < sprite_canny.shape[0] or captcha_canny.shape[1] < sprite_canny.shape[1]:
        return []

    h_s, w_s = sprite_canny.shape
    candidates = []
    for angle in [-15, 0, 15]:
        if angle != 0:
            M = cv2.getRotationMatrix2D((w_s // 2, h_s // 2), angle, 1.0)
            rotated = cv2.warpAffine(
                sprite_canny, M, (w_s, h_s),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
        else:
            rotated = sprite_canny
        if captcha_canny.shape[0] < rotated.shape[0] or captcha_canny.shape[1] < rotated.shape[1]:
            continue
        res = cv2.matchTemplate(captcha_canny, rotated, cv2.TM_CCOEFF_NORMED)
        res_work = res.copy()
        for _ in range(top_k):
            _, max_val, _, max_loc = cv2.minMaxLoc(res_work)
            if max_val <= 0:
                break
            cx = max_loc[0] + rotated.shape[1] // 2
            cy = max_loc[1] + rotated.shape[0] // 2
            candidates.append({"pos": (cx, cy), "score": float(max_val), "angle": angle})
            left = max(0, max_loc[0] - min_distance)
            top = max(0, max_loc[1] - min_distance)
            right = min(res_work.shape[1], max_loc[0] + rotated.shape[1] + min_distance)
            bottom = min(res_work.shape[0], max_loc[1] + rotated.shape[0] + min_distance)
            res_work[top:bottom, left:right] = -1.0
    return candidates


def main():
    captcha_path = sys.argv[1] if len(sys.argv) > 1 else "temp/captcha.jpg"
    sprite_full_path = sys.argv[2] if len(sys.argv) > 2 else "temp/sprite.jpg"

    if not os.path.exists(captcha_path) or not os.path.exists(sprite_full_path):
        print(f"图片不存在: {captcha_path} 或 {sprite_full_path}")
        print("请先用 rainyun 流程生成一组验证码图片，或传入路径作为参数")
        sys.exit(1)

    sprite_full = cv2.imread(sprite_full_path)
    w_raw = sprite_full.shape[1]
    os.makedirs("temp", exist_ok=True)
    sprite_paths = []
    for i in range(3):
        p = f"temp/sprite_{i + 1}.jpg"
        cv2.imwrite(p, sprite_full[:, w_raw // 3 * i: w_raw // 3 * (i + 1)])
        sprite_paths.append(p)

    provider = TencentCaptchaProvider()

    print(f"=== 对比 captcha={captcha_path}  sprite={sprite_full_path} ===\n")
    header = f"{'sprite#':<10}{'旧版 top1 响应分':<18}{'新版 top1 响应分':<18}{'提升倍数':<10}"
    print(header)
    print("-" * len(header))

    old_total = 0.0
    new_total = 0.0
    for i, sp in enumerate(sprite_paths):
        old = old_edge_template(captcha_path, sp, top_k=3)
        new = provider._find_edge_template_candidates(sp, captcha_path, top_k=3)

        old_top1 = old[0]["score"] if old else 0.0
        new_top1 = new[0]["score"] if new else 0.0
        boost = (new_top1 / old_top1) if old_top1 > 0 else float("inf")

        old_total += old_top1
        new_total += new_top1
        print(f"{i + 1:<10}{old_top1:<18.4f}{new_top1:<18.4f}{boost:<10.2f}x")

    print(f"\n总分（旧版 top1 之和）: {old_total:.4f}")
    print(f"总分（新版 top1 之和）: {new_total:.4f}")
    if old_total > 0:
        print(f"总分提升倍数: {new_total / old_total:.2f}x")


if __name__ == "__main__":
    main()