"""OCR 模型全局单例。

提供线程安全的 ddddocr 模型加载与复用，避免每次验证码挑战都重新加载模型。
"""

import logging
import threading

logger = logging.getLogger(__name__)

_ocr_model = None
_det_model = None
_model_lock = threading.Lock()
_inference_lock = threading.Lock()


def get_shared_ocr_models():
    """加载并返回 ddddocr OCR / 检测模型单例（线程安全）。"""
    global _ocr_model, _det_model
    if _ocr_model is None or _det_model is None:
        with _model_lock:
            if _ocr_model is None or _det_model is None:
                import ddddocr

                logger.info("正在加载OCR模型...")
                _ocr_model = ddddocr.DdddOcr(ocr=True, show_ad=False)
                _det_model = ddddocr.DdddOcr(det=True, show_ad=False)
    return _ocr_model, _det_model


def get_inference_lock():
    """返回 OCR 推理互斥锁，避免多线程同时调用 ddddocr 推理。"""
    return _inference_lock
