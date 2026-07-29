"""Store / Service 寫入路徑測試（spec §11）：原子寫入、upsert、init 守衛、gm_note。"""

import json
import os

import pytest

from story_weaver.recap.models import AgentProfile, PlayerDecision
from story_weaver.recap.service import OpeningMissingError, RecapService
from story_weaver.recap.store import StoryRecapStore

PROFILES = [
    AgentProfile(name="阿珍", occupation="作家", personality="固執"),
    AgentProfile(name="阿強", occupation="藥劑師", personality="沉默"),
]


def make_service(tmp_path, llm=None):
    return RecapService(
        checkpoints_root=str(tmp_path),
        llm=llm or FakeLLM(),
        template_path="data/prompts",
    )


class FakeLLM:
    def completion(self, prompt, retry=10, callback=None, failsafe=None,
                   return_type=None, caller="llm_normal", **kwargs):
        return failsafe

    def get_summary(self):
        return {"model": "fake", "summary": {}}


def test_init_story_and_idempotent(tmp_path):
    svc = make_service(tmp_path)
    recap = svc.init_story("s1", "一封信引發嘅恩怨", PROFILES)
    assert recap.opening == "一封信引發嘅恩怨"
    # 第一回合前 cumulative 直出模板，唔使 LLM
    assert recap.cumulative_recap.status == "ok"
    assert "故事即將展開" in recap.cumulative_recap.text
    # 冪等：第二次唔覆蓋
    again = svc.init_story("s1", "另一個開端", PROFILES)
    assert again.opening == "一封信引發嘅恩怨"


def test_init_story_rejects_empty_opening(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(OpeningMissingError):
        svc.init_story("s1", "   ", PROFILES)
    # 唔會寫出半個檔
    assert not os.path.exists(os.path.join(str(tmp_path), "s1", "story_recap.json"))


def test_atomic_write_valid_json(tmp_path):
    svc = make_service(tmp_path)
    svc.init_story("s1", "開端", PROFILES)
    path = os.path.join(str(tmp_path), "s1", "story_recap.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)  # 永遠係合法 JSON
    assert data["opening"] == "開端"
    # 無 tmp 殘檔
    assert not [f for f in os.listdir(os.path.dirname(path)) if ".tmp." in f]


def test_stale_tmp_cleaned_on_start(tmp_path):
    sim_dir = os.path.join(str(tmp_path), "s1")
    os.makedirs(sim_dir)
    stale = os.path.join(sim_dir, "story_recap.json.tmp.99999")
    with open(stale, "w") as f:
        f.write("寫到一半")
    StoryRecapStore(str(tmp_path))  # 啟動時清理
    assert not os.path.exists(stale)


def test_decision_upsert_by_round(tmp_path):
    svc = make_service(tmp_path)
    svc.init_story("s1", "開端", PROFILES)
    first = svc.record_player_decision(
        "s1", 2, PlayerDecision(type="option", text="第一次決定", chosen_at="t1")
    )
    assert first is False  # 首次寫入
    second = svc.record_player_decision(
        "s1", 2, PlayerDecision(type="custom", text="改咗主意", chosen_at="t2")
    )
    assert second is True  # upsert 覆蓋
    decision = svc.get_player_decision("s1", 2)
    assert decision.text == "改咗主意"
    assert decision.round == 2


def test_record_gm_note(tmp_path):
    svc = make_service(tmp_path)
    svc.init_story("s1", "開端", PROFILES)
    svc.record_gm_note("s1", 1, "阿珍對阿強嘅好感降至 -40")
    recap = svc.get_recap("s1")
    assert recap.rounds[0].events[0].type == "gm_note"
    assert recap.rounds[0].events[0].agent == "GM"
    with pytest.raises(ValueError):
        svc.record_gm_note("s1", 1, "  ")


def test_build_gm_context(tmp_path):
    svc = make_service(tmp_path)
    svc.init_story("s1", "開端", PROFILES)
    ctx = svc.build_gm_context("s1")
    assert ctx.opening == "開端"
    assert ctx.round_count == 0
    assert ctx.latest_round is None
    assert [a.name for a in ctx.agents] == ["阿珍", "阿強"]
    assert svc.build_gm_context("唔存在") is None
