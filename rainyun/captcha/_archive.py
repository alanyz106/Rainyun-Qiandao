"""验证码样本归档。

每次验证码挑战结束后，把本轮的图片（大图/图案/候选框/拼合图）归档到
logs/captcha_archive/<日期>/<账号前缀>_a<次数>_<结果>/，用于后续算法回归测试。
"""

import json
import logging
import os
import shutil

from rainyun.captcha._cv_utils import make_safe_name
from rainyun.config import now_local

logger = logging.getLogger(__name__)


def save_captcha_archive_bundle(logger_adapter, attempt_index, outcome, extra=None):
    """每次验证码挑战结束后归档本轮图片样本。

    与 _save_captcha_debug_bundle（只在特定失败分支触发）不同，本函数对**每一次**
    挑战（无论通过/重试/异常）都归档，保证样本完整。

    :param logger_adapter: LoggerAdapter 实例
    :param attempt_index: 第几次挑战（0-based，取自 retry_stats['count']）
    :param outcome: "pass" / "retry" / "error"
    :param extra: 额外元数据（得分、点击坐标、原因等），写入 metadata.json
    """
    if os.getenv("CAPTCHA_ARCHIVE", "1") == "0":
        return

    account_prefix = make_safe_name(
        getattr(logger_adapter, "extra", {}).get("prefix", "unknown")
    )
    date_str = now_local().strftime("%Y-%m-%d")
    bundle_name = f"a{attempt_index:02d}_{outcome}"
    bundle_dir = os.path.join("logs", "captcha_archive", date_str, account_prefix, bundle_name)
    os.makedirs(bundle_dir, exist_ok=True)

    temp_dir = "temp"
    copied = []
    if os.path.isdir(temp_dir):
        wanted_exact = {"captcha.jpg", "sprite.jpg", "combined_captcha.jpg"}
        for filename in sorted(os.listdir(temp_dir)):
            if not (
                filename in wanted_exact
                or filename.startswith("sprite_")
                or filename.startswith("spec_")
            ):
                continue
            src = os.path.join(temp_dir, filename)
            if not os.path.isfile(src):
                continue
            shutil.copy2(src, os.path.join(bundle_dir, filename))
            copied.append(filename)

    metadata = {
        "outcome": outcome,
        "attempt_index": attempt_index,
        "account_prefix": account_prefix,
        "captured_at": now_local().isoformat(timespec="seconds"),
        "files": copied,
        "extra": extra or {},
    }
    with open(os.path.join(bundle_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    if copied:
        logger_adapter.info(f"已归档验证码样本（第{attempt_index}次, {outcome}）: {bundle_dir}")
