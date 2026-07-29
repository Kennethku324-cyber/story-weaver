"""story_weaver.gm.relations — 好感度 currently 前綴（spec §1.6、§2.3）。

每回合開始將【人際關係】快照前綴注入各 agent 嘅 scratch.currently，
令好感度數值進入 base_desc → 影響 decide_chat / generate_chat / reflect。
前綴係「回合級快照」：回合內被 agents 自身 retrieve_currently 覆寫屬預期，
下回合開始再重注。剝離上回合前綴防無限堆疊。
"""

from __future__ import annotations

import logging

from story_weaver.affinity.store import AffinityStore

logger = logging.getLogger(__name__)

PREFIX_HEADER = "【人際關係】"


def render_relations_block(store: AffinityStore, agent_name: str) -> str:
    """生成【人際關係】前綴文字。全部關係都係 0 → 空字串（唔佔 prompt 空間）。"""
    parts = []
    for other in store.agent_names:
        if other == agent_name:
            continue
        out_entry = store.get(agent_name, other)
        in_entry = store.get(other, agent_name)
        if out_entry.value == 0 and in_entry.value == 0:
            continue
        seg = f"你對{other}的好感度為 {out_entry.value}"
        if out_entry.value != 0:
            seg += f"（{AffinityStore.band_of(out_entry.value)}）"
            if out_entry.label:
                seg += f"，{out_entry.label}"
        seg += f"；{other}對你的好感度為 {in_entry.value}。"
        parts.append(seg)
    if not parts:
        return ""
    return PREFIX_HEADER + "".join(parts)


def apply_relations_prefix(
    store: AffinityStore,
    agents: dict,
    last_prefixes: dict[str, str] | None = None,
) -> dict[str, str]:
    """對每個 agent：剝離上回合前綴（若 currently 以佢開頭），再套新前綴。

    回傳本回合前綴 dict（落 gm_state["last_relations_prefix"] 供下回合剝離）。
    任何單個 agent 失敗唔影響其他（log + continue）。
    """
    last_prefixes = last_prefixes or {}
    new_prefixes: dict[str, str] = {}
    for name, agent in agents.items():
        try:
            currently = getattr(agent.scratch, "currently", "") or ""
            old = last_prefixes.get(name, "")
            if old and currently.startswith(old):
                currently = currently[len(old):].lstrip("\n")
            prefix = render_relations_block(store, name)
            if prefix:
                agent.scratch.currently = prefix + "\n" + currently if currently else prefix
            else:
                agent.scratch.currently = currently
            new_prefixes[name] = prefix
        except Exception:
            logger.warning("gm relations: 前綴注入失敗（%s）", name, exc_info=True)
            new_prefixes[name] = ""
    return new_prefixes
