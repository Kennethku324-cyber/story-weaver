"""story_weaver.gameui.round_runner — RoundRunner：回合引擎（spec §2.4，整合版）。

同 spec 嘅差異（drift 決策）：GM 同注入唔係外部系統——直接調
GMDirector（gm-director）同 RecapService（story-recap），冇 contracts.py 雙軌。
pending 決策由 gm_state.json 持有；本類只管狀態機 + 線程 + 全局鎖。

全局單線程推演：GenerativeAgentsMap 係全局單例（modules/game.py），
一個進程同一時間只可以有一個 simulate 線程（class-level lock）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading

from pydantic import BaseModel

from story_weaver.gm import GMDirector, load_gm_config
from story_weaver.gm.models import PlayerChoice
from story_weaver.recap import RecapService

from .incremental import FrameBuffer, load_maze
from .models import UIStatus
from .state_store import GameUIStateStore, create_initial_state

logger = logging.getLogger(__name__)

SIMULATE_FILE_RE = re.compile(r"^simulate-\d{8}-\d{4}\.json$")  # simulate-YYYYMMDD-HHMM.json（start.py 寫檔格式）


class RoundBusyError(RuntimeError):
    """已有推演線程 / status 唔啱（route 轉 409）。"""


class RecoveryInfo(BaseModel):
    recovered: bool = False
    last_complete_step: int = 0
    skipped_files: list[str] = []
    message: str = ""


def scan_checkpoints(checkpoints_folder: str) -> tuple[int, list[str], dict | None]:
    """容忍爛檔嘅 checkpoint 掃描。返 (last_complete_step, skipped_files, latest_config)。"""
    last_step = 0
    skipped: list[str] = []
    latest_config: dict | None = None
    try:
        files = sorted(f for f in os.listdir(checkpoints_folder) if SIMULATE_FILE_RE.match(f))
    except OSError:
        return 0, [], None
    for file_name in files:
        try:
            with open(os.path.join(checkpoints_folder, file_name), "r", encoding="utf-8") as f:
                data = json.load(f)
            step = int(data.get("step", 0))
        except Exception:
            skipped.append(file_name)
            continue
        if step >= last_step:
            last_step = step
            latest_config = data
    return last_step, skipped, latest_config


class RoundRunner:
    """包裝 SimulateServer + GMDirector + RecapService + FrameBuffer。"""

    _global_sim_lock = threading.Lock()  # process-wide：GenerativeAgentsMap 係全局單例

    def __init__(
        self,
        session: str,
        checkpoints_folder: str,
        static_root: str = "frontend/static",
        gm: GMDirector | None = None,
        recap: RecapService | None = None,
        maze=None,
        server_factory=None,
    ) -> None:
        self.session = session
        self.folder = checkpoints_folder
        self.static_root = static_root
        self.store = GameUIStateStore(checkpoints_folder)
        self.maze = maze if maze is not None else load_maze(static_root)
        self.frames = FrameBuffer(checkpoints_folder, self.maze)
        self._gm = gm
        self._recap = recap
        self._server = None
        self._server_lock = threading.Lock()
        self._server_factory = server_factory  # 測試注入用
        self._thread: threading.Thread | None = None
        self._narrator = None
        self._narrating = False  # 同一時間最多一個旁白 thread

    # ---------------------------------------------------------------- 懶加載

    def _load_story_meta(self) -> dict:
        try:
            with open(os.path.join(self.folder, "story.json"), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _load_sim_config(self) -> dict:
        with open(os.path.join(self.folder, "sim_config.json"), "r", encoding="utf-8") as f:
            return json.load(f)

    def init_if_needed(self) -> None:
        """首次見到 session：建 game_ui_state + gm_state + story_recap（全部冪等）。"""
        meta = self._load_story_meta()
        agents = meta.get("characters") or list((self._load_sim_config().get("agents") or {}).keys())
        state = create_initial_state(self.folder, self.session, agents)
        opening = meta.get("story_opening", "")
        gm = self._get_gm()
        if opening:
            gm.state.init_new(opening, agents)
            if self._get_recap() is not None:
                from story_weaver.recap.models import AgentProfile

                profiles = [AgentProfile(name=n) for n in agents]
                try:
                    self._get_recap().init_story(self.session, opening, profiles)
                except Exception:
                    logger.warning("round_runner: recap init 失敗", exc_info=True)
        if not state.agents and agents:
            with self.store.mutate() as s:
                s.agents = agents

    def _get_gm(self) -> GMDirector:
        if self._gm is None:
            gm_config = load_gm_config()
            self._gm = GMDirector.resume(
                sim_name=self.session,
                checkpoints_folder=self.folder,
                llm_config=gm_config.get("llm", {}),
                gm_config=gm_config,
            )
        return self._gm

    def _get_narrator(self):
        """即時劇情旁白（用 GM 嘅 LLM 配置，獨立實例）。"""
        if self._narrator is None:
            try:
                from modules.model.llm_model import create_llm_model

                from .narrator import StepNarrator

                llm = create_llm_model(load_gm_config().get("llm", {}))
                self._narrator = StepNarrator(llm)
            except Exception:
                logger.warning("round_runner: narrator 初始化失敗，旁白停用", exc_info=True)
                from .narrator import StepNarrator

                self._narrator = StepNarrator(None)
        return self._narrator

    def steps_per_round(self) -> int:
        """每回合步數：gm_config.json 優先（即改即生效），fallback 返 state 預設。"""
        try:
            cfg = load_gm_config()
            value = int(cfg.get("steps_per_round", 0))
            if 1 <= value <= 20:
                return value
        except Exception:
            pass
        return self.store.load().steps_per_round

    def max_rounds(self) -> int:
        """總回合數：gm_config.json 優先（即改即生效），fallback 返 state 預設。"""
        try:
            cfg = load_gm_config()
            value = int(cfg.get("max_rounds", 0))
            if 2 <= value <= 50:
                return value
        except Exception:
            pass
        return self.store.load().max_rounds

    def _get_recap(self) -> RecapService | None:
        if self._recap is None:
            try:
                self._recap = RecapService(
                    checkpoints_root=os.path.dirname(self.folder.rstrip("/")) or ".",
                )
            except Exception:
                logger.warning("round_runner: RecapService 初始化失敗", exc_info=True)
        return self._recap

    # ---------------------------------------------------------------- 恢復

    def recover(self) -> RecoveryInfo:
        """server 啟動 / 首次訪問時調：掃 checkpoint、回滾死喺中途嘅狀態、重建 FrameBuffer。"""
        info = RecoveryInfo()
        last_step, skipped, _config = scan_checkpoints(self.folder)
        info.last_complete_step = last_step
        info.skipped_files = skipped
        try:
            state = self.store.load()
        except Exception:
            return info
        if state.status == UIStatus.SIMULATING:
            # 上次進程死喺推演中途：回滾
            with self.store.mutate() as s:
                s.status = UIStatus.IDLE
                s.sim_step_cursor = last_step
                s.error = f"已恢復至第 {s.round + 1} 回合（{last_step} 步），部分進度遺失"
            info.recovered = True
            info.message = f"已恢復至第 {state.round + 1} 回合（{last_step} 步），部分進度遺失"
        # 重建 FrameBuffer（全量掃一次；memory-only）
        self.frames.scan(processed_steps=[])
        if skipped:
            logger.warning("round_runner: 跳過損毀 checkpoint：%s", skipped)
        return info

    # ---------------------------------------------------------------- 推演

    def _build_server(self):
        if self._server_factory is not None:
            return self._server_factory(self)
        from start import SimulateServer, get_config_from_log

        last_step, _, latest = scan_checkpoints(self.folder)
        if latest is not None:
            config = get_config_from_log(self.folder)
            start_step = config["step"]
        else:
            config = self._load_sim_config()
            start_step = 0
        return SimulateServer(
            self.session, self.static_root, self.folder, config,
            start_step=start_step, verbose="warn",
        )

    def _get_server(self):
        # 鎖住：prewarm 同 start_round 可能同時建 server（GenerativeAgentsMap 係全局單例）
        with self._server_lock:
            if self._server is None:
                self._server = self._build_server()
        return self._server

    def prewarm(self) -> None:
        """背景預建 SimulateServer：等玩家撳掣時唔使即場等重建（新舊故事都做）。"""
        if self._server is not None or self._server_factory is not None:
            return
        try:
            threading.Thread(target=self._prewarm_run, daemon=True).start()
        except Exception:
            logger.warning("round_runner: prewarm 失敗", exc_info=True)

    def _prewarm_run(self) -> None:
        try:
            self._get_server()
            logger.info("round_runner: %s server 預熱完成", self.session)
        except Exception:
            logger.warning("round_runner: %s server 預熱失敗", self.session, exc_info=True)

    def start_round(self) -> int:
        """開下一回合。status 唔啱 / 已有推演 → RoundBusyError。返回回合編號。
        server 建造同 GM 準備全部喺背景線程做，API 即刻返（前端即見「推演中」）。"""
        state = self.store.load()
        if state.status == UIStatus.FINISHED:
            raise RoundBusyError("故事已完結")
        if state.status == UIStatus.WAITING_DECISION:
            raise RoundBusyError("等待玩家決策")
        if state.status == UIStatus.SIMULATING:
            raise RoundBusyError("推演進行中")
        if not RoundRunner._global_sim_lock.acquire(blocking=False):
            raise RoundBusyError("推演進行中")

        round_no = state.round + 1
        try:
            with self.store.mutate() as s:
                s.status = UIStatus.SIMULATING
                s.error = None
            self._thread = threading.Thread(
                target=self._run_round, args=(round_no,), daemon=True
            )
            self._thread.start()
            return round_no
        except Exception:
            RoundRunner._global_sim_lock.release()
            raise

    def _run_round(self, round_no: int) -> None:
        try:
            server = self._get_server()
            gm = self._get_gm()
            gm.on_round_start(server)
            steps = self.steps_per_round()
            state = self.store.load()
            server.simulate(steps, state.stride)
            self._on_round_complete(server, round_no, steps)
        except Exception as e:
            logger.exception("round_runner: 回合 %d 推演失敗", round_no)
            try:
                with self.store.mutate() as s:
                    s.status = UIStatus.ERROR
                    s.error = f"推演失敗：{e}"
            except Exception:
                pass
        finally:
            RoundRunner._global_sim_lock.release()

    def _on_round_complete(self, server, round_no: int, steps: int | None = None) -> None:
        state = self.store.load()
        steps = steps or state.steps_per_round
        # 1. 壓縮新 checkpoint → frame + feed
        self.frames.scan(state.processed_steps)
        # 2. story-recap 提取（背景生成敘事）
        recap = self._get_recap()
        if recap is not None:
            prev_cursor = state.sim_step_cursor
            try:
                recap.on_round_end(
                    self.session,
                    round_no,
                    (prev_cursor + 1, prev_cursor + steps),
                    background=True,
                )
            except Exception:
                logger.warning("round_runner: recap on_round_end 失敗", exc_info=True)
        # 3. GM 分析 → pending 決策（持久化喺 gm_state）
        gm = self._get_gm()
        gm.on_round_end(server, round_no)
        # 4. 狀態推進
        with self.store.mutate() as s:
            s.sim_step_cursor += steps
            s.processed_steps = list(
                set(s.processed_steps) | set(range(s.sim_step_cursor - steps + 1,
                                                   s.sim_step_cursor + 1))
            )
            s.status = UIStatus.WAITING_DECISION

    # ---------------------------------------------------------------- 旁白

    def poll_scan(self) -> None:
        """輪詢時即場掃新 checkpoint（角色即時郁，唔使等回合完）。"""
        try:
            self.frames.scan(self.store.load().processed_steps)
        except Exception:
            logger.warning("round_runner: poll scan 失敗", exc_info=True)

    def maybe_narrate(self, new_feed: list) -> None:
        """有新事件/對話 → 背景 thread 寫一句劇情旁白入 feed（同一時間最多一個）。"""
        items = [f for f in new_feed if getattr(f, "kind", None) and f.kind.value in ("event", "chat")]
        if not items or self._narrating:
            return
        events = [f"{f.actor}喺{f.location}：{f.text}" for f in items if f.kind.value == "event"]
        dialogues = [f"{d.speaker}：「{d.line}」" for f in items if f.kind.value == "chat" for d in f.dialogue]
        sim_time = items[-1].sim_time

        def _run():
            self._narrating = True
            try:
                text = self._get_narrator().narrate(sim_time, events[-12:], dialogues[-12:])
                if text:
                    self.frames.add_narrative_feed(text, sim_time)
            finally:
                self._narrating = False

        threading.Thread(target=_run, daemon=True).start()

    # ---------------------------------------------------------------- 決策

    def apply_decision(self, choice: PlayerChoice):
        """玩家確認決策。返 (InjectionReport, new_status)。"""
        state = self.store.load()
        if state.status != UIStatus.WAITING_DECISION:
            raise RoundBusyError("而家唔係決策時間")
        round_no = state.round + 1
        server = self._get_server()
        gm = self._get_gm()

        report = gm.apply_player_choice(server, round_no, choice)
        if not report.ok:
            return report, state.status  # 邊界 6：拒絕唔消耗回合

        # 好感度變動 → 系統 feed
        for change in report.affinity_changes:
            self.frames.add_system_feed(
                f"好感度變化：{change.from_agent} 對 {change.to_agent} "
                f"{change.delta:+d}（{change.reason}）"
            )
        # 玩家決定 → recap 記錄（驅動下一回合）
        decision_text = choice.text or ""
        if not decision_text and choice.option_id:
            pending = gm.get_pending_decision()
            # pending 已清；由 report.injected 攞內容
            if report.injected:
                decision_text = report.injected[0].content
        recap = self._get_recap()
        if recap is not None and decision_text:
            try:
                from story_weaver.recap.models import PlayerDecision
                from story_weaver.recap.service import _now_iso

                recap.record_player_decision(
                    self.session,
                    round_no + 1,
                    PlayerDecision(
                        type="custom" if choice.type == "custom" else "option",
                        text=decision_text,
                        chosen_at=_now_iso(),
                    ),
                )
            except Exception:
                logger.warning("round_runner: recap 記錄決策失敗", exc_info=True)

        finished = choice.type == "finish" or (round_no >= self.max_rounds())
        with self.store.mutate() as s:
            s.round = round_no
            s.status = UIStatus.FINISHED if finished else UIStatus.IDLE
            s.error = None
        if finished:
            try:
                gm.generate_finale(server)
            except Exception:
                logger.warning("round_runner: finale 生成失敗", exc_info=True)
        return report, UIStatus.FINISHED if finished else UIStatus.IDLE

    # ---------------------------------------------------------------- 查詢

    def status(self) -> UIStatus:
        return self.store.load().status

    def latest_agents_meta(self) -> tuple[str, dict, dict]:
        """由最新完整 checkpoint 讀 sim_time / agents_meta / affinity。"""
        _step, _skipped, config = scan_checkpoints(self.folder)
        if config is None:
            try:
                config = self._load_sim_config()
            except Exception:
                config = {}
        sim_time = config.get("time", "")
        if isinstance(sim_time, dict):
            sim_time = sim_time.get("start", "")
        agents_meta = {}
        for name, data in (config.get("agents") or {}).items():
            event = ((data or {}).get("action") or {}).get("event") or {}
            agents_meta[name] = {
                "currently": (data or {}).get("currently", ""),
                "action": event.get("describe", ""),
                "location": "，".join((event.get("address") or [])[1:]),
            }
        affinity = config.get("affinity", {})
        return sim_time, agents_meta, affinity
