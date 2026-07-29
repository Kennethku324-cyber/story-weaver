"""relations 前綴測試（spec §1.6、§7）：渲染、剝離防堆疊、空矩陣。"""

from story_weaver.affinity.store import AffinityStore
from story_weaver.gm.relations import (
    PREFIX_HEADER,
    apply_relations_prefix,
    render_relations_block,
)

NAMES = ["阿珍", "阿強", "小美", "阿明"]


class FakeScratch:
    def __init__(self, currently=""):
        self.currently = currently


class FakeAgent:
    def __init__(self, name, currently=""):
        self.name = name
        self.scratch = FakeScratch(currently)


def make_store():
    return AffinityStore({}, list(NAMES))


def test_render_empty_matrix_returns_empty():
    store = make_store()
    assert render_relations_block(store, "阿珍") == ""


def test_render_with_values_and_labels():
    store = make_store()
    store.set_affinity("阿珍", "阿強", -65, "舊情人，分手時鬧得好僵")
    store.set_affinity("阿強", "阿珍", 40)
    block = render_relations_block(store, "阿珍")
    assert block.startswith(PREFIX_HEADER)
    assert "你對阿強的好感度為 -65（死敵/痛恨）" in block
    assert "舊情人，分手時鬧得好僵" in block
    assert "阿強對你的好感度為 40" in block
    # 其他全 0 嘅關係唔出現
    assert "小美" not in block


def test_render_band_labels():
    store = make_store()
    store.set_affinity("阿珍", "阿強", 80)
    block = render_relations_block(store, "阿珍")
    assert "（摯愛/至交）" in block


def test_apply_prefix_injects():
    store = make_store()
    store.set_affinity("阿珍", "阿強", 50)
    agents = {n: FakeAgent(n, "諗緊今日做咩") for n in NAMES}
    prefixes = apply_relations_prefix(store, agents, {})
    assert agents["阿珍"].scratch.currently.startswith(PREFIX_HEADER)
    assert "諗緊今日做咩" in agents["阿珍"].scratch.currently
    # 全 0 嘅阿明：無前綴，currently 原樣
    assert prefixes["阿明"] == ""
    assert agents["阿明"].scratch.currently == "諗緊今日做咩"


def test_apply_prefix_strips_old_no_stacking():
    store = make_store()
    store.set_affinity("阿珍", "阿強", 50)
    agents = {"阿珍": FakeAgent("阿珍", "原始諗法")}
    first = apply_relations_prefix(store, agents, {})
    # 模擬第二回合：currently 而家係 前綴+原始諗法，剝離後再套新前綴
    store.set_affinity("阿珍", "阿強", -30)
    second = apply_relations_prefix(store, agents, first)
    currently = agents["阿珍"].scratch.currently
    assert currently.count(PREFIX_HEADER) == 1  # 唔會堆疊
    assert "原始諗法" in currently
    assert "-30" in currently and "50" not in currently
    assert second["阿珍"] != first["阿珍"]


def test_apply_prefix_survives_agent_overwrite():
    # 回合內 agent 自己改寫咗 currently（前綴被覆蓋）→ 下回合直接套新前綴，唔會炸
    store = make_store()
    store.set_affinity("阿珍", "阿強", 50)
    agents = {"阿珍": FakeAgent("阿珍", "原始諗法")}
    first = apply_relations_prefix(store, agents, {})
    agents["阿珍"].scratch.currently = "agent 自己寫嘅新諗法"  # 前綴冇咗
    apply_relations_prefix(store, agents, first)
    currently = agents["阿珍"].scratch.currently
    assert currently.count(PREFIX_HEADER) == 1
    assert "agent 自己寫嘅新諗法" in currently
