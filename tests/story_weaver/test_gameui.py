"""game-ui 測試：FrameBuffer、state store、RoundRunner 狀態機、compress 白名單回歸。

全部用 fake server / fake maze / fake LLM，唔掂真模擬同真 LLM。
"""

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from story_weaver.gm import GMDirector
from story_weaver.gm.models import (
    AffinitySuggestion,
    GMRoundAnalysis,
    GMOption,
    PlayerChoice,
)
from story_weaver.gameui.incremental import FrameBuffer
from story_weaver.gameui.models import UIStatus
from story_weaver.gameui.round_runner import RoundBusyError, RoundRunner, scan_checkpoints
from story_weaver.gameui.state_store import GameUIStateStore, create_initial_state

GEN_DIR = "/Users/kenneth/Projects/story-weaver/generative_agents"
PROMPTS_GM = os.path.join(GEN_DIR, "data", "prompts_gm")

AGENTS = ["阿珍", "阿強", "小美", "阿明"]


# ---------------------------------------------------------------- fakes


class FakeMaze:
    def find_path(self, source, target):
        return [list(source), list(target)]


class FakeConcept:
    def __init__(self, node_id, describe, poignancy):
        self.node_id = node_id
        self.node_type = "event"
        self.describe = describe
        self.poignancy = poignancy


class FakeAssociate:
    def __init__(self):
        self.memory = {"event": [], "chat": [], "thought": []}
        self._concepts = {}

    def find_concept(self, node_id):
        return self._concepts.get(node_id)


class FakeAgent:
    def __init__(self, name):
        self.name = name
        self.status = {"poignancy": 0}
        self.scratch = SimpleNamespace(currently="")
        self.associate = FakeAssociate()
        self.schedule = SimpleNamespace(
            daily_schedule=[],
            current_plan=lambda: (
                {"start": 0, "duration": 60, "describe": "工作", "decompose": []},
                {"start": 0, "duration": 15, "describe": "在書桌前工作"},
            ),
        )
        self._n = 0

    def _add_concept(self, e_type, event, create=None, expire=None, filling=None,
                     poignancy_override=None):
        self._n += 1
        concept = FakeConcept(f"{self.name}-{self._n}", event.get_describe(), poignancy_override)
        self.associate.memory[e_type].insert(0, concept.node_id)
        self.associate._concepts[concept.node_id] = concept
        return concept

    def is_awake(self):
        return True

    def get_tile(self):
        return SimpleNamespace(get_address=lambda: ["village", "house"])

    def revise_schedule(self, event, start, duration):
        pass  # fake: no-op


class FakeSimServer:
    """模仿 SimulateServer：simulate() 寫 checkpoint + conversation，更新 config。"""

    def __init__(self, folder, block_event=None):
        self.folder = folder
        self.start_step = 0  # [story-weaver:start-step] 模擬 SimulateServer.start_step
        self.game = SimpleNamespace(
            agents={n: FakeAgent(n) for n in AGENTS},
            conversation={},
        )
        self.config = {
            "time": "20240213-09:30",
            "step": 0,
            "stride": 10,
            "agents": {n: {"currently": "", "status": {}, "action": {}} for n in AGENTS},
            "affinity": {"阿珍": {"阿強": {"value": -65, "label": "舊情人"}}},
        }
        self._block = block_event  # 測試「推演中」用：設咗就等到 release

    def simulate(self, steps, stride):
        if self._block is not None:
            self._block.wait(timeout=10)
        for _ in range(steps):
            self.config["step"] += 1
            step = self.config["step"]
            minute = 30 + step * 10
            sim_time = f"20240213-{9 + minute // 60:02d}:{minute % 60:02d}"
            self.config["time"] = sim_time
            agents = {}
            for n in AGENTS:
                agents[n] = {
                    "coord": [100 + step, 50],
                    "currently": f"{n} 諗緊嘢",
                    "status": {},
                    "action": {
                        "event": {
                            "subject": n, "predicate": "正在", "object": "散步",
                            "describe": f"{n}喺大街散步（第{step}步）",
                            "address": ["小鎮", "大街"], "emoji": "",
                        }
                    },
                }
            self.config["agents"] = agents
            fname = f"simulate-{sim_time.replace(':', '')}.json"
            with open(os.path.join(self.folder, fname), "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False)
            if step == 1:
                self.game.conversation[sim_time] = [
                    {"阿珍 -> 阿強 @ 小鎮，大街": [["阿珍", "你收埋封信做咩？"], ["阿強", "唔關你事。"]]}
                ]
                with open(os.path.join(self.folder, "conversation.json"), "w", encoding="utf-8") as f:
                    json.dump(self.game.conversation, f, ensure_ascii=False)


class FakeLLM:
    def __init__(self):
        self.calls = []

    def completion(self, prompt, retry=10, callback=None, failsafe=None,
                   return_type=None, caller="llm_normal", **kwargs):
        self.calls.append(caller)
        if caller == "gm_round_summary":
            return GMRoundAnalysis(
                summary="阿珍質問阿強封信嘅事，兩人不歡而散。",
                branch_point="阿珍會唔會繼續追查？",
                options=[
                    GMOption(id="A", title="繼續追查", predicted="阿珍搵到更多線索"),
                    GMOption(id="B", title="暫時罷休", predicted="兩人冷戰"),
                ],
                suggested_affinity_changes=[
                    AffinitySuggestion(from_agent="阿珍", to_agent="阿強", delta=-10, reason="質問失敗")
                ],
            )
        if caller == "gm_finale":
            from story_weaver.gm.models import FinaleNarrative

            return FinaleNarrative(
                ending="小鎮嘅故事喺呢度畫上句號。" * 10,
                character_epilogues=[{"name": n, "epilogue": "繼續生活。"} for n in AGENTS],
            )
        return failsafe


def make_session(tmp_path, name="s1"):
    """建一個假 session 目錄（story.json + sim_config.json）。"""
    folder = os.path.join(str(tmp_path), name)
    os.makedirs(folder)
    with open(os.path.join(folder, "story.json"), "w", encoding="utf-8") as f:
        json.dump({"story_name": name, "story_opening": "一封信引發嘅恩怨",
                   "characters": AGENTS, "locked": True}, f, ensure_ascii=False)
    with open(os.path.join(folder, "sim_config.json"), "w", encoding="utf-8") as f:
        json.dump({"stride": 10, "time": {"start": "20240213-09:30"},
                   "agents": {n: {} for n in AGENTS},
                   "affinity": {}}, f, ensure_ascii=False)
    return folder


def make_runner(tmp_path, name="s1", block_event=None, **kwargs):
    folder = make_session(tmp_path, name)
    gm = GMDirector(name, folder, {}, AGENTS, prompts_dir=PROMPTS_GM, llm=FakeLLM())
    server = FakeSimServer(folder, block_event=block_event)
    runner = RoundRunner(
        name, folder,
        gm=gm, recap=None, maze=FakeMaze(),
        server_factory=lambda r: server,
        **kwargs,
    )
    runner.init_if_needed()
    return runner, server, gm


def wait_status(runner, status, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if runner.store.load().status == status:
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- state store


def test_initial_state_idempotent(tmp_path):
    folder = make_session(tmp_path)
    s1 = create_initial_state(folder, "s1", AGENTS)
    s2 = create_initial_state(folder, "s1", ["其他人"])
    assert s1.agents == AGENTS
    assert s2.agents == AGENTS  # 唔覆蓋


def test_state_mutate_atomic(tmp_path):
    folder = make_session(tmp_path)
    store = GameUIStateStore(folder)
    create_initial_state(folder, "s1", AGENTS)
    with store.mutate() as s:
        s.round = 3
    assert store.load().round == 3
    assert not [f for f in os.listdir(folder) if ".tmp." in f]


# ---------------------------------------------------------------- FrameBuffer


def test_framebuffer_scan(tmp_path):
    folder = make_session(tmp_path)
    server = FakeSimServer(folder)
    server.simulate(2, 10)
    buf = FrameBuffer(folder, FakeMaze())
    result = buf.scan([])
    # [story-weaver:movement-interp] 每 step 全部 60 幀都生成（路徑插值），
    # 唔再只得移動嗰兩幀：step 1 → keys 1-60；step 2 → keys 61-120
    assert buf.latest_frame_key() == 120
    frames = buf.frames_since(-1)
    assert "0" in frames  # 第 0 帧初始位置
    assert "61" in frames
    # 事件 feed：每 step 每 agent 一條；對話 feed：step 1 一條
    chat_feed = [f for f in buf.feed_since(0) if f.kind.value == "chat"]
    assert len(chat_feed) == 1
    assert chat_feed[0].dialogue[0].line == "你收埋封信做咩？"  # 原文
    event_feed = [f for f in buf.feed_since(0) if f.kind.value == "event"]
    assert len(event_feed) == 8  # 2 steps × 4 agents
    # 增量：再 scan 唔會重複
    result2 = buf.scan([])
    assert result2["new_frames"] == {}
    assert buf.feed_latest == len(buf._feed)


def test_framebuffer_skips_corrupt(tmp_path):
    folder = make_session(tmp_path)
    server = FakeSimServer(folder)
    server.simulate(1, 10)
    with open(os.path.join(folder, "simulate-20240213-0950.json"), "w") as f:
        f.write('{"step": 2, "time": "20240213-09:50", "agents": {"阿珍')
    buf = FrameBuffer(folder, FakeMaze())
    result = buf.scan([])
    assert result["skipped"] == ["simulate-20240213-0950.json"]
    assert buf.last_step == 1


# ---------------------------------------------------------------- RoundRunner 狀態機


def test_round_happy_path(tmp_path):
    runner, server, gm = make_runner(tmp_path)
    round_no = runner.start_round()
    assert round_no == 1
    assert wait_status(runner, UIStatus.WAITING_DECISION)
    state = runner.store.load()
    assert state.sim_step_cursor == 6  # steps_per_round 預設 6
    # GM pending 已出
    pending = gm.get_pending_decision()
    assert pending is not None
    assert [o.id for o in pending.options] == ["A", "B"]
    # 決策
    report, new_status = runner.apply_decision(PlayerChoice(type="option", option_id="A"))
    assert report.ok is True
    assert new_status == UIStatus.IDLE
    assert runner.store.load().round == 1
    # 全部 agent 收到注入
    assert all(len(a.associate.memory["event"]) >= 1 for a in server.game.agents.values())


def test_start_round_busy(tmp_path):
    runner, server, gm = make_runner(tmp_path)
    runner.start_round()
    wait_status(runner, UIStatus.WAITING_DECISION)
    with pytest.raises(RoundBusyError, match="等待玩家決策"):
        runner.start_round()


def test_start_round_finished(tmp_path):
    runner, server, gm = make_runner(tmp_path)
    with runner.store.mutate() as s:
        s.status = UIStatus.FINISHED
    with pytest.raises(RoundBusyError, match="故事已完結"):
        runner.start_round()


def test_simulating_conflict(tmp_path):
    block = threading.Event()
    runner, server, gm = make_runner(tmp_path, block_event=block)
    runner.start_round()
    assert wait_status(runner, UIStatus.SIMULATING)
    with pytest.raises(RoundBusyError, match="推演進行中"):
        runner.start_round()
    block.set()
    assert wait_status(runner, UIStatus.WAITING_DECISION)


def test_finish_choice_generates_finale(tmp_path):
    runner, server, gm = make_runner(tmp_path)
    runner.start_round()
    wait_status(runner, UIStatus.WAITING_DECISION)
    report, new_status = runner.apply_decision(PlayerChoice(type="finish"))
    assert new_status == UIStatus.FINISHED
    assert runner.store.load().status == UIStatus.FINISHED
    finale = gm.state.get_finale()
    assert finale is not None
    assert "句號" in finale.narrative.ending


def test_recover_rolls_back_simulating(tmp_path):
    runner, server, gm = make_runner(tmp_path)
    # 模擬「死喺推演中途」：status=SIMULATING + 一個完整 checkpoint + 一個半截
    server.simulate(1, 10)
    with open(os.path.join(runner.folder, "simulate-20240213-0950.json"), "w") as f:
        f.write('{"step": 2, 截斷')
    with runner.store.mutate() as s:
        s.status = UIStatus.SIMULATING
    info = runner.recover()
    state = runner.store.load()
    assert info.recovered is True
    assert state.status == UIStatus.IDLE
    assert state.sim_step_cursor == 1
    assert "恢復" in state.error
    assert info.skipped_files == ["simulate-20240213-0950.json"]


def test_scan_checkpoints_tolerant(tmp_path):
    folder = make_session(tmp_path)
    server = FakeSimServer(folder)
    server.simulate(2, 10)
    with open(os.path.join(folder, "simulate-20240213-1000.json"), "w") as f:
        f.write("唔係 json")
    step, skipped, config = scan_checkpoints(folder)
    assert step == 2
    assert skipped == ["simulate-20240213-1000.json"]
    assert config["step"] == 2


# ---------------------------------------------------------------- compress 白名單回歸


def test_compress_ignores_metadata_json(tmp_path, monkeypatch):
    """離線流程無退化：gm_state/story_recap/game_ui_state/sim_config 唔會炸 compress。"""
    import shutil
    import sys

    # compress/start 喺 import 時 parse_args，要清走 pytest argv
    monkeypatch.setattr(sys, "argv", ["compress.py"])
    monkeypatch.chdir(GEN_DIR)
    import compress

    folder = os.path.join(str(tmp_path), "ck")
    compressed = os.path.join(str(tmp_path), "cp")
    os.makedirs(folder)
    os.makedirs(compressed)

    # 用真實 maze：agent 企喺原哂（coord 唔變），find_path 即刻返回
    # insert_frame0 要讀 agent.json → 整個臨時檔（測試後刪除）
    agent_dir = os.path.join(GEN_DIR, "frontend", "static", "assets", "village", "agents", "阿珍")
    os.makedirs(agent_dir, exist_ok=True)
    try:
        with open(os.path.join(agent_dir, "agent.json"), "w", encoding="utf-8") as f:
            json.dump({
                "name": "阿珍", "coord": [123, 57], "currently": "",
                "scratch": {"age": 30, "innate": "", "learned": "", "lifestyle": ""},
                "spatial": {"address": {"living_area": ["小鎮", "屋企", "睡房"]}},
            }, f)
        for step in (1, 2):
            sim_time = f"20240213-09:{30 + step * 10}"
            data = {
                "time": sim_time, "step": step, "stride": 10,
                "agents": {"阿珍": {
                    "coord": [123, 57], "currently": "",
                    "action": {"event": {
                        "subject": "阿珍", "predicate": "正在", "object": "諗嘢",
                        "describe": f"阿珍喺屋企諗嘢（第{step}步）",
                        "address": ["小鎮", "屋企"], "emoji": "",
                    }},
                }},
            }
            fname = f"simulate-{sim_time.replace(':', '')}.json"
            with open(os.path.join(folder, fname), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        # 各系統嘅 metadata json（內容同 checkpoint 完全唔同）
        for junk in ["gm_state.json", "story_recap.json", "game_ui_state.json", "sim_config.json"]:
            with open(os.path.join(folder, junk), "w") as f:
                f.write('{"呢個": "唔係 checkpoint", "no_agents": true}')
        result = compress.generate_movement(folder, compressed, "movement.json")
        assert result["all_movement"]
        assert "61" in result["all_movement"]  # 第 2 個 step 嘅 frame
    finally:
        shutil.rmtree(agent_dir, ignore_errors=True)


def test_max_rounds_auto_finale(tmp_path, monkeypatch):
    """行到總回合數 → 自動 FINISHED + LLM 生成結局。"""
    from story_weaver.gameui import round_runner as rr_mod

    monkeypatch.setattr(
        rr_mod, "load_gm_config",
        lambda *a, **k: {"steps_per_round": 1, "max_rounds": 2, "option_poignancy": 8,
                         "custom_poignancy": 10, "poignancy_boost": 20,
                         "min_rounds_to_finish": 2},
    )
    runner, server, gm = make_runner(tmp_path)
    with runner.store.mutate() as s:
        s.steps_per_round = 1
    # Round 1
    runner.start_round()
    assert wait_status(runner, UIStatus.WAITING_DECISION)
    runner.apply_decision(PlayerChoice(type="skip"))
    # Round 2 = 最後一回合
    runner.start_round()
    assert wait_status(runner, UIStatus.WAITING_DECISION)
    report, new_status = runner.apply_decision(PlayerChoice(type="skip"))
    assert new_status == UIStatus.FINISHED
    finale = gm.state.get_finale()
    assert finale is not None
    assert "句號" in finale.narrative.ending
    # 完結後唔可以再開回合
    with pytest.raises(RoundBusyError, match="故事已完結"):
        runner.start_round()
