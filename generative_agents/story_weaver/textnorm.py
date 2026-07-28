"""story_weaver.textnorm — 簡體轉繁體（香港）工具.

本系統產出嘅所有文字必須係繁體香港中文書面語。模板嘅
lifestyle / daily_plan 等欄位係簡體原文，寫入新 agent.json
之前要經 `to_traditional` 過一次。無 OpenCC 時原樣通過並 log warning。
"""

import logging

logger = logging.getLogger(__name__)

_converter = None
_available = None


def _get_converter():
    global _converter, _available
    if _available is None:
        try:
            from opencc import OpenCC

            _converter = OpenCC("s2hk")
            _available = True
        except Exception as e:  # pragma: no cover - 環境依賴
            logger.warning("OpenCC 不可用，簡體原文將唔會轉換：%s", e)
            _converter = None
            _available = False
    return _converter


def to_traditional(text: str) -> str:
    """簡體轉繁體（香港用字）。無 OpenCC 時原樣返回。

    注意：OpenCC s2hk 會將「床」轉做「牀」，但 maze.json 嘅地址用「床」，
    呢度統一轉返「床」，避免日後誤用喺地址字串時整斷 maze 查找。
    """
    if not text:
        return text
    converter = _get_converter()
    if converter is None:
        return text
    return converter.convert(text).replace("牀", "床")
