"""腾讯验证码 YOLO 定类 API 服务端（部署在 huasuanyun 服务器）。

端点：
- GET  /health           健康检查，返回模型加载状态
- POST /solve            multipart 上传 captcha + sprite，返回 3 个点击坐标

请求：
  POST /solve
  headers: X-API-Token: <token>（可选，设了 CAPTCHA_API_TOKEN 则必填）
  files:  captcha  -> 大图 (jpg/png)
          sprite   -> 整条提示条 (170x50)，服务端竖切 3 段

响应：
  {"ok": true, "points": ["x,y","x,y","x,y"], "classes": [...], "scores": [...]}
  {"ok": false, "error": "..."}
"""

import logging
import os

import cv2
import numpy as np
from flask import Flask, jsonify, request

from _yolo import solve_points

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("captcha-api")

app = Flask(__name__)

_API_TOKEN = os.getenv("CAPTCHA_API_TOKEN", "").strip()


def _decode_image(file_storage):
    """读取上传文件为 BGR ndarray。"""
    data = file_storage.read()
    if not data:
        return None
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


def _split_sprite(raw_sprite, n=3):
    """整条 sprite 竖切 n 段（与 _tencent.py 生产切片方式一致）。"""
    w = raw_sprite.shape[1]
    return [raw_sprite[:, w // n * i: w // n * (i + 1)] for i in range(n)]


def _check_auth():
    if not _API_TOKEN:
        return True
    token = request.headers.get("X-API-Token", "")
    return token == _API_TOKEN


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


if __name__ == "__main__":
    port = int(os.getenv("CAPTCHA_API_PORT", "8501"))
    logger.info(f"captcha-api listening on 0.0.0.0:{port} (token={'set' if _API_TOKEN else 'none'})")
    app.run(host="0.0.0.0", port=port, threaded=True)
