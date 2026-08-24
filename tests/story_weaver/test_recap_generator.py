"""Generator 測試（spec §11）：LLM 全敗 fallback、validator 拒垃圾、token 紅線、捷徑。"""

import os
from pathlib import Path

from story_weaver.recap.generator import (
    CONTEXT_BUDGET_RATIO,
    RecapGenerator,
    build_round_fallback,
    make_validator,
)
from story_weaver.recap.models import (
    DialogueBlock,
    DialogueLine,
    RoundRecap,
    TimelineEvent,
)
from story_weaver.recap.prompts import RecapPrompt

GEN_DIR = str(Path(__file__).resolve().parents[2] / "generative_agents")
PROMPTS_DIR = os.path.join(GEN_DIR, "data", "prompts")

NAMES = ["阿珍", "阿強"]


class FakeLLM:
    """模仿 LLMModel.completion 嘅 callback/retry/failsafe 語義。"""

    def __init__(self, outputs=None):
        # outputs: list — 逐次 retry 回應；None 表示該次失敗
        self.outputs = outputs or []
        self.calls = []

    def completion(self, prompt, retry=10, callback=None, failsafe=None,
                   return_type=None, caller="llm_normal", **kwargs):
        self.calls.append({"caller": caller, "prompt": prompt})
        for i in range(retry):
            output = self.outputs[i] if i < len(self.outputs) else None
            if output is None:
                continue
            response = callback(output) if callback else output
            if response is not None:
                return response
        return failsafe

    def get_summary(self):
        return {"model": "fake-llm", "summary": {}}


def make_round(events=True, dialogues=True):
    return RoundRecap(
        round=1,
        sim_time_start="20240213-09:30",
        sim_time_end="20240213-12:00",
        step_range=(1, 12),
        events=[TimelineEvent(
            sim_time="20240213-09:45", agent="阿珍", type="action",
            location="學院，圖書館", describe="阿珍正在圖書館查閱資料", poignancy=5, step=1,
        )] if events else [],
        dialogues=[DialogueBlock(
            sim_time="20240213-10:15", participants=["阿珍", "阿強"],
            location="玫瑰酒吧",
            lines=[DialogueLine(speaker="阿珍", text="你收埋封信做咩？")],
        )] if dialogues else [],
    )


class FakeResponse:
    def __init__(self, res):
        self.res = res


# ---------------------------------------------------------------- validator


def test_validator_rules():
    v = make_validator(NAMES)
    good = FakeResponse("阿珍喺圖書館搵到一啲線索，佢決定繼續追查落去。" + "細節" * 20)
    assert v(good) is not None
    assert v(FakeResponse("")) is None                       # 空
    assert v(FakeResponse("太短")) is None                    # <50 字
    assert v(FakeResponse("阿珍" + "長" * 60 + "{opening}")) is None  # 未渲染 placeholder
    assert v(FakeResponse("佢哋" + "長" * 60)) is None        # 無角色名


# ---------------------------------------------------------------- round recap


def test_llm_total_failure_fallback():
    llm = FakeLLM(outputs=[])  # 全部失敗
    gen = RecapGenerator(llm=llm, template_path=PROMPTS_DIR)
    text, status = gen.generate_round_recap("開端", make_round(), [], NAMES)
    assert status == "fallback"
    assert "故事摘要暫時不可用" in text
    # 降級文本保留事件同對白原文
    assert "阿珍正在圖書館查閱資料" in text
    assert "你收埋封信做咩？" in text


def test_llm_success():
    good = FakeResponse("阿珍喺圖書館搵到線索之後，去咗酒吧質問阿強，兩人氣氛緊張。" + "補充" * 20)
    llm = FakeLLM(outputs=[good])
    gen = RecapGenerator(llm=llm, template_path=PROMPTS_DIR)
    text, status = gen.generate_round_recap("開端", make_round(), [], NAMES)
    assert status == "ok"
    assert "阿珍" in text


def test_quiet_round_shortcut_no_llm():
    llm = FakeLLM()
    gen = RecapGenerator(llm=llm, template_path=PROMPTS_DIR)
    text, status = gen.generate_round_recap("開端", make_round(events=False, dialogues=False), [], NAMES)
    assert status == "ok"
    assert "風平浪靜" in text
    assert llm.calls == []  # 唔調 LLM


def test_fallback_text_includes_raw_records():
    text = build_round_fallback(make_round())
    assert "第 1 回合（20240213-09:30 – 20240213-12:00）" in text
    assert "阿珍：「你收埋封信做咩？」" in text


# ---------------------------------------------------------------- cumulative


def test_first_round_shortcut_no_llm():
    llm = FakeLLM()
    gen = RecapGenerator(llm=llm, template_path=PROMPTS_DIR)
    text, status = gen.generate_cumulative("一封信引發嘅恩怨", [], NAMES)
    assert status == "ok"
    assert "故事即將展開" in text
    assert llm.calls == []


def test_cumulative_llm_failure_fallback():
    llm = FakeLLM(outputs=[])
    gen = RecapGenerator(llm=llm, template_path=PROMPTS_DIR)
    text, status = gen.generate_cumulative("開端", [make_round()], NAMES)
    assert status == "fallback"
    assert "開端" in text
    assert "阿珍正在圖書館查閱資料" in text


def test_layered_token_budget():
    """10 回合假數據 → cumulative prompt 唔超過 60% context 紅線。"""
    prompts = RecapPrompt(PROMPTS_DIR)
    rounds = []
    for i in range(1, 11):
        r = make_round()
        r.round = i
        r.round_recap = f"第{i}回合發生咗好多事。" + "劇情" * 90  # ~200字，會被截斷
        rounds.append(r)
    summaries = [{"round": r.round, "recap": r.round_recap} for r in rounds]
    prompt = prompts.cumulative_input("開端", summaries, rounds[-1])
    gen = RecapGenerator(llm=FakeLLM(), template_path=PROMPTS_DIR)
    assert gen.estimate_tokens(prompt) <= int(8192 * CONTEXT_BUDGET_RATIO)
    # 每段 round_recap 被截到 200 字保底
    for i in range(1, 11):
        segment = f"第{i}回合：" + ("第%d回合發生咗好多事。" % i + "劇情" * 90)[:200 - 12]
        assert f"第{i}回合：" in prompt


def test_prompt_templates_render(tmp_path):
    """兩個模板可以正常渲染（無 $var 殘留）。"""
    prompts = RecapPrompt(PROMPTS_DIR)
    p1 = prompts.round_input("開端", make_round(), [])
    assert "${" not in p1 and "$opening" not in p1
    p2 = prompts.cumulative_input("開端", [], make_round())
    assert "${" not in p2 and "$opening" not in p2
