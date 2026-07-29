"""API 測試（spec §11）：schema、分頁、markdown 導出、降級提示、LLM 失敗唔 500。"""

import os

import pytest
from flask import Flask

from story_weaver.recap.api import configure, recap_bp
from story_weaver.recap.models import AgentProfile
from story_weaver.recap.service import RecapService

from test_recap_extractors import make_checkpoint, make_sim  # noqa: E402

GEN_DIR = "/Users/kenneth/Projects/story-weaver/generative_agents"
PROMPTS_DIR = os.path.join(GEN_DIR, "data", "prompts")

PROFILES = [
    AgentProfile(name="阿珍", occupation="作家", personality="固執"),
    AgentProfile(name="阿強", occupation="藥劑師", personality="沉默"),
]


class FakeLLM:
    def __init__(self, output=None):
        self.output = output
        self.calls = []

    def completion(self, prompt, retry=10, callback=None, failsafe=None,
                   return_type=None, caller="llm_normal", **kwargs):
        self.calls.append(caller)
        if self.output is None:
            return failsafe
        return callback(self.output) if callback else self.output

    def get_summary(self):
        return {"model": "fake", "summary": {}}


class FakeResponse:
    def __init__(self, res):
        self.res = res


GOOD_RESPONSE = FakeResponse("阿珍喺酒吧質問阿強封信嘅事，兩人關係跌到冰點。" + "經過" * 20)


@pytest.fixture()
def client(tmp_path):
    root = str(tmp_path / "ckpts")
    os.makedirs(root)
    sim_dir = os.path.join(root, "s1")
    os.makedirs(sim_dir)
    # 一個回合嘅 checkpoint + 對話
    make_checkpoint(sim_dir, 1, "20240213-09:30", {
        "阿珍": ("正在", "寫信", "阿珍正在房間寫信", ["小鎮", "屋企", "房間"]),
    })
    make_checkpoint(sim_dir, 2, "20240213-09:40", {
        "阿珍": ("正在", "出門", "阿珍出門去酒吧", ["小鎮", "玫瑰酒吧"]),
    })
    import json

    with open(os.path.join(sim_dir, "conversation.json"), "w", encoding="utf-8") as f:
        json.dump({"20240213-09:40": [{"阿珍 -> 阿強 @ 小鎮，玫瑰酒吧": [["阿珍", "你收埋封信做咩？"]]}]}, f, ensure_ascii=False)

    svc = RecapService(checkpoints_root=root, llm=FakeLLM(GOOD_RESPONSE),
                       template_path=PROMPTS_DIR)
    svc.init_story("s1", "一封信引發嘅恩怨", PROFILES)
    svc.on_round_end("s1", 1, (1, 2), background=False)

    app = Flask(__name__)
    app.register_blueprint(recap_bp)
    configure(svc)
    return app.test_client()


def test_recap_schema(client):
    resp = client.get("/api/story/s1/recap")
    assert resp.status_code == 200
    data = resp.get_json()
    for key in ("sim_name", "opening", "agents", "cumulative_recap", "rounds", "ui_hints"):
        assert key in data
    assert data["opening"] == "一封信引發嘅恩怨"
    assert data["cumulative_recap"]["status"] == "ok"
    r = data["rounds"][0]
    assert r["round"] == 1
    assert r["recap_status"] == "ok"
    assert r["dialogues"][0]["lines"][0]["text"] == "你收埋封信做咩？"  # 原文
    # ui_hints
    assert data["ui_hints"]["show_fallback_banner"] is False
    assert data["ui_hints"]["generating"] is False
    assert "GM 正在整理故事" in data["ui_hints"]["generating_message"]


def test_fallback_banner_when_llm_dead(tmp_path):
    root = str(tmp_path / "ckpts")
    sim_dir = os.path.join(root, "s1")
    os.makedirs(sim_dir)
    make_checkpoint(sim_dir, 1, "20240213-09:30", {
        "阿珍": ("正在", "寫信", "阿珍正在房間寫信", ["小鎮", "屋企"]),
    })
    svc = RecapService(checkpoints_root=root, llm=FakeLLM(None), template_path=PROMPTS_DIR)
    svc.init_story("s1", "開端", PROFILES)
    svc.on_round_end("s1", 1, (1, 1), background=False)
    app = Flask(__name__)
    app.register_blueprint(recap_bp)
    configure(svc)
    c = app.test_client()
    resp = c.get("/api/story/s1/recap")
    assert resp.status_code == 200  # LLM 失敗唔會 500
    data = resp.get_json()
    assert data["ui_hints"]["show_fallback_banner"] is True
    assert data["rounds"][0]["recap_status"] == "fallback"
    assert "阿珍正在房間寫信" in data["rounds"][0]["round_recap"]  # 降級都有原始記錄


def test_pagination(client):
    resp = client.get("/api/story/s1/recap?round=1")
    assert resp.status_code == 200
    assert len(resp.get_json()["rounds"]) == 1
    resp = client.get("/api/story/s1/recap?round=99")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "round_out_of_range"


def test_markdown_export(client):
    resp = client.get("/api/story/s1/recap?format=markdown")
    assert resp.status_code == 200
    assert "text/markdown" in resp.content_type
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    md = resp.get_data(as_text=True)
    assert "# s1：故事全記錄" in md
    assert "一封信引發嘅恩怨" in md
    assert "> 你收埋封信做咩？" in md  # compress.py 引用排版


def test_story_not_found(client):
    resp = client.get("/api/story/唔存在/recap")
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "story_not_found"


def test_post_decision_and_upsert(client):
    resp = client.post("/api/story/s1/recap/decision",
                       json={"round": 1, "type": "option", "text": "讓阿珍質問阿強"})
    assert resp.status_code == 200
    assert resp.get_json()["upserted"] is False
    resp = client.post("/api/story/s1/recap/decision",
                       json={"round": 1, "type": "custom", "text": "改為暗中調查"})
    assert resp.get_json()["upserted"] is True
    # 時間線有 ✦ 標記數據
    data = client.get("/api/story/s1/recap").get_json()
    assert data["rounds"][0]["player_decision"]["text"] == "改為暗中調查"


def test_post_decision_validation(client):
    assert client.post("/api/story/s1/recap/decision", json={"type": "option", "text": "x"}).status_code == 400
    assert client.post("/api/story/s1/recap/decision", json={"round": 1, "type": "bad", "text": "x"}).status_code == 400
    assert client.post("/api/story/s1/recap/decision", json={"round": 1, "type": "option", "text": ""}).status_code == 400
    assert client.post("/api/story/唔存在/recap/decision",
                       json={"round": 1, "type": "option", "text": "x"}).status_code == 404
