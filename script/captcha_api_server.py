"""腾讯验证码 YOLO 定类 API 服务端（部署在 huasuanyun 服务器）。

鉴权（卡密）：读 /opt/geetest/server_auth/keys.json 中 service=tcaptcha 的卡密。
传入方式任选其一（现有外部调用保持 X-API-Token 头不变）：
- header: X-API-Key（与极验服务统一）
- header: X-API-Token（原有调用方式，兼容）
- query:  ?token=xxx 或 ?key=xxx
环境变量 CAPTCHA_API_TOKEN 作为兜底（旧 token 平滑过渡，Secret 更新后实际作废）。
卡密在管理后台 https://cococaptcha.duckdns.org/admin/ 生成/作废，实时生效。

端点：
- GET  /health           健康检查，返回模型加载状态
- POST /solve            multipart 上传 captcha + sprite，返回 3 个点击坐标
- POST /solve_by_aid     纯协议场景：JSON 传 aid，服务端 prehandle 生成新挑战并求解

POST /solve 请求：
  headers: X-API-Token: <token>（可选，设了 CAPTCHA_API_TOKEN 则必填）
  files:  captcha  -> 大图 (jpg/png)
          sprite   -> 整条提示条 (170x50)，服务端竖切 3 段
  响应:  {"ok": true, "points": ["x,y","x,y","x,y"]}

POST /solve_by_aid 请求（无浏览器/纯协议场景）：
  headers: X-API-Token: <token>
  body:   {"aid": "2039519451", "subsid": 1}（aid 缺省用雨云 AppID）
  响应:  {"ok": true, "sess": "...", "points": [...],
          "captcha_url": "...", "sprite_url": "..."}
  说明:  该端点生成的是全新验证码挑战，只适用于纯协议提交；
         浏览器内嵌验证码必须用 /solve 传当前挑战的图片。
"""

import base64
import json
import logging
import os
import re
import time

import cv2
import numpy as np
import requests
from flask import Flask, jsonify, request

from _yolo import solve_points

# ===== 腾讯 tcaptcha 协议（与 script/fetch_live_captcha.py 一致）=====
TCAPTCHA_BASE = "https://turing.captcha.qcloud.com"
TCAPTCHA_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0")
TCAPTCHA_HEADERS = {"User-Agent": TCAPTCHA_UA, "Referer": "https://turing.captcha.gtimg.com/"}
AID_DEFAULT = "2039519451"


def _parse_jsonp(text):
    m = re.search(r"\((.*)\)\s*$", text, re.S)
    return json.loads(m.group(1)) if m else None


def _tcaptcha_prehandle(aid, subsid=1):
    """cap_union_prehandle 生成新验证码挑战，返回 (dyn_show_info, sess, raw)。"""
    ua_b64 = base64.b64encode(TCAPTCHA_UA.encode()).decode()
    params = {
        "aid": aid, "protocol": "https", "accver": "1", "showtype": "popup",
        "ua": ua_b64, "noheader": "1", "fb": "1", "aged": "0",
        "enableAged": "0", "enableDarkMode": "0", "grayscale": "1",
        "clientype": "2", "cap_cd": "", "uid": "", "lang": "zh-cn",
        "entry_url": "https://app.rainyun.com/account/reward/earn",
        "elder_captcha": "0", "js": "/tcaptcha-frame.48785b62.js",
        "login_appid": "", "wb": "2", "tkid": "904319250",
        "subsid": str(subsid),
        "callback": f"_aq_{int(time.time() * 1000)}", "sess": "",
    }
    r = requests.get(f"{TCAPTCHA_BASE}/cap_union_prehandle", params=params,
                     timeout=15, headers={"User-Agent": TCAPTCHA_UA})
    data = _parse_jsonp(r.text)
    if not data or data.get("state") != 1:
        return None, None, data
    d = data.get("data", {})
    dyn = d.get("dyn_show_info", {})
    # prehandle 响应没有独立 sess 字段，sess 只出现在图片 URL 查询参数里
    bg_url = dyn.get("bg_elem_cfg", {}).get("img_url", "")
    m = re.search(r"sess=([^&]+)", bg_url)
    sess = m.group(1) if m else ""
    return dyn, sess, data


def _tcaptcha_download(url):
    """下载腾讯验证码图片，兼容 // 与 / 开头相对 URL。"""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = TCAPTCHA_BASE + url
    r = requests.get(url, timeout=15, headers=TCAPTCHA_HEADERS)
    if r.status_code == 200 and len(r.content) > 1000:
        return r.content
    return None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("captcha-api")

app = Flask(__name__)

_API_TOKEN = os.getenv("CAPTCHA_API_TOKEN", "").strip()

# 与极验服务共享的卡密文件（service=tcaptcha 的卡密用于本服务）
TCAPTCHA_KEYS_FILE = "/opt/geetest/server_auth/keys.json"


def _decode_bytes(data):
    """bytes -> BGR ndarray。"""
    if not data:
        return None
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _decode_image(file_storage):
    """读取上传文件为 BGR ndarray。"""
    return _decode_bytes(file_storage.read())


def _split_sprite(raw_sprite, n=3):
    """整条 sprite 竖切 n 段（与 _tencent.py 生产切片方式一致）。"""
    w = raw_sprite.shape[1]
    return [raw_sprite[:, w // n * i: w // n * (i + 1)] for i in range(n)]


def _load_valid_tcaptcha_keys():
    """读取共享 keys.json 中 service=tcaptcha 的有效卡密。"""
    try:
        with open(TCAPTCHA_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return [
        k.get("key") for k in data.get("keys", [])
        if k.get("enabled", True) and k.get("service", "geetest") == "tcaptcha"
    ]


def _check_auth():
    """卡密校验。

    传入方式（任选其一）：header X-API-Key / X-API-Token / query token / query key。
    keys.json 的 tc_ 卡密为主；环境变量 CAPTCHA_API_TOKEN 兜底（旧 token 平滑过渡）。
    """
    token = (
        request.headers.get("X-API-Key", "")
        or request.headers.get("X-API-Token", "")
        or request.args.get("token", "")
        or request.args.get("key", "")
    ).strip()
    if not token:
        return False
    if token in _load_valid_tcaptcha_keys():
        return True
    # 兜底：环境变量 token（旧方式）。GH Secret 换成卡密后旧 token 实际作废。
    return bool(_API_TOKEN) and token == _API_TOKEN


@app.route("/health", methods=["GET"])
def health():
    # 触发一次模型加载检查
    from _yolo import _get_session, _get_siames
    status = {"ok": True, "service": "captcha-api"}
    try:
        _get_session()
        _get_siames()
        status["models"] = "loaded"
    except Exception as e:
        status["ok"] = False
        status["models"] = f"error: {e}"
    return jsonify(status)


@app.route("/solve", methods=["POST"])
def solve():
    if not _check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    captcha_f = request.files.get("captcha")
    sprite_f = request.files.get("sprite")
    if captcha_f is None or sprite_f is None:
        return jsonify({"ok": False, "error": "missing captcha or sprite file"}), 400

    captcha = _decode_image(captcha_f)
    raw_sprite = _decode_image(sprite_f)
    if captcha is None:
        return jsonify({"ok": False, "error": "captcha decode failed"}), 400
    if raw_sprite is None:
        return jsonify({"ok": False, "error": "sprite decode failed"}), 400

    try:
        sprites = _split_sprite(raw_sprite, 3)
        points = solve_points(captcha, sprites)
        if len(points) != 3:
            return jsonify({"ok": False, "error": "solve_points failed", "points": points})
        return jsonify({"ok": True, "points": points})
    except Exception as e:
        logger.exception("solve failed")
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


@app.route("/solve_by_aid", methods=["POST"])
def solve_by_aid():
    """纯协议场景：只传 aid，服务端 prehandle 生成新挑战并求解。

    与 /solve 不同，这里生成的是全新验证码挑战，坐标针对服务端拿到的图；
    适用于无浏览器/纯协议提交（拿到 sess + points 自己提交），
    浏览器内嵌验证码必须走 /solve 传当前挑战的图片。
    """
    if not _check_auth():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    aid = str(body.get("aid", "")).strip() or AID_DEFAULT
    try:
        subsid = int(body.get("subsid", 1))
    except (TypeError, ValueError):
        subsid = 1

    dyn, sess, raw = _tcaptcha_prehandle(aid, subsid)
    if not dyn:
        return jsonify({"ok": False, "error": "prehandle failed", "raw": str(raw)[:300]}), 502

    bg_url = dyn.get("bg_elem_cfg", {}).get("img_url", "")
    sprite_url = dyn.get("sprite_url", "")
    if not bg_url or not sprite_url:
        return jsonify({"ok": False, "error": "no image urls in prehandle response"}), 502

    bg = _tcaptcha_download(bg_url)
    sprite = _tcaptcha_download(sprite_url)
    if bg is None or sprite is None:
        return jsonify({"ok": False, "error": "captcha image download failed"}), 502

    captcha = _decode_bytes(bg)
    raw_sprite = _decode_bytes(sprite)
    if captcha is None or raw_sprite is None:
        return jsonify({"ok": False, "error": "captcha image decode failed"}), 502

    try:
        sprites = _split_sprite(raw_sprite, 3)
        points = solve_points(captcha, sprites)
        if len(points) != 3:
            return jsonify({"ok": False, "error": "solve_points failed", "points": points})
        return jsonify({
            "ok": True,
            "sess": sess,
            "points": points,
            "captcha_url": bg_url,
            "sprite_url": sprite_url,
        })
    except Exception as e:
        logger.exception("solve_by_aid failed")
        return jsonify({"ok": False, "error": str(e)[:500]}), 500


if __name__ == "__main__":
    port = int(os.getenv("CAPTCHA_API_PORT", "8501"))
    # 只监听本机，公网访问统一走 nginx 域名（https://cococaptcha.duckdns.org/yolo/）
    host = os.getenv("CAPTCHA_API_HOST", "127.0.0.1")
    logger.info(f"captcha-api listening on {host}:{port} "
                f"(auth: keys.json tcaptcha 卡密 {len(_load_valid_tcaptcha_keys())} 个, "
                f"env fallback={'set' if _API_TOKEN else 'none'})")
    app.run(host=host, port=port, threaded=True)
