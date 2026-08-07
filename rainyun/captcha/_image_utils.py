"""图片下载与 CSS 样式解析工具。

纯工具函数，无状态，不依赖项目其他模块。
"""

import logging
import os
import re

logger = logging.getLogger(__name__)


def download_image(url, filename, user_agent=None):
    """从 URL 下载图片并保存到 temp/ 目录。"""
    import requests

    os.makedirs("temp", exist_ok=True)

    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            path = os.path.join("temp", filename)
            with open(path, "wb") as f:
                f.write(response.content)
            return True
        else:
            logger.error(f"下载图片失败！状态码: {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"下载图片异常: {e}")
        return False


def get_url_from_style(style):
    """从 CSS style 属性中提取 url(...) 值。"""
    return re.search(r'url\(["\']?(.*?)["\']?\)', style).group(1)


def get_width_from_style(style):
    """从 CSS style 属性中提取 width 值。"""
    return re.search(r'width:\s*([\d.]+)px', style).group(1)


def get_height_from_style(style):
    """从 CSS style 属性中提取 height 值。"""
    return re.search(r'height:\s*([\d.]+)px', style).group(1)
