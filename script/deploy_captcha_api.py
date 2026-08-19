"""部署/更新 huasuanyun 上的 YOLO 验证码 API 服务。

用法：
    python script/deploy_captcha_api.py            # 上传代码 + 模型 + 重启服务
    python script/deploy_captcha_api.py --code     # 只更新代码（_yolo.py / server）
    python script/deploy_captcha_api.py --restart  # 只重启服务

说明：
- 模型文件（yolo_v4.onnx / siamese_encoder.onnx / siamese_refs.npz）不上 GitHub，
  首次部署或模型更新时用 --models 上传。
- API token 存在服务器 ~/captcha-api/api_token.txt，部署后需在 GitHub Secrets
  配置 CAPTCHA_API_URL / CAPTCHA_API_TOKEN 供 GH Actions 使用。
"""

import argparse
import os
import subprocess
import sys

HOST = "huasuanyun"
REMOTE_DIR = "~/captcha-api"

LOCAL_FILES = {
    "code": [
        "rainyun/captcha/_yolo.py",
        "script/captcha_api_server.py",
    ],
    "models": [
        "rainyun/captcha/models/yolo_v4.onnx",
        "rainyun/captcha/models/siamese_encoder.onnx",
        "rainyun/captcha/models/siamese_refs.npz",
    ],
}


def run(cmd):
    print(f"$ {cmd}")
    subprocess.run(cmd, shell=True, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", action="store_true", help="只更新代码")
    parser.add_argument("--models", action="store_true", help="上传模型文件")
    parser.add_argument("--restart", action="store_true", help="只重启服务")
    args = parser.parse_args()

    if args.restart:
        run(f'ssh {HOST} "cd {REMOTE_DIR} && pkill -f captcha_api_server.py; '
            f'TOKEN=$(cat api_token.txt) && CAPTCHA_API_TOKEN=$TOKEN setsid nohup '
            f'./venv/bin/python captcha_api_server.py > api.log 2>&1 < /dev/null & '
            f'sleep 2; tail -3 api.log"')
        return

    if not (args.code or args.models):
        args.code = args.models = True

    run(f'ssh {HOST} "mkdir -p {REMOTE_DIR}/models"')

    if args.code:
        for f in LOCAL_FILES["code"]:
            run(f'scp {f} {HOST}:{REMOTE_DIR}/')
    if args.models:
        for f in LOCAL_FILES["models"]:
            run(f'scp {f} {HOST}:{REMOTE_DIR}/models/')

    run(f'ssh {HOST} "cd {REMOTE_DIR} && pkill -f captcha_api_server.py; '
        f'TOKEN=$(cat api_token.txt) && CAPTCHA_API_TOKEN=$TOKEN setsid nohup '
        f'./venv/bin/python captcha_api_server.py > api.log 2>&1 < /dev/null & '
        f'sleep 2; tail -3 api.log"')

    print("\n完成。检查: curl -s http://186.241.81.51:8501/health")
    print("GitHub Secrets 需配置: CAPTCHA_API_URL=http://186.241.81.51:8501/solve, "
          "CAPTCHA_API_TOKEN=$(cat 服务器 api_token.txt)")


if __name__ == "__main__":
    main()
