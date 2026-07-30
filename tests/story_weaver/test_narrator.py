"""narrator + 節奏設定測試。"""

import json
import os

import pytest

from story_weaver.gameui import llm_settings
from story_weaver.gameui.narrator import StepNarrator


class FakeLLM:
    def __init__(self, out="阿珍喺大街追上阿強，兩人講返嗰封信嘅事。"):
        self.out = out
        self.calls = []

    def completion(self, prompt, retry=10, callback=None, failsafe=None,
                   return_type=None, caller="llm_normal", **kwargs):
        self.calls.append(prompt)
        return self.out


TEMPLATE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "generative_agents", "data", "prompts_gm", "gm_step_narrative.txt"))


def test_narrate_happy():
    n = StepNarrator(FakeLLM(), template_path=TEMPLATE)
    text = n.narrate("20240213-10:30", ["阿珍喺大街散步"], ["阿珍：「你收埋封信做咩？」"])
    assert "阿珍" in text


def test_narrate_includes_story_context():
    """The live narrator must receive the unresolved story thread."""
    llm = FakeLLM()
    n = StepNarrator(llm, template_path=TEMPLATE)

    n.narrate(
        "20240213-10:30",
        ["阿珍走到咖啡館"],
        [],
        story_context="阿珍仍未決定是否公開阿強藏起來的信。",
    )

    assert "阿珍仍未決定是否公開阿強藏起來的信。" in llm.calls[0]


def test_narrate_llm_none():
    n = StepNarrator(None, template_path=TEMPLATE)
    assert n.narrate("", ["x"], []) is None


def test_narrate_llm_failure():
    n = StepNarrator(FakeLLM(out=None), template_path=TEMPLATE)
    assert n.narrate("", ["x"], []) is None  # 靜默 skip，唔 throw


@pytest.fixture()
def cfg_files(tmp_path, monkeypatch):
    gm_path = tmp_path / "gm_config.json"
    gm_path.write_text(json.dumps({"llm": {}, "steps_per_round": 6}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"agent": {"chat_iter": 4}}), encoding="utf-8")
    monkeypatch.setattr(llm_settings, "GM_CONFIG_PATH", str(gm_path))
    monkeypatch.setattr(llm_settings, "CONFIG_PATH", str(config_path))
    return gm_path


def test_pace_roundtrip(cfg_files):
    s = llm_settings.load_settings()
    assert s["pace"]["steps_per_round"] == 6
    errors = llm_settings.save_settings({"pace": {"steps_per_round": 4, "chat_iter": 2, "max_rounds": 5}})
    assert errors == []
    assert llm_settings.load_settings()["pace"]["steps_per_round"] == 4
    assert llm_settings.load_settings()["pace"]["chat_iter"] == 2
    assert llm_settings.load_settings()["pace"]["max_rounds"] == 5


def test_pace_validation(cfg_files):
    errors = llm_settings.save_settings({"pace": {"steps_per_round": 99, "chat_iter": 2}})
    assert errors and "1-20" in errors[0]
    errors = llm_settings.save_settings({"pace": {"steps_per_round": 4, "chat_iter": 99}})
    assert errors and "1-8" in errors[0]
    errors = llm_settings.save_settings({"pace": {"steps_per_round": 4, "chat_iter": 2, "max_rounds": 1}})
    assert errors and "2-50" in errors[0]
    assert llm_settings.load_settings()["pace"]["steps_per_round"] == 6  # 無寫到
