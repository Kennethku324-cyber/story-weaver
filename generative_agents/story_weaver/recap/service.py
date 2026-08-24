"""story_weaver.recap.service — RecapService 門面（spec §7）。

回合觸發序列（回合管理系統執行，start.py 零改動）：
    prev_step = sim_config["step"]
    server.simulate(step=N, stride=stride)
    recap_service.on_round_end(sim_name, round_no,
        step_range=(prev_step + 1, server.config["step"]),
        player_decision=上一回合決策)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading

from .extractors import DialogueExtractor, EventExtractor
from .generator import RecapGenerator
from .models import (
    AgentProfile,
    CumulativeRecap,
    GMContext,
    PlayerDecision,
    RoundRecap,
    StoryRecap,
    TimelineEvent,
)
from .store import StoryRecapStore
from story_weaver.paths import DATA_CONFIG_PATH, RECAP_PROMPTS_ROOT

logger = logging.getLogger(__name__)


class OpeningMissingError(ValueError):
    """opening 為空或純空白時拋出；Setup 系統捕獲後返回佢自己嘅 400。"""


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _default_llm_config(data_config_path: str | None = None) -> dict | None:
    try:
        with open(data_config_path or DATA_CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        return config["agent"]["think"]["llm"]
    except Exception:
        logger.warning("recap service: 讀唔到 data/config.json 嘅 llm 配置", exc_info=True)
        return None


class RecapService:
    def __init__(
        self,
        checkpoints_root: str = "results/checkpoints",
        static_root: str = "frontend/static",
        llm_config: dict | None = None,
        llm=None,
        template_path: str | None = None,
        context_window: int = 8192,
    ) -> None:
        self._root = checkpoints_root
        self._static_root = static_root
        self._store = StoryRecapStore(checkpoints_root)
        if llm_config is None and llm is None:
            llm_config = _default_llm_config()
        self._generator = RecapGenerator(
            llm_config=llm_config, template_path=template_path or str(RECAP_PROMPTS_ROOT),
            llm=llm, context_window=context_window,
        )
        self._events = EventExtractor()
        self._dialogues = DialogueExtractor()

    def _sim_dir(self, sim_name: str) -> str:
        return os.path.join(self._root, sim_name)

    # ---------------------------------------------------------------- Setup 系統

    def init_story(self, sim_name: str, opening: str, agents: list[AgentProfile]) -> StoryRecap:
        """故事開始時調用。opening 空白 → OpeningMissingError。已存在 → 冪等返回。"""
        if not opening or not opening.strip():
            raise OpeningMissingError("故事開端唔可以係空嘅")
        existing = self._store.load(sim_name)
        if existing is not None:
            logger.info("recap service: %s 已初始化，冪等返回", sim_name)
            return existing
        recap = StoryRecap(
            sim_name=sim_name,
            opening=opening.strip(),
            created_at=_now_iso(),
            agents=list(agents),
        )
        # 第 1 回合決策前：cumulative 直出捷徑模板，唔調 LLM
        text, status = self._generator.generate_cumulative(recap.opening, [], [])
        recap.cumulative_recap = CumulativeRecap(
            text=text, generated_at_round=0, status=status,
            model=self._generator.model_name,
        )
        self._store.save(recap)
        return recap

    # ---------------------------------------------------------------- 回合管理系統

    def on_round_end(
        self,
        sim_name: str,
        round_no: int,
        step_range: tuple[int, int],
        player_decision: PlayerDecision | None = None,
        background: bool = True,
    ) -> RoundRecap | None:
        """同步提取（事件+對話、append pending、原子寫入）；
        background=True 時開 thread 做 LLM 敘事生成。"""
        sim_dir = self._sim_dir(sim_name)
        extraction = self._events.extract(sim_dir, step_range)
        dialogue_result = self._dialogues.extract(
            sim_dir, extraction.sim_time_start, extraction.sim_time_end
        )
        round_recap = RoundRecap(
            round=round_no,
            sim_time_start=extraction.sim_time_start,
            sim_time_end=extraction.sim_time_end,
            step_range=tuple(step_range),
            events=extraction.events,
            dialogues=dialogue_result.blocks,
            player_decision=player_decision,
            recap_status="pending",
            dialogue_health=dialogue_result.health,
            warnings=extraction.warnings + dialogue_result.warnings,
        )

        def _append(recap: StoryRecap) -> None:
            # 同 round 重複觸發 → 替換（冪等），唔 append
            recap.rounds = [r for r in recap.rounds if r.round != round_no]
            recap.rounds.append(round_recap)

        if self._store.update(sim_name, _append) is None:
            logger.warning("recap service: %s 未初始化，on_round_end 略過", sim_name)
            return None

        if background:
            thread = threading.Thread(
                target=self._generate_narratives,
                args=(sim_name, round_no),
                daemon=True,
            )
            thread.start()
        else:
            self._generate_narratives(sim_name, round_no)
        return round_recap

    def _generate_narratives(self, sim_name: str, round_no: int) -> None:
        """生成 round_recap + cumulative_recap，完成後原子寫入更新 status。"""
        try:
            recap = self._store.load(sim_name)
            if recap is None:
                return
            target = next((r for r in recap.rounds if r.round == round_no), None)
            if target is None:
                return
            agent_names = [a.name for a in recap.agents]
            prev_recaps = [r.round_recap for r in recap.rounds if r.round < round_no and r.round_recap]
            text, status = self._generator.generate_round_recap(
                recap.opening, target, prev_recaps, agent_names
            )

            def _update(recap: StoryRecap) -> None:
                t = next((r for r in recap.rounds if r.round == round_no), None)
                if t is None:
                    return
                t.round_recap = text
                t.recap_status = status
                cum_text, cum_status = self._generator.generate_cumulative(
                    recap.opening, recap.rounds, agent_names
                )
                recap.cumulative_recap = CumulativeRecap(
                    text=cum_text,
                    generated_at_round=round_no,
                    status=cum_status,
                    model=self._generator.model_name,
                )

            self._store.update(sim_name, _update)
        except Exception:
            # 敘事生成失敗唔可以影響模擬循環；留低 pending 都會喺下次讀取時被 ui_hints 反映
            logger.warning("recap service: %s 第 %d 回合敘事生成失敗", sim_name, round_no, exc_info=True)

    # ---------------------------------------------------------------- 決策 modal / 注入系統

    def record_player_decision(self, sim_name: str, round_no: int, decision: PlayerDecision) -> bool:
        """按 round upsert（同 round 只留最新）。返 True = upsert（覆蓋咗舊嘅）。"""
        decision.round = round_no
        upserted = False

        def _apply(recap: StoryRecap) -> None:
            nonlocal upserted
            for r in recap.rounds:
                if r.round == round_no:
                    upserted = r.player_decision is not None
                    r.player_decision = decision
                    return
            # 回合未生成（玩家喺提取前提交）→ 開一個空回合殼裝住
            recap.rounds.append(
                RoundRecap(round=round_no, sim_time_start="", sim_time_end="",
                           player_decision=decision)
            )

        return self._store.update(sim_name, _apply) is not None and upserted

    def get_player_decision(self, sim_name: str, round_no: int) -> PlayerDecision | None:
        recap = self._store.load(sim_name)
        if recap is None:
            return None
        for r in recap.rounds:
            if r.round == round_no:
                return r.player_decision
        return None

    # ---------------------------------------------------------------- GM 系統

    def record_gm_note(self, sim_name: str, round_no: int, note: str, sim_time: str | None = None) -> None:
        """GM 調整好感度/設定後調用，append 一條 type="gm_note" 事件。"""
        if not note or not note.strip():
            raise ValueError("gm_note 唔可以係空嘅")

        def _apply(recap: StoryRecap) -> None:
            target = next((r for r in recap.rounds if r.round == round_no), None)
            if target is None:
                target = RoundRecap(round=round_no, sim_time_start="", sim_time_end="")
                recap.rounds.append(target)
            target.events.append(
                TimelineEvent(
                    sim_time=sim_time or target.sim_time_end or "",
                    agent="GM",
                    type="gm_note",
                    location="",
                    describe=note.strip(),
                    poignancy=8,
                )
            )

        self._store.update(sim_name, _apply)

    def build_gm_context(self, sim_name: str) -> GMContext | None:
        """壓縮時間線俾 GM prompt（spec §3.3）。同步、純讀。"""
        recap = self._store.load(sim_name)
        if recap is None:
            return None
        return GMContext(
            sim_name=sim_name,
            opening=recap.opening,
            round_count=len(recap.rounds),
            round_summaries=[
                {"round": r.round, "recap": r.round_recap, "status": r.recap_status}
                for r in recap.rounds
            ],
            latest_round=recap.rounds[-1] if recap.rounds else None,
            agents=list(recap.agents),
            generated_at=_now_iso(),
        )

    # ---------------------------------------------------------------- UI / 導出

    def get_cumulative_text(self, sim_name: str) -> str:
        """[story-weaver:continuity] 返回連貫故仔敘事文本。"""
        recap = self._store.load(sim_name)
        if recap is None:
            return ""
        cr = recap.cumulative_recap
        if cr and cr.text:
            return cr.text
        # fallback：手動砌返開端 + 各回合
        lines = [f"【故事開端】{recap.opening}"]
        for r in recap.rounds:
            if r.round_recap:
                lines.append(r.round_recap)
        return "\n\n".join(lines)

    def get_recap(self, sim_name: str, round_no: int | None = None) -> StoryRecap | None:
        """純讀。round_no 指定時只返回該回合（分頁），其餘欄位照附。"""
        recap = self._store.load(sim_name)
        if recap is None:
            return None
        if round_no is not None:
            recap.rounds = [r for r in recap.rounds if r.round == round_no]
        return recap

    def export_markdown(self, sim_name: str) -> str | None:
        from .markdown_export import export_markdown

        recap = self._store.load(sim_name)
        if recap is None:
            return None
        return export_markdown(recap)
