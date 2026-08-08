#!/usr/bin/env python3
"""siamese_slide_experiment.py — 短期方案验证：密集滑窗 Siamese（不依赖 ddddocr det）

思路（验证「去掉 ddddocr 检测、直接用 Siamese 滑窗定位」能否解决漏框问题）：
  - 对每个 sprite 切片，在 captcha 大图上做「由粗到细」的密集滑窗：
      1) 多个尺度(图像金字塔) × 大步长粗扫，取 Top-K 峰值
      2) 在峰值邻域 × 小步长精修，取全局最高相似度的窗口中心 = 点击坐标
  - 模型 word_click_matcher.onnx 输入固定 (1,3,52,52)，内部 resize 归一化尺度，
    故滑窗裁任意尺寸窗口即可，尺度差异由模型吸收。

真值：outcome=pass 样本的 metadata.extra.click_coords 视为可信真值。

产物目录（不污染 captcha_samples / stats / logs/captcha_archive）：
  logs/ci_artifacts/local_debug/siamese_offline/output_slide/{viz,report.json,report.csv}

用法：
  .venv/Scripts/python.exe script/siamese_slide_experiment.py
  .venv/Scripts/python.exe script/siamese_slide_experiment.py --only a03_pass
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPERIMENT_ROOT = ROOT / "logs/ci_artifacts/local_debug/siamese_offline"
DEFAULT_MODEL = EXPERIMENT_ROOT / "models/word_click_matcher.onnx"
DEFAULT_OUT = EXPERIMENT_ROOT / "output_slide"

_HIT_THRESH = 40  # 与真值中心距离命中阈值(px)

# ---- Siamese 推理 ----
_session = None
_session_lock = threading.Lock()
_input_names = None


def get_session(model_path):
    global _session, _input_names
    if _session is not None:
        return _session
    with _session_lock:
        if _session is not None:
            return _session
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.intra_op_num_threads = max(1, min(4, (os_cpu := __import__("os").cpu_count()) or 4))
        so.inter_op_num_threads = 1
        _session = ort.InferenceSession(
            str(model_path), sess_options=so, providers=["CPUExecutionProvider"]
        )
        ins = _session.get_inputs()
        _input_names = (ins[0].name, ins[1].name)
        return _session


def _prep(img_bgr: np.ndarray) -> np.ndarray:
    resized = cv2.resize(img_bgr, (52, 52))
    arr = np.transpose(resized, (2, 0, 1)).astype(np.float32) / 255.0
    return arr[None, ...]


def _siamese_single(crop_bgr, ref_prep, sess, n0, n1):
    inp = _prep(crop_bgr)
    pred = sess.run(None, {n0: inp, n1: ref_prep})[0]
    return float(np.asarray(pred).reshape(-1)[0])


# ---- 由粗到细滑窗搜索 ----
def slide_search(cap_bgr, sprite_prep, sess, n0, n1,
                 scales=(0.85, 1.0, 1.15), win=72,
                 coarse_stride=16, refine_stride=5, topk=4,
                 refine_radius=20, exclude_centers=None, exclude_r=28):
    """返回 (best_score, (cx, cy)|None)。exclude_centers 用于多 sprite 贪心互斥。"""
    # --- 粗扫：收集所有尺度下的峰值候选 ---
    peaks = []  # (score, cx_orig, cy_orig)
    for sc in scales:
        img = cv2.resize(cap_bgr, None, fx=sc, fy=sc)
        H, W = img.shape[:2]
        for y in range(0, H - win + 1, coarse_stride):
            for x in range(0, W - win + 1, coarse_stride):
                win_img = img[y:y + win, x:x + win]
                score = _siamese_single(win_img, sprite_prep, sess, n0, n1)
                cx = (x + win / 2) / sc
                cy = (y + win / 2) / sc
                if exclude_centers:
                    if any(abs(cx - ex) < exclude_r and abs(cy - ey) < exclude_r
                           for (ex, ey) in exclude_centers):
                        continue
                peaks.append((score, cx, cy))
    if not peaks:
        return (-float("inf"), None)
    peaks.sort(reverse=True)
    top = peaks[:topk]

    # --- 精修：在 Top-K 峰值邻域小步长搜索（窗口以峰值为中心，确保可容纳） ---
    # 先用 coarse 最高峰初始化 best，避免 refine 无候选时返回 None
    best_score = top[0][0]
    best_center = (top[0][1], top[0][2])
    half = win / 2.0
    for (_, pcx, pcy) in top:
        for sc in scales:
            img = cv2.resize(cap_bgr, None, fx=sc, fy=sc)
            H, W = img.shape[:2]
            # 窗口中心应在 [pcx±refine_radius] 内 -> 窗口左上角 x 范围
            x0 = int((pcx - refine_radius) * sc - half)
            x1 = int((pcx + refine_radius) * sc - half)
            y0 = int((pcy - refine_radius) * sc - half)
            y1 = int((pcy + refine_radius) * sc - half)
            for y in range(y0, y1 + 1, refine_stride):
                if y < 0 or y + win > H:
                    continue
                for x in range(x0, x1 + 1, refine_stride):
                    if x < 0 or x + win > W:
                        continue
                    win_img = img[y:y + win, x:x + win]
                    score = _siamese_single(win_img, sprite_prep, sess, n0, n1)
                    cx = (x + half) / sc
                    cy = (y + half) / sc
                    if exclude_centers:
                        if any(abs(cx - ex) < exclude_r and abs(cy - ey) < exclude_r
                               for (ex, ey) in exclude_centers):
                            continue
                    if score > best_score:
                        best_score, best_center = score, (cx, cy)
    return (best_score, best_center)


# ---- 真值解析（与 siamese_experiment 一致） ----
def parse_truth(meta):
    extra = meta.get("extra", {}) or {}
    coords = extra.get("click_coords") or meta.get("click_coords")
    if coords:
        return [(int(c[0]), int(c[1])) for c in coords], True
    return None, False


def parse_current(meta):
    """当前方案的预测坐标（click_positions），仅作对比展示，不可信为真值。"""
    extra = meta.get("extra", {}) or {}
    pos = extra.get("click_positions") or meta.get("click_positions")
    if pos:
        return [(int(p.split(",")[0]), int(p.split(",")[1])) for p in pos]
    return None


def run_sample(sample_dir: Path, model_path, out_root: Path):
    cap = cv2.imread(str(sample_dir / "captcha.jpg"))
    if cap is None:
        return None
    sprite_paths = sorted(
        sample_dir.glob("sprite_*.jpg"),
        key=lambda p: int("".join(filter(str.isdigit, p.name)) or 0),
    )
    meta = json.loads((sample_dir / "metadata.json").read_text(encoding="utf-8"))
    truth_pts, truth_ok = parse_truth(meta)
    current_pts = parse_current(meta)

    sess = get_session(model_path)
    n0, n1 = _input_names

    results = []
    used_centers = []
    for sp in sprite_paths:
        spr = cv2.imread(str(sp))
        if spr is None:
            results.append({"sprite": Path(sp).name, "center": None,
                            "score": None, "hit": None, "dist_to_truth": None})
            continue
        ref_prep = _prep(spr)
        score, center = slide_search(
            cap, ref_prep, sess, n0, n1, exclude_centers=used_centers
        )
        if center is not None:
            used_centers.append(center)
        results.append({"sprite": Path(sp).name, "center": center,
                        "score": score, "hit": None, "dist_to_truth": None})

    # 逐 sprite 顺序命中（假设 click_coords 顺序与 sprite_1..n 一致）
    for i, r in enumerate(results):
        if r["center"] and truth_pts and i < len(truth_pts):
            tx, ty = truth_pts[i]
            d = ((r["center"][0] - tx) ** 2 + (r["center"][1] - ty) ** 2) ** 0.5
            r["dist_to_truth"] = round(d, 1)
            r["hit"] = truth_ok and d <= _HIT_THRESH
        else:
            r["hit"] = None

    # 可视化
    viz_dir = out_root / "viz"
    viz_dir.mkdir(parents=True, exist_ok=True)
    img = cap.copy()
    for r in results:
        if r["center"]:
            cx, cy = int(r["center"][0]), int(r["center"][1])
            col = (0, 200, 0) if (r["hit"] is True) else (0, 165, 255)
            cv2.circle(img, (cx, cy), 9, col, 2)
            cv2.putText(img, f"{r['score']:.3f}", (cx + 10, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)
    if truth_pts:
        for (tx, ty) in truth_pts:
            cv2.circle(img, (tx, ty), 7, (255, 0, 0), 2)
    if current_pts:
        for (cx0, cy0) in current_pts:
            cv2.drawMarker(img, (cx0, cy0), (255, 255, 0), cv2.MARKER_CROSS, 12, 2)
    cv2.imwrite(str(viz_dir / f"{sample_dir.name}.png"), img)

    return {
        "name": sample_dir.name,
        "outcome": meta.get("outcome"),
        "truth_is_reliable": truth_ok,
        "n_targets": len(sprite_paths),
        "matches": results,
        "truth_coords": truth_pts,
        "current_coords": current_pts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples-root", default=str(ROOT / "captcha_samples"))
    ap.add_argument("--model-path", default=str(DEFAULT_MODEL))
    ap.add_argument("--out-root", default=str(DEFAULT_OUT))
    ap.add_argument("--only", default=None, help="只跑某个样本目录名(如 a03_pass)")
    ap.add_argument("--limit", type=int, default=0, help="最多跑前 N 个样本")
    args = ap.parse_args()

    model_path = Path(args.model_path)
    if not model_path.is_file():
        print(f"[错误] 模型不存在: {model_path}")
        sys.exit(1)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    sample_dirs = sorted({p.parent for p in Path(args.samples_root).rglob("metadata.json")})
    if args.only:
        sample_dirs = [d for d in sample_dirs if d.name == args.only]
    if args.limit:
        sample_dirs = sample_dirs[:args.limit]

    records = []
    for sd in sample_dirs:
        print(f"处理 {sd.name} ...")
        rec = run_sample(sd, model_path, out_root)
        if rec:
            records.append(rec)

    reliable = [r for r in records if r["truth_is_reliable"]]
    n_all_hit = sum(1 for r in reliable if r["matches"] and all(m["hit"] is True for m in r["matches"]))
    n_targets_hit = sum(1 for r in records for m in r["matches"] if m["hit"] is True)
    n_targets_total = sum(1 for r in records for m in r["matches"] if m["center"] is not None)
    scores = [m["score"] for r in records for m in r["matches"] if m["score"] is not None and m["score"] > -1e9]
    summary = {
        "method": "dense sliding-window Siamese (no ddddocr det)",
        "n_samples": len(records),
        "n_with_reliable_truth": len(reliable),
        "n_samples_all_targets_hit": n_all_hit,
        "targets_hit": f"{n_targets_hit}/{n_targets_total}",
        "hit_threshold_px": _HIT_THRESH,
        "mean_matched_score": round(sum(scores) / len(scores), 4) if scores else None,
    }

    (out_root / "report.json").write_text(
        json.dumps({"summary": summary, "samples": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with open(out_root / "report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sample", "outcome", "reliable", "target", "score",
                    "center_x", "center_y", "dist_to_truth", "hit"])
        for r in records:
            for m in r["matches"]:
                w.writerow([
                    r["name"], r["outcome"], r["truth_is_reliable"], m["sprite"],
                    m["score"],
                    int(m["center"][0]) if m["center"] else "",
                    int(m["center"][1]) if m["center"] else "",
                    m["dist_to_truth"], m["hit"],
                ])

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
