"""story_weaver.recap.generator — 敘事生成 + 降級（spec §5.2、§5.3、§5.4）。

LLM 經獨立 LLMModel 實例（create_llm_model），retry=10 用盡 → failsafe=None →
返 None → 落模板降級（fallback）。validator 四條規則唔過 → callback 返 None 觸發 retry。
"""

from __future__ import annotations

import logging

from modules.model.llm_model import create_llm_model

from .models import RoundRecap
from .prompts import CumulativeRecapResponse, RecapPrompt, RoundRecapResponse

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 8192
CONTEXT_BUDGET_RATIO = 0.6  # cumulative prompt 唔超過模型 context 嘅 60%（PRD 紅線）

FALLBACK_BANNER = "【故事摘要暫時不可用，以下為原始記錄】"


def make_validator(agent_names: list[str]):
    """PRD 指定四條規則：非空、≥50 字、無未渲染 placeholder、提及至少一個角色名。"""

    def validator(output) -> str | None:
        text = output.res if hasattr(output, "res") else str(output or "")
        if not text or len(text.strip()) < 50:
            return None
        if "{" in text or "${" in text:
            return None
        if agent_names and not any(name in text for name in agent_names):
            return None
        return text.strip()

    return validator


def build_round_fallback(round_recap: RoundRecap) -> str:
    """LLM 全敗時嘅降級輸出 = 純模板拼接（spec §5.4）。事件同對白原文齊全。"""
    lines = [
        FALLBACK_BANNER,
        f"第 {round_recap.round} 回合（{round_recap.sim_time_start} – {round_recap.sim_time_end}）",
    ]
    for e in round_recap.events:
        lines.append(f"· {e.sim_time} {e.agent} @ {e.location}：{e.describe}")
    for block in round_recap.dialogues:
        participants = " -> ".join(block.participants)
        lines.append(f"【對話】{participants} @ {block.location}")
        for line in block.lines:
            lines.append(f"{line.speaker}：「{line.text}」")
    return "\n".join(lines)


def build_cumulative_fallback(opening: str, rounds: list[RoundRecap]) -> str:
    lines = [FALLBACK_BANNER, f"【故事開端】{opening}"]
    for r in rounds:
        lines.append(f"\n第 {r.round} 回合（{r.sim_time_start} – {r.sim_time_end}）")
        for e in r.events:
            lines.append(f"· {e.sim_time} {e.agent} @ {e.location}：{e.describe}")
        for block in r.dialogues:
            participants = " -> ".join(block.participants)
            lines.append(f"【對話】{participants} @ {block.location}")
            for line in block.lines:
                lines.append(f"{line.speaker}：「{line.text}」")
    return "\n".join(lines)


def quiet_round_text(round_recap: RoundRecap) -> str:
    """零對話零重要事件嘅捷徑模板（唔調 LLM）。"""
    return (
        f"第 {round_recap.round} 回合風平浪靜：小鎮嘅眾人各自作息，"
        "無特別嘅事情發生。"
    )


def opening_only_text(opening: str) -> str:
    """第 1 回合決策前（rounds 為空）嘅 cumulative 捷徑模板（唔調 LLM）。"""
    return f"【故事開端】{opening}\n\n故事即將展開。"


class RecapGenerator:
    def __init__(self, llm_config: dict | None = None, template_path: str = "data/prompts",
                 llm=None, context_window: int = DEFAULT_CONTEXT_WINDOW) -> None:
        self._prompts = RecapPrompt(template_path)
        self._context_window = context_window
        self._model_name = ""
        if llm is not None:
            self._llm = llm
        elif llm_config:
            try:
                self._llm = create_llm_model(llm_config)
            except Exception:
                logger.warning("recap generator: LLM 初始化失敗，全部行 fallback", exc_info=True)
                self._llm = None
        else:
            self._llm = None
        if self._llm is not None:
            try:
                self._model_name = self._llm.get_summary().get("model", "")
            except Exception:
                self._model_name = ""

    @property
    def model_name(self) -> str:
        return self._model_name

    def estimate_tokens(self, text: str) -> int:
        """粗略計數：len(text)（中文 1 字 ≈ 1 token 嘅保守上界）。"""
        return len(text or "")

    def generate_round_recap(
        self, opening: str, round_recap: RoundRecap, prev_recaps: list[str],
        agent_names: list[str],
    ) -> tuple[str, str]:
        """返 (text, status)。零事件零對話 → 捷徑模板；LLM 全敗 → fallback。"""
        if not round_recap.events and not round_recap.dialogues:
            return quiet_round_text(round_recap), "ok"
        if self._llm is None:
            return build_round_fallback(round_recap), "fallback"
        try:
            prompt = self._prompts.round_input(opening, round_recap, prev_recaps)
        except Exception:
            logger.warning("recap generator: round prompt 組裝失敗", exc_info=True)
            return build_round_fallback(round_recap), "fallback"
        result = self._llm.completion(
            prompt,
            retry=10,
            callback=make_validator(agent_names),
            failsafe=None,
            return_type=RoundRecapResponse,
            caller="story_recap",
        )
        if result is None:
            return build_round_fallback(round_recap), "fallback"
        return result, "ok"

    def generate_cumulative(
        self, opening: str, rounds: list[RoundRecap], agent_names: list[str],
    ) -> tuple[str, str]:
        """分層摘要（spec §5.3）。rounds 為空 → 捷徑模板；LLM 全敗 → fallback。"""
        if not rounds:
            return opening_only_text(opening), "ok"
        if self._llm is None:
            return build_cumulative_fallback(opening, rounds), "fallback"
        round_summaries = [
            {"round": r.round, "recap": r.round_recap} for r in rounds if r.round_recap
        ]
        latest_round = rounds[-1]
        try:
            prompt = self._prompts.cumulative_input(opening, round_summaries, latest_round)
        except Exception:
            logger.warning("recap generator: cumulative prompt 組裝失敗", exc_info=True)
            return build_cumulative_fallback(opening, rounds), "fallback"
        budget = int(self._context_window * CONTEXT_BUDGET_RATIO)
        if self.estimate_tokens(prompt) > budget:
            logger.warning(
                "recap generator: cumulative prompt %d tokens 超過 60%% 紅線（%d）",
                self.estimate_tokens(prompt), budget,
            )
        result = self._llm.completion(
            prompt,
            retry=10,
            callback=make_validator(agent_names),
            failsafe=None,
            return_type=CumulativeRecapResponse,
            caller="story_recap",
        )
        if result is None:
            return build_cumulative_fallback(opening, rounds), "fallback"
        return result, "ok"
