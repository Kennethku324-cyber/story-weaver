"""story_weaver.gameui.game_server — 遊戲主 server（spec §3，整合版）。

一個 Flask app 服務晒：Setup 頁（setup_bp）、好感度 API（affinity_bp）、
故事回顧 API（recap_bp）、遊戲主介面 + 回合引擎（本檔）。
GM/注入喺進程內（GMDirector），冇 /api/gm/result（spec §3.6 廢除）。

行法：cd generative_agents && ../.venv/bin/python -m story_weaver.gameui.game_server
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

from flask import Flask, jsonify, render_template, request

from story_weaver.affinity.api import affinity_bp
from story_weaver.gm.models import PlayerChoice
from story_weaver.recap.api import configure as configure_recap
from story_weaver.recap.api import recap_bp
from story_weaver.recap.service import RecapService
from story_weaver.routes import setup_bp

from .models import UIStatus
from .llm_settings import load_settings, save_settings, test_llm
from .round_runner import RoundBusyError, RoundRunner

logger = logging.getLogger(__name__)

CONTROL_LEASE_SECONDS = 15

# generative_agents/ 根目錄（template/static 用絕對路徑，唔受 cwd 影響）
GEN_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_runners: dict[str, RoundRunner] = {}
_runners_lock = threading.Lock()
_runner_factory = None  # 測試注入用


def set_runner_factory(factory) -> None:
    """測試用：自訂 RoundRunner 建造（注入 fake server / fake llm）。"""
    global _runner_factory
    _runner_factory = factory


def _checkpoints_root() -> str:
    return os.environ.get("STORY_WEAVER_CHECKPOINTS", "results/checkpoints")


def get_runner(session: str) -> RoundRunner | None:
    """按 session 攞 RoundRunner（唔存在 → 新建；目錄唔存在 → None）。"""
    folder = os.path.join(_checkpoints_root(), session)
    if not os.path.isdir(folder):
        return None
    with _runners_lock:
        if session not in _runners:
            if _runner_factory is not None:
                runner = _runner_factory(session, folder)
            else:
                runner = RoundRunner(session, folder)
            try:
                runner.init_if_needed()
                runner.recover()
            except Exception:
                logger.warning("game_server: %s init/recover 失敗", session, exc_info=True)
            _runners[session] = runner
        return _runners[session]


def reset_runners() -> None:
    """測試用：清空 registry。"""
    with _runners_lock:
        _runners.clear()


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(GEN_ROOT, "frontend", "templates"),
        static_folder=os.path.join(GEN_ROOT, "frontend", "static"),
        static_url_path="/static",
    )
    app.register_blueprint(setup_bp)
    app.register_blueprint(affinity_bp)
    configure_recap(RecapService(checkpoints_root=_checkpoints_root()))
    app.register_blueprint(recap_bp)

    # ---------------------------------------------------------------- 頁面

    @app.get("/")
    def home():
        return (
            "<div style='font-family:sans-serif;max-width:480px;margin:4em auto;text-align:center'>"
            "<h2>Story Weaver</h2>"
            "<p>你係導演，唔係玩家——設定角色，睇住佢哋生活，喺關鍵時刻插手。</p>"
            "<p><a href='/setup' style='display:inline-block;padding:.7em 1.8em;"
            "background:#b3541e;color:#fff;border-radius:999px;text-decoration:none;"
            "font-weight:bold'>開始新故事</a></p>"
            "<p style='margin-top:1em'><a href='/settings' style='color:#888'>⚙ LLM 設定</a></p>"
            "</div>"
        )

    # ---------------------------------------------------------------- LLM 設定

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html")

    @app.get("/api/settings/llm")
    def api_settings_get():
        return jsonify(load_settings())

    @app.post("/api/settings/llm")
    def api_settings_save():
        payload = request.get_json(silent=True) or {}
        errors = save_settings(payload)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
        return jsonify({"ok": True, "message": "已儲存。新故事同新一局會用新設定；行緊嘅故事唔受影響。"})

    @app.post("/api/settings/llm/test")
    def api_settings_test():
        cfg = request.get_json(silent=True) or {}
        ok, message = test_llm(cfg)
        return jsonify({"ok": ok, "message": message})

    @app.get("/game")
    def game():
        name = request.args.get("name", "")
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": f"session 唔存在：results/checkpoints/{name}"}), 404
        state = runner.store.load()
        # 角色素材：由 sim_config 嘅 config_path 派生（故事角色喺 story_agents/，
        # 舊故事可能仲喺 agents/——跟 config_path 就兩邊都啱）
        persona_init_pos: dict = {}
        persona_textures: dict = {}
        try:
            sim_config = runner._load_sim_config()
        except Exception:
            sim_config = {"agents": {}}
        for agent in state.agents:
            under = agent.replace(" ", "_")
            config_path = ((sim_config.get("agents") or {}).get(agent) or {}).get(
                "config_path", os.path.join("assets", "village", "agents", under, "agent.json")
            )
            agent_dir = os.path.dirname(config_path)
            try:
                with open(os.path.join(runner.static_root, config_path), "r", encoding="utf-8") as f:
                    persona_init_pos[under] = json.load(f).get("coord", [0, 0])
            except Exception:
                persona_init_pos[under] = [0, 0]
            persona_textures[under] = {
                "texture": os.path.join(agent_dir, "texture.png").replace(os.sep, "/"),
                "portrait": os.path.join(agent_dir, "portrait.png").replace(os.sep, "/"),
            }
        # 遊戲開始時間 + stride（game_script.html 嘅時鐘用）
        start_datetime = "2024-02-13T09:30:00"
        stride = state.stride
        try:
            sim_config = runner._load_sim_config()
            start_raw = (sim_config.get("time") or {}).get("start", "")
            stride = int(sim_config.get("stride", state.stride))
            if start_raw:
                import datetime as _dt

                start_datetime = _dt.datetime.strptime(start_raw, "%Y%m%d-%H:%M").isoformat()
        except Exception:
            pass
        return render_template(
            "game.html",
            session=name,
            persona_names=[a.replace(" ", "_") for a in state.agents],
            persona_display={a.replace(" ", "_"): a for a in state.agents},
            persona_init_pos=persona_init_pos,
            persona_textures=persona_textures,
            agents_real=state.agents,
            sec_per_step=stride,
            start_datetime=start_datetime,
            zoom=float(request.args.get("zoom", 0.8)),
        )

    # ---------------------------------------------------------------- 輪詢

    @app.get("/api/state")
    def api_state():
        name = request.args.get("name", "")
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": "session 唔存在"}), 404
        since_frame = int(request.args.get("since_frame", 0))
        since_feed = int(request.args.get("since_feed", 0))
        client_id = request.args.get("client_id", "")

        state = runner.store.load()
        sim_time, agents_meta, affinity = runner.latest_agents_meta()
        readonly = _is_readonly(state, client_id)

        pending = None
        if state.status == UIStatus.WAITING_DECISION:
            decision = runner._get_gm().get_pending_decision()
            if decision is not None:
                pending = json.loads(decision.model_dump_json())

        finale = None
        if state.status == UIStatus.FINISHED:
            f = runner._get_gm().state.get_finale()
            if f is not None:
                finale = json.loads(f.model_dump_json())

        return jsonify({
            "status": state.status.value,
            "round": state.round,
            "max_rounds": state.max_rounds,
            "sim_time": sim_time,
            "sim_step_cursor": state.sim_step_cursor,
            "new_frames": runner.frames.frames_since(since_frame),
            "frame_latest": runner.frames.latest_frame_key(),
            "new_feed": [f.model_dump(mode="json") for f in runner.frames.feed_since(since_feed)],
            "feed_latest": runner.frames.feed_latest,
            "agents_meta": agents_meta,
            "pending_decision": pending,
            "affinity": affinity,
            "agents": state.agents,
            "readonly": readonly,
            "finale": finale,
            "error": state.error,
        })

    # ---------------------------------------------------------------- 回合控制

    @app.post("/api/round/start")
    def api_round_start():
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": "session 唔存在"}), 404
        if _is_readonly(runner.store.load(), data.get("client_id", "")):
            return jsonify({"error": "唯讀模式"}), 403
        try:
            round_no = runner.start_round()
        except RoundBusyError as e:
            return jsonify({"accepted": False, "reason": str(e)}), 409
        except Exception as e:
            logger.exception("game_server: start_round 失敗")
            return jsonify({"accepted": False, "reason": f"推演未能開始：{e}"}), 500
        return jsonify({"accepted": True, "round": round_no})

    @app.post("/api/decision")
    def api_decision():
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": "session 唔存在"}), 404
        if _is_readonly(runner.store.load(), data.get("client_id", "")):
            return jsonify({"error": "唯讀模式"}), 403
        state = runner.store.load()

        try:
            choice = _build_player_choice(data, state.agents)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            report, new_status = runner.apply_decision(choice)
        except RoundBusyError as e:
            return jsonify({"error": str(e)}), 409
        body = json.loads(report.model_dump_json())
        body["accepted"] = report.ok
        body["status"] = new_status.value if hasattr(new_status, "value") else new_status
        body["round"] = runner.store.load().round
        return jsonify(body)

    # ---------------------------------------------------------------- 故事數據

    @app.get("/api/timeline")
    def api_timeline():
        name = request.args.get("name", "")
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": "session 唔存在"}), 404
        timeline = runner._get_gm().state.build_story_timeline()
        return jsonify({"timeline": [json.loads(t.model_dump_json()) for t in timeline]})

    @app.post("/api/heartbeat")
    def api_heartbeat():
        data = request.get_json(silent=True) or {}
        name = data.get("name", "")
        client_id = data.get("client_id", "")
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": "session 唔存在"}), 404
        now = time.time()
        with runner.store.mutate() as s:
            if not client_id:
                is_owner = False
            elif s.control_owner in (None, client_id) or s.control_lease_until < now:
                s.control_owner = client_id
                s.control_lease_until = now + CONTROL_LEASE_SECONDS
                is_owner = True
            else:
                is_owner = False
        return jsonify({"is_owner": is_owner, "readonly": not is_owner})

    @app.get("/api/export/story")
    def api_export_story():
        from flask import Response

        name = request.args.get("name", "")
        fmt = request.args.get("format", "md")
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": "session 唔存在"}), 404
        if fmt == "json":
            timeline = runner._get_gm().state.build_story_timeline()
            return Response(
                json.dumps([json.loads(t.model_dump_json()) for t in timeline],
                           ensure_ascii=False, indent=2),
                mimetype="application/json; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{name}-story.json"'},
            )
        recap = runner._get_recap()
        md = recap.export_markdown(name) if recap is not None else None
        if md is None:
            timeline = runner._get_gm().state.build_story_timeline()
            md = "\n\n".join(f"## 第 {t.round} 回合\n\n{t.summary}" for t in timeline)
        return Response(
            md,
            mimetype="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}-story.md"'},
        )

    return app


def _is_readonly(state, client_id: str) -> bool:
    """有控制者且租約未過期、而 client 唔係控制者 → 唯讀。"""
    if not state.control_owner:
        return False
    if state.control_lease_until < time.time():
        return False
    return state.control_owner != client_id


def _build_player_choice(data: dict, agents: list[str]) -> PlayerChoice:
    """request JSON → PlayerChoice（gm-director 嘅模型）。"""
    choice_id = data.get("choice_id")
    custom = data.get("custom_command")
    finish = data.get("finish", False)
    overrides = data.get("affinity_overrides") or []

    if finish:
        return PlayerChoice(type="finish", affinity_overrides=overrides)
    if choice_id and custom:
        text = _custom_text(custom, agents)
        return PlayerChoice(type="option+custom", option_id=choice_id, text=text,
                            affinity_overrides=overrides)
    if choice_id:
        return PlayerChoice(type="option", option_id=choice_id, affinity_overrides=overrides)
    if custom:
        return PlayerChoice(type="custom", text=_custom_text(custom, agents),
                            affinity_overrides=overrides)
    return PlayerChoice(type="skip", affinity_overrides=overrides)


def _custom_text(custom: dict, agents: list[str]) -> str:
    target = (custom.get("target_agent") or "").strip()
    text = (custom.get("text") or "").strip()
    if not text or len(text) > 500:
        raise ValueError("自訂命令必填，≤500 字")
    if target and target not in agents:
        raise ValueError(f"角色「{target}」唔喺本局名單入面")
    return f"【對象：{target}】{text}" if target else text


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=5001, threaded=True)
