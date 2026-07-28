"""GM 調整 + 記憶注入測試（spec §7）。LLM 全部 stub，唔准真 call。"""

import logging

from story_weaver.affinity import (
    AffinityStore,
    GMAdjustmentItem,
    GMAdjustmentResponse,
    apply_gm_response,
    initial_poignancy,
    inject_change,
    inject_initial,
)
from story_weaver.affinity.models import AffinityChange

NAMES = ["阿珍", "阿強", "小美", "阿明"]
logger = logging.getLogger("test")


class FakeAgent:
    """記錄 _add_concept 呼叫嘅 stub（唔掂 LLM / embedding）。"""

    def __init__(self, name):
        self.name = name
        self.concepts = []

    def _add_concept(self, e_type, event, create=None, expire=None, filling=None,
                     poignancy_override=None):
        self.concepts.append(
            {"e_type": e_type, "event": event, "poignancy": poignancy_override}
        )


def make_env():
    store = AffinityStore({}, list(NAMES))
    agents = {n: FakeAgent(n) for n in NAMES}
    rounds_log = []
    return store, agents, rounds_log


# ---------------------------------------------------------------- apply_gm_response


def test_apply_happy_path():
    store, agents, rounds_log = make_env()
    store.set_affinity("阿珍", "阿強", -65, "舊情人")
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強", delta=15, reason="阿強幫阿珍解圍"),
    ])
    changes = apply_gm_response(store, resp, agents, rounds_log, 1, logger)
    assert len(changes) == 1
    assert changes[0].old == -65 and changes[0].new == -50 and changes[0].delta == 15
    assert store.get("阿珍", "阿強").value == -50
    # |delta| >= 10 → 注入記憶，poignancy 8
    assert len(agents["阿珍"].concepts) == 1
    assert agents["阿珍"].concepts[0]["poignancy"] == 8
    assert agents["阿珍"].concepts[0]["e_type"] == "thought"
    # rounds_log append
    assert rounds_log[0]["round"] == 1
    assert rounds_log[0]["changes"][0]["reason"] == "阿強幫阿珍解圍"


def test_apply_filters_hallucinated_agents():
    store, agents, rounds_log = make_env()
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="鬼", delta=10, reason="幻覺"),
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強", delta=5, reason="正常"),
    ])
    changes = apply_gm_response(store, resp, agents, rounds_log, 1, logger)
    assert len(changes) == 1
    assert changes[0].to_agent == "阿強"


def test_apply_delta_clamped_25():
    store, agents, rounds_log = make_env()
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強", delta=25, reason="大好事"),
    ])
    changes = apply_gm_response(store, resp, agents, rounds_log, 1, logger)
    assert changes[0].delta == 25
    # pydantic 層已經擋咗超 ±25 嘅輸入
    import pytest
    with pytest.raises(Exception):
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強", delta=26)


def test_apply_absolute_reset():
    store, agents, rounds_log = make_env()
    store.set_affinity("阿珍", "阿強", -65)
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強",
                         set_absolute=True, absolute_value=50, reason="世紀和解"),
    ])
    changes = apply_gm_response(store, resp, agents, rounds_log, 2, logger)
    assert changes[0].new == 50 and changes[0].absolute is True
    # absolute → poignancy 10
    assert agents["阿珍"].concepts[0]["poignancy"] == 10


def test_apply_small_delta_no_memory_injection():
    store, agents, rounds_log = make_env()
    resp = GMAdjustmentResponse(adjustments=[
        GMAdjustmentItem(from_agent="阿珍", to_agent="阿強", delta=5, reason="點頭之交"),
    ])
    changes = apply_gm_response(store, resp, agents, rounds_log, 1, logger)
    assert len(changes) == 1
    assert agents["阿珍"].concepts == []  # |delta| < 10 唔注入


def test_apply_empty_response_still_appends_round():
    store, agents, rounds_log = make_env()
    changes = apply_gm_response(
        store, GMAdjustmentResponse(adjustments=[]), agents, rounds_log, 3, logger
    )
    assert changes == []
    assert rounds_log[-1] == {
        "round": 3, "step": 0, "time": rounds_log[-1]["time"], "changes": []
    }


def test_apply_none_response_no_crash():
    store, agents, rounds_log = make_env()
    changes = apply_gm_response(store, None, agents, rounds_log, 1, logger)
    assert changes == []
    assert len(rounds_log) == 1


def test_failsafe_is_empty_adjustments():
    """LLM 連續垃圾 → failsafe 空調整，模擬唔斷。"""
    from story_weaver.affinity import build_gm_prompt  # noqa: F401

    failsafe = GMAdjustmentResponse(adjustments=[])
    store, agents, rounds_log = make_env()
    changes = apply_gm_response(store, failsafe, agents, rounds_log, 1, logger)
    assert changes == []


# ---------------------------------------------------------------- 記憶注入


def test_initial_poignancy_bands():
    assert initial_poignancy(0) == 8
    assert initial_poignancy(60) == 8
    assert initial_poignancy(-61) == 9
    assert initial_poignancy(80) == 9
    assert initial_poignancy(-81) == 10
    assert initial_poignancy(100) == 10


def test_inject_initial_only_nonzero_and_idempotent():
    store, agents, _ = make_env()
    store.set_affinity("阿珍", "阿強", -65, "舊情人")
    store.set_affinity("阿強", "阿珍", 90, "想挽回")
    meta = {}
    count = inject_initial(store, agents, meta, logger)
    assert count == 2
    assert agents["阿珍"].concepts[0]["poignancy"] == 9   # |-65| → 9
    assert agents["阿強"].concepts[0]["poignancy"] == 10  # 90 → 10
    assert "舊情人" in agents["阿珍"].concepts[0]["event"].get_describe()
    assert meta["affinity_initialized"] is True
    # 冧等：第二次 skip
    assert inject_initial(store, agents, meta, logger) == 0
    assert len(agents["阿珍"].concepts) == 1


def test_inject_initial_empty_matrix_zero():
    store, agents, _ = make_env()
    assert inject_initial(store, agents, {}, logger) == 0


def test_inject_change_describe_format():
    agent = FakeAgent("阿珍")
    change = AffinityChange(
        from_agent="阿珍", to_agent="阿強", old=-65, new=-50,
        delta=15, reason="阿強幫阿珍解圍",
    )
    inject_change(agent, change, logger)
    desc = agent.concepts[0]["event"].get_describe()
    assert "阿強幫阿珍解圍" in desc and "-65→-50" in desc


def test_build_gm_prompt_fills_template(tmp_path):
    from story_weaver.affinity import build_gm_prompt

    # S1 回歸：唔好寫死相對路徑，用絕對路徑（邊個 cwd 行都得）
    import os
    prompts_dir = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "generative_agents", "data", "prompts"
    ))
    store, _, _ = make_env()
    store.set_affinity("阿珍", "阿強", -65, "舊情人")
    res = build_gm_prompt(
        store, ["阿珍同阿強嗌交"], {"阿珍 -> 阿強 @ 咖啡廳": [("阿珍", "你走開")]},
        logger, template_path=prompts_dir,
    )
    assert "-65" in res.prompt and "嗌交" in res.prompt and "你走開" in res.prompt
    assert res.failsafe.adjustments == []
    assert res.return_type is GMAdjustmentResponse
