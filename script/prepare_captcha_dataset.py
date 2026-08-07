#!/usr/bin/env python3
"""
准备验证码训练数据集。

扫描仓库本地的 `captcha_samples/` 目录（含已解压目录和 `archives/*.zip`），
把所有样本汇总到一个输出目录，并生成清单 CSV，方便半年后批量训练。

用法：
    .venv/Scripts/python.exe script/prepare_captcha_dataset.py
    .venv/Scripts/python.exe script/prepare_captcha_dataset.py --output dataset/captcha_v1
    .venv/Scripts/python.exe script/prepare_captcha_dataset.py --extract-archives --output dataset/captcha_v1
"""
import argparse
import csv
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_ROOT = ROOT / "captcha_samples"
ARCHIVES_DIR = SAMPLES_ROOT / "archives"


def extract_archives(archives_dir: Path, samples_root: Path):
    """把所有月度 zip 解压到临时/输出目录，并返回解压出的路径列表。"""
    extracted = []
    if not archives_dir.exists():
        return extracted
    for zf in sorted(archives_dir.glob("*.zip")):
        print(f"解压: {zf.name}")
        with zipfile.ZipFile(zf, "r") as z:
            z.extractall(samples_root)
        extracted.append(zf.stem)
    return extracted


def collect_samples(samples_root: Path):
    """
    收集所有 attempt 目录下的原始样本信息。

    目录结构预期：
        captcha_samples/<YYYY-MM>/<YYYY-MM-DD>/<run_number>/<账号>/a<attempt_index>_<outcome>/
    """
    records = []
    for month_dir in sorted(samples_root.iterdir()):
        if not month_dir.is_dir() or month_dir.name == "archives":
            continue
        for day_dir in sorted(month_dir.iterdir()):
            if not day_dir.is_dir():
                continue
            for run_dir in sorted(day_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                for account_dir in sorted(run_dir.iterdir()):
                    if not account_dir.is_dir():
                        continue
                    for attempt_dir in sorted(account_dir.iterdir()):
                        if not attempt_dir.is_dir():
                            continue
                        meta_file = attempt_dir / "metadata.json"
                        if not meta_file.exists():
                            continue
                        try:
                            meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        except Exception:
                            continue
                        outcome = meta.get("outcome", "unknown")
                        extra = meta.get("extra", {})
                        click_coords = extra.get("click_coords")
                        click_positions = extra.get("click_positions")
                        captured_at = meta.get("captured_at", "")

                        # 统一真值坐标格式：只有 outcome == "pass" 时才视为真值
                        #   - 2captcha 通过：extra.click_coords（元组/列表）
                        #   - 本地通过：extra.click_positions（"x,y" 字符串或 [x,y] 列表）
                        truth_coords = None
                        truth_source = None
                        predicted_coords = None
                        if outcome == "pass":
                            if click_coords and len(click_coords) > 0:
                                truth_coords = click_coords
                                truth_source = "2captcha" if truth_source is None else truth_source
                            elif click_positions and len(click_positions) > 0:
                                parsed = []
                                for p in click_positions:
                                    if isinstance(p, str):
                                        try:
                                            x, y = map(int, p.split(","))
                                            parsed.append([x, y])
                                        except Exception:
                                            continue
                                    elif isinstance(p, (list, tuple)) and len(p) == 2:
                                        parsed.append([int(p[0]), int(p[1])])
                                if parsed:
                                    truth_coords = parsed
                                    truth_source = "local"
                        elif click_positions and len(click_positions) > 0:
                            # retry 样本记录本地预测坐标（非真值），方便后续分析
                            parsed = []
                            for p in click_positions:
                                if isinstance(p, str):
                                    try:
                                        x, y = map(int, p.split(","))
                                        parsed.append([x, y])
                                    except Exception:
                                        continue
                                elif isinstance(p, (list, tuple)) and len(p) == 2:
                                    parsed.append([int(p[0]), int(p[1])])
                            if parsed:
                                predicted_coords = parsed

                        # 原始样本图片列表
                        files = [f for f in meta.get("files", []) if f.endswith(".jpg")]
                        records.append({
                            "month": month_dir.name,
                            "day": day_dir.name,
                            "run": run_dir.name,
                            "account": account_dir.name,
                            "attempt": attempt_dir.name,
                            "outcome": outcome,
                            "captured_at": captured_at,
                            "has_captcha": "captcha.jpg" in files,
                            "has_combined": "combined_captcha.jpg" in files,
                            "has_sprite": "sprite.jpg" in files,
                            "has_truth_coords": bool(truth_coords),
                            "truth_source": truth_source or "",
                            "click_count": len(truth_coords) if truth_coords else (len(predicted_coords) if predicted_coords else 0),
                            "truth_coords": json.dumps(truth_coords) if truth_coords else "",
                            "predicted_coords": json.dumps(predicted_coords) if predicted_coords else "",
                            "path": str(attempt_dir.relative_to(ROOT)),
                        })
    return records


def copy_samples(records, output_dir: Path):
    """把每个 attempt 的原始样本复制到输出目录，保持结构。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for r in records:
        src = ROOT / r["path"]
        dst = output_dir / r["path"]
        dst.mkdir(parents=True, exist_ok=True)
        for f in ["captcha.jpg", "sprite.jpg", "combined_captcha.jpg"] + [f"sprite_{i}.jpg" for i in range(1, 4)] + [f"spec_{i}.jpg" for i in range(1, 10)]:
            src_f = src / f
            if src_f.exists():
                shutil.copy2(src_f, dst / f)
                copied += 1
    return copied


def main():
    parser = argparse.ArgumentParser(description="准备验证码训练数据集")
    parser.add_argument("--output", default="dataset/captcha_samples", help="输出目录")
    parser.add_argument("--extract-archives", action="store_true", help="解压 archives/*.zip")
    parser.add_argument("--skip-copy", action="store_true", help="只生成清单，不复制文件")
    args = parser.parse_args()

    output_dir = ROOT / args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.extract_archives:
        print("正在解压月度归档...")
        extract_archives(ARCHIVES_DIR, SAMPLES_ROOT)

    print("正在收集样本...")
    records = collect_samples(SAMPLES_ROOT)
    if not records:
        print("未找到任何验证码样本。请确认 captcha_samples/ 目录存在。")
        return

    pass_count = sum(1 for r in records if r["outcome"] == "pass")
    retry_count = sum(1 for r in records if r["outcome"] == "retry")
    with_truth = sum(1 for r in records if r["has_truth_coords"])
    print(f"\n共收集 {len(records)} 个 attempt：")
    print(f"  - 成功(pass): {pass_count}")
    print(f"  - 重试(retry): {retry_count}")
    print(f"  - 含真值坐标: {with_truth}")

    # 生成清单 CSV
    csv_path = output_dir / f"manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = ["month", "day", "run", "account", "attempt", "outcome", "captured_at",
                  "has_captcha", "has_combined", "has_sprite", "has_truth_coords",
                  "truth_source", "click_count", "truth_coords", "predicted_coords", "path"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow(r)
    print(f"\n清单已保存: {csv_path.relative_to(ROOT)}")

    if not args.skip_copy:
        copied = copy_samples(records, output_dir)
        print(f"已复制 {copied} 个文件到 {output_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
