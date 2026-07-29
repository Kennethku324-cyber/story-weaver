"""story_weaver.gm.delta — RoundDeltaCollector：回合增量採集（spec §1.7、§2.5）。

Agent.concepts 每 step 被 percept() 重置，無法回溯成個回合；
但 Associate.memory 嘅 node_id 只增唔減（新節點 insert(0)），
所以用「回合開始影相、回合結束 diff」嘅方式零侵入採集。
baseline 落 gm_state.json，中途斷線可重建。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_MEMORY_TYPES = ("event", "chat", "thought")


@dataclass
class RoundBaseline:
    conversation_keys: list[str] = field(default_factory=list)
    memory_node_ids: dict[str, list[str]] = field(default_factory=dict)  # agent_name -> node_ids
    sim_time: str = ""  # config["time"]，例 "20240213-14:30"


@dataclass
class RoundDelta:
    conversations_delta: dict = field(default_factory=dict)  # {時間key: [{"A -> B @ 地址": [[名, 對白], ...]}]}，原文
    events_delta: list[dict] = field(default_factory=list)  # Concept.abstract() 展開 + agent_name 欄位
    agent_states: dict = field(default_factory=dict)  # config["agents"][名] 嘅 currently/status/action 摘要
    is_quiet: bool = False  # 邊界 9：無新對話且全部新 event poignancy <= 1


class RoundDeltaCollector:
    """無狀態採集器（baseline 由 GMDirector / GMStateStore 持有）。"""

    def snapshot(self, server) -> RoundBaseline:
        """回合開始：記錄 conversation keys + 各 agent memory node_ids。"""
        baseline = RoundBaseline()
        try:
            baseline.conversation_keys = list(server.game.conversation.keys())
        except Exception:
            logger.warning("gm delta: conversation 快照失敗", exc_info=True)
        for name, agent in server.game.agents.items():
            try:
                ids: list[str] = []
                memory = agent.associate.memory
                for n_type in _MEMORY_TYPES:
                    ids.extend(memory.get(n_type, []))
                baseline.memory_node_ids[name] = ids
            except Exception:
                logger.warning("gm delta: %s memory 快照失敗", name, exc_info=True)
                baseline.memory_node_ids[name] = []
        try:
            baseline.sim_time = str(server.config.get("time", ""))
        except Exception:
            pass
        return baseline

    def collect(self, server, baseline: RoundBaseline) -> RoundDelta:
        """回合結束：diff 出新 conversation + 新 concepts + agent 狀態摘要。"""
        delta = RoundDelta()
        seen_conv = set(baseline.conversation_keys)
        try:
            for key, convo in server.game.conversation.items():
                if key not in seen_conv:
                    delta.conversations_delta[key] = convo
        except Exception:
            logger.warning("gm delta: conversation diff 失敗", exc_info=True)

        max_new_poignancy = 0
        for name, agent in server.game.agents.items():
            seen = set(baseline.memory_node_ids.get(name, []))
            try:
                memory = agent.associate.memory
                for n_type in _MEMORY_TYPES:
                    for node_id in memory.get(n_type, []):
                        if node_id in seen:
                            continue
                        concept = agent.associate.find_concept(node_id)
                        if concept is None:
                            continue
                        entry = {
                            "agent_name": name,
                            "node_type": n_type,
                            "describe": concept.describe,
                            "poignancy": concept.poignancy,
                        }
                        delta.events_delta.append(entry)
                        if n_type == "event":
                            max_new_poignancy = max(max_new_poignancy, concept.poignancy)
            except Exception:
                logger.warning("gm delta: %s concept diff 失敗", name, exc_info=True)

        for name, agent_cfg in (server.config.get("agents") or {}).items():
            try:
                delta.agent_states[name] = {
                    "currently": agent_cfg.get("currently", ""),
                    "status": agent_cfg.get("status", {}),
                    "action": agent_cfg.get("action", {}),
                }
            except Exception:
                delta.agent_states[name] = {}

        # 邊界 9：靜默回合 = 無新對話 且 全部新 event poignancy <= 1
        delta.is_quiet = not delta.conversations_delta and max_new_poignancy <= 1
        return delta
