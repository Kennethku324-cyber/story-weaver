"""story_weaver.gm.prompter — GM prompt 組裝 + LLM 呼叫（spec §5）。

模板用 string.Template（同 modules/prompt/scratch.py 手法），
呼叫 LLMModel.completion(prompt, return_type=XxxResponse, failsafe=...)。
OpenAILLMModel._completion 會自動拆 .res，所以 completion 返內層模型。
"""

from __future__ import annotations

import logging
import os
from string import Template

from .models import (
    CustomCommandParse,
    CustomCommandParseResponse,
    FinaleNarrative,
    FinaleNarrativeResponse,
    GMRoundAnalysis,
    GMRoundAnalysisResponse,
)

logger = logging.getLogger(__name__)


def _load_template(prompts_dir: str, filename: str) -> Template:
    with open(os.path.join(prompts_dir, filename), "r", encoding="utf-8") as f:
        return Template(f.read())


def _format_conversations(conversations: dict) -> str:
    """{時間key: [{"A -> B @ 地址": [[名, 對白], ...]}]} → 可讀文本（保留原文）。"""
    lines = []
    for time_key, blocks in (conversations or {}).items():
        lines.append(f"〔{time_key}〕")
        for block in blocks:
            for header, chats in block.items():
                lines.append(f"- {header}")
                for name, text in chats:
                    lines.append(f"  {name}：{text}")
    return "\n".join(lines) or "（本回合無對話）"


def _format_events(events: list[dict]) -> str:
    lines = []
    for e in events or []:
        lines.append(
            f"- [{e.get('agent_name', '?')}]（{e.get('node_type', '?')}"
            f"/重要度{e.get('poignancy', '?')}）{e.get('describe', '')}"
        )
    return "\n".join(lines) or "（本回合無特別事件）"


class GMPrompter:
    """三個 GM 模板嘅載入、組裝、呼叫。所有方法 LLM 失敗都返 failsafe，唔 raise。"""

    def __init__(self, llm, prompts_dir: str = "data/prompts_gm") -> None:
        self._llm = llm
        self._dir = prompts_dir

    def round_analysis(
        self,
        agent_names: list[str],
        story_seed: str,
        branch_history: list[str],
        events: list[dict],
        conversations: dict,
        matrix_text: str,
    ) -> GMRoundAnalysis | None:
        """一次 call 出摘要+分支點+選項+好感度建議。全敗 → None（上層行 failsafe）。"""
        try:
            template = _load_template(self._dir, "gm_round_summary.txt")
            prompt = template.substitute(
                agent_names="、".join(agent_names),
                story_seed=story_seed or "（無）",
                branch_history="\n".join(f"- {b}" for b in branch_history) or "（暫無）",
                events=_format_events(events),
                conversations=_format_conversations(conversations),
                matrix_text=matrix_text or "（無數據）",
            )
        except Exception:
            logger.warning("gm prompter: round_summary 模板組裝失敗", exc_info=True)
            return None
        return self._llm.completion(
            prompt,
            return_type=GMRoundAnalysisResponse,
            failsafe=None,
            caller="gm_round_summary",
        )

    def parse_custom_command(
        self,
        agent_names: list[str],
        story_context: str,
        command_text: str,
    ) -> CustomCommandParse:
        """自訂命令解析。全敗 → feasible=False（拒絕好過亂注，邊界 6）。"""
        failsafe = CustomCommandParse(
            targets=[],
            command_event_describe="",
            feasible=False,
            refuse_reason="命運之線暫時模糊，未能解讀你的命令，請稍後再試。",
        )
        try:
            template = _load_template(self._dir, "gm_custom_command.txt")
            prompt = template.substitute(
                agent_names="、".join(agent_names),
                story_context=story_context or "（故事剛開始）",
                command_text=command_text,
            )
        except Exception:
            logger.warning("gm prompter: custom_command 模板組裝失敗", exc_info=True)
            return failsafe
        result = self._llm.completion(
            prompt,
            return_type=CustomCommandParseResponse,
            failsafe=None,
            caller="gm_custom_command",
        )
        return result if result is not None else failsafe

    def finale(
        self,
        agent_names: list[str],
        story_seed: str,
        timeline_text: str,
        matrix_text: str,
    ) -> FinaleNarrative | None:
        """終章敘事。全敗 → None（上層行 failsafe）。"""
        try:
            template = _load_template(self._dir, "gm_finale.txt")
            prompt = template.substitute(
                agent_names="、".join(agent_names),
                story_seed=story_seed or "（無）",
                timeline=timeline_text,
                matrix_text=matrix_text or "（無數據）",
            )
        except Exception:
            logger.warning("gm prompter: finale 模板組裝失敗", exc_info=True)
            return None
        return self._llm.completion(
            prompt,
            return_type=FinaleNarrativeResponse,
            failsafe=None,
            caller="gm_finale",
        )
