"""Siamese 图标匹配模块。
用 crack-tcaptcha 的 word_click_matcher.onnx 对候选框 crop 和 sprite 做视觉相似度评分，
替代不可靠的像素模板匹配。

模型获取（按优先级）：
1. 已安装的 crack-tcaptcha 包内（pip install crack-tcaptcha[word-click]）
2. 本地 debug 目录
3. 自动从 PyPI 下载 crack-tcaptcha wheel 并提取模型（GitHub Actions 兼容）

用法：
    from rainyun.captcha._siamese import match_sprites_to_boxes

    scores = match_sprites_to_boxes(captcha_bgr, bboxes, sprite_img_list)
    # scores[j][k] = sprite_j 与 bboxes[k] 的相似度 (0~1, 越高越像)
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_SIAMESE_INPUT = (52, 52)
_sessions = {}  # model_path -> session, 支持多路径
_sess_lock = threading.Lock()
_inp_names_cache: dict[str, tuple[str, str]] = {}

_MODEL_CACHE_DIR = Path.home() / ".cache" / "rainyun_captcha"


def _find_model() -> Path:
    """查找或自动下载 word_click_matcher.onnx"""
    model_name = "word_click_matcher.onnx"

    # 1. 已安装的 crack-tcaptcha 包
    try:
        import crack_tcaptcha
        pkg_dir = Path(crack_tcaptcha.__file__).parent
        p = pkg_dir / "solvers" / "models" / model_name
        if p.exists():
            return p
    except ImportError:
        pass

    # 2. site-packages 直接路径（不依赖 import）
    site_paths = []
    for sp in sys.path:
        if "site-packages" in sp or "dist-packages" in sp:
            p = Path(sp) / "crack_tcaptcha" / "solvers" / "models" / model_name
            if p.exists():
                return p

    # 3. 本地 debug 目录
    local = Path("logs/ci_artifacts/local_debug/siamese_offline/models") / model_name
    if local.exists():
        return local

    # 4. 缓存目录（之前下载过的）
    cached = _MODEL_CACHE_DIR / model_name
    if cached.exists():
        return cached

    # 5. 自动从 PyPI 下载
    logger.info("正在从 PyPI 下载 Siamese 模型（首次启动约需 30s）...")
    try:
        _MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [sys.executable, "-m", "pip", "download", "--no-deps",
                 "crack-tcaptcha==0.3.1", "-d", tmp],
                check=True, capture_output=True, timeout=120,
            )
            wheels = list(Path(tmp).glob("crack_tcaptcha-*.whl"))
            if not wheels:
                raise FileNotFoundError("crack-tcaptcha wheel not found")
            with zipfile.ZipFile(wheels[0]) as zf:
                for name in zf.namelist():
                    if name.endswith(model_name):
                        zf.extract(name, tmp)
                        src = Path(tmp) / name
                        shutil.copy2(src, cached)
                        logger.info(f"模型已下载并缓存到 {cached}")
                        return cached
    except Exception as e:
        logger.warning(f"自动下载模型失败: {e}")

    raise FileNotFoundError(
        f"{model_name} not found. "
        "Run: pip install crack-tcaptcha[word-click]"
    )


def _get_sessions(model_path: Path):
    """按模型路径获取或创建 ONNX session"""
    mp = str(model_path)
    if mp in _sessions:
        return _sessions[mp]
    with _sess_lock:
        if mp in _sessions:
            return _sessions[mp]
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 4))
        so.inter_op_num_threads = 1
        sess = ort.InferenceSession(mp, sess_options=so, providers=["CPUExecutionProvider"])
        ins = sess.get_inputs()
        _inp_names_cache[mp] = (ins[0].name, ins[1].name)
        _sessions[mp] = sess
        return sess


def _prep(img_bgr: np.ndarray) -> np.ndarray:
    """BGR (H,W,3) -> (1,3,52,52) float32 [0,1]"""
    resized = cv2.resize(img_bgr, _SIAMESE_INPUT)
    arr = np.transpose(resized, (2, 0, 1)).astype(np.float32) / 255.0
    return arr[None, ...]


def _siamese_pair(crop_bgr: np.ndarray, ref_bgr: np.ndarray) -> float:
    """单对相似度（0~1）"""
    model_path = _find_model()
    sess = _get_sessions(model_path)
    mp = str(model_path)
    n0, n1 = _inp_names_cache[mp]
    pred = sess.run(None, {n0: _prep(crop_bgr), n1: _prep(ref_bgr)})[0]
    return float(np.asarray(pred).reshape(-1)[0])


def match_sprites_to_boxes(
    captcha_bgr: np.ndarray,
    bboxes: Sequence[tuple[int, int, int, int]],
    sprite_imgs: Sequence[np.ndarray],
) -> np.ndarray:
    """对每个 sprite 和每个 bbox 的 crop 计算 Siamese 相似度。

    Args:
        captcha_bgr: 验证码大图 (H,W,3)
        bboxes: [(x1,y1,x2,y2), ...] 候选框列表
        sprite_imgs: [sprite1, sprite2, sprite3] BGR 图

    Returns:
        score_matrix: shape (len(sprite_imgs), len(bboxes)), 值域 0~1
    """
    crops = [captcha_bgr[y1:y2, x1:x2] for (x1, y1, x2, y2) in bboxes]
    scores = np.zeros((len(sprite_imgs), len(bboxes)), dtype=np.float32)
    for j, spr in enumerate(sprite_imgs):
        for k, cr in enumerate(crops):
            scores[j, k] = _siamese_pair(cr, spr)
    return scores


def hungry_assign(score_matrix: np.ndarray) -> tuple[list[int], float]:
    """匈牙利/排列分配：3 sprite × N 候选，找最优一对一组合。

    Returns:
        (assignment, total_score)
        assignment: [spec_idx_for_sprite_0, spec_idx_for_sprite_1, spec_idx_for_sprite_2]
    """
    N = score_matrix.shape[1]
    if N < 3:
        return [], 0.0
    import itertools

    best = None
    best_total = -np.inf
    for perm in itertools.permutations(range(N), 3):
        t = float(score_matrix[0][perm[0]] + score_matrix[1][perm[1]] + score_matrix[2][perm[2]])
        if t > best_total:
            best_total = t
            best = list(perm)
    if best is None:
        return [], 0.0
    return best, best_total
