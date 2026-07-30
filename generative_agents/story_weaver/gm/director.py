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
    entries = [e for e in timeline if getattr(e, "round", 0) > 0]
    recent = entries[-max_entries:] if len(entries) > max_entries else entries
    if not recent:
        return "（故事剛剛開始，未有過往回合）"
    lines = []
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
        prompts_dir: str = "data/prompts_gm",
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
        self._prompter = GMPrompter(self._llm, prompts_dir) if self._llm else None
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
        """拍增量基線快照 + 注入好感度 currently 前綴 + 主題錨定。"""
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
        # [story-weaver:theme-anchor] 每回合開始將故事主題重新錨定入 agent 當前意識，
        # 確保推演內容跟隨故事開端，唔會漂移成日常流水賬
        try:
            story_seed = (self.state.data.get("story_seed") or "").strip()
            if story_seed:
                for name, agent in server.game.agents.items():
                    try:
                        current = agent.scratch.currently
                        # 避免重複疊加（已含主題就唔再加）
                        if story_seed[:20] not in current:
                            agent.scratch.currently = (
                                f"【故事主題】{story_seed}。{current}"
                            )
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("gm: 主題錨定失敗", exc_info=True)
            self._log_error(0, "theme_anchor", e)

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

        # 3. 砌決策 + 半成品 timeline entry（player_choice 留空，apply 時補完）
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
        if choice.type not in ("skip",):
            self.state.data["choice_applied"] = True
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
        describe = f"{option.title}。{option.predicted}"
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
                        f"【命運轉折】{describe}。因此，{agent.scratch.currently}"
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
                        _plan, de_plan = agent.schedule.current_plan()
                        start = utils.get_timer().daily_time(de_plan["start"])
                        duration = de_plan["duration"]
                        agent.revise_schedule(event_obj, start, duration)
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
                        f"【命運驅使】{parsed.command_event_describe}。因此，"
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
                        _plan, de_plan = agents[name].schedule.current_plan()
                        start = utils.get_timer().daily_time(de_plan["start"])
                        duration = de_plan["duration"]
                        agents[name].revise_schedule(event_obj, start, duration)
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
        # 移所有 agent 到共同地點
        for agent in agents.values():
            try:
                agent.coord = meeting_coord[:]
                agent.path = []
            except Exception:
                pass
        # 記錄見面對話
        choice_text = getattr(choice, "text", "") or choice.option_id or "命運嘅安排"
        sim_time = server.config.get("time", "")
        if not sim_time:
            from modules import utils
            sim_time = utils.get_timer().get_date("%Y%m%d-%H:%M")
        agent_list = list(agents.keys())
        speakers = " -> ".join(agent_list)
        meeting_location = "小鎮，咖啡館"
        convo_lines = [[name, f"（應約前來——命運嘅齒輪開始轉動）"] for name in agent_list]
        convo_block = {f"{speakers} @ {meeting_location}": convo_lines}
        if sim_time not in server.game.conversation:
            server.game.conversation[sim_time] = []
        server.game.conversation[sim_time].append(convo_block)
        logger.info("gm: 強制見面 — %d agents @ %s", len(agent_list), meeting_location)

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
        # 後置校驗一：targets 過濾到角色名單（LLM 幻覺嘅名直接丟棄）
        known = [t for t in parsed.targets if t in self.agent_names]
        if parsed.targets and not known and parsed.feasible:
            parsed.feasible = False
            parsed.refuse_reason = "命令涉及的角色不在小鎮之中。"
        parsed.targets = known
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
        prompts_dir: str = "data/prompts_gm",
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


def load_gm_config(path: str = "data/gm_config.json") -> dict:
    """讀 data/gm_config.json；唔存在返預設。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("gm: gm_config.json 讀取失敗，用預設值", exc_info=True)
        return dict(_DEFAULT_GM_CONFIG)
