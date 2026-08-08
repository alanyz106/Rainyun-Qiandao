#!/usr/bin/env python3
"""siamese_experiment.py — 离线实验：用 crackTCaptcha 的 Siamese 模型评估
「图标 / 数字 点选」验证码的本地匹配准确率，并与现有 ddddocr 方案对比。

核心思路（借鉴 crackTCaptcha.word_ocr）：
  - 候选框提取仍用 ddddocr det（与现有方案候选池一致，控制变量）
  - 匹配打分从「OCR + 形状 + 模板 + 边缘」手工特征 换成 Siamese 视觉相似度
  - 每个 sprite 切片与每个候选框 crop 过 Siamese，贪心选最高分且未使用的框

真值来源：
  - outcome=pass 样本：metadata.extra.click_coords 视为可信真值
  - outcome=retry 样本：metadata.extra.click_positions 仅作「当前方案坐标」展示对比

产物目录（不污染 captcha_samples / stats / logs/captcha_archive）：
  logs/ci_artifacts/local_debug/siamese_offline/output/{viz,report.json,report.csv}

用法：
  .venv/Scripts/python.exe script/siamese_experiment.py
  .venv/Scripts/python.exe script/siamese_experiment.py \
      --samples-root captcha_samples \
      --model-path logs/ci_artifacts/local_debug/siamese_offline/models/word_click_matcher.onnx \
      --out-root   logs/ci_artifacts/local_debug/siamese_offline/output
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPERIMENT_ROOT = ROOT / "logs/ci_artifacts/local_debug/siamese_offline"
DEFAULT_MODEL = EXPERIMENT_ROOT / "models/word_click_matcher.onnx"
DEFAULT_OUT = EXPERIMENT_ROOT / "output"

_SIAMESE_INPUT = (52, 52)
_HIT_THRESH = 40  # 与真值中心距离的命中阈值(px)

# ---- Siamese 推理（改编自 crackTCaptcha.solvers.word_ocr，去掉字体渲染） ----
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
        so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 4))
        so.inter_op_num_threads = 1
        _session = ort.InferenceSession(
            str(model_path), sess_options=so, providers=["CPUExecutionProvider"]
        )
        ins = _session.get_inputs()
        _input_names = (ins[0].name, ins[1].name)
        return _session


def _prep(img_bgr: np.ndarray) -> np.ndarray:
    """BGR 图 -> (1,3,52,52) float32 [0,1]，与 crackTCaptcha 预处理一致。"""
    resized = cv2.resize(img_bgr, _SIAMESE_INPUT)
    arr = np.transpose(resized, (2, 0, 1)).astype(np.float32) / 255.0
    return arr[None, ...]


def siamese_scores(crops, ref_bgr, model_path):
    """返回每个 crop 相对 ref 的相似度分数（越高越像）。

    注：0.3.1 的 word_click_matcher.onnx 固定 batch=1，不支持动态批量，
    故逐对推理（单对 crop+ref）。
    """
    sess = get_session(model_path)
    n0, n1 = _input_names
    if not crops:
        return []
    ref = _prep(ref_bgr)
    out = []
    for c in crops:
        inp = _prep(c)
        pred = sess.run(None, {n0: inp, n1: ref})[0]
        out.append(float(np.asarray(pred).reshape(-1)[0]))
    return out


# ---- 候选框检测（ddddocr det，与现有方案一致） ----
_det = None
_det_lock = threading.Lock()


def detect_candidates(captcha_bgr):
    global _det
    if _det is None:
        with _det_lock:
            if _det is None:
                import ddddocr

                _det = ddddocr.DdddOcr(det=True, show_ad=False)
    ok, buf = cv2.imencode(".png", captcha_bgr)
    if not ok:
        return []
    bboxes = _det.detection(buf.tobytes())
    out = []
    for b in bboxes:
        x1, y1, x2, y2 = [int(v) for v in b[:4]]
        w, h = x2 - x1, y2 - y1
        if w < 8 or h < 8 or w > 220 or h > 220:
            continue
        out.append((x1, y1, x2, y2))
    return out[:80]


def match_sprites(captcha_bgr, sprite_paths, model_path):
    bboxes = detect_candidates(captcha_bgr)
    crops = [captcha_bgr[y1:y2, x1:x2] for (x1, y1, x2, y2) in bboxes]
    centers = [((x1 + x2) // 2, (y1 + y2) // 2) for (x1, y1, x2, y2) in bboxes]
    results = []
    used = set()
    for sp in sprite_paths:
        spr = cv2.imread(str(sp))
        if spr is None:
            results.append({"sprite": Path(sp).name, "best_idx": -1, "center": None,
                            "score": None, "top2_gap": None})
            continue
        scores = siamese_scores(crops, spr, model_path)
        if not scores:
            results.append({"sprite": Path(sp).name, "best_idx": -1, "center": None,
                            "score": None, "top2_gap": None})
            continue
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        pick = next((i for i in order if i not in used), order[0])
        used.add(pick)
        results.append({
            "sprite": Path(sp).name,
            "best_idx": pick,
            "center": list(centers[pick]),
            "score": scores[pick],
            "top2_gap": (scores[order[0]] - scores[order[1]]) if len(scores) > 1 else 0.0,
        })
    return bboxes, results


# ---- 真值解析 ----
def parse_truth(meta):
    extra = meta.get("extra", {}) or {}
    coords = extra.get("click_coords") or meta.get("click_coords")
    if coords:
        pts = [(int(c[0]), int(c[1])) for c in coords]
        return pts, True  # 可信真值
    pos = extra.get("click_positions") or meta.get("click_positions")
    if pos:
        pts = [(int(p.split(",")[0]), int(p.split(",")[1])) for p in pos]
        return pts, False  # 当前方案坐标，仅对比用
    return None, False


def _nearest_pair_avg_dist(siamese_centers, truth_pts):
    """贪心最近配对：每个真值点匹配最近未用的 Siamese 中心，返回平均距离。"""
    if not siamese_centers or not truth_pts:
        return None
    used = set()
    total = 0.0
    for (tx, ty) in truth_pts:
        best_i, best_d = -1, float("inf")
        for i, (cx, cy) in enumerate(siamese_centers):
            if i in used:
                continue
            d = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
            if d < best_d:
                best_d, best_i = d, i
        if best_i >= 0:
            used.add(best_i)
            total += best_d
    return round(total / max(1, len(truth_pts)), 1)


# ---- 可视化 ----
def visualize(captcha_bgr, bboxes, results, truth_pts, out_path):
    img = captcha_bgr.copy()
    for (x1, y1, x2, y2) in bboxes:
        cv2.rectangle(img, (x1, y1), (x2, y2), (190, 190, 190), 1)
    for r in results:
        if r["best_idx"] < 0 or r["center"] is None:
            continue
        x1, y1, x2, y2 = bboxes[r["best_idx"]]
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(img, f"{r['score']:.3f}", (x1, max(0, y1 - 4)),
                     cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 0), 1)
    if truth_pts:
        for (tx, ty) in truth_pts:
            cv2.circle(img, (tx, ty), 7, (255, 0, 0), 2)
    cv2.imwrite(str(out_path), img)


# ---- 单个样本 ----
def run_sample(sample_dir: Path, model_path, out_root: Path):
    cap = cv2.imread(str(sample_dir / "captcha.jpg"))
    if cap is None:
        return None
    sprite_paths = sorted(
        sample_dir.glob("sprite_*.jpg"),
        key=lambda p: int("".join(filter(str.isdigit, p.name)) or 0),
    )
    bboxes, results = match_sprites(cap, sprite_paths, model_path)

    meta = json.loads((sample_dir / "metadata.json").read_text(encoding="utf-8"))
    truth_pts, truth_ok = parse_truth(meta)

    siamese_centers = [r["center"] for r in results if r["center"]]
    pair_avg = _nearest_pair_avg_dist(siamese_centers, truth_pts) if truth_pts else None

    # 逐 sprite 顺序命中（假设 click_coords 顺序与 sprite_1..n 一致）
    for i, r in enumerate(results):
        if r["center"] and truth_pts and i < len(truth_pts):
            tx, ty = truth_pts[i]
            d = ((r["center"][0] - tx) ** 2 + (r["center"][1] - ty) ** 2) ** 0.5
            r["dist_to_truth"] = round(d, 1)
            r["hit"] = truth_ok and d <= _HIT_THRESH
        else:
            r["dist_to_truth"] = None
            r["hit"] = None

    viz_dir = out_root / "viz"
    viz_dir.mkdir(parents=True, exist_ok=True)
    visualize(cap, bboxes, results, truth_pts, viz_dir / f"{sample_dir.name}.png")

    return {
        "name": sample_dir.name,
        "outcome": meta.get("outcome"),
        "truth_is_reliable": truth_ok,
        "n_candidates": len(bboxes),
        "candidate_centers": [[(x1 + x2) // 2, (y1 + y2) // 2] for (x1, y1, x2, y2) in bboxes],
        "pair_avg_dist_to_truth": pair_avg,
        "matches": results,
        "current_coords": truth_pts,  # 真值或当前方案坐标（用于对比）
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples-root", default=str(ROOT / "captcha_samples"))
    ap.add_argument("--model-path", default=str(DEFAULT_MODEL))
    ap.add_argument("--out-root", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    samples_root = Path(args.samples_root)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    model_path = Path(args.model_path)
    if not model_path.is_file():
        print(f"[错误] 模型文件不存在: {model_path}")
        sys.exit(1)

    sample_dirs = sorted({p.parent for p in samples_root.rglob("metadata.json")})
    records = []
    for sd in sample_dirs:
        print(f"处理 {sd.relative_to(samples_root)} ...")
        rec = run_sample(sd, model_path, out_root)
        if rec:
            records.append(rec)

    # 汇总
    reliable = [r for r in records if r["truth_is_reliable"]]
    n_pass_hit = sum(1 for r in reliable if all(m["hit"] for m in r["matches"] if m["hit"] is not None))
    gaps = [m["top2_gap"] for r in records for m in r["matches"] if m["top2_gap"] is not None]
    summary = {
        "n_samples": len(records),
        "n_with_reliable_truth": len(reliable),
        "n_samples_all_hit": n_pass_hit,
        "mean_top2_gap": round(sum(gaps) / len(gaps), 4) if gaps else None,
        "hit_threshold_px": _HIT_THRESH,
    }

    (out_root / "report.json").write_text(
        json.dumps({"summary": summary, "samples": records}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with open(out_root / "report.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sample", "outcome", "reliable_truth", "n_candidates",
                    "pair_avg_dist", "sprite", "matched_idx", "score", "top2_gap",
                    "center_x", "center_y", "dist_to_truth", "hit"])
        for r in records:
            for m in r["matches"]:
                w.writerow([
                    r["name"], r["outcome"], r["truth_is_reliable"], r["n_candidates"],
                    r["pair_avg_dist_to_truth"], m["sprite"], m["best_idx"], m["score"],
                    m["top2_gap"],
                    m["center"][0] if m["center"] else "",
                    m["center"][1] if m["center"] else "",
                    m["dist_to_truth"], m["hit"],
                ])

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
