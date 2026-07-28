"""共用轉換邏輯：glossary 詞彙優先 + OpenCC s2hk 兜底。

所有 scripts/localization/ 腳本共用呢個 converter，保證 build-time 轉換
同 runtime normalize 層（modules/model/text_normalize.py）行為一致：
- glossary 嘅 place_names / agent_names / keywords 值同 OpenCC s2hk 輸出一致
- vocabulary 係書面語措辭覆蓋，優先於 OpenCC
- protected_tokens（${...} 佔位符、living_area、sleeping、the Ville）唔准郁
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GLOSSARY = REPO_ROOT / "generative_agents" / "data" / "glossary_s2hk.json"

_PLACEHOLDER_RE = re.compile(r"\$\{[^}]*\}")
_SENTINEL = "\x00{}\x01"

# OpenCC s2hk 嘅香港習字表字形 → 香港日常常用字形（只適用於敘述文字，
# 地名 token 行 place_names 查表，唔經呢度）。
# 牀→床 同時係 keywords.py KW_BED="床" 嘅硬性要求（睡覺地址推導）。
VARIANT_FIX = {"牀": "床", "衞": "衛", "啓": "啟"}

_opencc_instance = None


def _get_opencc():
    global _opencc_instance
    if _opencc_instance is None:
        try:
            from opencc import OpenCC

            _opencc_instance = OpenCC("s2hk")
        except Exception:  # pragma: no cover - opencc 缺裝降級
            _opencc_instance = False
    return _opencc_instance or None


def load_glossary(path: str | Path = DEFAULT_GLOSSARY) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TextConverter:
    """簡→繁（香港書面語）文字轉換器。"""

    def __init__(self, glossary: dict):
        self.glossary = glossary
        # 替換表：vocabulary（措辭覆蓋）+ keywords + agent_names + place_names
        # 全部按 key 長度降序，避免短詞搶先命中（「咖啡馆」唔會畀「咖啡」截糊）
        self._replacements: list[tuple[str, str]] = []
        for section in ("vocabulary", "keywords", "agent_names", "place_names"):
            for src, dst in glossary.get(section, {}).items():
                self._replacements.append((src, dst))
        self._replacements.sort(key=lambda kv: len(kv[0]), reverse=True)
        self.protected = list(glossary.get("protected_tokens", []))

    def _protect(self, text: str) -> tuple[str, list[str]]:
        """將 protected token 同 ${...} 佔位符換做 sentinel，轉換後還原。"""
        stash: list[str] = []

        def _sub(m: re.Match) -> str:
            stash.append(m.group(0))
            return _SENTINEL.format(len(stash) - 1)

        text = _PLACEHOLDER_RE.sub(_sub, text)
        for tok in self.protected:
            if tok == "${":  # 佔位符已由 regex 處理
                continue
            if tok in text:
                stash.append(tok)
                text = text.replace(tok, _SENTINEL.format(len(stash) - 1))
        return text, stash

    @staticmethod
    def _restore(text: str, stash: list[str]) -> str:
        for i, tok in enumerate(stash):
            text = text.replace(_SENTINEL.format(i), tok)
        return text

    def convert_text(self, text: str) -> str:
        if not text:
            return text
        protected, stash = self._protect(text)
        for src, dst in self._replacements:
            if src in protected:
                protected = protected.replace(src, dst)
        cc = _get_opencc()
        if cc is not None:
            protected = cc.convert(protected)
        for variant, common in VARIANT_FIX.items():
            if variant in protected:
                protected = protected.replace(variant, common)
        return self._restore(protected, stash)

    def convert_json_values(self, obj):
        """遞歸轉換 JSON 結構入面嘅字串 value；dict key 唔郁。"""
        if isinstance(obj, str):
            return self.convert_text(obj)
        if isinstance(obj, list):
            return [self.convert_json_values(v) for v in obj]
        if isinstance(obj, dict):
            return {k: self.convert_json_values(v) for k, v in obj.items()}
        return obj


def has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def convert_address_token(token: str, glossary: dict) -> str:
    """地名 token 精確映射：protected 原樣、place_names 查表、非 CJK 原樣。

    查唔到表嘅 CJK token 拋 KeyError（caller 收集做 error，唔好靜默放過）。
    """
    if token in glossary.get("protected_tokens", []):
        return token
    mapping = glossary.get("place_names", {})
    if token in mapping:
        return mapping[token]
    if token in mapping.values():  # 已轉換嘅 token 原樣放行（rerun 安全）
        return token
    if not has_cjk(token):
        return token
    raise KeyError(f"地名 token 未喺 glossary place_names 登記：{token!r}")
