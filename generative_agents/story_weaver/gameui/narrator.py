"""story_weaver.gameui.narrator — 即時劇情旁白（實時交代劇情進展）。

每輪詢掃到新事件/對話，背景 thread 叫 GM LLM 寫一段講故事口吻嘅旁白，
入 feed 做「故事情節」。失敗唔阻推演（靜默 skip）。
"""

from __future__ import annotations

import logging
import os
from string import Template

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _Narrative(BaseModel):
    res: str = Field(description="劇情旁白，繁體中文書面語，1-3 句")


_TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "data", "prompts_gm", "gm_step_narrative.txt",
)


class StepNarrator:
    def __init__(self, llm, template_path: str | None = None) -> None:
        self._llm = llm
        path = template_path or os.path.normpath(_TEMPLATE_PATH)
        with open(path, "r", encoding="utf-8") as f:
            self._template = Template(f.read())

    def narrate(
        self,
        sim_time: str,
        events: list[str],
        dialogues: list[str],
        story_context: str = "",
    ) -> str | None:
        """events: ["阿珍喺大街散步", ...]；dialogues: ["阿珍：「……」", ...]。
        失敗 → None（上層靜默 skip）。"""
        if self._llm is None:
            return None
        try:
            prompt = self._template.substitute(
                story_context=story_context or "（尚未有前情）",
                sim_time=sim_time or "（時間不詳）",
                events="\n".join(f"- {e}" for e in events) or "（無新事件）",
                dialogues="\n".join(dialogues) or "（無新對話）",
            )
            result = self._llm.completion(
                prompt,
                retry=2,
                failsafe=None,
                return_type=_Narrative,
                caller="step_narrative",
            )
            if result is None:
                return None
            text = result if isinstance(result, str) else getattr(result, "res", None)
            return (text or "").strip() or None
        except Exception:
            logger.warning("narrator: 旁白生成失敗", exc_info=True)
            return None
