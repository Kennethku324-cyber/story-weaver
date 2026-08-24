"""story_weaver.gm.prompter — GM prompt 組裝 + LLM 呼叫（spec §5）。

模板用 string.Template（同 modules/prompt/scratch.py 手法），
呼叫 LLMModel.completion(prompt, return_type=XxxResponse, failsafe=...)。
OpenAILLMModel._completion 會自動拆 .res，所以 completion 返內層模型。
"""

from __future__ import annotations

import logging
import os
from string import Template

from story_weaver.paths import GM_PROMPTS_ROOT

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

    def __init__(self, llm, prompts_dir: str | None = None) -> None:
        self._llm = llm
        self._dir = prompts_dir or str(GM_PROMPTS_ROOT)

    def round_analysis(
        self,
        agent_names: list[str],
        story_seed: str,
        branch_history: list[str],
        recent_history: str = "",
        events: list[dict] | None = None,
        conversations: dict | None = None,
        agent_inner: str = "",
        matrix_text: str = "",
        round_no: int = 1,
        max_rounds: int = 4,
        dramatic_pressure: int = 1,
        unresolved_threads: list[str] | None = None,
        allow_negative_elements: bool = False,
    ) -> GMRoundAnalysis | None:
        """一次 call 出摘要+分支點+選項+好感度建議。全敗 → None（上層行 failsafe）。"""
        try:
            template = _load_template(self._dir, "gm_round_summary.txt")
            remaining = max_rounds - round_no
            if round_no >= max_rounds:
                ending_pressure = "⚠️ 呢個係最後一回合——你必須將所有伏筆收束，推動故事走向一個完整嘅結局。角色必須面對故事開端所設定嘅核心衝突。"
            elif remaining <= 2:
                ending_pressure = f"⚠️ 仲有 {remaining} 個回合就要結局。劇情必須明顯推進——唔可以再拖、唔可以再等。角色必須開始行動、見面，直面故事開端嘅核心矛盾。"
            else:
                ending_pressure = f"故事正處於第 {round_no} 回合，要為後面嘅高潮做好鋪墊。引導角色相遇、為故事開端嘅衝突埋下伏筆。"
            prompt = template.substitute(
                agent_names="、".join(agent_names),
                story_seed=story_seed or "（無）",
                branch_history="\n".join(f"- {b}" for b in branch_history) or "（暫無）",
                recent_history=recent_history or "（故事剛剛開始）",
                events=_format_events(events or []),
                conversations=_format_conversations(conversations or {}),
                agent_inner=agent_inner or "（無法讀取角色狀態）",
                matrix_text=matrix_text or "（無數據）",
                round_no=str(round_no),
                max_rounds=str(max_rounds),
                ending_pressure=ending_pressure,
                dramatic_pressure=str(dramatic_pressure),
                unresolved_threads="\n".join(f"- {thread}" for thread in (unresolved_threads or [])) or "（暫無）",
                negative_elements_policy=(
                    "可按故事需要使用負面元素，但避免過度血腥或不適合學生的細節。"
                    if allow_negative_elements else
                    "不得加入死亡、暴力傷害、恐怖威脅、霸凌、仇恨或其他負面元素；用安全而建設性的衝突推動故事。"
                ),
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
