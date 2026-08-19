"""YOLO 13 类图标检测模块（自训 yolov8s → ONNX，生产推理）。

替代 ddddocr det + 多特征打分 + Siamese 的复杂链路：
- 一次 ONNX 前向直接输出 13 类候选框（类别 = 匹配结果）
- 跨类 NMS：同位置不同类框只保留置信度最高者（消除 2/5 双框）
- sprite 定类：自训孪生编码器（siamese_encoder.onnx）提特征，与 13 类质心余弦匹配
"""

import logging
import os
import threading

import cv2
import numpy as np

logger = logging.getLogger(__name__)

CLASSES = ["temple", "5", "castle", "map", "2", "1", "phone", "cat", "0", "9", "8", "7", "4"]
CLASS_TO_ID = {c: i for i, c in enumerate(CLASSES)}
ID_TO_CLASS = {i: c for i, c in enumerate(CLASSES)}

# unique 图标模板 → 13 类映射（用户确认 2026-08-18）
ICON_TO_CLASS = {
    "icon_001": "temple",
    "icon_002": "5",
    "icon_003": "castle",
    "icon_005": "map",
    "icon_006": "2",
    "icon_007": "1",
    "icon_008": "phone",
    "icon_010": "cat",
    "icon_011": "0",
    "icon_013": "9",
    "icon_015": "8",
    "icon_016": "7",
    "icon_021": "4",
}

_DEFAULT_MODEL = os.path.join(os.path.dirname(__file__), "models", "yolo_v4.onnx")
_SIAMES_ENCODER = os.path.join(os.path.dirname(__file__), "models", "siamese_encoder.onnx")
_SIAMES_REFS = os.path.join(os.path.dirname(__file__), "models", "siamese_refs.npz")
_IMGSZ = 640
_NMS_IOU = 0.45
_CROSS_CLASS_IOU = 0.3
_MIN_CONF = 0.25
_SPRITE_INPUT = 52
_SPRITE_MIN_SCORE = 0.4
_KNN_K = 5

_session = None
_session_lock = threading.Lock()
_inference_lock = threading.Lock()
_siames_session = None
_siames_lock = threading.Lock()
_siames_refs = None


def _get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                import onnxruntime as ort
                _session = ort.InferenceSession(
                    _DEFAULT_MODEL, providers=["CPUExecutionProvider"]
                )
    return _session


def get_inference_lock():
    return _inference_lock


def _letterbox(img, size=640, color=114):
    h0, w0 = img.shape[:2]
    r = size / max(h0, w0)
    nw, nh = int(round(w0 * r)), int(round(h0 * r))
    resized = cv2.resize(img, (nw, nh))
    canvas = np.full((size, size, 3), color, np.uint8)
    dx, dy = (size - nw) // 2, (size - nh) // 2
    canvas[dy:dy + nh, dx:dx + nw] = resized
    return canvas, r, dx, dy


def _iou(b1, b2):
    ix1, iy1 = max(b1[0], b2[0]), max(b1[1], b2[1])
    ix2, iy2 = min(b1[2], b2[2]), min(b1[3], b2[3])
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = (b1[2] - b1[0]) * (b1[3] - b1[1]) + (b2[2] - b2[0]) * (b2[3] - b2[1]) - inter
    return inter / union if union else 0


def _nms(boxes, iou_thr=_NMS_IOU):
    """boxes: list of (cls, box, conf)；同类 NMS。"""
    kept = []
    ordered = sorted(boxes, key=lambda t: -t[2])
    for c, b, s in ordered:
        dup = False
        for c2, b2, s2 in kept:
            if c == c2 and _iou(b, b2) > iou_thr:
                dup = True
                break
        if not dup:
            kept.append((c, b, s))
    return kept


def _cross_class_nms(boxes, iou_thr=_CROSS_CLASS_IOU):
    """不同类别的框若高度重叠，只保留置信度最高的（2/5 双框消解）。"""
    keep = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(keep)):
            for j in range(i + 1, len(keep)):
                a, b = keep[i], keep[j]
                if a[0] == b[0]:
                    continue
                if _iou(a[1], b[1]) > iou_thr:
                    drop = j if a[2] >= b[2] else i
                    keep.pop(drop)
                    changed = True
                    break
            if changed:
                break
    return keep


def detect(img_bgr, conf_thr=_MIN_CONF, img_path=None):
    """YOLO 检测大图，返回按置信度降序的检测框。

    :param img_bgr: BGR 大图（672x480 验证码）
    :return: list[(cls_id, (x1,y1,x2,y2), conf)]，坐标映射回原图
    """
    sess = _get_session()
    canvas, r, dx, dy = _letterbox(img_bgr, _IMGSZ)
    blob = canvas[:, :, ::-1].astype(np.float32) / 255.0
    blob = blob.transpose(2, 0, 1)[None]
    with get_inference_lock():
        preds = sess.run(None, {"images": blob})[0]
    preds = preds[0].T  # (8400, 84)

    dets = []
    for row in preds:
        conf = float(row[4:].max())
        if conf < conf_thr:
            continue
        cls = int(row[4:].argmax())
        cx, cy, bw, bh = row[:4]
        x1, y1 = (cx - bw / 2 - dx) / r, (cy - bh / 2 - dy) / r
        x2, y2 = (cx + bw / 2 - dx) / r, (cy + bh / 2 - dy) / r
        dets.append((cls, (x1, y1, x2, y2), conf))
    dets = _nms(dets)
    dets = _cross_class_nms(dets)
    dets.sort(key=lambda t: -t[2])
    return dets


def detect_file(image_path, conf_thr=_MIN_CONF):
    img = cv2.imread(image_path)
    if img is None:
        return []
    return detect(img, conf_thr=conf_thr)


def _sprite_prep(img_bgr, size=_SPRITE_INPUT):
    """sprite 图 -> (1,1,52,52) float32，白底黑线反相（线为亮）。"""
    if len(img_bgr.shape) == 3:
        g = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    else:
        g = img_bgr
    g = cv2.resize(g, (size, size), interpolation=cv2.INTER_LINEAR)
    arr = g.astype(np.float32) / 255.0
    arr = 1.0 - arr
    return arr[None, None, :, :]


def _get_siames():
    global _siames_session, _siames_refs
    if _siames_session is None:
        with _siames_lock:
            if _siames_session is None:
                import onnxruntime as ort
                if not os.path.exists(_SIAMES_ENCODER) or not os.path.exists(_SIAMES_REFS):
                    raise FileNotFoundError(
                        f"siamese 模型缺失: {_SIAMES_ENCODER} / {_SIAMES_REFS}"
                    )
                _siames_session = ort.InferenceSession(
                    _SIAMES_ENCODER, providers=["CPUExecutionProvider"]
                )
                data = np.load(_SIAMES_REFS)
                embs = data["embs"].astype(np.float32)
                embs /= (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9)
                _siames_refs = (embs, data["cls"])  # (N,256) L2归一化, (N,)
    return _siames_session


def _embed_sprite(img_bgr):
    sess = _get_siames()
    blob = _sprite_prep(img_bgr)
    emb = sess.run(None, {"image": blob})[0][0].astype(np.float32)
    return emb / (np.linalg.norm(emb) + 1e-9)


def classify_sprite(img_bgr, conf_thr=_SPRITE_MIN_SCORE):
    """sprite 定类：孪生编码器提特征，与生产域参考库 KNN 投票取 top1。

    返回 (class_name, score)；无法定类返回 (None, 0.0)。
    """
    if img_bgr is None:
        return None, 0.0
    try:
        emb = _embed_sprite(img_bgr)
    except FileNotFoundError:
        logger.warning("siamese 模型缺失，sprite 定类不可用")
        return None, 0.0
    refs, ref_cls = _siames_refs
    sims = refs @ emb
    order = np.argsort(-sims)[:_KNN_K]
    votes = {}
    for idx in order:
        c = int(ref_cls[idx])
        votes[c] = votes.get(c, 0.0) + float(sims[idx])
    best_id = max(votes, key=votes.get)
    top2 = sorted(votes.values(), reverse=True)
    gap = top2[0] - (top2[1] if len(top2) > 1 else 0.0)
    best_score = float(sims[order[0]])
    if best_score < conf_thr:
        return None, 0.0
    return CLASSES[best_id], best_score


def classify_sprite_path(path, conf_thr=_SPRITE_MIN_SCORE):
    img = cv2.imread(path)
    if img is None:
        return None, 0.0
    return classify_sprite(img, conf_thr)


def solve_points(captcha_img, sprite_imgs, conf_thr=_MIN_CONF):
    """端到端：大图检测 + sprite 定类 + 类别对齐，输出点击坐标。

    :param captcha_img: BGR 大图
    :param sprite_imgs: list[BGR] 3 张提示条 sprite 切片
    :return: list[str] "x,y" 点击坐标（按 sprite 顺序），不足 3 个则失败
    """
    dets = detect(captcha_img, conf_thr=conf_thr)
    by_class = {}
    for cls, box, conf in dets:
        by_class.setdefault(CLASSES[cls], []).append((box, conf))

    points = []
    for i, spr in enumerate(sprite_imgs):
        cls_name, conf = classify_sprite(spr)
        if cls_name is None:
            logger.info(f"sprite {i + 1} 定类失败")
            return []
        candidates = by_class.get(cls_name, [])
        if not candidates:
            logger.info(f"sprite {i + 1} 类别 {cls_name} 在大图中未检出")
            return []
        box, box_conf = max(candidates, key=lambda t: t[1])
        cx, cy = int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2)
        points.append(f"{cx},{cy}")
        logger.info(
            f"sprite {i + 1} 定类={cls_name}({conf:.2f}) -> 大图框 conf={box_conf:.2f} ({cx},{cy})"
        )
    return points if len(points) == 3 else []