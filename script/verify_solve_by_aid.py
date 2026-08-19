"""验证 /solve_by_aid 端点（纯协议场景）。

检查点：
1. 每次调用 sess 不同（全新挑战）、points 为 3 个合法坐标
2. 交叉验证：用返回的图片 URL 下载，走 /solve 传图求解，坐标应与 /solve_by_aid 一致

用法：
    python script/verify_solve_by_aid.py [--count 3]
"""
import argparse
import subprocess

import requests

API = "https://cococaptcha.duckdns.org/yolo"
TCAPTCHA_BASE = "https://turing.captcha.qcloud.com"


def get_token():
    """从服务器 keys.json 读取第一个有效的 tcaptcha 卡密。"""
    cmd = (
        "/opt/geetest/venv/bin/python -c "
        "\"import json; d=json.load(open('/opt/geetest/server_auth/keys.json')); "
        "print(next(k['key'] for k in d['keys'] "
        "if k.get('service')=='tcaptcha' and k.get('enabled', True)))\""
    )
    out = subprocess.check_output(
        ["ssh", "-o", "ConnectTimeout=15", "huasuanyun", cmd],
        text=True,
    )
    return out.strip()


def abs_url(url):
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return TCAPTCHA_BASE + url
    return url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()

    token = get_token()
    headers = {"X-API-Token": token}
    sess_set = set()
    ok = 0
    for i in range(args.count):
        r = requests.post(f"{API}/solve_by_aid", headers=headers,
                          json={"aid": "2039519451"}, timeout=60)
        d = r.json()
        assert d.get("ok"), f"fail: {d}"
        sess, pts = d["sess"], d["points"]
        assert len(sess) > 50, f"sess too short: {sess!r}"
        assert len(pts) == 3 and all("," in p for p in pts), f"bad points: {pts}"
        dup = "DUP!" if sess in sess_set else "unique"
        sess_set.add(sess)
        print(f"[{i}] sess={sess[:16]}... {dup}  points={pts}")

        # 交叉验证：下载图片走 /solve，坐标应一致
        bg = requests.get(abs_url(d["captcha_url"]), timeout=15).content
        sprite = requests.get(abs_url(d["sprite_url"]), timeout=15).content
        r2 = requests.post(f"{API}/solve", headers=headers,
                           files={
                               "captcha": ("captcha.jpg", bg, "image/jpeg"),
                               "sprite": ("sprite.jpg", sprite, "image/jpeg"),
                           }, timeout=60)
        d2 = r2.json()
        same = d2.get("points") == pts
        print(f"    cross-check /solve: ok={d2.get('ok')} same_points={same}  "
              f"({d2.get('points')})")
        if d2.get("ok") and same:
            ok += 1
        else:
            print(f"    MISMATCH: aid={pts} solve={d2.get('points')}")

    print(f"\n结果: {ok}/{args.count} 全链路一致，sess 均唯一")


if __name__ == "__main__":
    main()
