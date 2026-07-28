"""story_weaver.affinity.gm — GM 好感度調整（spec §2.4、§3.1）。

build_gm_prompt 組 prompt；apply_gm_response 係 GM 系統唯一寫入路徑，
負責白名單過濾、雙重 clamp、記憶注入、rounds log。
任何異常都唔會 throw 上 SimulateServer（內部 try/except + failsafe）。
"""

from __future__ import annotations

import logging
import os
from string import Template
from typing import Optional

from pydantic import BaseModel, Field

from .memory import inject_change
from .models import AFFINITY_MAX, AFFINITY_MIN, DELTA_CLAMP, AffinityChange
from .store import AffinityStore

logger = logging.getLogger(__name__)


class GMAdjustmentItem(BaseModel):
    from_agent: str
    to_agent: str
    delta: int = Field(default=0, ge=-DELTA_CLAMP, le=DELTA_CLAMP)
    reason: str = Field(default="", max_length=200)
    set_absolute: bool = False
    absolute_value: Optional[int] = Field(default=None, ge=AFFINITY_MIN, le=AFFINITY_MAX)


class GMAdjustmentResponse(BaseModel):
    adjustments: list[GMAdjustmentItem] = []


def build_gm_prompt(
    store: AffinityStore,
    round_events: list[str],
    round_conversations: dict,
    logger=None,
    template_path: str = "data/prompts",
):
    """讀 gm_adjust_affinity.txt，填入 ${matrix_text}/${events}/${conversations}。

    返回 modules.prompt.scratch.Result 同構 namedtuple，
    failsafe = GMAdjustmentResponse(adjustments=[])（LLM 連續垃圾 → 本回合靜默唔調整）。
    """
    from modules.prompt.scratch import Result  # lazy import，避免引擎未 ready 時出事

    log = logger or globals()["logger"]
    with open(
        os.path.join(template_path, "gm_adjust_affinity.txt"), "r", encoding="utf-8"
    ) as f:
        template = Template(f.read())
    events_text = "\n".join(f"- {e}" for e in round_events) or "（本回合無特別事件）"
    conv_lines = []
    for key, chats in (round_conversations or {}).items():
        conv_lines.append(f"- {key}")
        for name, text in chats:
            conv_lines.append(f"  {name}：{text}")
    conversations_text = "\n".join(conv_lines) or "（本回合無對話）"
    prompt = template.substitute(
        matrix_text=store.full_matrix_text(),
        events=events_text,
        conversations=conversations_text,
    )
    log.debug("affinity GM prompt built (%d chars)", len(prompt))
    return Result(prompt, None, GMAdjustmentResponse(adjustments=[]), GMAdjustmentResponse)


def _now() -> tuple[int, str]:
    """由全域 timer 攞 step 近似值同時間字串；任何異常返回 (0, "")。"""
    try:
        from modules import utils

        timer = utils.get_timer()
        return 0, timer.get_date("%Y%m%d-%H:%M")
    except Exception:
        return 0, ""


def apply_gm_response(
    store: AffinityStore,
    response: GMAdjustmentResponse,
    agents: dict,
    rounds_log: list,
    round_no: int,
    logger=None,
) -> list[AffinityChange]:
    """逐條 GMAdjustmentItem 生效；最後 append 回合記錄落 rounds_log。

    保證任何異常都唔會 throw 上嚟。返回本回合實際生效嘅 AffinityChange 列表。
    """
    log = logger or globals()["logger"]
    changes: list[AffinityChange] = []
    try:
        for item in (response.adjustments if response else []):
            if item.from_agent not in agents or item.to_agent not in agents:
                log.warning(
                    "affinity GM: 跳過未知角色「%s → %s」（LLM 幻覺？）",
                    item.from_agent,
                    item.to_agent,
                )
                continue
            try:
                change = store.adjust(
                    item.from_agent,
                    item.to_agent,
                    item.delta,
                    item.reason,
                    absolute=item.set_absolute,
                    absolute_value=item.absolute_value,
                )
            except Exception:
                log.warning(
                    "affinity GM: 調整失敗（%s → %s）", item.from_agent, item.to_agent,
                    exc_info=True,
                )
                continue
            if change is None:
                continue
            change.round = round_no
            _, change.time = _now()
            if abs(change.delta) >= 10 or change.absolute:
                inject_change(agents[change.from_agent], change, log)
            changes.append(change)
    except Exception:
        log.warning("affinity GM: apply_gm_response 中途異常", exc_info=True)
    try:
        step, time_str = _now()
        rounds_log.append(
            {
                "round": round_no,
                "step": step,
                "time": time_str,
                "changes": [c.to_dict() for c in changes],
            }
        )
    except Exception:
        log.warning("affinity GM: rounds_log append 失敗", exc_info=True)
    return changes
