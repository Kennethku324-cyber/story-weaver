"""故事列表 + 刪除 API 測試。"""

import json
import os

import pytest

from story_weaver.gameui import game_server

from test_gameui import AGENTS, make_session


@pytest.fixture()
def client(tmp_path, monkeypatch):
    root = str(tmp_path)
    monkeypatch.setenv("STORY_WEAVER_CHECKPOINTS", root)
    game_server.reset_runners()
    make_session(tmp_path, "故事甲")
    make_session(tmp_path, "故事乙")
    app = game_server.create_app()
    app.testing = True
    yield app.test_client(), root
    game_server.reset_runners()


def test_list_stories(client):
    c, _root = client
    resp = c.get("/api/stories")
    assert resp.status_code == 200
    names = [s["name"] for s in resp.get_json()["stories"]]
    assert sorted(names) == sorted(["故事甲", "故事乙"])
    assert resp.get_json()["stories"][0]["characters"] == AGENTS


def test_delete_story(client, monkeypatch, tmp_path):
    c, root = client
    # 假 GEN_ROOT 指向另一個目錄（唔好放喺 checkpoints root 入面）
    fake_gen = tmp_path / "fake_gen"
    story_assets = fake_gen / "frontend/static/assets/village/story_agents" / "故事甲"
    story_assets.mkdir(parents=True)
    monkeypatch.setattr(game_server, "GEN_ROOT", str(fake_gen))

    resp = c.post("/api/story/delete", json={"name": "故事甲"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert not os.path.exists(os.path.join(root, "故事甲"))
    assert not story_assets.exists()  # story_agents/<故事> 都刪埋
    # 列表淨返故事乙
    names = [s["name"] for s in c.get("/api/stories").get_json()["stories"]]
    assert names == ["故事乙"]


def test_delete_not_found(client):
    c, _root = client
    resp = c.post("/api/story/delete", json={"name": "唔存在"})
    assert resp.status_code == 404


def test_delete_bad_name(client):
    c, _root = client
    assert c.post("/api/story/delete", json={"name": "../etc"}).status_code == 400
    assert c.post("/api/story/delete", json={"name": ""}).status_code == 400
