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
from .state_store import GameUIStateStore

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
                runner.prewarm()  # 背景預建引擎，撳掣唔使等
            except Exception:
                logger.warning("game_server: %s init/recover 失敗", session, exc_info=True)
            _runners[session] = runner
        return _runners[session]


def reset_runners() -> None:
    """測試用：清空 registry。"""
    with _runners_lock:
        _runners.clear()


# [story-weaver:deploy] 簡單 rate limiter — 防止 abuse 燒錢
_rate_store: dict[str, list] = {}

def _rate_limit(key: str, max_req: int = 10, window: int = 60) -> bool:
    """return True if rate limited"""
    import time as _time
    now = _time.time()
    times = _rate_store.get(key, [])
    times = [t for t in times if now - t < window]
    if len(times) >= max_req:
        return True
    times.append(now)
    _rate_store[key] = times
    return False


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=os.path.join(GEN_ROOT, "frontend", "templates"),
        static_folder=os.path.join(GEN_ROOT, "frontend", "static"),
        static_url_path="/static",
    )
    # 開發期：模板改咗即時生效，唔使重啟 server
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.register_blueprint(setup_bp)
    app.register_blueprint(affinity_bp)
    configure_recap(RecapService(checkpoints_root=_checkpoints_root()))
    app.register_blueprint(recap_bp)

    # ---------------------------------------------------------------- 頁面

    @app.get("/")
    def home():
        return render_template("home.html")

    # ---------------------------------------------------------------- 故事管理

    @app.get("/api/stories")
    def api_stories():
        """列出全部故事（主頁用）。"""
        root = _checkpoints_root()
        stories = []
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                folder = os.path.join(root, name)
                if not os.path.isdir(folder):
                    continue
                # 只認真故事目錄（有 story.json 或 game_ui_state.json）
                if not (os.path.exists(os.path.join(folder, "story.json"))
                        or os.path.exists(os.path.join(folder, "game_ui_state.json"))):
                    continue
                meta = {}
                try:
                    with open(os.path.join(folder, "story.json"), "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    pass
                entry = {
                    "name": name,
                    "opening": (meta.get("story_opening") or "")[:60],
                    "characters": meta.get("characters") or [],
                    "created_at": meta.get("created_at", ""),
                    "round": None,
                    "status": None,
                }
                try:
                    state = GameUIStateStore(folder).load()
                    entry["round"] = state.round
                    entry["status"] = state.status.value
                except Exception:
                    pass
                stories.append(entry)
        return jsonify({"stories": stories})

    @app.post("/api/story/delete")
    def api_story_delete():
        """刪除故事：checkpoints 目錄 + story_agents 素材 + server 記憶體狀態。"""
        import shutil

        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name or "/" in name or ".." in name:
            return jsonify({"error": "故事名唔啱"}), 400
        folder = os.path.join(_checkpoints_root(), name)
        if not os.path.isdir(folder):
            return jsonify({"error": f"搵唔到故事「{name}」"}), 404
        # 推演中唔准刪
        with _runners_lock:
            runner = _runners.get(name)
        if runner is not None:
            try:
                if runner.store.load().status == UIStatus.SIMULATING:
                    return jsonify({"error": "推演進行中，等佢停先好刪"}), 409
            except Exception:
                pass
        shutil.rmtree(folder, ignore_errors=True)
        story_agents = os.path.join(GEN_ROOT, "frontend", "static", "assets", "village", "story_agents", name)
        shutil.rmtree(story_agents, ignore_errors=True)
        with _runners_lock:
            _runners.pop(name, None)
        logger.info("game_server: 故事「%s」已刪除", name)
        return jsonify({"ok": True})

    @app.post("/api/story/reset")
    def api_story_reset():
        """重玩故事：清空推演進度，保留角色設定同故事開端。"""
        import glob as _glob

        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not name or "/" in name or ".." in name:
            return jsonify({"error": "故事名唔啱"}), 400
        folder = os.path.join(_checkpoints_root(), name)
        if not os.path.isdir(folder):
            return jsonify({"error": f"搵唔到故事「{name}」"}), 404

        # 推演中唔准重設
        with _runners_lock:
            runner = _runners.get(name)
        if runner is not None:
            try:
                if runner.store.load().status == UIStatus.SIMULATING:
                    return jsonify({"error": "推演進行中，等佢停先好重玩"}), 409
            except Exception:
                pass

        # 刪除推演進度檔（保留 story.json 同 sim_config.json）
        for pattern in ["simulate-*.json", "conversation.json",
                        "gm_state.json", "gm_state.json.tmp",
                        "game_ui_state.json", "game_ui_state.json.tmp",
                        "story_recap.json", "story_recap.json.tmp"]:
            for f in _glob.glob(os.path.join(folder, pattern)):
                try:
                    os.remove(f)
                except OSError:
                    pass
        # 成個 storage dir 剷走（llama_index vector index，會自動重建）
        storage_dir = os.path.join(folder, "storage")
        if os.path.isdir(storage_dir):
            import shutil as _shutil
            _shutil.rmtree(storage_dir, ignore_errors=True)

        # 移除 runner（下次訪問會重新 init）
        with _runners_lock:
            _runners.pop(name, None)
        logger.info("game_server: 故事「%s」已重設", name)
        return jsonify({"ok": True})

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
        # [story-weaver:char-profiles] 讀取角色簡介（年齡、性格、背景、關係）
        persona_profiles: dict = {}
        persona_relations: dict = {}
        for agent in state.agents:
            under = agent.replace(" ", "_")
            config_path = ((sim_config.get("agents") or {}).get(agent) or {}).get(
                "config_path", os.path.join("assets", "village", "agents", under, "agent.json")
            )
            try:
                with open(os.path.join(runner.static_root, config_path), "r", encoding="utf-8") as f:
                    agent_cfg = json.load(f)
                scratch = agent_cfg.get("scratch", {})
                profile_parts = []
                age = scratch.get("age", "")
                if age:
                    profile_parts.append(f"{age}歲")
                innate = scratch.get("innate", "")
                if innate:
                    profile_parts.append(innate)
                learned = scratch.get("learned", "")
                if learned:
                    profile_parts.append(learned)
                persona_profiles[under] = "；".join(profile_parts) if profile_parts else ""
                # 角色關係（AgentA 對其他人嘅關係描述）
                rels = agent_cfg.get("relationships", {})
                if rels:
                    rel_lines = []
                    for other, rel in rels.items():
                        if isinstance(rel, dict):
                            desc = rel.get("desc", "")
                            score = rel.get("score", 0)
                            if desc and desc != "陌生人":
                                rel_lines.append(f"{other}：{desc}（{score:+d}）")
                    persona_relations[under] = rel_lines
            except Exception:
                persona_profiles[under] = ""

        return render_template(
            "game.html",
            session=name,
            persona_names=[a.replace(" ", "_") for a in state.agents],
            persona_display={a.replace(" ", "_"): a for a in state.agents},
            persona_init_pos=persona_init_pos,
            persona_textures=persona_textures,
            persona_profiles=persona_profiles,  # [story-weaver:char-profiles]
            persona_relations=persona_relations,  # [story-weaver:char-profiles]
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

        # 即場掃新 checkpoint（角色即時郁）+ 觸發劇情旁白
        feed_before = runner.frames.feed_latest
        runner.poll_scan()
        new_feed_items = runner.frames.feed_since(since_feed)
        new_frames = runner.frames.frames_since(since_frame)
        if new_frames:
            keys = sorted(new_frames.keys(), key=lambda k: int(k) if k.isdigit() else 0)
            logger.info(
                "game_server: poll state=%s — %d new frames (keys %s..%s), %d new feed",
                state.status.value, len(new_frames),
                keys[0] if keys else "-", keys[-1] if keys else "-",
                len(new_feed_items),
            )
        if state.status == UIStatus.SIMULATING:
            runner.maybe_narrate(runner.frames.feed_since(feed_before))

        pending = None
        story_narrative = ""
        if state.status == UIStatus.WAITING_DECISION:
            decision = runner._get_gm().get_pending_decision()
            if decision is not None:
                pending = json.loads(decision.model_dump_json())
            # [story-weaver:continuity] 提供連貫故仔敘事（recap service 生成）
            try:
                recap = runner._get_recap()
                if recap is not None:
                    cr = recap.get_cumulative_text(name)
                    if cr:
                        story_narrative = cr
            except Exception:
                pass

        finale = None
        if state.status == UIStatus.FINISHED:
            f = runner._get_gm().state.get_finale()
            if f is not None:
                finale = json.loads(f.model_dump_json())

        # [story-weaver:story-banner] 持久顯示故事狀態（場景目標 + 壓力 + 上回摘要）
        story_state = {}
        try:
            gm_data = runner._get_gm().state.data
            story_state["scene_goal"] = gm_data.get("next_scene_goal", "") or ""
            story_state["pressure"] = int(gm_data.get("dramatic_pressure", 1))
            # 上回摘要（story_timeline 最尾非 round-0 entry）
            timeline = runner._get_gm().state.build_story_timeline()
            if len(timeline) > 1:
                last_entry = timeline[-1]
                story_state["last_summary"] = last_entry.summary or ""
        except Exception:
            story_state = {}

        return jsonify({
            "status": state.status.value,
            "round": state.round,
            "max_rounds": runner.max_rounds(),
            "sim_time": sim_time,
            "sim_step_cursor": state.sim_step_cursor,
            "new_frames": runner.frames.frames_since(since_frame),
            "frame_latest": runner.frames.latest_frame_key(),
            "new_feed": [f.model_dump(mode="json") for f in new_feed_items],
            "feed_latest": runner.frames.feed_latest,
            "agents_meta": agents_meta,
            "pending_decision": pending,
            "story_narrative": story_narrative,  # [story-weaver:continuity] 連貫故仔
            "affinity": affinity,
            "agents": state.agents,
            "readonly": readonly,
            "finale": finale,
            "story_state": story_state,  # [story-weaver:story-banner]
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
        # [story-weaver:rate-limit] 防止小朋友狂撳：1 秒 cooldown + 60 秒 5 次上限
        if _rate_limit(f"round_cooldown:{name}", max_req=1, window=1):
            return jsonify({"accepted": False, "reason": "撳得太快，等一秒再試"}), 429
        if _rate_limit(f"round_start:{name}", max_req=5, window=60):
            return jsonify({"accepted": False, "reason": "推演太頻密，請等一陣再試"}), 429
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
        # [story-weaver:rate-limit] 防 double-click：1 秒 cooldown
        if _rate_limit(f"decision_cooldown:{name}", max_req=1, window=1):
            return jsonify({"accepted": False, "message": "撳得太快，等一秒再試"}), 429
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
        force = bool(data.get("force", False))
        runner = get_runner(name)
        if runner is None:
            return jsonify({"error": "session 唔存在"}), 404
        now = time.time()
        with runner.store.mutate() as s:
            if not client_id:
                is_owner = False
            elif force or s.control_owner in (None, client_id) or s.control_lease_until < now:
                s.control_owner = client_id
                s.control_lease_until = now + CONTROL_LEASE_SECONDS
                is_owner = True
            else:
                is_owner = False
        return jsonify({"is_owner": is_owner, "readonly": not is_owner})

    @app.get("/api/export/story")
    def api_export_story():
        from flask import Response
        import re as _re

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
        # 內容：優先 recap，fallback timeline
        recap = runner._get_recap()
        md = recap.export_markdown(name) if recap is not None else None
        if md is None:
            timeline = runner._get_gm().state.build_story_timeline()
            md = "\n\n".join(f"## 第 {t.round} 回合\n\n{t.summary}" for t in timeline)
        if fmt == "txt":
            # 剝走 markdown 格式 → 純文字
            txt = _re.sub(r"^#{1,6}\s+", "", md, flags=_re.MULTILINE)  # headings
            txt = _re.sub(r"\*\*(.+?)\*\*", r"\1", txt)                 # bold
            txt = _re.sub(r"\*(.+?)\*", r"\1", txt)                     # italic
            txt = _re.sub(r"\[(.+?)\]\(.+?\)", r"\1", txt)              # links
            txt = _re.sub(r"^[-*]\s+", "· ", txt, flags=_re.MULTILINE)  # bullets
            return Response(
                txt,
                mimetype="text/plain; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{name}-故事.txt"'},
            )
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
    _timing_handler = logging.FileHandler(
        os.path.join(GEN_ROOT, "results", "timing.log"), encoding="utf-8"
    )
    _timing_handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    for _name in ("llm_timing", "sim_timing"):
        _tl = logging.getLogger(_name)
        _tl.setLevel(logging.INFO)
        _tl.addHandler(_timing_handler)
        _tl.propagate = False
    app.run(host="127.0.0.1", port=5001, threaded=True)
