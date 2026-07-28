"""story_weaver.affinity.api — Flask Blueprint（spec §3.2）。

無狀態，註冊去邊個 Flask app 都得。checkpoint 目錄預設 "results/checkpoints"（相對 cwd），
可以用 app.config["AFFINITY_CHECKPOINTS_ROOT"] 覆蓋（測試用）。
"""

from __future__ import annotations

import glob
import json
import os

from flask import Blueprint, current_app, jsonify, request
from pydantic import ValidationError

from .models import AFFINITY_MAX, AFFINITY_MIN, BANDS, SetupAffinityPayload
from .store import SetupValidationError, validate_setup

affinity_bp = Blueprint("affinity", __name__, url_prefix="/api/affinity")


def _checkpoints_root() -> str:
    return current_app.config.get("AFFINITY_CHECKPOINTS_ROOT", "results/checkpoints")


def _latest_checkpoint(sim_name: str) -> dict | None:
    folder = os.path.join(_checkpoints_root(), sim_name)
    files = sorted(glob.glob(os.path.join(folder, "simulate-*.json")))
    if not files:
        return None
    with open(files[-1], "r", encoding="utf-8") as f:
        return json.load(f)


def _display(change: dict) -> str:
    """modal 摘要字串：「阿珍 → 阿強：-65 → -50（+15）：阿強幫阿珍解圍」"""
    text = (
        f"{change['from_agent']} → {change['to_agent']}："
        f"{change['old']} → {change['new']}（{change['delta']:+d}）"
    )
    if change.get("reason"):
        text += f"：{change['reason']}"
    return text


@affinity_bp.get("/bands")
def bands():
    return jsonify(
        {
            "bands": [{"min": lo, "max": hi, "label": label} for lo, hi, label in BANDS],
            "default": 0,
            "min": AFFINITY_MIN,
            "max": AFFINITY_MAX,
        }
    )


@affinity_bp.post("/validate")
def validate():
    try:
        payload = SetupAffinityPayload.model_validate(request.get_json(force=True) or {})
    except ValidationError as e:
        return jsonify({"errors": e.errors()}), 400
    try:
        result = validate_setup(payload)
    except SetupValidationError as e:
        return (
            jsonify({"errors": [err.model_dump(by_alias=True) for err in e.errors]}),
            400,
        )
    # 可直接做 config["affinity"]
    return jsonify(result.model_dump())


@affinity_bp.get("/<sim_name>")
def get_affinity(sim_name: str):
    config = _latest_checkpoint(sim_name)
    if config is None:
        return jsonify({"error": f"simulation「{sim_name}」唔存在或無 checkpoint"}), 404
    if "affinity" not in config:
        return jsonify({"affinity": {}, "legacy": True})
    return jsonify(
        {
            "affinity": config["affinity"],
            "step": config.get("step", 0),
            "time": config.get("time", ""),
        }
    )


@affinity_bp.get("/<sim_name>/changes")
def get_changes(sim_name: str):
    config = _latest_checkpoint(sim_name)
    if config is None:
        return jsonify({"error": f"simulation「{sim_name}」唔存在或無 checkpoint"}), 404
    rounds = config.get("affinity_rounds", [])
    round_param = request.args.get("round", type=int)
    record = None
    if round_param is not None:
        for r in rounds:
            if r.get("round") == round_param:
                record = r
                break
        if record is None:
            return jsonify({"error": f"搵唔到第 {round_param} 回合嘅關係變動記錄"}), 404
    elif rounds:
        record = rounds[-1]
    else:
        return jsonify({"round": 0, "changes": [], "display": []})
    changes = record.get("changes", [])
    return jsonify(
        {
            "round": record.get("round", 0),
            "changes": changes,
            "display": [_display(c) for c in changes],
        }
    )
