"""story_weaver.affinity.memory — 記憶流投影（spec §3.1）。

初始注入（遊戲開始）+ 變動注入（GM 調整後），
共用 Agent._add_concept(poignancy_override=...) 路徑。
"""

from __future__ import annotations

import logging

from modules.memory import Event

from .models import AffinityChange
from .store import AffinityStore

logger = logging.getLogger(__name__)


def initial_poignancy(value: int) -> int:
    """|v| <= 60 → 8；61~80 → 9；81~100 → 10。"""
    v = abs(value)
    if v <= 60:
        return 8
    if v <= 80:
        return 9
    return 10


def inject_initial(store: AffinityStore, agents: dict, meta: dict, logger=None) -> int:
    """遊戲開始時，對每條 value != 0 嘅關係，向 from 角色記憶流注入 thought concept。

    冧等：meta["affinity_initialized"] == True 就 skip（resume 唔會重複注入）。
    返回注入數量。
    """
    log = logger or logging.getLogger(__name__)
    if meta.get("affinity_initialized"):
        return 0
    count = 0
    for a in store.agent_names:
        agent = agents.get(a)
        if agent is None:
            continue
        for b, raw in store.to_dict().get(a, {}).items():
            entry = store.get(a, b)
            if entry.value == 0:
                continue
            band = AffinityStore.band_of(entry.value)
            describe = (
                f"{a}與{b}的關係：{entry.label or band}"
                f"（好感度{entry.value}，{band}）"
            )
            event = Event(subject=a, predicate="對", object=b, describe=describe)
            try:
                agent._add_concept(
                    "thought", event, poignancy_override=initial_poignancy(entry.value)
                )
                count += 1
            except Exception:
                log.warning("affinity: 初始關係記憶注入失敗（%s → %s）", a, b, exc_info=True)
    meta["affinity_initialized"] = True
    return count


def inject_change(agent, change: AffinityChange, logger=None) -> None:
    """|delta| >= 10 或 absolute 時注入 thought；absolute 重置 poignancy 10，否則 8。"""
    log = logger or logging.getLogger(__name__)
    if abs(change.delta) < 10 and not change.absolute:
        return
    if change.absolute:
        poignancy = 10
        describe = (
            f"{change.reason}，{change.from_agent}對{change.to_agent}的態度徹底改變了"
            f"（好感度{change.old}→{change.new}）"
        )
    else:
        poignancy = 8
        describe = (
            f"{change.reason}，{change.from_agent}對{change.to_agent}的態度轉變了"
            f"（好感度{change.old}→{change.new}）"
        )
    event = Event(
        subject=change.from_agent,
        predicate="對",
        object=change.to_agent,
        describe=describe,
    )
    try:
        agent._add_concept("thought", event, poignancy_override=poignancy)
    except Exception:
        log.warning(
            "affinity: 變動記憶注入失敗（%s → %s）", change.from_agent, change.to_agent,
            exc_info=True,
        )
