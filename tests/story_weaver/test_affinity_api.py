"""Flask blueprint 測試（spec §3.2、§7）。"""

import json
import os

import pytest
from flask import Flask

from story_weaver.affinity.api import affinity_bp

NAMES = ["阿珍", "阿強", "小美", "阿明"]


@pytest.fixture()
def client(tmp_path):
    app = Flask("affinity_test")
    app.register_blueprint(affinity_bp)
    app.config["AFFINITY_CHECKPOINTS_ROOT"] = str(tmp_path / "checkpoints")
    return app.test_client()


def write_checkpoint(root, sim_name, config):
    folder = os.path.join(root, sim_name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "simulate-20240213-1100.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


# ---------------------------------------------------------------- bands


def test_bands(client):
    res = client.get("/api/affinity/bands")
    assert res.status_code == 200
    body = res.get_json()
    assert body["min"] == -100 and body["max"] == 100 and body["default"] == 0
    labels = [b["label"] for b in body["bands"]]
    assert labels == ["摯愛/至交", "友好", "略有好感", "陌生/中立", "略有反感", "敵對", "死敵/痛恨"]


# ---------------------------------------------------------------- validate


def test_validate_ok(client):
    res = client.post("/api/affinity/validate", json={
        "agents": NAMES,
        "relations": [{"from": "阿珍", "to": "阿強", "affinity": -65, "label": "舊情人，分手時鬧得好僵"}],
    })
    assert res.status_code == 200
    matrix = res.get_json()["affinity"]
    assert matrix["阿珍"]["阿強"] == {"value": -65, "label": "舊情人，分手時鬧得好僵"}
    assert matrix["阿珍"]["小美"] == {"value": 0, "label": ""}
    assert set(matrix.keys()) == set(NAMES)


def test_validate_400_typo(client):
    res = client.post("/api/affinity/validate", json={
        "agents": NAMES,
        "relations": [{"from": "阿珍", "to": "阿強強", "affinity": 10}],
    })
    assert res.status_code == 400
    errors = res.get_json()["errors"]
    assert errors[0]["to"] == "阿強強"
    assert "阿強強" in errors[0]["message"]


def test_validate_400_diagonal(client):
    res = client.post("/api/affinity/validate", json={
        "agents": NAMES,
        "relations": [{"from": "阿珍", "to": "阿珍", "affinity": 10}],
    })
    assert res.status_code == 400


def test_validate_400_malformed(client):
    res = client.post("/api/affinity/validate", json={"agents": []})
    assert res.status_code == 400


# ---------------------------------------------------------------- GET /<sim>


def test_get_sim_404(client):
    res = client.get("/api/affinity/唔存在嘅故仔")
    assert res.status_code == 404


def test_get_sim_legacy(client, tmp_path):
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "old-sim", {"step": 6, "time": "20240213-11:00"})
    res = client.get("/api/affinity/old-sim")
    assert res.status_code == 200
    assert res.get_json() == {"affinity": {}, "legacy": True}


def test_get_sim_ok(client, tmp_path):
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-a", {
        "step": 6, "time": "20240213-11:00",
        "affinity": {"阿珍": {"阿強": {"value": -50, "label": "舊情人"}}},
    })
    res = client.get("/api/affinity/sim-a")
    assert res.status_code == 200
    body = res.get_json()
    assert body["step"] == 6
    assert body["affinity"]["阿珍"]["阿強"]["value"] == -50


# ---------------------------------------------------------------- GET /<sim>/changes


def _rounds_config():
    return {
        "affinity": {},
        "affinity_rounds": [
            {"round": 1, "step": 6, "time": "20240213-10:30", "changes": []},
            {"round": 2, "step": 12, "time": "20240213-11:00", "changes": [
                {"from_agent": "阿珍", "to_agent": "阿強", "old": -65, "new": -50,
                 "delta": 15, "reason": "阿強幫阿珍解圍", "absolute": False},
            ]},
        ],
    }


def test_changes_latest_round(client, tmp_path):
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-b", _rounds_config())
    res = client.get("/api/affinity/sim-b/changes")
    assert res.status_code == 200
    body = res.get_json()
    assert body["round"] == 2
    assert body["display"] == ["阿珍 → 阿強：-65 → -50（+15）：阿強幫阿珍解圍"]


def test_changes_display_format(client, tmp_path):
    """PRD Done When 第 9 條：modal 顯示 -65 → -50（+15）格式。"""
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-c", _rounds_config())
    body = client.get("/api/affinity/sim-c/changes?round=2").get_json()
    assert "（+15）" in body["display"][0]
    assert "-65 → -50" in body["display"][0]


def test_changes_specific_empty_round(client, tmp_path):
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-d", _rounds_config())
    body = client.get("/api/affinity/sim-d/changes?round=1").get_json()
    assert body["round"] == 1
    assert body["changes"] == [] and body["display"] == []


def test_changes_round_not_found(client, tmp_path):
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-e", _rounds_config())
    assert client.get("/api/affinity/sim-e/changes?round=99").status_code == 404


def test_changes_no_rounds_key(client, tmp_path):
    root = str(tmp_path / "checkpoints")
    write_checkpoint(root, "sim-f", {"affinity": {}})
    body = client.get("/api/affinity/sim-f/changes").get_json()
    assert body == {"round": 0, "changes": [], "display": []}
