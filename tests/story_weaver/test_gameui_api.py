"""game_server API 測試：routes、輪詢 schema、決策流程、heartbeat、導出。"""

import json
import os
import time
from pathlib import Path

import pytest
from flask import Flask

from story_weaver.gm import GMDirector
from story_weaver.gameui import game_server
from story_weaver.gameui.models import UIStatus
from story_weaver.gameui.round_runner import RoundRunner
from story_weaver.recap import RecapService

from test_gameui import (
    AGENTS,
    FakeLLM,
    FakeMaze,
    FakeSimServer,
    make_session,
    wait_status,
)

GEN_DIR = str(Path(__file__).resolve().parents[2] / "generative_agents")
PROMPTS_GM = os.path.join(GEN_DIR, "data", "prompts_gm")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setenv("STORY_WEAVER_CHECKPOINTS", root)
    game_server.reset_runners()

    def factory(session, folder):
        gm = GMDirector(session, folder, {}, AGENTS, prompts_dir=PROMPTS_GM, llm=FakeLLM())
        server = FakeSimServer(folder)
        return RoundRunner(
            session, folder, gm=gm, recap=RecapService(checkpoints_root=str(tmp_path), llm=FakeLLM()), maze=FakeMaze(),
            server_factory=lambda r: server,
        )

    game_server.set_runner_factory(factory)
    make_session(tmp_path, "s1")
    app = game_server.create_app()
    app.testing = True
    yield app.test_client()
    game_server.set_runner_factory(None)
    game_server.reset_runners()


def test_state_schema(client):
    resp = client.get("/api/state?name=s1")
    assert resp.status_code == 200
    data = resp.get_json()
    for key in ("status", "round", "max_rounds", "sim_time", "new_frames", "new_feed",
                "agents_meta", "pending_decision", "affinity", "readonly", "error"):
        assert key in data
    assert data["status"] == "idle"
    assert data["agents"] == AGENTS


def test_state_404(client):
    assert client.get("/api/state?name=唔存在").status_code == 404


def test_game_page_404(client):
    assert client.get("/game?name=唔存在").status_code == 404


def test_full_round_flow(client):
    # 開始推演
    resp = client.post("/api/round/start", json={"name": "s1", "command": "start"})
    assert resp.status_code == 200
    assert resp.get_json() == {"accepted": True, "round": 1}

    runner = game_server.get_runner("s1")
    assert wait_status(runner, UIStatus.WAITING_DECISION)

    # 推演完再撳 → 409
    resp = client.post("/api/round/start", json={"name": "s1", "command": "next"})
    assert resp.status_code == 409
    assert resp.get_json()["accepted"] is False

    # 輪詢：pending_decision + feed + frames
    data = client.get("/api/state?name=s1").get_json()
    assert data["status"] == "waiting_decision"
    assert data["pending_decision"]["options"][0]["id"] == "A"
    assert data["pending_decision"]["story_timeline"]
    assert len(data["new_feed"]) > 0
    assert data["frame_latest"] > 0

    # 決策
    resp = client.post("/api/decision", json={"name": "s1", "choice_id": "A"})
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["accepted"] is True
    assert body["round"] == 1
    assert body["status"] == "idle"
    assert len(body["injected"]) == 1

    # timeline 有嘢睇
    tl = client.get("/api/timeline?name=s1").get_json()["timeline"]
    assert tl[0]["round"] == 0  # 故事開端
    assert any(e["round"] == 1 for e in tl)


def test_decision_invalid_target(client):
    client.post("/api/round/start", json={"name": "s1"})
    runner = game_server.get_runner("s1")
    assert wait_status(runner, UIStatus.WAITING_DECISION)
    resp = client.post("/api/decision", json={
        "name": "s1",
        "custom_command": {"target_agent": "鬼", "text": "做啲嘢"},
    })
    assert resp.status_code == 400


def test_decision_wrong_status(client):
    resp = client.post("/api/decision", json={"name": "s1", "choice_id": "A"})
    assert resp.status_code == 409


def test_heartbeat_ownership(client):
    r1 = client.post("/api/heartbeat", json={"name": "s1", "client_id": "tab-a"})
    assert r1.get_json()["is_owner"] is True
    r2 = client.post("/api/heartbeat", json={"name": "s1", "client_id": "tab-b"})
    body = r2.get_json()
    assert body["is_owner"] is False
    assert body["readonly"] is True
    # 非控制 tab 唔可以開始推演
    resp = client.post("/api/round/start", json={"name": "s1", "client_id": "tab-b"})
    assert resp.status_code == 403
    # 控制 tab 可以
    resp = client.post("/api/round/start", json={"name": "s1", "client_id": "tab-a"})
    assert resp.status_code == 200
    # 等推演完成（全局鎖係 process-wide，唔等會影響下一個測試）
    runner = game_server.get_runner("s1")
    assert wait_status(runner, UIStatus.WAITING_DECISION)


def test_export_story(client):
    client.post("/api/round/start", json={"name": "s1"})
    runner = game_server.get_runner("s1")
    assert wait_status(runner, UIStatus.WAITING_DECISION)
    resp = client.get("/api/export/story?name=s1&format=md")
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    resp = client.get("/api/export/story?name=s1&format=json")
    assert resp.status_code == 200
    assert resp.get_data(as_text=True).startswith("[")


def test_game_page_renders(client):
    resp = client.get("/game?name=s1")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "game-container" in html
    assert "decision-modal" in html
    assert "阿珍" in html


def test_heartbeat_force_takeover(client):
    client.post("/api/heartbeat", json={"name": "s1", "client_id": "tab-a"})
    # tab-b 正常搶唔到；force 就可以
    r = client.post("/api/heartbeat", json={"name": "s1", "client_id": "tab-b"})
    assert r.get_json()["is_owner"] is False
    r = client.post("/api/heartbeat", json={"name": "s1", "client_id": "tab-b", "force": True})
    assert r.get_json()["is_owner"] is True
    # tab-a 之後正常 heartbeat 都搶唔返（租約未過期）
    r = client.post("/api/heartbeat", json={"name": "s1", "client_id": "tab-a"})
    assert r.get_json()["is_owner"] is False
