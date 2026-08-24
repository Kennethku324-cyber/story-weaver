"""story_weaver.recap.prompts — RecapPrompt loader + LLM 輸出校驗模型（spec §5.1）。

模板用 string.Template（同 scratch.py 手法），放 data/prompts/（第 30、31 個模板）。
唔入 Scratch（嗰個係 agent 專用）。
"""

from __future__ import annotations

import logging
import os
from string import Template

from pydantic import BaseModel, Field
from story_weaver.paths import RECAP_PROMPTS_ROOT

logger = logging.getLogger(__name__)


class RoundRecapResponse(BaseModel):
    res: str = Field(description="本回合敘事摘要，繁體中文書面語，150-250字")


class CumulativeRecapResponse(BaseModel):
    res: str = Field(description="由開端到而家嘅完整敘事回顧，繁體中文書面語，300-600字")


class RecapPrompt:
    def __init__(self, template_path: str | None = None) -> None:
        self._dir = template_path or str(RECAP_PROMPTS_ROOT)

    def _load(self, filename: str) -> Template:
        with open(os.path.join(self._dir, filename), "r", encoding="utf-8") as f:
            return Template(f.read())

    def build(self, filename: str, data: dict) -> str:
        return self._load(filename).substitute(**data)

    def round_input(self, opening: str, round_recap, prev_recaps: list[str]) -> str:
        """渲染 story_recap_round.txt。"""
        events_text = "\n".join(
            f"- {e.sim_time} {e.agent} @ {e.location}：{e.describe}"
            for e in round_recap.events
        ) or "（本回合無特別事件）"
        dialogues_text = _format_dialogues(round_recap.dialogues)
        prev_text = "\n".join(
            f"第{i + 1}回合：{r}" for i, r in enumerate(prev_recaps)
        ) or "（呢個係第一回合）"
        decision_text = "（玩家上一回合無干預）"
        if round_recap.player_decision:
            decision_text = f"玩家嘅決定：{round_recap.player_decision.text}"
        return self.build(
            "story_recap_round.txt",
            {
                "opening": opening,
                "round_no": str(round_recap.round),
                "sim_time_start": round_recap.sim_time_start,
                "sim_time_end": round_recap.sim_time_end,
                "prev_recaps": prev_text,
                "player_decision": decision_text,
                "events": events_text,
                "dialogues": dialogues_text,
            },
        )

    def cumulative_input(self, opening: str, round_summaries: list[dict], latest_round) -> str:
        """渲染 story_recap_cumulative.txt（分層摘要，spec §5.3）。

        input = 開端 + 各回合 round_recap（每段截 200 字保底）+ 最新回合完整事件/對話。
        絕對唔將 N 回合原文一次過塞入。
        """
        summaries_text = "\n".join(
            f"第{s['round']}回合：{(s.get('recap') or '')[:200]}"
            for s in round_summaries
        ) or "（暫無回合記錄）"
        if latest_round is not None:
            events_text = "\n".join(
                f"- {e.sim_time} {e.agent} @ {e.location}：{e.describe}"
                for e in latest_round.events
            ) or "（本回合無特別事件）"
            dialogues_text = _format_dialogues(latest_round.dialogues)
            latest_header = f"第{latest_round.round}回合"
        else:
            events_text = "（暫無）"
            dialogues_text = "（暫無）"
            latest_header = "（暫無）"
        return self.build(
            "story_recap_cumulative.txt",
            {
                "opening": opening,
                "round_summaries": summaries_text,
                "latest_header": latest_header,
                "latest_events": events_text,
                "latest_dialogues": dialogues_text,
            },
        )


def _format_dialogues(dialogues) -> str:
    """對話區塊 → 文本，對白原文逐字放入引號。"""
    out = []
    for block in dialogues or []:
        participants = " -> ".join(block.participants)
        header = f"【對話】{participants} @ {block.location}"
        if block.degraded:
            header += "（對話原文已散佚，以下為角色回憶）"
        out.append(header)
        for line in block.lines:
            out.append(f"{line.speaker}：「{line.text}」")
    return "\n".join(out) or "（本回合無對話）"
