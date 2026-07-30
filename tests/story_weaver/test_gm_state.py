"""GMStateStore 測試（spec §7）：原子寫入、損毀恢復、pending 逐字重現、timeline 累積。"""

import json
import os

from story_weaver.gm.models import GMDecision, GMOption, TimelineEntry
from story_weaver.gm.state import GMStateStore


def make_store(tmp_path):
    return GMStateStore(str(tmp_path / "gm_state.json"))


def test_save_load_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.init_new("一個關於背叛嘅故事", ["阿珍", "阿強", "小美", "阿明"])
    loaded = GMStateStore.load(str(tmp_path / "gm_state.json"))
    assert loaded.data["story_seed"] == "一個關於背叛嘅故事"
    assert loaded.data["agent_names"] == ["阿珍", "阿強", "小美", "阿明"]
    # story_seed 做第 0 項
    timeline = loaded.build_story_timeline()
    assert len(timeline) == 1 and timeline[0].round == 0
    assert timeline[0].summary == "一個關於背叛嘅故事"


def test_atomic_write_no_tmp_left(tmp_path):
    store = make_store(tmp_path)
    store.init_new("開端", ["阿珍", "阿強", "小美", "阿明"])
    assert os.path.exists(tmp_path / "gm_state.json")
    assert not os.path.exists(tmp_path / "gm_state.json.tmp")


def test_corrupted_main_recovers_from_tmp(tmp_path):
    path = str(tmp_path / "gm_state.json")
    store = GMStateStore(path)
    store.init_new("開端", ["阿珍", "阿強", "小美", "阿明"])
    # 模擬寫到一半斷電：主檔截斷，.tmp 留低完整備份
    with open(path, "r", encoding="utf-8") as f:
        good = f.read()
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(good)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"version": 1, "story_seed": "開')  # 截斷
    loaded = GMStateStore.load(path)
    assert loaded.data["story_seed"] == "開端"
    assert any("備份" in e["error"] for e in loaded.data["errors"])


def test_corrupted_both_starts_fresh(tmp_path):
    path = str(tmp_path / "gm_state.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("唔係 json")
    loaded = GMStateStore.load(path)
    assert loaded.data["timeline"] == []
    assert any("歸零" in e["error"] for e in loaded.data["errors"])
    # 流程唔斷：可以繼續用
    loaded.init_new("新開始", ["阿珍", "阿強", "小美", "阿明"])
    assert GMStateStore.load(path).data["story_seed"] == "新開始"


def test_missing_file_is_new_game(tmp_path):
    loaded = GMStateStore.load(str(tmp_path / "唔存在.json"))
    assert loaded.data["story_seed"] == ""
    assert loaded.get_pending_decision() is None


def test_pending_decision_roundtrip(tmp_path):
    store = make_store(tmp_path)
    store.init_new("開端", ["阿珍", "阿強", "小美", "阿明"])
    decision = GMDecision(
        round_no=3,
        summary="阿珍喺酒吧撞破阿強嘅秘密。",
        branch_point="阿珍會唔會當面質問阿強？",
        options=[
            GMOption(id="A", title="當面對質", predicted="兩人關係破裂"),
            GMOption(id="B", title="暗中調查", predicted="阿珍掌握更多把柄"),
        ],
        can_finish=True,
    )
    store.set_pending_decision(decision)
    # 重新 load，逐字重現（邊界 3：resume 唔重跑 LLM）
    loaded = GMStateStore.load(store.path)
    restored = loaded.get_pending_decision()
    assert restored is not None
    assert restored.model_dump(mode="json") == decision.model_dump(mode="json")
    loaded.clear_pending_decision()
    assert GMStateStore.load(store.path).get_pending_decision() is None


def test_timeline_accumulates_10_rounds(tmp_path):
    store = make_store(tmp_path)
    store.init_new("開端", ["阿珍", "阿強", "小美", "阿明"])
    for r in range(1, 11):
        store.append_timeline(
            TimelineEntry(
                round=r,
                summary=f"第{r}回合嘅事",
                dialogues=[{
                    "speakers": "阿珍 -> 阿強",
                    "address": "玫瑰酒吧",
                    "lines": [["阿珍", f"第{r}回合嘅對白原文"]],
                }],
            )
        )
    loaded = GMStateStore.load(store.path)
    timeline = loaded.build_story_timeline()
    assert len(timeline) == 11  # seed + 10
    # 對白逐字保留
    assert timeline[5].dialogues[0].lines[0][1] == "第5回合嘅對白原文"
    assert timeline[10].summary == "第10回合嘅事"


def test_init_new_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.init_new("開端一", ["阿珍", "阿強", "小美", "阿明"])
    store.init_new("開端二", ["阿明"])  # 唔應該覆寫
    assert store.data["story_seed"] == "開端一"
    assert store.data["agent_names"] == ["阿珍", "阿強", "小美", "阿明"]


def test_branch_point_history(tmp_path):
    store = make_store(tmp_path)
    store.init_new("開端", ["阿珍", "阿強", "小美", "阿明"])
    store.append_timeline(TimelineEntry(round=1, summary="s", branch_point="轉折一"))
    store.append_timeline(TimelineEntry(round=2, summary="s"))  # 無分支點唔入 history
    store.append_timeline(TimelineEntry(round=3, summary="s", branch_point="轉折三"))
    assert store.data["branch_point_history"] == ["轉折一", "轉折三"]


def test_dramatic_pressure_tracks_unresolved_threads(tmp_path):
    store = make_store(tmp_path)
    store.init_new("seed", ["阿珍", "阿強"])

    store.add_unresolved_thread("阿珍要不要公開那封信？")
    store.add_unresolved_thread("阿強會否阻止她？")

    assert store.data["dramatic_pressure"] == 3
    assert store.data["unresolved_threads"] == ["阿珍要不要公開那封信？", "阿強會否阻止她？"]

    store.resolve_unresolved_thread()

    assert store.data["dramatic_pressure"] == 2
    assert store.data["unresolved_threads"] == ["阿強會否阻止她？"]


def test_stores_do_not_share_state(tmp_path):
    """regression：_DEFAULTS 淺拷貝會令兩個 store 共享巢狀 list（timeline/errors 互相污染）。"""
    s1 = GMStateStore(str(tmp_path / "a.json"))
    s2 = GMStateStore(str(tmp_path / "b.json"))
    s1.init_new("開端一", ["阿珍", "阿強", "小美", "阿明"])
    s1.append_timeline(TimelineEntry(round=1, summary="s", branch_point="轉折一"))
    assert s2.data["timeline"] == []
    assert s2.data["branch_point_history"] == []
    assert s2.data["errors"] == []
