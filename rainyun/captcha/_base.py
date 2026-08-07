"""验证码提供者基类、组合模式 与 工厂函数。

提供：
- CaptchaProvider 抽象基类
- CompositeCaptchaProvider 组合提供者（主方案 + 备用方案）
- CaptchaFactory 工厂类
- get_captcha_provider() 自动选择入口
"""

import logging
import os

logger = logging.getLogger(__name__)


class CaptchaProvider:
    """验证码提供者基类。"""

    def solve(self, driver, timeout, retry_stats, logger_adapter):
        """尝试解决验证码。

        :param driver: Selenium WebDriver 实例
        :param timeout: 等待超时（秒）
        :param retry_stats: dict，包含 'count' 键用于追踪重试次数
        :param logger_adapter: LoggerAdapter 实例
        :return: None 表示通过，False 表示放弃
        """
        raise NotImplementedError


class CompositeCaptchaProvider(CaptchaProvider):
    """复合验证码方案：先尝试主方案，失败后回退到备用方案。"""

    def __init__(self, primary, fallback):
        self.primary = primary
        self.fallback = fallback

    def solve(self, driver, timeout, retry_stats, logger_adapter):
        result = self.primary.solve(driver, timeout, retry_stats, logger_adapter)

        if result is False:
            logger_adapter.warning(
                "本地方案已耗尽重试次数，切换到 2captcha 备用方案..."
            )
            self.fallback.solve(driver, timeout, retry_stats, logger_adapter)


class CaptchaFactory:
    """验证码工厂类。"""

    @classmethod
    def create_provider(cls, captcha_type="tencent"):
        """根据类型创建验证码提供者。

        :param captcha_type: "tencent" | "twocaptcha"
        """
        from rainyun.captcha._tencent import TencentCaptchaProvider
        from rainyun.captcha._twocaptcha import TwoCaptchaProvider

        if captcha_type == "tencent":
            return TencentCaptchaProvider()
        if captcha_type == "twocaptcha":
            return TwoCaptchaProvider()
        raise ValueError(f"Unknown captcha type: {captcha_type}")


def get_captcha_provider():
    """根据环境变量自动选择验证码破解方案。

    - 配置了 TWOCAPTCHA_API_KEY → 本地优先(CV) + 2captcha 备用
    - 未配置 → 纯本地 CV 方案
    """
    from rainyun.captcha._tencent import TencentCaptchaProvider
    from rainyun.captcha._twocaptcha import TwoCaptchaProvider

    twocaptcha_key = os.getenv("TWOCAPTCHA_API_KEY", "").strip()
    if twocaptcha_key:
        return CompositeCaptchaProvider(
            TencentCaptchaProvider(max_retries=3),
            TwoCaptchaProvider(),
        )
    return TencentCaptchaProvider(max_retries=2)
