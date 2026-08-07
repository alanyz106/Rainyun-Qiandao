"""整理 CI 验证码样本目录。

把 `logs/ci_artifacts/<run_number>/captcha/` 下的调试中间产物（debug_*.png/jpg、poc_*.png/jpg、
debug_viz/ 等）移动到 `logs/ci_artifacts/<run_number>/debug/`，只保留原始样本文件：

- captcha.jpg      验证码大图
- sprite.jpg       顶部提示条（3 个目标横向拼接）
- sprite_1~3.jpg   切开的 3 个目标
- spec_1~N.jpg     ddddocr 检测到的候选框
- combined_captcha.jpg  2captcha 用的拼接图（如有）
- metadata.json    本轮元数据（得分、点击坐标、结果等）

用法：
    .venv/Scripts/python.exe script/organize_captcha_samples.py

默认扫描 `logs/ci_artifacts/` 下所有 run 的 captcha 目录。可用 --run 指定单个 run：

    .venv/Scripts/python.exe script/organize_captcha_samples.py --run 100
"""

import argparse
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_ROOT = ROOT / "logs" / "ci_artifacts"

# 保留在 captcha/ 样本目录的原始文件
KEEP_NAMES = {"captcha.jpg", "sprite.jpg", "combined_captcha.jpg", "metadata.json"}
KEEP_PREFIXES = ("sprite_", "spec_")


def is_sample_file(name: str) -> bool:
    return name in KEEP_NAMES or any(name.startswith(p) for p in KEEP_PREFIXES)


def organize_run(run_dir: Path, dry_run: bool = False) -> dict:
    captcha_root = run_dir / "captcha"
    if not captcha_root.exists():
        return {"moved": 0, "errors": [], "note": "无 captcha 目录"}

    debug_root = run_dir / "debug"
    moved_count = 0
    errors = []

    # 1) 移动每个 attempt 子目录里的调试图
    for date_dir in captcha_root.iterdir():
        if not date_dir.is_dir():
            continue
        for account_dir in date_dir.iterdir():
            if not account_dir.is_dir():
                continue
            for attempt_dir in account_dir.iterdir():
                if not attempt_dir.is_dir():
                    continue
                dst_dir = debug_root / date_dir.name / account_dir.name / attempt_dir.name
                if not dry_run:
                    dst_dir.mkdir(parents=True, exist_ok=True)

                for f in sorted(attempt_dir.iterdir()):
                    if not f.is_file():
                        continue
                    if is_sample_file(f.name):
                        continue
                    try:
                        if dry_run:
                            print(f"[dry-run] 将移动: {f.relative_to(ROOT)}")
                        else:
                            shutil.move(str(f), str(dst_dir / f.name))
                        moved_count += 1
                    except Exception as e:
                        errors.append(f"{f}: {e}")

            # 2) 移动 account 级别 debug_viz 目录
            debug_viz_src = account_dir / "debug_viz"
            if debug_viz_src.exists() and debug_viz_src.is_dir():
                debug_viz_dst = debug_root / date_dir.name / account_dir.name / "debug_viz"
                try:
                    if dry_run:
                        print(f"[dry-run] 将移动目录: {debug_viz_src.relative_to(ROOT)}")
                    else:
                        if debug_viz_dst.exists():
                            shutil.rmtree(debug_viz_dst)
                        shutil.move(str(debug_viz_src), str(debug_viz_dst))
                    moved_count += 1
                except Exception as e:
                    errors.append(f"{debug_viz_src}: {e}")

    return {"moved": moved_count, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="整理 CI 验证码样本目录")
    parser.add_argument("--run", help="只处理指定 run id（默认处理所有 run）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不实际移动文件")
    args = parser.parse_args()

    if not ARTIFACTS_ROOT.exists():
        print(f"找不到 CI artifact 根目录: {ARTIFACTS_ROOT}")
        sys.exit(1)

    run_dirs = []
    if args.run:
        run_dir = ARTIFACTS_ROOT / f"run{args.run}"
        if not run_dir.exists():
            run_dir = ARTIFACTS_ROOT / args.run
        if run_dir.exists():
            run_dirs.append(run_dir)
        else:
            print(f"找不到 run 目录: {run_dir}")
            sys.exit(1)
    else:
        run_dirs = [d for d in ARTIFACTS_ROOT.iterdir() if d.is_dir()]

    total_moved = 0
    for run_dir in sorted(run_dirs):
        result = organize_run(run_dir, dry_run=args.dry_run)
        print(f"\n[{run_dir.name}] 移动 {result['moved']} 个文件/目录到 debug/")
        if result["errors"]:
            print(f"  错误 {len(result['errors'])} 个:")
            for err in result["errors"]:
                print(f"    - {err}")
        total_moved += result["moved"]

    print(f"\n总计: {total_moved} 个文件/目录{'将被' if args.dry_run else '已'}整理")


if __name__ == "__main__":
    main()
