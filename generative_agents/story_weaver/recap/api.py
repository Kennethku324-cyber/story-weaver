"""story_weaver.recap.api — Flask Blueprint recap_bp（spec §8）。

掛喺遊戲主 server（game-ui / 回合管理系統嘅 app），url_prefix /api/story。
永遠唔會因 LLM 失敗返 500 —— LLM 失敗只反映喺 status 欄位。
文案全部繁體香港書面語，由後端出。
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from .models import PlayerDecision
from .service import RecapService

logger = logging.getLogger(__name__)

recap_bp = Blueprint("recap", __name__, url_prefix="/api/story")

_service: RecapService | None = None


def configure(service: RecapService) -> None:
    """注入 RecapService（app startup 或測試用）。"""
    global _service
    _service = service


def _svc() -> RecapService:
    if _service is None:
        configure(RecapService())
    return _service


def _ui_hints(recap) -> dict:
    latest = recap.rounds[-1] if recap.rounds else None
    show_fallback = recap.cumulative_recap.status == "fallback" or (
        latest is not None and latest.recap_status == "fallback"
    )
    generating = latest is not None and latest.recap_status == "pending"
    return {
        "fallback_banner": "故事摘要暫時不可用，以下為原始記錄",
        "show_fallback_banner": show_fallback,
        "generating": generating,
        "generating_message": "GM 正在整理故事……",
    }


@recap_bp.get("/<sim_name>/recap")
def get_recap(sim_name: str):
    svc = _svc()
    fmt = request.args.get("format", "json")
    round_arg = request.args.get("round")

    if fmt == "markdown":
        md = svc.export_markdown(sim_name)
        if md is None:
            return jsonify({"error": "story_not_found", "sim_name": sim_name}), 404
        from flask import Response

        return Response(
            md,
            mimetype="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{sim_name}-story.md"'
            },
        )

    round_no = None
    if round_arg is not None:
        try:
            round_no = int(round_arg)
        except ValueError:
            return jsonify({"error": "invalid_round", "round": round_arg}), 400

    recap = svc.get_recap(sim_name)  # 先攞全量做 round 範圍校驗
    if recap is None:
        return jsonify({"error": "story_not_found", "sim_name": sim_name}), 404
    if round_no is not None:
        round_count = len(recap.rounds)
        if round_no < 1 or round_no > max(1, round_count):
            return jsonify(
                {"error": "round_out_of_range", "round": round_no, "round_count": round_count}
            ), 400
        recap = svc.get_recap(sim_name, round_no=round_no)

    body = recap.to_dict()
    body["ui_hints"] = _ui_hints(recap)
    return jsonify(body), 200


@recap_bp.post("/<sim_name>/recap/decision")
def post_decision(sim_name: str):
    svc = _svc()
    data = request.get_json(silent=True) or {}
    round_no = data.get("round")
    dtype = data.get("type")
    text = (data.get("text") or "").strip()
    if not isinstance(round_no, int) or round_no < 1:
        return jsonify({"error": "invalid_decision", "detail": "round 必填，係 ≥1 嘅整數"}), 400
    if dtype not in ("option", "custom"):
        return jsonify({"error": "invalid_decision", "detail": "type 必須係 option 或 custom"}), 400
    if not text or len(text) > 500:
        return jsonify({"error": "invalid_decision", "detail": "text 必填，≤500 字"}), 400
    if not svc._store.exists(sim_name):
        return jsonify({"error": "story_not_found", "sim_name": sim_name}), 404

    from .service import _now_iso

    upserted = svc.record_player_decision(
        sim_name,
        round_no,
        PlayerDecision(type=dtype, text=text, chosen_at=_now_iso()),
    )
    return jsonify({"ok": True, "round": round_no, "upserted": upserted}), 200
