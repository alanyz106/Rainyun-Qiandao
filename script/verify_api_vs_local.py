"""对比本地 solve_points 与远程 YOLO API 结果，验证 API 封装无回归。"""
import glob
import os
import sys
import time

import cv2
import requests

sys.path.insert(0, r"D:\Rainyun-Qiandao")
from rainyun.captcha._yolo import solve_points

API_URL = os.getenv("CAPTCHA_API_URL", "http://186.241.81.51:8501/solve")
API_TOKEN = os.getenv("CAPTCHA_API_TOKEN", "").strip()
if not API_TOKEN:
    print("请设置 CAPTCHA_API_TOKEN 环境变量")
    sys.exit(1)

base = r"D:\Rainyun-Qiandao\dataset\live-captcha-v2"
samples = sorted(glob.glob(os.path.join(base, "*")))[:15]

agree = 0
local_ok = 0
api_ok = 0
fail = 0
t_api = 0.0
for d in samples:
    stem = os.path.basename(d)
    captcha = cv2.imread(os.path.join(d, "captcha.jpg"))
    sprites = [cv2.imread(os.path.join(d, f"sprite_{i}.jpg")) for i in (1, 2, 3)]
    local_pts = solve_points(captcha, sprites) if captcha is not None and all(s is not None for s in sprites) else []

    with open(os.path.join(d, "captcha.jpg"), "rb") as f:
        cap_b = f.read()
    with open(os.path.join(d, "sprite.jpg"), "rb") as f:
        spr_b = f.read()
    t0 = time.time()
    r = requests.post(
        API_URL,
        headers={"X-API-Token": API_TOKEN},
        files={"captcha": ("captcha.jpg", cap_b, "image/jpeg"),
               "sprite": ("sprite.jpg", spr_b, "image/jpeg")},
        timeout=60,
    )
    dt = time.time() - t0
    t_api += dt
    data = r.json() if r.ok else {}
    api_pts = data.get("points", [])

    if len(local_pts) == 3:
        local_ok += 1
    if len(api_pts) == 3:
        api_ok += 1
    match = local_pts == api_pts
    if match:
        agree += 1
    else:
        fail += 1
        print(f"[DIFF] {stem}: local={local_pts} api={api_pts}")

print(f"\n对比 {len(samples)} 张: 一致={agree} 不一致={fail}")
print(f"本地 OK={local_ok}  API OK={api_ok}")
print(f"API 平均耗时: {t_api / len(samples):.2f}s")
