"""story_weaver.memory_seed — 記憶注入（推演啟動系統嘅契約接口）.

故事創建後、第一個 step 前，對每個 agent 注入：
1) 故事開端 event（poignancy 9）
2) 每段以佢為 from 嘅關係 thought（poignancy 按 |score| 映射）

前提：Game 已 create（timer 已 set）。即 SimulateServer __init__ 之後、
simulate(step=1) 之前。玩家指令注入系統重用 inject_event 同一條路徑。
"""

import logging

from modules.memory.event import Event

logger = logging.getLogger(__name__)

OPENING_POIGNANCY: int = 9


def score_to_poignancy(score: int) -> int:
    """|score| 線性映射：0→1，100→8。"""
    return int(1 + 7 * abs(score) / 100)


def opening_event(agent_name: str, story_opening: str, address: list[str]) -> Event:
    return Event(
        subject=agent_name,
        predicate="經歷",
        object="故事開端",
        describe=story_opening,
        address=address,
    )


def relationship_thought(agent_name: str, to: str, score: int, desc: str) -> Event:
    describe = f"我對{to}嘅好感係 {score:+d}：{desc or '陌生'}"
    return Event(
        subject=agent_name,
        predicate="諗住",
        object=to,
        describe=describe,
    )


def inject_story_memories(game, story: dict) -> dict[str, int]:
    """對 story["characters"] 每個 agent 注入開端 event + 關係 thought。

    返 {agent_name: 注入咗幾多 node}。任一 agent 注入唔到會 raise（唔静默）。
    """
    counts: dict[str, int] = {}
    relationships = story.get("relationships", [])
    for name in story["characters"]:
        agent = game.get_agent(name)
        home = list(agent.spatial.address.get("living_area", []))
        agent.associate.add_node(
            "event",
            opening_event(name, story["story_opening"], home),
            poignancy=OPENING_POIGNANCY,
        )
        injected = 1
        for rel in relationships:
            if rel.get("from") != name:
                continue
            agent.associate.add_node(
                "thought",
                relationship_thought(name, rel["to"], rel["score"], rel.get("desc", "")),
                poignancy=score_to_poignancy(rel["score"]),
            )
            injected += 1
        counts[name] = injected
        logger.info("已注入 %d 條記憶俾「%s」", injected, name)
    return counts


def inject_event(game, agent_name: str, describe: str, poignancy: int) -> None:
    """通用單條注入——玩家指令注入系統重用呢條路徑。poignancy ∈ [1,10]。"""
    if not 1 <= poignancy <= 10:
        raise ValueError("poignancy 必須喺 1 到 10 之間")
    agent = game.get_agent(agent_name)
    try:
        address = agent.get_tile().get_address(as_list=True)
    except Exception:
        address = []
    agent.associate.add_node(
        "event",
        Event(subject=agent_name, predicate="經歷", object="事件",
              describe=describe, address=address),
        poignancy=poignancy,
    )
