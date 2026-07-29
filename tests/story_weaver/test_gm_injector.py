"""MemoryInjector 測試（spec §7）：注入路徑、poignancy boost、FORBIDDEN_TOKENS 攔截。

用 FakeAgent stub 記錄 _add_concept 呼叫（同 affinity tests 手法），唔掂真 LLM / embedding。
"""

import pytest

from modules.prompt.keywords import KW_CHAT, KW_IDLE, KW_PENDING, KW_SLEEPING
from story_weaver.gm.injector import (
    INJECT_OBJECT,
    PREDICATE_CUSTOM,
    PREDICATE_OPTION,
    MemoryInjector,
)


class FakeConcept:
    def __init__(self, node_id):
        self.node_id = node_id


class FakeAgent:
    def __init__(self, name):
        self.name = name
        self.status = {"poignancy": 5}
        self.calls = []

    def _add_concept(self, e_type, event, create=None, expire=None, filling=None,
                     poignancy_override=None):
        self.calls.append(
            {"e_type": e_type, "event": event, "poignancy_override": poignancy_override}
        )
        return FakeConcept(f"node-{len(self.calls)}")


def test_inject_happy_path():
    agent = FakeAgent("阿珍")
    injector = MemoryInjector()
    node_id = injector.inject(
        agent,
        "阿珍得知阿強一直隱瞞著一封信的存在。",
        predicate=PREDICATE_OPTION,
        poignancy=8,
    )
    assert node_id == "node-1"
    call = agent.calls[0]
    assert call["e_type"] == "event"  # GM 意志係「發生過嘅事」，唔係 thought
    assert call["poignancy_override"] == 8  # 跳過 LLM 評分
    assert call["event"].subject == "阿珍"
    assert call["event"].predicate == PREDICATE_OPTION
    assert call["event"].object == INJECT_OBJECT  # 避開 KW_IDLE fallback
    # poignancy boost 推 agent 過 reflect 閾值
    assert agent.status["poignancy"] == 5 + 20


def test_inject_custom_poignancy_10():
    agent = FakeAgent("阿強")
    MemoryInjector().inject(
        agent,
        "阿強被命運驅使，決定主動約阿珍去玫瑰酒吧見面。",
        predicate=PREDICATE_CUSTOM,
        poignancy=10,
    )
    assert agent.calls[0]["poignancy_override"] == 10


def test_forbidden_tokens_rejected():
    injector = MemoryInjector()
    for token in (KW_SLEEPING, KW_CHAT, KW_IDLE, KW_PENDING):
        with pytest.raises(ValueError):
            injector.inject(
                FakeAgent("阿珍"), f"阿珍{token}了一陣。", PREDICATE_OPTION, 8
            )


def test_forbidden_tokens_are_traditional_keywords():
    # 禁詞表必須引用 keywords.py 常量（本地化 SSoT），唔係自行 hardcode 簡體
    assert set(MemoryInjector.FORBIDDEN_TOKENS) == {
        KW_SLEEPING, KW_CHAT, KW_IDLE, KW_PENDING,
    }
    assert "睡觉" not in MemoryInjector.FORBIDDEN_TOKENS  # 簡體版本唔喺度


def test_empty_describe_rejected():
    with pytest.raises(ValueError):
        MemoryInjector().inject(FakeAgent("阿珍"), "", PREDICATE_OPTION, 8)
    with pytest.raises(ValueError):
        MemoryInjector().inject(FakeAgent("阿珍"), "   ", PREDICATE_OPTION, 8)
