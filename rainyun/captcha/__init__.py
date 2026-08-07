"""验证码模块 — 公共 API。

提供：
- get_captcha_provider() — 自动选择验证码方案
- CaptchaFactory              — 工厂类
- TencentCaptchaProvider      — 本地 CV 方案
- TwoCaptchaProvider          — 2captcha API 方案
- CaptchaProvider             — 抽象基类
- save_captcha_archive_bundle — 样本归档工具
"""

from rainyun.captcha._archive import save_captcha_archive_bundle
from rainyun.captcha._base import (
    CaptchaFactory,
    CaptchaProvider,
    CompositeCaptchaProvider,
    get_captcha_provider,
)
from rainyun.captcha._tencent import TencentCaptchaProvider
from rainyun.captcha._twocaptcha import TwoCaptchaProvider

__all__ = [
    "get_captcha_provider",
    "CaptchaFactory",
    "TencentCaptchaProvider",
    "TwoCaptchaProvider",
    "CaptchaProvider",
    "CompositeCaptchaProvider",
    "save_captcha_archive_bundle",
]
