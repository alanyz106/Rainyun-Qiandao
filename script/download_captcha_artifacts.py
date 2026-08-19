#!/usr/bin/env python3
"""
批量下载 GitHub Actions 的 captcha-images 验证码样本 artifact。

用法：
    python script/download_captcha_artifacts.py [options]

默认拉取最近 30 天内、当前 workflow 产生的 captcha-images-<run_number> artifact，
解压到 logs/ci_artifacts/<run_number>/ 目录。只保留存在验证码样本的 run。
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"
CI_ARTIFACTS_DIR = LOGS_DIR / "ci_artifacts"
RUNS_JSON = CI_ARTIFACTS_DIR / "runs.json"


def run_gh(args, check=True, timeout=120):
    cmd = ["gh"] + args
    result = subprocess.run(
        cmd,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def fetch_runs(limit: int, repo: str, workflow: str):
    print(f"正在列出最近 {limit} 条 workflow runs ...")
    result = run_gh([
        "run", "list", "--repo", repo, "--workflow", workflow,
        "--limit", str(limit),
        "--json", "databaseId,number,createdAt,conclusion,status,event,headBranch,url",
    ])
    CI_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_JSON.write_text(result.stdout, encoding="utf-8")
    return json.loads(result.stdout)


def fetch_artifacts_for_run(run_id: int, repo: str):
    """通过 GitHub API 获取某个 run 的 artifact 列表，返回未过期的 captcha-images artifact。"""
    result = run_gh(["api", f"repos/{repo}/actions/runs/{run_id}/artifacts"])
    data = json.loads(result.stdout)
    artifacts = []
    for a in data.get("artifacts", []):
        if a.get("expired"):
            continue
        if a.get("name", "").startswith("captcha-images-"):
            artifacts.append(a)
    return artifacts


import re

def normalize_extracted_dir(dest: Path, run_number: int) -> Path:
    """
    gh run download 解压 artifact 后，实际目录可能是：
        dest/logs/captcha_archive/<日期>/<账号>/...
        dest/captcha_archive/<日期>/<账号>/...
        dest/<日期>/<账号>/...          （artifact 直接打包了 captcha_archive 里的内容）
    这里把内容统一规整到 dest/captcha/<日期>/<账号>/，并删除空目录。
    """
    possible_roots = [
        dest / "logs" / "captcha_archive",
        dest / "captcha_archive",
    ]
    final_root = dest / "captcha"
    final_root.mkdir(parents=True, exist_ok=True)

    def collect_and_move(src: Path):
        """把 src 下的日期目录/账号目录移动到 final_root。"""
        for item in src.iterdir():
            if item == final_root:
                # src 本身已含 captcha 目录（artifact 以 captcha 为根），跳过避免自移入
                continue
            if item.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", item.name):
                dst = final_root / item.name
                if dst.exists():
                    merge_dirs(item, dst)
                    if not any(item.iterdir()):
                        item.rmdir()
                else:
                    item.rename(dst)
            elif item.is_dir():
                # 非日期目录也保留（如 debug/）
                dst = final_root / item.name
                if dst.exists():
                    merge_dirs(item, dst)
                    if not any(item.iterdir()):
                        item.rmdir()
                else:
                    item.rename(dst)
            elif item.is_file():
                item.rename(final_root / item.name)

    found = False
    for src in possible_roots:
        if src.exists() and any(src.rglob("*.jpg")):
            collect_and_move(src)
            found = True
            cleanup_empty(src)
            cleanup_empty(src.parent if "logs" in str(src) else src)
            break

    if not found:
        # 可能是 artifact 直接以日期目录为根
        for item in list(dest.iterdir()):
            if item.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", item.name):
                collect_and_move(dest)
                found = True
                break

    return final_root if found and any(final_root.rglob("*.jpg")) else None


def merge_dirs(src: Path, dst: Path):
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            if target.exists():
                merge_dirs(item, target)
                if not any(item.iterdir()):
                    item.rmdir()
            else:
                item.rename(target)
        else:
            item.rename(target)


def cleanup_empty(path: Path):
    try:
        if path.exists() and path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def download_artifact(run: dict, repo: str, dry_run: bool = False):
    import shutil
    import tempfile

    run_id = run["databaseId"]
    run_number = run["number"]
    created = run["createdAt"]
    dest = CI_ARTIFACTS_DIR / str(run_number)
    final_dir = dest / "captcha"

    if final_dir.exists() and any(final_dir.rglob("*.jpg")):
        print(f"  run {run_number} ({created}): 本地已存在，跳过")
        return True

    # 先通过 API 确认有没有可用的 captcha artifact
    artifacts = fetch_artifacts_for_run(run_id, repo)
    if not artifacts:
        print(f"  run {run_number} ({created}): 无可用 captcha artifact")
        return False

    artifact = artifacts[0]
    artifact_name = artifact["name"]
    print(f"  run {run_number} ({created}): 下载 artifact '{artifact_name}' ...")
    if dry_run:
        return True

    # 用临时目录下载，避免覆盖已有目录时触发安全删除限制
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"{run_number}_", dir=CI_ARTIFACTS_DIR))
    try:
        run_gh([
            "run", "download", str(run_id),
            "--repo", repo,
            "-n", artifact_name,
            "-D", str(tmp_dir),
        ], timeout=120)
    except RuntimeError as e:
        print(f"    -> 下载失败: {e}")
        return False
    finally:
        # 规整到最终目录
        final_root = normalize_extracted_dir(tmp_dir, run_number)
        if final_root is not None and any(final_root.rglob("*.jpg")):
            # 如果 dest 已存在（旧结构），重命名备份而不是删除，避免安全删除限制
            if dest.exists():
                backup_name = f"{run_number}_old_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                backup_path = CI_ARTIFACTS_DIR / backup_name
                try:
                    dest.rename(backup_path)
                except OSError:
                    print(f"    -> 无法备份旧目录 {dest}，跳过")
                    return False
            tmp_dir.rename(dest)
            print(f"    -> 成功保存到 {dest / 'captcha'}")
            return True
        else:
            print(f"    -> artifact 解压后没有 jpg 样本")
            return False

    return False


def main():
    parser = argparse.ArgumentParser(description="下载历史 CI 验证码样本 artifact")
    parser.add_argument("--repo", default="alanyz106/Rainyun-Qiandao", help="GitHub 仓库")
    parser.add_argument("--workflow", default="雨云每日签到", help="Workflow 名称")
    parser.add_argument("--limit", type=int, default=100, help="最多查询多少条 runs")
    parser.add_argument("--days", type=int, default=30, help="只下载 N 天内的 runs")
    parser.add_argument("--dry-run", action="store_true", help="只列出要下载的 run，不执行")
    parser.add_argument("--refresh-runs", action="store_true", help="强制重新获取 runs 列表")
    args = parser.parse_args()

    if not RUNS_JSON.exists() or args.refresh_runs:
        runs = fetch_runs(args.limit, args.repo, args.workflow)
    else:
        runs = json.loads(RUNS_JSON.read_text(encoding="utf-8"))

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    targets = []
    for r in runs:
        created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
        if created >= cutoff:
            targets.append(r)

    print(f"\n最近 {args.days} 天内共有 {len(targets)} 条 runs，准备下载 captcha-images artifact ...")
    ok = failed = skipped = 0
    for r in targets:
        exists = (CI_ARTIFACTS_DIR / str(r["number"]) / "captcha").exists()
        if exists:
            skipped += 1
            print(f"  run {r['number']}: 本地已存在，跳过")
            continue
        success = download_artifact(r, args.repo, dry_run=args.dry_run)
        if success:
            ok += 1
        else:
            failed += 1

    print(f"\n完成：成功 {ok}，跳过 {skipped}，失败/无样本 {failed}（dry_run={args.dry_run}）")


if __name__ == "__main__":
    main()
