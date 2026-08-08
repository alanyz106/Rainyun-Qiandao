#!/usr/bin/env python3
"""siamese_fast_experiment.py — 可落地的快速版：粗网格 Siamese + 组合分配 + 局部精修

替代 script/siamese_slide_experiment.py（暴力滑窗 ~90s/验证码）。
本脚本在「准确率」与「速度」间取平衡：
  1) 粗网格 Siamese：在大图上以较大步长(stride)做多尺度滑窗，收集每个 sprite 的 Top-K 候选
     —— 粗网格天然覆盖全图（含 ddddocr det 漏掉的城堡），保证召回
  2) 组合枚举分配：3 个 sprite 各自 Top-A 候选做组合枚举，挑「位置互异 + 总分最高」
  3) 局部精修：对每个分配到的点，在原图 ±radius 邻域小步长 Siamese 搜索，把坐标 nail 到 <10px

成本：粗网格 ~1000 窗 + 精修 ~200 窗 ≈ 1200 次 Siamese × 22ms ≈ 25-30s/验证码
（背景定时签到可接受；如需更快须上图标 YOLO 检测）。

产物目录（不污染 captcha_samples / stats / logs/captcha_archive）：
  logs/ci_artifacts/local_debug/siamese_offline/output_fast/{viz,report.json,report.csv}

用法：
  .venv/Scripts/python.exe script/siamese_fast_experiment.py
  .venv/Scripts/python.exe script/siamese_fast_experiment.py --only a03_pass
"""
from __future__ import annotations

import argparse
import csv
import itertools
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
DEFAULT_OUT = EXPERIMENT_ROOT / "output_fast"

_HIT_THRESH = 40
_COARSE_STRIDE = 44
_COARSE_WIN = 72
_COARSE_SCALES = (0.85, 1.0, 1.15)
_COARSE_TOPK = 15
_REFINE_RADIUS = 40
_REFINE_STRIDE = 10
_REFINE_WIN = 72
_ASSIGN_TOPA = 15
_ASSIGN_MIN_DIST = 30

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
        import os

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
    resized = cv2.resize(img_bgr, (52, 52))
    arr = np.transpose(resized, (2, 0, 1)).astype(np.float32) / 255.0
    return arr[None, ...]


def _siamese_single(crop_bgr, ref_prep, sess, n0, n1):
    inp = _prep(crop_bgr)
    pred = sess.run(None, {n0: inp, n1: ref_prep})[0]
    return float(np.asarray(pred).reshape(-1)[0])


# ---- 步骤1：粗网格 Siamese 候选 ----
def sprite_candidates(cap_bgr, sprite_bgr, sess, n0, n1,
                      scales=_COARSE_SCALES, stride=_COARSE_STRIDE,
                      win=_COARSE_WIN, topk=_COARSE_TOPK):
    ref_prep = _prep(sprite_bgr)
    cands = []  # (score, cx_orig, cy_orig, crop)
    for sc in scales:
        img = cv2.resize(cap_bgr, None, fx=sc, fy=sc)
        H, W = img.shape[:2]
        for y in range(0, H - win + 1, stride):
            for x in range(0, W - win + 1, stride):
                crop = img[y:y + win, x:x + win]
                s = _siamese_single(crop, ref_prep, sess, n0, n1)
                cands.append((s, (x + win / 2) / sc, (y + win / 2) / sc, crop))
    cands.sort(key=lambda t: t[0], reverse=True)
    dedup = []
    for s, cx, cy, crop in cands:
        if any(abs(cx - d[1]) < 16 and abs(cy - d[2]) < 16 for d in dedup):
            continue
        dedup.append((s, cx, cy, crop))
        if len(dedup) >= topk * 2:
            break
    return [{"score": s, "center": (cx, cy), "crop": crop} for s, cx, cy, crop in dedup[:topk]]


# ---- 步骤3：局部精修 ----
def refine_point(cap_bgr, sprite_bgr, center, sess, n0, n1,
                 radius=_REFINE_RADIUS, stride=_REFINE_STRIDE, win=_REFINE_WIN):
    ref_prep = _prep(sprite_bgr)
    cx, cy = center
    best_s, best_c = -float("inf"), center
    half = win / 2.0
    x0, x1 = int(cx - radius - half), int(cx + radius - half)
    y0, y1 = int(cy - radius - half), int(cy + radius - half)
    for y in range(y0, y1 + 1, stride):
        if y < 0 or y + win > cap_bgr.shape[0]:
            continue
        for x in range(x0, x1 + 1, stride):
            if x < 0 or x + win > cap_bgr.shape[1]:
                continue
            crop = cap_bgr[y:y + win, x:x + win]
            s = _siamese_single(crop, ref_prep, sess, n0, n1)
            if s > best_s:
                best_s, best_c = s, (x + half, y + half)
    return best_s, best_c


# ---- 步骤2：组合枚举最优分配（替代匈牙利，零依赖） ----
def assign(sprite_cands, dist_thresh=_ASSIGN_MIN_DIST, topa=_ASSIGN_TOPA):
    choices = [c[:topa] for c in sprite_cands]
    if any(len(c) == 0 for c in choices):
        return None
    best = None
    for combo in itertools.product(*choices):
        centers = [c["center"] for c in combo]
        ok = True
        for i in range(len(centers)):
            for j in range(i + 1, len(centers)):
                dx = centers[i][0] - centers[j][0]
                dy = centers[i][1] - centers[j][1]
                if (dx * dx + dy * dy) ** 0.5 < dist_thresh:
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            continue
        total = sum(c["score"] for c in combo)
        if best is None or total > best[0]:
            best = (total, combo)
    return best


def parse_truth(meta):
    extra = meta.get("extra", {}) or {}
    coords = extra.get("click_coords") or meta.get("click_coords")
    if coords:
        return [(int(c[0]), int(c[1])) for c in coords], True
    return None, False


def parse_current(meta):
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

    sprite_data = []  # (sprite_bgr, coarse_candidates)
    for sp in sprite_paths:
        spr = cv2.imread(str(sp))
        if spr is None:
            sprite_data.append((None, []))
        else:
            sprite_data.append((spr, sprite_candidates(cap, spr, sess, n0, n1)))

    asg = assign([c for _, c in sprite_data])
    results = []
    if asg:
        _, combo = asg
        for i, cand in enumerate(combo):
            spr = sprite_data[i][0]
            if spr is not None:
                s, center = refine_point(cap, spr, cand["center"], sess, n0, n1)
            else:
                s, center = cand["score"], cand["center"]
            cx, cy = center
            results.append({"sprite": Path(sprite_paths[i]).name,
                            "center": [cx, cy], "score": s,
                            "hit": None, "dist_to_truth": None})
    else:
        for i, (spr, c) in enumerate(sprite_data):
            if c:
                cx, cy = c[0]["center"]
                results.append({"sprite": Path(sprite_paths[i]).name,
                                "center": [cx, cy], "score": c[0]["score"],
                                "hit": None, "dist_to_truth": None})
            else:
                results.append({"sprite": Path(sprite_paths[i]).name,
                                "center": None, "score": None,
                                "hit": None, "dist_to_truth": None})

    for i, r in enumerate(results):
        if r["center"] and truth_pts and i < len(truth_pts):
            tx, ty = truth_pts[i]
            d = ((r["center"][0] - tx) ** 2 + (r["center"][1] - ty) ** 2) ** 0.5
            r["dist_to_truth"] = round(d, 1)
            r["hit"] = truth_ok and d <= _HIT_THRESH

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
    ap.add_argument("--only", default=None)
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
        "method": "coarse-grid Siamese + combinatorial assign + local refine",
        "n_samples": len(records),
        "n_with_reliable_truth": len(reliable),
        "n_samples_all_targets_hit": n_all_hit,
        "targets_hit_on_reliable": f"{n_targets_hit}/{n_targets_total}",
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
