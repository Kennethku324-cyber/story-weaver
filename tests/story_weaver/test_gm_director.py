"""GMDirector 測試（spec §7）：回合流程、failsafe、pending 恢復、命令解析、終章冪等。

LLM 全部用 FakeLLM stub（按 caller 回應），server 用 FakeServer，唔掂真模擬。
"""

import os
import sys
from types import SimpleNamespace

import pytest

from story_weaver.gm.director import GMDirector
from story_weaver.gm.models import (
    AffinitySuggestion,
    CustomCommandParse,
    FinaleNarrative,
    GMRoundAnalysis,
    GMOption,
    PlayerChoice,
)

GEN_DIR = "/Users/kenneth/Projects/story-weaver/generative_agents"
PROMPTS_DIR = os.path.join(GEN_DIR, "data", "prompts_gm")

NAMES = ["阿珍", "阿強", "小美", "阿明"]


# ---------------------------------------------------------------- fakes


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

    def add(self, concept):
        self.memory["event"].insert(0, concept.node_id)
        self._concepts[concept.node_id] = concept

    def find_concept(self, node_id):
        return self._concepts.get(node_id)


class FakeScratch:
    def __init__(self):
        self.currently = ""


class FakeAgent:
    def __init__(self, name):
        self.name = name
        self.status = {"poignancy": 0}
        self.scratch = FakeScratch()
        self.associate = FakeAssociate()
        self.schedule = SimpleNamespace(
            daily_schedule=[],
            current_plan=lambda: (
                {"start": 0, "duration": 60, "describe": "工作", "decompose": []},
                {"start": 0, "duration": 15, "describe": "在書桌前工作"},
            ),
        )
        self.injected = []

    def _add_concept(self, e_type, event, create=None, expire=None, filling=None,
                     poignancy_override=None):
        concept = FakeConcept(
            f"{self.name}-node-{len(self.injected)}", event.get_describe(), poignancy_override
        )
        self.associate.add(concept)
        self.injected.append(
            {"e_type": e_type, "event": event, "poignancy": poignancy_override}
        )
        return concept

    def is_awake(self):
        return True

    def get_tile(self):
        return SimpleNamespace(get_address=lambda: ["village", "house"])

    def revise_schedule(self, event, start, duration):
        pass  # fake: no-op


class FakeGame:
    def __init__(self):
        self.agents = {n: FakeAgent(n) for n in NAMES}
        self.conversation = {}


class FakeServer:
    def __init__(self):
        self.game = FakeGame()
        self.config = {
            "time": "20260729-10:00",
            "step": 0,
            "agents": {n: {"currently": "", "status": {}, "action": {}} for n in NAMES},
        }


class FakeLLM:
    """按 caller 回應；冇 script 嘅 caller 返 failsafe（即 None）。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def completion(self, prompt, retry=10, callback=None, failsafe=None,
                   return_type=None, caller="llm_normal", **kwargs):
        self.calls.append({"caller": caller, "prompt": prompt})
        return self.responses.get(caller, failsafe)


def make_analysis():
    return GMRoundAnalysis(
        summary="阿珍喺酒吧撞破阿強收埋一封信，兩人差啲嗌交。",
        branch_point="阿珍會唔會當面質問阿強封信嘅內容？",
        options=[
            GMOption(id="A", title="當面對質", predicted="兩人關係進一步破裂"),
            GMOption(id="B", title="暗中調查", predicted="阿珍掌握更多線索"),
        ],
        suggested_affinity_changes=[
            AffinitySuggestion(from_agent="阿珍", to_agent="阿強", delta=-15, reason="阿強隱瞞令阿珍起疑")
        ],
    )


def make_director(tmp_path, llm, names=NAMES):
    ckpt = str(tmp_path / "ckpt")
    os.makedirs(ckpt, exist_ok=True)
    gm = GMDirector(
        sim_name="test-sim",
        checkpoints_folder=ckpt,
        llm_config={},
        agent_names=names,
        prompts_dir=PROMPTS_DIR,
        llm=llm,
    )
    gm.state.init_new("一封信引發嘅恩怨", names)
    return gm


def add_conversation(server):
    server.game.conversation["20260729-11:00"] = [
        {"阿珍 -> 阿強 @ 玫瑰酒吧": [["阿珍", "你收埋封信做咩？"], ["阿強", "唔關你事。"]]}
    ]


# ---------------------------------------------------------------- 回合流程


def test_on_round_end_quiet_round(tmp_path):
    """[story-weaver:no-quiet] 靜默回合（冇新事件）都會 call LLM 分析，
    唔再直接出「平靜的一日」。LLM 失敗先行 failsafe。"""
    llm = FakeLLM()
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    decision = gm.on_round_end(server, 1)
    assert decision.is_quiet is True
    # LLM 冇 script → failsafe，所以 options 為空
    assert decision.is_failsafe is True
    # 每回合都會 call LLM（唔再 skip）
    assert any(c["caller"] == "gm_round_summary" for c in llm.calls)
    # story_timeline：seed + 本回合
    assert len(decision.story_timeline) == 2


def test_on_round_end_happy_path(tmp_path):
    llm = FakeLLM({"gm_round_summary": make_analysis()})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    decision = gm.on_round_end(server, 1)
    assert decision.is_quiet is False
    assert decision.is_failsafe is False
    assert decision.branch_point == "阿珍會唔會當面質問阿強封信嘅內容？"
    assert [o.id for o in decision.options] == ["A", "B"]
    assert decision.suggested_affinity_changes[0].delta == -15
    assert decision.can_finish is False  # round 1 < min_rounds_to_finish 2
    # 對白原文入 timeline
    last = decision.story_timeline[-1]
    assert last.dialogues[0].lines[0] == ("阿珍", "你收埋封信做咩？")
    # pending 已持久化
    assert gm.get_pending_decision() is not None


def test_llm_total_failure_failsafe(tmp_path):
    llm = FakeLLM()  # 全部返 None
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    decision = gm.on_round_end(server, 1)
    assert decision.is_failsafe is True
    assert decision.options == []
    assert "命運之線" in decision.summary
    # error 入 log，流程完成
    assert any(e["stage"] == "round_summary" for e in gm.state.data["errors"])


def test_pending_decision_survives_resume(tmp_path):
    llm = FakeLLM({"gm_round_summary": make_analysis()})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    original = gm.on_round_end(server, 1)
    # 模擬斷線重開：新 GMDirector 由 gm_state.json 重建
    gm2 = GMDirector.resume(
        sim_name="test-sim",
        checkpoints_folder=gm.checkpoints_folder,
        llm_config={},
        prompts_dir=PROMPTS_DIR,
        llm=FakeLLM(),
    )
    restored = gm2.get_pending_decision()
    assert restored is not None
    assert restored.model_dump(mode="json") == original.model_dump(mode="json")
    assert gm2.agent_names == NAMES


def test_on_round_start_applies_relations_prefix(tmp_path):
    gm = make_director(tmp_path, FakeLLM())
    server = FakeServer()
    server.config["affinity"] = {"阿珍": {"阿強": {"value": -65, "label": "舊情人"}}}
    gm.on_round_start(server)
    # [story-weaver:theme-anchor] 主題錨定會加喺【人際關係】前面
    assert "【人際關係】" in server.game.agents["阿珍"].scratch.currently
    assert "-65" in server.game.agents["阿珍"].scratch.currently


# ---------------------------------------------------------------- 玩家選擇


def test_apply_option_injects_all_agents(tmp_path):
    llm = FakeLLM({"gm_round_summary": make_analysis()})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    gm.on_round_end(server, 1)
    report = gm.apply_player_choice(server, 1, PlayerChoice(type="option", option_id="A"))
    assert report.ok is True
    # 全部 agent 注入 event，poignancy 8
    for name in NAMES:
        agent = server.game.agents[name]
        assert len(agent.injected) == 1
        assert agent.injected[0]["e_type"] == "event"
        assert agent.injected[0]["poignancy"] == 8
        assert "當面對質" in agent.injected[0]["event"].get_describe()
        assert agent.status["poignancy"] == 20
    # timeline 補完 + pending 清除
    assert gm.get_pending_decision() is None
    last = gm.state.build_story_timeline()[-1]
    assert last.player_choice.type == "option"
    assert last.player_choice.option_id == "A"


def test_apply_skip_zero_injection(tmp_path):
    llm = FakeLLM({"gm_round_summary": make_analysis()})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    gm.on_round_end(server, 1)
    report = gm.apply_player_choice(server, 1, PlayerChoice(type="skip"))
    assert report.ok is True
    assert report.injected == []
    assert all(len(server.game.agents[n].injected) == 0 for n in NAMES)
    assert gm.get_pending_decision() is None
    last = gm.state.build_story_timeline()[-1]
    assert last.player_choice.type == "skip"


def test_apply_custom_refused_keeps_pending(tmp_path):
    refusal = CustomCommandParse(
        targets=[], command_event_describe="", feasible=False,
        refuse_reason="命令涉及的角色不在小鎮之中。",
    )
    llm = FakeLLM({"gm_round_summary": make_analysis(), "gm_custom_command": refusal})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    gm.on_round_end(server, 1)
    report = gm.apply_player_choice(
        server, 1, PlayerChoice(type="custom", text="叫外星人接走阿珍")
    )
    assert report.ok is False
    assert report.refused is not None
    assert "不在小鎮" in report.refused.refuse_reason
    # 邊界 6：唔消耗回合，pending 保留
    assert gm.get_pending_decision() is not None


def test_apply_custom_happy_path(tmp_path):
    parsed = CustomCommandParse(
        targets=["阿珍", "阿強"],
        command_event_describe="阿珍主動約阿強今晚去玫瑰酒吧見面，打算問清楚封信嘅來龍去脈。",
        feasible=True,
        refuse_reason=None,
    )
    llm = FakeLLM({"gm_round_summary": make_analysis(), "gm_custom_command": parsed})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    gm.on_round_end(server, 1)
    report = gm.apply_player_choice(
        server, 1, PlayerChoice(type="custom", text="命令阿珍主動約阿強去酒吧")
    )
    assert report.ok is True
    assert report.injected[0].source == "custom"
    assert set(report.injected[0].targets) == {"阿珍", "阿強"}
    # 只有 targets 被注入，poignancy 10
    assert server.game.agents["阿珍"].injected[0]["poignancy"] == 10
    assert len(server.game.agents["小美"].injected) == 0


def test_parse_command_filters_hallucinated_targets(tmp_path):
    parsed = CustomCommandParse(
        targets=["阿珍", "鬼", "哈利波特"],
        command_event_describe="阿珍決定將封信嘅事話俾全小鎮知。",
        feasible=True,
        refuse_reason=None,
    )
    llm = FakeLLM({"gm_custom_command": parsed})
    gm = make_director(tmp_path, llm)
    result = gm.parse_custom_command("阿珍爆料")
    assert result.feasible is True
    assert result.targets == ["阿珍"]  # 幻覺名被過濾


def test_parse_command_all_unknown_targets_infeasible(tmp_path):
    parsed = CustomCommandParse(
        targets=["鬼"], command_event_describe="鬼做咗啲嘢。",
        feasible=True, refuse_reason=None,
    )
    llm = FakeLLM({"gm_custom_command": parsed})
    gm = make_director(tmp_path, llm)
    result = gm.parse_custom_command("叫鬼出場")
    assert result.feasible is False


def test_parse_command_empty_text():
    gm = None
    llm = FakeLLM()
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        gm = GMDirector("t", d, {}, NAMES, prompts_dir=PROMPTS_DIR, llm=llm)
        result = gm.parse_custom_command("   ")
        assert result.feasible is False
        assert not llm.calls  # 空文本唔 call LLM


def test_parse_command_forbidden_describe_rejected(tmp_path):
    parsed = CustomCommandParse(
        targets=["阿珍"], command_event_describe="阿珍去睡覺了。",
        feasible=True, refuse_reason=None,
    )
    llm = FakeLLM({"gm_custom_command": parsed})
    gm = make_director(tmp_path, llm)
    result = gm.parse_custom_command("叫阿珍去瞓")
    assert result.feasible is False  # 命中 KW_SLEEPING 禁詞


# ---------------------------------------------------------------- 好感度 + 終章


def test_affinity_overrides_applied(tmp_path):
    llm = FakeLLM({"gm_round_summary": make_analysis()})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    gm.on_round_end(server, 1)
    report = gm.apply_player_choice(
        server,
        1,
        PlayerChoice(
            type="skip",
            affinity_overrides=[
                AffinitySuggestion(from_agent="阿珍", to_agent="阿強", delta=-15, reason="阿強隱瞞")
            ],
        ),
    )
    assert report.ok is True
    assert len(report.affinity_changes) == 1
    assert report.affinity_changes[0].new == -15
    assert server.config["affinity"]["阿珍"]["阿強"]["value"] == -15
    # 好感度變動入 timeline
    last = gm.state.build_story_timeline()[-1]
    assert len(last.affinity_changes) == 1


def test_finale_idempotent(tmp_path):
    narrative = FinaleNarrative(
        ending="封信最終揭開咗兩人多年嘅誤會，小鎮回復平靜。" * 10,
        character_epilogues=[{"name": n, "epilogue": "繼續生活。"} for n in NAMES],
    )
    llm = FakeLLM({"gm_round_summary": make_analysis(), "gm_finale": narrative})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    gm.on_round_end(server, 1)
    gm.apply_player_choice(server, 1, PlayerChoice(type="option", option_id="B"))
    finale1 = gm.generate_finale(server)
    finale2 = gm.generate_finale(server)
    assert finale1.narrative.ending == finale2.narrative.ending
    # 第二次唔再 call LLM
    assert sum(1 for c in llm.calls if c["caller"] == "gm_finale") == 1
    assert len(finale1.timeline) == 2  # seed + round 1
    # 角色結局齊人
    assert {e["name"] for e in finale1.narrative.character_epilogues} == set(NAMES)


def test_finale_failsafe_when_llm_dead(tmp_path):
    gm = make_director(tmp_path, FakeLLM())
    server = FakeServer()
    finale = gm.generate_finale(server)
    assert "命運之線" in finale.narrative.ending
    assert len(finale.narrative.character_epilogues) == len(NAMES)


def test_apply_expired_option_rejected(tmp_path):
    """選項唔喺 pending 入面（過期/重複提交）→ 拒絕，回合唔推進。"""
    llm = FakeLLM({"gm_round_summary": make_analysis()})
    gm = make_director(tmp_path, llm)
    server = FakeServer()
    gm.on_round_start(server)
    add_conversation(server)
    gm.on_round_end(server, 1)
    report = gm.apply_player_choice(server, 1, PlayerChoice(type="option", option_id="Z"))
    assert report.ok is False
    assert "過期" in report.message
    # pending 保留，可以重試
    assert gm.get_pending_decision() is not None
    assert all(len(server.game.agents[n].injected) == 0 for n in NAMES)
