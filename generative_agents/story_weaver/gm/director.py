"""story_weaver.gm.director — GMDirector：敘事總監（spec §2.1）。

GM 唔係 Agent，唔改 simulate()，唔碰 agent 嘅 action/schedule。
佢係獨立 LLM 客戶端 + 一組狀態存儲，透過「記憶注入」同「好感度」兩個槓桿影響 agents。
所有公開方法都唔會拋異常出回合流程（內部 failsafe + error log）。
"""

from __future__ import annotations

import json
import logging
import os

from modules import memory, utils
from modules.model.llm_model import create_llm_model
from story_weaver.affinity.gm import GMAdjustmentItem, GMAdjustmentResponse, apply_gm_response
from story_weaver.affinity.store import AffinityStore
from story_weaver.paths import GM_CONFIG_PATH, GM_PROMPTS_ROOT

from .delta import RoundBaseline, RoundDeltaCollector
from .injector import INJECT_OBJECT, PREDICATE_CUSTOM, PREDICATE_OPTION, MemoryInjector
from .models import (
    AffinityChange,
    CustomCommandParse,
    DialogueBlock,
    Finale,
    FinaleNarrative,
    GMDecision,
    GMOption,
    InjectionRecord,
    InjectionReport,
    PlayerChoice,
    TimelineEntry,
)
from .prompter import GMPrompter
from .relations import apply_relations_prefix
from .state import GMStateStore

logger = logging.getLogger(__name__)

_DEFAULT_GM_CONFIG = {
    "option_poignancy": 8,
    "custom_poignancy": 10,
    "poignancy_boost": 20,
    "max_rounds": 10,
    "min_rounds_to_finish": 2,
}

QUIET_SUMMARY = "平靜的一日，小鎮沒有特別的事情發生。"
FAILSAFE_SUMMARY = "命運之線暫時模糊……本回合的敘事未能織成，但小鎮的生活仍在繼續。"


def _safe_option_event_description(title: str, predicted: str) -> str:
    """將選項轉成可放進 agent event memory 嘅文字。"""
    describe = f"{title}。{predicted}"
    return (
        describe.replace("對話", "交談")
        .replace("睡覺", "休息")
        .replace("空閒", "留有時間")
        .replace("待開始", "即將展開")
    )


def _dialogue_blocks(conversations: dict) -> list[DialogueBlock]:
    """conversation 增量 → DialogueBlock 列表，對白逐字保留原文。"""
    blocks: list[DialogueBlock] = []
    for _time_key, convo in (conversations or {}).items():
        for block in convo:
            for header, chats in block.items():
                speakers, _, address = header.partition(" @ ")
                blocks.append(
                    DialogueBlock(
                        speakers=speakers,
                        address=address,
                        lines=[(n, t) for n, t in chats],
                    )
                )
    return blocks


def _format_agent_inner(server, agent_names: list[str]) -> str:
    """[story-weaver:inner-world] 收集每個 agent 嘅內心狀態同當前動作。
    即使冇外部事件/對話，GM 都可以由角色嘅內心世界推斷劇情。"""
    lines = []
    for name in agent_names:
        agent = server.game.agents.get(name)
        if agent is None:
            continue
        try:
            currently = getattr(agent.scratch, "currently", "") or ""
            action = ""
            try:
                action = agent.action.get_event().get_describe() or ""
            except Exception:
                pass
            if currently or action:
                lines.append(f"【{name}】")
                if currently:
                    lines.append(f"  內心：{currently}")
                if action:
                    lines.append(f"  正在：{action}")
        except Exception:
            pass
    return "\n".join(lines) or "（無法讀取角色狀態）"


def _key_events(events_delta: list[dict], limit: int = 5) -> list[str]:
    """按 poignancy 取頭幾條事件 describe 做 key_events。"""
    ordered = sorted(events_delta, key=lambda e: e.get("poignancy", 0), reverse=True)
    return [e.get("describe", "") for e in ordered[:limit] if e.get("describe")]


def _build_recent_history(timeline: list, max_entries: int = 3) -> str:
    """[story-weaver:continuity] 由 story_timeline 砌返最近 N 個回合嘅背景，
    包括摘要同玩家選擇，等 GM 可以保持故事連貫。"""
    story_seed = next(
        (e for e in timeline if getattr(e, "round", 0) == 0 and getattr(e, "summary", "")),
        None,
    )
    entries = [e for e in timeline if getattr(e, "round", 0) > 0]
    recent = entries[-max_entries:] if len(entries) > max_entries else entries
    if not recent and not story_seed:
        return "（故事剛剛開始，未有過往回合）"
    lines = []
    if story_seed:
        lines.append(f"【故事開端】{story_seed.summary}")
    for e in recent:
        lines.append(f"第{e.round}回合摘要：{e.summary}")
        if e.player_choice:
            choice_type = e.player_choice.type
            choice_text = e.player_choice.text or ""
            if not choice_text and e.player_choice.option_id:
                choice_text = f"揀咗選項 {e.player_choice.option_id}"
            if choice_text:
                lines.append(f"  → 玩家決定（{choice_type}）：{choice_text}")
    return "\n".join(lines)


class GMDirector:
    """敘事總監。非 Agent，獨立 LLM 客戶端。所有方法均不會拋異常出回合流程（內部 failsafe）。"""

    def __init__(
        self,
        sim_name: str,
        checkpoints_folder: str,
        llm_config: dict,
        agent_names: list[str],
        prompts_dir: str | None = None,
        gm_config: dict | None = None,
        llm=None,
        state: GMStateStore | None = None,
    ) -> None:
        self.sim_name = sim_name
        self.checkpoints_folder = checkpoints_folder
        self.agent_names = list(agent_names)
        self._gm_config = dict(_DEFAULT_GM_CONFIG)
        if gm_config:
            self._gm_config.update({k: v for k, v in gm_config.items() if k != "llm"})
        self.state = state or GMStateStore.load(
            os.path.join(checkpoints_folder, "gm_state.json")
        )
        if llm is not None:
            self._llm = llm
        else:
            try:
                self._llm = create_llm_model(llm_config)
            except Exception:
                logger.warning("gm: LLM 初始化失敗，全部 LLM 功能行 failsafe", exc_info=True)
                self._llm = None
        self._prompter = GMPrompter(self._llm, prompts_dir or str(GM_PROMPTS_ROOT)) if self._llm else None
        self._injector = MemoryInjector()
        self._collector = RoundDeltaCollector()

    # ---------------------------------------------------------------- 內部工具

    def _store(self, server) -> AffinityStore:
        """包住 server config 嘅 affinity 引用（in-place，隨 checkpoint 持久化）。"""
        data = server.config.setdefault("affinity", {})
        return AffinityStore(data, self.agent_names)

    def _log_error(self, round_no: int, stage: str, error) -> None:
        try:
            at = ""
            try:
                from modules import utils

                at = utils.get_timer().get_date("%Y%m%d-%H:%M")
            except Exception:
                pass
            self.state.log_error(round_no, stage, str(error), at)
        except Exception:
            logger.warning("gm: error log 寫入失敗", exc_info=True)

    # ---------------------------------------------------------------- 回合生命週期

    def on_round_start(self, server) -> None:
        """拍增量基線快照 + 注入好感度 currently 前綴 + 主題錨定 + 場景指導落地。"""
        try:
            baseline = self._collector.snapshot(server)
            self.state.set_round_baseline(baseline)
        except Exception as e:
            logger.warning("gm: round baseline 快照失敗", exc_info=True)
            self._log_error(0, "round_start_snapshot", e)
        try:
            store = self._store(server)
            prefixes = apply_relations_prefix(
                store, server.game.agents, self.state.data.get("last_relations_prefix")
            )
            self.state.data["last_relations_prefix"] = prefixes
            self.state.save()
        except Exception as e:
            logger.warning("gm: 好感度前綴注入失敗", exc_info=True)
            self._log_error(0, "round_start_prefix", e)

        # [story-weaver:scene-direction] 讀取上回合 GM 規劃嘅場景指導
        scene_goal = (self.state.data.get("next_scene_goal") or "").strip()
        forced_meetings = self.state.data.get("next_forced_meetings") or []
        pressure = int(self.state.data.get("dramatic_pressure", 1))

        # [story-weaver:always-meet] 每個回合都自動配對角色見面
        if not forced_meetings:
            try:
                forced_meetings = self._pick_conflict_pair(server)
                if forced_meetings:
                    logger.info(
                        "gm: 壓力 %d 自動配對 → %s", pressure, forced_meetings
                    )
            except Exception:
                pass

        # [story-weaver:day-boundary] 偵測跨日：agent schedule 會被 regenerate，
        # 必須更 aggressive 咁 re-anchor 場景指導同故事主題
        try:
            current_date = str(server.config.get("time", ""))[:8]  # YYYYMMDD
            last_date = (self.state.data.get("last_round_date") or "")[:8]
            is_new_day = current_date and last_date and current_date != last_date
        except Exception:
            is_new_day = False

        # 落地場景指導（theme anchor + scene enforcement + agent goals 三合一）
        try:
            story_seed = (self.state.data.get("story_seed") or "").strip()
            agent_goals = self.state.data.get("agent_goals") or {}
            agents = server.game.agents
            for name, agent in agents.items():
                try:
                    current = agent.scratch.currently
                    # 故事主題
                    if story_seed and story_seed[:20] not in current:
                        current = f"【故事主題】{story_seed}。{current}"
                    # 場景指導
                    if scene_goal and scene_goal[:20] not in current:
                        current = f"【本回合】{scene_goal}。{current}"
                    # 跨日 re-anchor：更 aggressive wording
                    if is_new_day:
                        current = (
                            f"【命運延續】新的一天開始，但故事仍在繼續——"
                            f"{scene_goal or story_seed}。{current}"
                        )
                    # [story-weaver:persistent-goals] 每回合 re-inject agent goal
                    goal = agent_goals.get(name, "")
                    if goal and goal[:20] not in current:
                        current = f"【個人目標】{goal}。{current}"
                    agent.scratch.currently = current
                except Exception:
                    pass
        except Exception as e:
            logger.warning("gm: 主題/場景錨定失敗", exc_info=True)
            self._log_error(0, "theme_anchor", e)

        # [story-weaver:pre-move] 由 gm_state 恢復 between-rounds 嘅 pre-move 位置，
        # 等 FrameBuffer 可以生成行路動畫
        saved_pre = self.state.data.get("_pre_move_positions")
        if saved_pre:
            server.config["_pre_move_positions"] = saved_pre
            self.state.data.pop("_pre_move_positions", None)
            self.state.save()

        # 落地 forced meetings：teleport pairs 到同一地點
        if forced_meetings:
            try:
                self._apply_scene_direction(server, scene_goal, forced_meetings)
            except Exception as e:
                logger.warning("gm: 場景指導落地失敗", exc_info=True)
                self._log_error(0, "scene_direction_apply", e)

        # 清走已使用嘅場景指導，下一回合由新 analysis 覆蓋
        self.state.data["next_scene_goal"] = ""
        self.state.data["next_forced_meetings"] = []
        self.state.save()

    # ---------------------------------------------------------------- 場景指導 helpers

    def _apply_scene_direction(
        self, server, scene_goal: str, forced_meetings: list[list[str]]
    ) -> None:
        """[story-weaver:scene-direction] 將場景指導落地：強制角色見面。
        對每對 forced_meetings，將兩個 agent 移去同一地點，改寫 schedule + currently。"""
        agents = server.game.agents
        meeting_coords = self._find_meeting_locations(forced_meetings, agents)

        # [story-weaver:pre-move] 記錄 teleport 前嘅位置，俾 FrameBuffer 可以生成行路動畫
        pre_move_positions = {}
        for pair in forced_meetings:
            for name in pair:
                if name not in pre_move_positions and name in agents:
                    pre_move_positions[name] = list(agents[name].coord)

        for pair in forced_meetings:
            if len(pair) < 2:
                continue
            name_a, name_b = pair[0], pair[1]
            agent_a = agents.get(name_a)
            agent_b = agents.get(name_b)
            if agent_a is None or agent_b is None:
                continue

            coord = meeting_coords.get(tuple(sorted([name_a, name_b])))
            if coord is None:
                continue

            try:
                agent_a.move(list(coord))
                agent_b.move(list(coord))
                logger.info("gm: 場景指導 — %s ↔ %s 強制見面 @ %s", name_a, name_b, coord)

                # [story-weaver:chat-steer] 注入「思想」concept — 確保見面之後嘅對話
                # 圍繞場景主題而唔係日常傾閒偈
                chat_topic = scene_goal or f"{name_a} 同 {name_b} 必須見面交談"
                for agent, other_name in [(agent_a, name_b), (agent_b, name_a)]:
                    if not agent.is_awake():
                        continue
                    try:
                        from modules import memory as mem_module
                        thought_event = mem_module.Event(
                            agent.name,
                            "思考",
                            f"即將與{other_name}見面",
                            describe=(
                                f"{agent.name} 反覆思考：即將同 {other_name} 見面——"
                                f"{chat_topic}。呢次見面好重要，必須認真對待。"
                            ),
                            address=agent.get_tile().get_address(),
                        )
                        agent._add_concept("thought", thought_event, poignancy_override=12)
                    except Exception:
                        pass

                # 改寫 currently
                for agent, other_name in [(agent_a, name_b), (agent_b, name_a)]:
                    try:
                        if scene_goal and scene_goal[:20] not in (agent.scratch.currently or ""):
                            agent.scratch.currently = (
                                f"【命運相遇】你即將見到 {other_name}。{scene_goal}。"
                                f"{agent.scratch.currently}"
                            )
                    except Exception:
                        pass

                # 改寫 schedule（強制中斷當前 routine + overwrite plan block）
                for agent in [agent_a, agent_b]:
                    if not agent.is_awake():
                        continue
                    try:
                        from modules import memory as mem_module, utils

                        meet_desc = f"{agent.name} 去見另一個人——呢個見面將會改變一切。"
                        meet_event = mem_module.Event(
                            agent.name,
                            "被命運引導",
                            "命運的提示",
                            describe=meet_desc,
                            address=agent.get_tile().get_address(),
                        )
                        plan, de_plan = agent.schedule.current_plan()
                        start = utils.get_timer().daily_time(de_plan["start"])
                        duration = de_plan["duration"]
                        agent.revise_schedule(meet_event, start, duration)
                        # [story-weaver:plan-overwrite] 直接改寫 plan block describe，
                        # 令 agent 嘅成個日程單元都指向戲劇行動，唔係日常 routine
                        try:
                            plan["describe"] = meet_desc
                        except Exception:
                            pass
                    except Exception:
                        logger.warning(
                            "gm: %s schedule revise 失敗（場景指導）", agent.name, exc_info=True
                        )
            except Exception:
                logger.warning(
                    "gm: 場景指導落地失敗 pair=%s/%s", name_a, name_b, exc_info=True
                )

        if hasattr(server, "sync_agent_positions"):
            server.sync_agent_positions()
        # [story-weaver:pre-move] 儲存 teleport 前位置（server.config → checkpoint；
        # gm_state → survive server rebuild）
        if pre_move_positions:
            server.config["_pre_move_positions"] = pre_move_positions
            self.state.data["_pre_move_positions"] = pre_move_positions
            self.state.save()

    def _find_meeting_locations(
        self, pairs: list[list[str]], agents: dict
    ) -> dict[tuple, list]:
        """為每對角色搵一個共同見面地點（優先揀是但一方嘅當前位置）。"""
        coords: dict[tuple, list] = {}
        for pair in pairs:
            if len(pair) < 2:
                continue
            key = tuple(sorted([pair[0], pair[1]]))
            if key in coords:
                continue
            a = agents.get(pair[0])
            b = agents.get(pair[1])
            coord = None
            if a is not None and a.is_awake():
                coord = list(a.coord) if a.coord else None
            if coord is None and b is not None and b.is_awake():
                coord = list(b.coord) if b.coord else None
            if coord is None:
                coord = [94, 21]  # fallback：小鎮中心
            coords[key] = coord
        return coords

    def _pick_conflict_pair(self, server) -> list[list[str]]:
        """[story-weaver:scene-direction] 由 affinity matrix 揀最極端值嘅 pair。
        優先揀最憎（最低值），其次最愛（最高值），冇極端值就 random 兩個。"""
        agents = server.game.agents
        names = list(agents.keys())
        if len(names) < 2:
            return []

        # 讀 affinity matrix
        pairs: list[tuple[str, str, int]] = []  # [(from, to, value), ...]
        try:
            config = getattr(server, "config", {}) or {}
            affinity = config.get("affinity", {})
            for from_name, targets in affinity.items():
                if not isinstance(targets, dict):
                    continue
                for to_name, info in targets.items():
                    if not isinstance(info, dict):
                        continue
                    value = info.get("value", 0)
                    if from_name != to_name and from_name in names and to_name in names:
                        pairs.append((from_name, to_name, value))
        except Exception:
            pass

        if not pairs:
            # 冇 affinity data：random 兩個
            import random
            chosen = random.sample(names, 2)
            return [[chosen[0], chosen[1]]]

        # 揀最極端（abs 最大）
        pairs.sort(key=lambda x: abs(x[2]), reverse=True)
        best = pairs[0]
        return [[best[0], best[1]]]

    def on_round_end(self, server, round_no: int) -> GMDecision:
        """採增量 → 靜默判斷 → LLM 分析 → 持久化 pending → 回傳 GMDecision。"""
        store = self._store(server)
        # 1. 採增量
        try:
            raw_baseline = self.state.get_round_baseline()
            if raw_baseline:
                baseline = RoundBaseline(**raw_baseline)
            else:
                baseline = self._collector.snapshot(server)  # 斷線重建：當無增量
            delta = self._collector.collect(server, baseline)
        except Exception as e:
            logger.warning("gm: 回合增量採集失敗", exc_info=True)
            self._log_error(round_no, "delta_collect", e)
            from .delta import RoundDelta

            delta = RoundDelta(is_quiet=True)

        # 2. LLM 分析 — 每回合都必須做，唔再跳過「靜默回合」
        # （玩家期望每回合都有劇情推進，GM 必須認真睇每個回合）
        analysis = None
        is_failsafe = False
        had_choice = self.state.data.get("choice_applied", False)
        if self._prompter is not None:
            try:
                # [story-weaver:continuity] 砌返最近回合嘅背景，確保故事連貫
                recent_history = _build_recent_history(
                    self.state.build_story_timeline(), max_entries=3
                )
                # [story-weaver:scene-direction] 附加上回玩家選擇，令 GM 有連貫意識
                last_choice = (self.state.data.get("last_player_choice") or "").strip()
                if last_choice:
                    recent_history += f"\n【上回玩家決定】{last_choice}\n（你必須承接呢個決定去規劃今回合同下回合嘅劇情。）"
                # [story-weaver:inner-world] 收集 agent 內心狀態（currently + 當前動作）
                agent_inner = _format_agent_inner(server, self.agent_names)
                analysis = self._prompter.round_analysis(
                    agent_names=self.agent_names,
                    story_seed=self.state.data.get("story_seed", ""),
                    branch_history=self.state.data.get("branch_point_history", []),
                    recent_history=recent_history,
                    events=delta.events_delta,
                    conversations=delta.conversations_delta,
                    agent_inner=agent_inner,
                    matrix_text=store.full_matrix_text(),
                    round_no=round_no,
                    max_rounds=self._gm_config.get("max_rounds", 4),
                    dramatic_pressure=self.state.data.get("dramatic_pressure", 1),
                    unresolved_threads=self.state.data.get("unresolved_threads", []),
                    allow_negative_elements=bool(self._gm_config.get("allow_negative_elements", False)),
                )
            except Exception as e:
                logger.warning("gm: 回合分析 LLM 異常", exc_info=True)
                self._log_error(round_no, "round_summary", e)
                analysis = None
        if analysis is None:
            is_failsafe = True
            self._log_error(round_no, "round_summary", "LLM 全敗，行 failsafe 決策")
            summary = FAILSAFE_SUMMARY
        else:
            summary = analysis.summary

        # [story-weaver:no-quiet] 用過就清，唔會影響下一回合
        if had_choice:
            self.state.data["choice_applied"] = False
            self.state.save()

        # [story-weaver:scene-direction] 儲存 GM 規劃嘅下回合場景指導
        if analysis is not None:
            if analysis.next_scene_goal:
                self.state.data["next_scene_goal"] = analysis.next_scene_goal
            if analysis.next_forced_meetings:
                self.state.data["next_forced_meetings"] = analysis.next_forced_meetings
            self.state.save()

        # [story-weaver:day-boundary] 記錄今回合 game date，俾下回合 detect 跨日
        try:
            self.state.data["last_round_date"] = str(server.config.get("time", ""))[:8]
        except Exception:
            pass

        # [story-weaver:persistent-goals] 由場景指導自動 populate agent goals
        # 每個 forced_meeting pair 入面嘅角色都會得到對應嘅 goal
        try:
            agent_goals = self.state.data.setdefault("agent_goals", {})
            scene = (self.state.data.get("next_scene_goal") or "").strip()
            meetings = self.state.data.get("next_forced_meetings") or []
            for pair in meetings:
                if len(pair) < 2:
                    continue
                for name in pair:
                    if name in self.agent_names:
                        other = pair[1] if pair[0] == name else pair[0]
                        goal = scene or f"與 {other} 見面交談"
                        agent_goals[name] = goal
            # 檢查已完成嘅 goals：如果 agent 之間已經傾過偈（對話出現喺 delta），
            # 而且內容似乎對應到 goal，可以考慮清除——但留俾 GM prompt 判斷
            self.state.save()
        except Exception:
            pass

        # 3. 砌決策 + 半成品 timeline entry（player_choice 留空，apply 時補完）

        # [story-weaver:affinity-threshold] 好感度極端值觸發壓力
        try:
            affinity_data = store.to_dict()
            for from_agent, targets in affinity_data.items():
                for to_agent, info in targets.items():
                    if not isinstance(info, dict):
                        continue
                    value = info.get("value", 0)
                    if value >= 80:
                        self.state.add_unresolved_thread(
                            f"{from_agent} 對 {to_agent} 嘅感情達到頂點（{value}），需要一個交代"
                        )
                    elif value <= -80:
                        self.state.add_unresolved_thread(
                            f"{from_agent} 同 {to_agent} 嘅關係瀕臨破裂（{value}），衝突一觸即發"
                        )
        except Exception:
            pass  # affinity check 失敗唔影響主流程
        options = analysis.options if analysis else []
        suggestions = analysis.suggested_affinity_changes if analysis else []
        branch_point = analysis.branch_point if analysis else None
        entry = TimelineEntry(
            round=round_no,
            summary=summary,
            key_events=_key_events(delta.events_delta),
            dialogues=_dialogue_blocks(delta.conversations_delta),
            branch_point=branch_point,
            options_offered=options,
            is_quiet=delta.is_quiet,
            had_error=is_failsafe,
        )
        try:
            self.state.append_timeline(entry)
            if branch_point:
                self.state.add_unresolved_thread(branch_point)
        except Exception as e:
            logger.warning("gm: timeline 寫入失敗", exc_info=True)
            self._log_error(round_no, "timeline_append", e)

        decision = GMDecision(
            round_no=round_no,
            summary=summary,
            branch_point=branch_point,
            options=options,
            suggested_affinity_changes=suggestions,
            story_timeline=self.state.build_story_timeline(),
            is_failsafe=is_failsafe,
            is_quiet=delta.is_quiet,
            affinity_snapshot=store.to_dict(),
            can_finish=round_no >= self._gm_config["min_rounds_to_finish"],
        )
        try:
            self.state.set_pending_decision(decision)
        except Exception as e:
            logger.warning("gm: pending 決策持久化失敗", exc_info=True)
            self._log_error(round_no, "pending_save", e)
        return decision

    def apply_player_choice(self, server, round_no: int, choice: PlayerChoice) -> InjectionReport:
        """依 choice 注入記憶 + 調好感度 + 補完 timeline + 清 pending。"""
        records: list[InjectionRecord] = []
        affinity_changes: list[AffinityChange] = []
        agents = server.game.agents

        # 1. 自訂命令解析（邊界 6：feasible=False 唔消耗回合，pending 保留）
        parsed: CustomCommandParse | None = None
        if choice.type in ("custom", "option+custom") and choice.text:
            parsed = self.parse_custom_command(choice.text)
            if not parsed.feasible:
                return InjectionReport(
                    ok=False,
                    message=parsed.refuse_reason or "命令未能執行。",
                    refused=parsed,
                )

        # 2. 記憶注入
        if choice.type in ("option", "option+custom") and choice.option_id:
            record = self._inject_option(server, round_no, choice.option_id, agents)
            if record is None:
                # 選項唔喺 pending 入面（過期/重複提交）→ 拒絕，唔好靜默推進回合
                return InjectionReport(
                    ok=False,
                    message="呢個選項已經過期，請重新打開決策視窗再揀。",
                )
            records.append(record)
        if parsed is not None and parsed.feasible:
            record = self._inject_custom(server, round_no, parsed, agents)
            if record:
                records.append(record)

        # [story-weaver:force-meeting] 外部世界幹預：玩家嘅決定唔止係內心想法，
        # 更加係小鎮真實發生嘅事件。強制相關角色移動到共同地點見面。
        if choice.type in ("option", "option+custom") and choice.option_id:
            try:
                self._force_agent_meeting(server, agents, choice, round_no)
                if hasattr(server, "sync_agent_positions"):
                    server.sync_agent_positions()
            except Exception:
                logger.warning("gm: 強制見面失敗", exc_info=True)

        # 3. 好感度調整（玩家 slider，delta 語義，經 affinity 嘅 apply_gm_response 落地）
        if choice.affinity_overrides:
            try:
                store = self._store(server)
                items = [
                    GMAdjustmentItem(
                        from_agent=o.from_agent,
                        to_agent=o.to_agent,
                        delta=max(-30, min(30, int(o.delta))),
                        reason=o.reason or "玩家調整",
                    )
                    for o in choice.affinity_overrides
                ]
                affinity_changes = apply_gm_response(
                    store,
                    GMAdjustmentResponse(adjustments=items),
                    agents,
                    self.state.data.setdefault("affinity_rounds_log", []),
                    round_no,
                    logger,
                )
            except Exception as e:
                logger.warning("gm: 好感度調整失敗", exc_info=True)
                self._log_error(round_no, "affinity_apply", e)

        # 4. 補完 timeline 半成品 entry + 清 pending
        try:
            pending = self.state.get_pending_decision()
            entry = TimelineEntry(
                round=round_no,
                summary=pending.summary if pending else "",
                key_events=[],
                dialogues=[],
                branch_point=pending.branch_point if pending else None,
                options_offered=pending.options if pending else [],
                player_choice=choice,
                affinity_changes=affinity_changes,
                is_quiet=pending.is_quiet if pending else False,
                had_error=pending.is_failsafe if pending else False,
            )
            # 保留 on_round_end 影低嘅 key_events / dialogues（story_timeline 最尾項）
            if pending and pending.story_timeline:
                last = pending.story_timeline[-1]
                if last.round == round_no:
                    entry.key_events = last.key_events
                    entry.dialogues = last.dialogues
            self.state.replace_last_timeline(entry)
            self.state.clear_pending_decision()
        except Exception as e:
            logger.warning("gm: timeline 補完失敗", exc_info=True)
            self._log_error(round_no, "timeline_complete", e)

        for r in records:
            try:
                self.state.log_injection(r)
            except Exception:
                pass

        messages = {
            "option": "你的意志已注入小鎮。",
            "custom": "你的意志已注入小鎮。",
            "option+custom": "你的意志已注入小鎮。",
            "skip": "任由發展，小鎮繼續它的日常。",
            "finish": "故事即將進入終章。",
        }
        # [story-weaver:no-quiet] 標記玩家已做選擇，下回合唔會俾靜默判定吞咗
        # [story-weaver:scene-direction] 記錄玩家選擇俾下回合 GM 參考
        if choice.type not in ("skip",):
            self.state.resolve_unresolved_thread()
            self.state.data["choice_applied"] = True
            # 記錄玩家意圖，令下回合 GM prompt 可以引用
            choice_text = choice.text or ""
            if not choice_text and choice.option_id:
                # 由 injected record 攞內容
                if records:
                    choice_text = records[0].content
            if choice_text:
                self.state.data["last_player_choice"] = choice_text
            self.state.save()
        else:
            # 玩家 skip：張力累積
            self.state.add_unresolved_thread("玩家選擇唔幹預——小鎮嘅命運懸而未決")
            self.state.save()
        return InjectionReport(
            ok=True,
            message=messages.get(choice.type, "已處理。"),
            injected=records,
            affinity_changes=affinity_changes,
        )

    def _inject_option(self, server, round_no: int, option_id: str, agents: dict) -> InjectionRecord | None:
        pending = self.state.get_pending_decision()
        option: GMOption | None = None
        if pending:
            for o in pending.options:
                if o.id == option_id:
                    option = o
                    break
        if option is None:
            self._log_error(round_no, "inject_option", f"搵唔到選項 {option_id}")
            return None
        describe = _safe_option_event_description(option.title, option.predicted)
        node_ids: dict[str, str] = {}
        error = None
        try:
            for name, agent in agents.items():
                node_ids[name] = self._injector.inject(
                    agent,
                    describe,
                    predicate=PREDICATE_OPTION,
                    poignancy=self._gm_config["option_poignancy"],
                    poignancy_boost=self._gm_config["poignancy_boost"],
                )
                # [story-weaver:scratch-inject] 更新當前意識 → 後續 LLM 決策跟隨玩家選擇
                try:
                    agent.scratch.currently = (
                        f"【命運轉折】{describe}。呢件事徹底改變咗你今日嘅計劃——"
                        f"你必須優先處理呢件事，其他一切都可以等。因此，{agent.scratch.currently}"
                    )
                except Exception:
                    pass
                # [story-weaver:schedule-revise] 修改當前行動計劃（只改清醒 agent）
                if agent.is_awake():
                    try:
                        event_obj = memory.Event(
                            agent.name,
                            PREDICATE_OPTION,
                            INJECT_OBJECT,
                            describe=describe,
                            address=agent.get_tile().get_address(),
                        )
                        plan, de_plan = agent.schedule.current_plan()
                        start = utils.get_timer().daily_time(de_plan["start"])
                        duration = de_plan["duration"]
                        agent.revise_schedule(event_obj, start, duration)
                        # [story-weaver:plan-overwrite] 直接改寫 plan block describe，
                        # 等於玩家嘅導演決定成為 agent 日程嘅正式項目
                        try:
                            plan["describe"] = describe
                        except Exception:
                            pass
                    except Exception:
                        logger.warning(
                            "gm: %s schedule revise 失敗", agent.name, exc_info=True
                        )
        except Exception as e:
            logger.warning("gm: 選項注入失敗", exc_info=True)
            error = str(e)
            self._log_error(round_no, "inject_option", e)
        return InjectionRecord(
            round=round_no,
            targets=list(node_ids.keys()),
            content=describe,
            node_ids=node_ids,
            poignancy=self._gm_config["option_poignancy"],
            source="option",
            error=error,
        )

    def _inject_custom(self, server, round_no: int, parsed: CustomCommandParse, agents: dict) -> InjectionRecord | None:
        targets = [t for t in parsed.targets if t in agents] or list(agents.keys())
        node_ids: dict[str, str] = {}
        error = None
        try:
            for name in targets:
                node_ids[name] = self._injector.inject(
                    agents[name],
                    parsed.command_event_describe,
                    predicate=PREDICATE_CUSTOM,
                    poignancy=self._gm_config["custom_poignancy"],
                    poignancy_boost=self._gm_config["poignancy_boost"],
                )
                # [story-weaver:scratch-inject] 更新當前意識 → 後續 LLM 決策跟隨玩家命令
                try:
                    agents[name].scratch.currently = (
                        f"【命運驅使】{parsed.command_event_describe}。"
                        f"你無法抗拒——呢件事必須成為你當下最重要嘅行動。因此，"
                        f"{agents[name].scratch.currently}"
                    )
                except Exception:
                    pass
                # [story-weaver:schedule-revise] 修改當前行動計劃（只改清醒 agent）
                if agents[name].is_awake():
                    try:
                        event_obj = memory.Event(
                            agents[name].name,
                            PREDICATE_CUSTOM,
                            INJECT_OBJECT,
                            describe=parsed.command_event_describe,
                            address=agents[name].get_tile().get_address(),
                        )
                        plan, de_plan = agents[name].schedule.current_plan()
                        start = utils.get_timer().daily_time(de_plan["start"])
                        duration = de_plan["duration"]
                        agents[name].revise_schedule(event_obj, start, duration)
                        # [story-weaver:plan-overwrite] 直接改寫 plan block describe
                        try:
                            plan["describe"] = parsed.command_event_describe
                        except Exception:
                            pass
                    except Exception:
                        logger.warning(
                            "gm: %s schedule revise 失敗", agents[name].name, exc_info=True
                        )
        except Exception as e:
            logger.warning("gm: 自訂命令注入失敗", exc_info=True)
            error = str(e)
            self._log_error(round_no, "inject_custom", e)
        return InjectionRecord(
            round=round_no,
            targets=targets,
            content=parsed.command_event_describe,
            node_ids=node_ids,
            poignancy=self._gm_config["custom_poignancy"],
            source="custom",
            error=error,
        )

    def _force_agent_meeting(self, server, agents: dict, choice, round_no: int) -> None:
        """[story-weaver:force-meeting] 玩家選擇 → 外部世界真實事件。
        將所有 agent 移動到共同地點，記錄一次對話。"""
        if len(agents) < 2:
            return
        # 揀共同地點
        meeting_coord = None
        for agent in agents.values():
            try:
                if agent.is_awake():
                    meeting_coord = list(agent.coord)
                    break
            except Exception:
                pass
        if meeting_coord is None:
            meeting_coord = [94, 21]
        # [story-weaver:pre-move] 記錄原位 → FrameBuffer 可生成行路動畫
        pre_move = {}
        for name, agent in agents.items():
            try:
                pre_move[name] = list(agent.coord)
            except Exception:
                pass
        # 移所有 agent 到共同地點
        for agent in agents.values():
            try:
                agent.move(meeting_coord[:])
            except Exception:
                pass
        if pre_move:
            server.config["_pre_move_positions"] = pre_move
            # [story-weaver:pre-move] 同時 save 去 gm_state，因為 server rebuild 時會
            # 清走 config，但 gm_state 會保留到下回合
            self.state.data["_pre_move_positions"] = pre_move
            self.state.save()
        logger.info("gm: 強制見面 — %d agents", len(agents))

    def parse_custom_command(self, text: str) -> CustomCommandParse:
        """自訂命令解析 + 後置校驗。feasible=False 唔消耗回合（邊界 6）。"""
        if not text or not text.strip():
            return CustomCommandParse(
                feasible=False, refuse_reason="命令唔可以係空嘅。"
            )
        if self._prompter is None:
            return CustomCommandParse(
                feasible=False,
                refuse_reason="命運之線暫時模糊，未能解讀你的命令，請稍後再試。",
            )
        try:
            story_context = "；".join(
                e.summary for e in self.state.build_story_timeline()[-3:] if e.summary
            )
            parsed = self._prompter.parse_custom_command(
                self.agent_names, story_context, text.strip()
            )
        except Exception as e:
            logger.warning("gm: 命令解析異常", exc_info=True)
            self._log_error(0, "parse_command", e)
            return CustomCommandParse(
                feasible=False,
                refuse_reason="命運之線暫時模糊，未能解讀你的命令，請稍後再試。",
            )
        # 後置校驗一：targets 過濾到角色名單（LLM 幻覺嘅名丟棄，
        # 冇 valid target 就 fallback 全部角色，唔好因為 LLM 認錯名就 reject）
        known = [t for t in parsed.targets if t in self.agent_names]
        if parsed.targets and not known:
            # LLM 幻覺咗全部 target 名 → 清空 targets，apply to all
            logger.warning(
                "gm: 命令解析 targets 全幻覺（%s），fallback 全部角色", parsed.targets
            )
        parsed.targets = known if known else []  # 空 = apply to all
        # 後置校驗二：describe 過禁詞檢查
        if parsed.feasible:
            try:
                self._injector.validate_describe(parsed.command_event_describe)
            except ValueError as e:
                parsed.feasible = False
                parsed.refuse_reason = "命令改寫後仍不符合小鎮的規則，請換個講法。"
                self._log_error(0, "parse_command_validate", e)
        return parsed

    # ---------------------------------------------------------------- 終章

    def generate_finale(self, server) -> Finale:
        """用累積 timeline 跑 gm_finale.txt。冪等：已生成則直接回傳。"""
        existing = self.state.get_finale()
        if existing is not None:
            return existing
        timeline = self.state.build_story_timeline()
        narrative = None
        if self._prompter is not None:
            try:
                timeline_text = "\n".join(
                    f"第{e.round}回合：{e.summary}"
                    + (f"（分支點：{e.branch_point}）" if e.branch_point else "")
                    for e in timeline
                )
                store = self._store(server)
                narrative = self._prompter.finale(
                    self.agent_names,
                    self.state.data.get("story_seed", ""),
                    timeline_text,
                    store.full_matrix_text(),
                )
            except Exception as e:
                logger.warning("gm: 終章 LLM 異常", exc_info=True)
                self._log_error(0, "finale", e)
        if narrative is None:
            self._log_error(0, "finale", "LLM 全敗，行 failsafe 終章")
            narrative = FinaleNarrative(
                ending="命運之線在此收束。小鎮的日子仍會繼續，只是再無人執筆為它們記錄。",
                character_epilogues=[
                    {"name": n, "epilogue": "繼續在小鎮過著平常的日子。"}
                    for n in self.agent_names
                ],
            )
        finale = Finale(
            timeline=timeline,
            narrative=narrative,
            affinity_table=self._build_affinity_table(timeline),
        )
        try:
            self.state.set_finale(finale)
        except Exception as e:
            logger.warning("gm: 終章持久化失敗", exc_info=True)
            self._log_error(0, "finale_save", e)
        return finale

    def _build_affinity_table(self, timeline: list[TimelineEntry]) -> list[AffinityChange]:
        """全劇好感度變化總表：每條邊取首次 old → 最後 new。"""
        edges: dict[tuple[str, str], AffinityChange] = {}
        for entry in timeline:
            for change in entry.affinity_changes:
                key = (change.from_agent, change.to_agent)
                if key in edges:
                    first = edges[key]
                    edges[key] = AffinityChange(
                        from_agent=first.from_agent,
                        to_agent=first.to_agent,
                        old=first.old,
                        new=change.new,
                        delta=change.new - first.old,
                        reason=change.reason or first.reason,
                        absolute=change.absolute,
                        round=change.round,
                        time=change.time,
                    )
                else:
                    edges[key] = change
        return list(edges.values())

    # ---------------------------------------------------------------- 恢復

    def get_pending_decision(self) -> GMDecision | None:
        """resume 時前端重開 modal 用（邊界 3）。選項逐字取自持久化，唔重跑 LLM。"""
        return self.state.get_pending_decision()

    @classmethod
    def resume(
        cls,
        sim_name: str,
        checkpoints_folder: str,
        llm_config: dict,
        prompts_dir: str | None = None,
        gm_config: dict | None = None,
        llm=None,
    ) -> "GMDirector":
        """從 gm_state.json 重建。檔案缺失時視為新遊戲（邊界 4）。"""
        state = GMStateStore.load(os.path.join(checkpoints_folder, "gm_state.json"))
        agent_names = state.data.get("agent_names") or []
        return cls(
            sim_name=sim_name,
            checkpoints_folder=checkpoints_folder,
            llm_config=llm_config,
            agent_names=agent_names,
            prompts_dir=prompts_dir,
            gm_config=gm_config,
            llm=llm,
            state=state,
        )


def load_gm_config(path: str | None = None) -> dict:
    """讀 data/gm_config.json；唔存在返預設。"""
    try:
        with open(path or GM_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("gm: gm_config.json 讀取失敗，用預設值", exc_info=True)
        return dict(_DEFAULT_GM_CONFIG)
