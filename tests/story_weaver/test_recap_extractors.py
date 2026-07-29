"""提取器測試（spec §11）：事件去重、對白位元級、同分鐘多組、損毀降級。"""

import hashlib
import json
import os

from story_weaver.recap.extractors import (
    DialogueExtractor,
    EventExtractor,
    MemoryFallbackExtractor,
)

AGENT = "阿珍"
OTHER = "阿強"


def make_checkpoint(sim_dir, step, sim_time, events):
    """events: {agent_name: (predicate, object, describe, address)}"""
    agents = {}
    for name, (pred, obj, describe, address) in events.items():
        agents[name] = {
            "action": {
                "event": {
                    "subject": name,
                    "predicate": pred,
                    "object": obj,
                    "describe": describe,
                    "address": address,
                    "emoji": "",
                }
            },
            "currently": "",
            "status": {},
        }
    data = {"time": sim_time, "step": step, "stride": 10, "agents": agents}
    fname = f"simulate-{sim_time.replace(':', '')}.json"
    with open(os.path.join(sim_dir, fname), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def make_sim(tmp_path):
    sim_dir = str(tmp_path / "sim1")
    os.makedirs(sim_dir)
    return sim_dir


# ---------------------------------------------------------------- 事件提取


def test_event_dedup(tmp_path):
    """同 agent 連續 3 step 同 describe + 地點 → 時間線只出 1 條。"""
    sim_dir = make_sim(tmp_path)
    for i, minute in enumerate(["09:30", "09:40", "09:50"]):
        make_checkpoint(sim_dir, i + 1, f"20240213-{minute}", {
            AGENT: ("正在", "查閱資料", "阿珍正在圖書館查閱資料", ["小鎮", "學院", "圖書館"]),
        })
    result = EventExtractor().extract(sim_dir, (1, 3))
    assert len(result.events) == 1
    assert result.events[0].sim_time == "20240213-09:30"
    assert result.events[0].location == "學院，圖書館"  # 第一級小鎮名被去掉


def test_event_step_range_filter(tmp_path):
    sim_dir = make_sim(tmp_path)
    for i, minute in enumerate(["09:30", "09:40", "09:50", "10:00"]):
        make_checkpoint(sim_dir, i + 1, f"20240213-{minute}", {
            AGENT: ("正在", f"做第{i+1}件事", f"阿珍做緊第{i+1}件事", ["小鎮", "屋企"]),
        })
    result = EventExtractor().extract(sim_dir, (2, 3))  # 只收 step 2-3
    describes = [e.describe for e in result.events]
    assert "阿珍做緊第2件事" in describes
    assert "阿珍做緊第3件事" in describes
    assert "阿珍做緊第1件事" not in describes
    assert "阿珍做緊第4件事" not in describes
    assert result.sim_time_start == "20240213-09:40"
    assert result.sim_time_end == "20240213-09:50"


def test_trivial_events_filtered(tmp_path):
    sim_dir = make_sim(tmp_path)
    make_checkpoint(sim_dir, 1, "20240213-09:30", {
        AGENT: ("此時", "空閒", "", ["小鎮", "屋企"]),          # idle 組合 → 剔除
        OTHER: ("正在", "睡覺", "阿強正在睡覺", ["小鎮", "屋企"]),  # 睡覺 + poignancy<3 → 剔除
    })
    make_checkpoint(sim_dir, 2, "20240213-09:40", {
        AGENT: ("正在", "寫信", "阿珍正在房間寫一封重要嘅信", ["小鎮", "屋企", "房間"]),
    })
    result = EventExtractor().extract(sim_dir, (1, 2))
    assert len(result.events) == 1
    assert "寫一封重要嘅信" in result.events[0].describe


def test_corrupt_checkpoint_skipped(tmp_path):
    sim_dir = make_sim(tmp_path)
    make_checkpoint(sim_dir, 1, "20240213-09:30", {
        AGENT: ("正在", "寫信", "阿珍寫緊信", ["小鎮", "屋企"]),
    })
    # step 2 寫到一半（JSON 截斷）
    with open(os.path.join(sim_dir, "simulate-20240213-0940.json"), "w") as f:
        f.write('{"time": "20240213-09:40", "step": 2, "agents": {"阿珍": {"act')
    make_checkpoint(sim_dir, 3, "20240213-09:50", {
        AGENT: ("正在", "寄信", "阿珍去咗寄信", ["小鎮", "郵局"]),
    })
    result = EventExtractor().extract(sim_dir, (1, 3))
    assert result.truncated is True
    assert any("損毀" in w for w in result.warnings)
    assert result.sim_time_end == "20240213-09:50"  # 最後合法 step
    describes = [e.describe for e in result.events]
    assert "阿珍寫緊信" in describes and "阿珍去咗寄信" in describes


# ---------------------------------------------------------------- 對話提取


def _write_conversation(sim_dir, conversation):
    with open(os.path.join(sim_dir, "conversation.json"), "w", encoding="utf-8") as f:
        json.dump(conversation, f, ensure_ascii=False)


def test_dialogue_bitexact(tmp_path):
    """lines[].text 與 conversation.json 原文逐條 sha256 比對一致。"""
    sim_dir = make_sim(tmp_path)
    original_lines = [
        [AGENT, "你收埋封信做咩？  （帶住  奇怪嘅空格同標點！）"],
        [OTHER, "唔關你事。\n第二行都有"],
    ]
    _write_conversation(sim_dir, {
        "20240213-10:15": [{"阿珍 -> 阿強 @ 小鎮，玫瑰酒吧": original_lines}]
    })
    result = DialogueExtractor().extract(sim_dir, "20240213-10:00", "20240213-11:00")
    assert result.health == "ok"
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.participants == ["阿珍", "阿強"]
    assert block.location == "小鎮，玫瑰酒吧"
    for extracted, original in zip(block.lines, original_lines):
        assert extracted.speaker == original[0]
        assert hashlib.sha256(extracted.text.encode()).hexdigest() == \
               hashlib.sha256(original[1].encode()).hexdigest()


def test_multi_chat_same_minute(tmp_path):
    """同一分鐘 key 兩組對話 → 兩個獨立 block，唔合併。"""
    sim_dir = make_sim(tmp_path)
    _write_conversation(sim_dir, {
        "20240213-10:15": [
            {"阿珍 -> 阿強 @ 小鎮，酒吧": [[AGENT, "第一句"]]},
            {"小美 -> 阿明 @ 小鎮，學校": [["小美", "另一組嘅第一句"]]},
        ]
    })
    result = DialogueExtractor().extract(sim_dir, "20240213-10:00", "20240213-11:00")
    assert len(result.blocks) == 2
    participants = {tuple(b.participants) for b in result.blocks}
    assert ("阿珍", "阿強") in participants
    assert ("小美", "阿明") in participants


def test_dialogue_time_range_filter(tmp_path):
    sim_dir = make_sim(tmp_path)
    _write_conversation(sim_dir, {
        "20240213-09:15": [{"阿珍 -> 阿強 @ 小鎮": [[AGENT, "太早"]]}],
        "20240213-10:15": [{"阿珍 -> 阿強 @ 小鎮": [[AGENT, "啱範圍"]]}],
        "20240213-12:15": [{"阿珍 -> 阿強 @ 小鎮": [[AGENT, "太遲"]]}],
    })
    result = DialogueExtractor().extract(sim_dir, "20240213-10:00", "20240213-11:00")
    assert len(result.blocks) == 1
    assert result.blocks[0].lines[0].text == "啱範圍"


def test_corrupt_conversation_missing(tmp_path):
    """conversation.json 唔存在 + 無記憶後備 → health=missing。"""
    sim_dir = make_sim(tmp_path)
    result = DialogueExtractor().extract(sim_dir, "20240213-10:00", "20240213-11:00")
    assert result.health == "missing"
    assert result.blocks == []


def test_corrupt_conversation_memory_fallback(tmp_path):
    """conversation.json 損毀 → 由 docstore chat concept 重建 degraded block。"""
    sim_dir = make_sim(tmp_path)
    with open(os.path.join(sim_dir, "conversation.json"), "w") as f:
        f.write("{唔係合法 json")
    # 建假 docstore：一個 chat concept
    docstore_dir = os.path.join(sim_dir, "storage", "阿珍", "associate")
    os.makedirs(docstore_dir)
    docstore = {
        "docstore/data": {
            "node-1": {
                "__data__": {
                    "text": "阿珍同阿強傾咗一陣關於封信嘅事",
                    "metadata": {
                        "node_type": "chat",
                        "subject": "阿珍",
                        "object": "阿強",
                        "address": "小鎮:玫瑰酒吧",
                        "create": "20240213-10:20:00",
                        "poignancy": 6,
                    },
                }
            }
        }
    }
    with open(os.path.join(docstore_dir, "docstore.json"), "w", encoding="utf-8") as f:
        json.dump(docstore, f, ensure_ascii=False)
    result = DialogueExtractor().extract(sim_dir, "20240213-10:00", "20240213-11:00")
    assert result.health == "degraded"
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert block.degraded is True
    assert block.participants == ["阿珍", "阿強"]
    assert block.lines[0].speaker == "（回憶）"
    assert "封信" in block.lines[0].text


def test_memory_fallback_time_filter(tmp_path):
    sim_dir = make_sim(tmp_path)
    os.makedirs(os.path.join(sim_dir, "storage", "阿珍", "associate"))
    docstore = {
        "docstore/data": {
            "n1": {"__data__": {"text": "範圍內", "metadata": {
                "node_type": "chat", "subject": "阿珍", "object": "阿強",
                "address": "", "create": "20240213-10:20:00", "poignancy": 5}}},
            "n2": {"__data__": {"text": "範圍外", "metadata": {
                "node_type": "chat", "subject": "阿珍", "object": "阿強",
                "address": "", "create": "20240213-15:20:00", "poignancy": 5}}},
            "n3": {"__data__": {"text": "唔係 chat", "metadata": {
                "node_type": "event", "subject": "阿珍", "object": "",
                "address": "", "create": "20240213-10:25:00", "poignancy": 5}}},
        }
    }
    with open(os.path.join(sim_dir, "storage", "阿珍", "associate", "docstore.json"),
              "w", encoding="utf-8") as f:
        json.dump(docstore, f, ensure_ascii=False)
    blocks = MemoryFallbackExtractor().scan_chat_concepts(
        sim_dir, ["阿珍"], "20240213-10:00", "20240213-11:00"
    )
    assert len(blocks) == 1
    assert blocks[0].lines[0].text == "範圍內"
