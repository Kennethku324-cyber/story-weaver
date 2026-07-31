"""story_weaver.gm.state — GMStateStore：gm_state.json 讀寫（spec §2.4、§3.3）。

原子寫入：先寫 .tmp 再 os.replace()，防寫到一半斷電（邊界 4）。
損毀降級：JSON 壞 → 試 .tmp 備份 → 再敗則從零開始 + errors 記錄，流程唔斷。
"""

from __future__ import annotations

import copy
import json
import logging
import os

from .models import Finale, GMDecision, InjectionRecord, TimelineEntry

logger = logging.getLogger(__name__)

STATE_VERSION = 1

# gm_state.json 頂層欄位預設值（load 時補齊，容許舊版檔案缺欄位）
_DEFAULTS: dict = {
    "version": STATE_VERSION,
    "story_seed": "",
    "agent_names": [],
    "timeline": [],
    "pending_decision": None,
    "injection_log": [],
    "branch_point_history": [],
    "round_baseline": None,
    "last_relations_prefix": {},
    "affinity_rounds_log": [],
    "finale": None,
    "errors": [],
    "choice_applied": False,  # [story-weaver:no-quiet] 今回合係咪啱啱應用咗玩家選擇
    "dramatic_pressure": 1,
    "unresolved_threads": [],
    # [story-weaver:persistent-goals] agent 級 persistent goal（導演 set goal → 每回合 re-inject 直到達成）
    "agent_goals": {},  # {agent_name: goal_description}
    # [story-weaver:day-boundary] 記錄上回合 game date，detect day change
    "last_round_date": "",
}


class GMStateStore:
    """gm_state.json 持有者。所有 mutating 方法都即時 save（GM 寫入低頻，唔使慳 IO）。"""

    def __init__(self, path: str, data: dict | None = None) -> None:
        self._path = path
        merged = copy.deepcopy(_DEFAULTS)  # 深拷貝：巢狀 list/dict 唔可以同其他 store 共享
        if data:
            merged.update(data)
        self._data = merged

    # ---------------------------------------------------------------- 基本

    @property
    def path(self) -> str:
        return self._path

    @property
    def data(self) -> dict:
        """原始 dict（director 內部用，例如 last_relations_prefix / affinity_rounds_log）。"""
        return self._data

    def save(self) -> None:
        """原子寫入：.tmp → os.replace。"""
        tmp_path = self._path + ".tmp"
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._path)

    @classmethod
    def load(cls, path: str) -> "GMStateStore":
        """主檔壞 → 試 .tmp；再敗 → 從零開始 + errors 記錄（邊界 4）。檔案唔存在 → 新遊戲。"""
        if not os.path.exists(path):
            return cls(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls(path, json.load(f))
        except Exception:
            logger.warning("gm_state: 主檔損毀，試 .tmp 備份（%s）", path, exc_info=True)
        tmp_path = path + ".tmp"
        if os.path.exists(tmp_path):
            try:
                with open(tmp_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                store = cls(path, data)
                store._log_error(0, "load", "主檔損毀，已從 .tmp 備份恢復")
                return store
            except Exception:
                logger.warning("gm_state: .tmp 備份都損毀（%s）", tmp_path, exc_info=True)
        store = cls(path)
        store._log_error(0, "load", "gm_state.json 損毀且無可用備份，timeline 歸零重新開始")
        return store

    # ---------------------------------------------------------------- 初始化

    def init_new(self, story_seed: str, agent_names: list[str]) -> None:
        """Setup 完成後呼叫一次。已有 timeline 唔會重置（冪等保護）。"""
        if self._data["timeline"] or self._data["story_seed"]:
            logger.info("gm_state: 已初始化過，init_new 略過")
            return
        self._data["story_seed"] = story_seed
        self._data["agent_names"] = list(agent_names)
        self._data["timeline"] = [
            TimelineEntry(round=0, summary=story_seed).model_dump(mode="json")
        ]
        self.save()

    # ---------------------------------------------------------------- timeline

    def append_timeline(self, entry: TimelineEntry) -> None:
        self._data["timeline"].append(entry.model_dump(mode="json"))
        if entry.branch_point:
            self._data["branch_point_history"].append(entry.branch_point)
        self.save()

    def replace_last_timeline(self, entry: TimelineEntry) -> None:
        """pending 決策嘅半成品 entry 喺 apply_player_choice 時補完後替換。"""
        if self._data["timeline"]:
            self._data["timeline"][-1] = entry.model_dump(mode="json")
        else:
            self._data["timeline"].append(entry.model_dump(mode="json"))
        self.save()

    def build_story_timeline(self) -> list[TimelineEntry]:
        """故事回顧輸出：story_seed 第 0 項 + 全部 entries，對白保留原文。"""
        out = []
        for raw in self._data["timeline"]:
            try:
                out.append(TimelineEntry.model_validate(raw))
            except Exception:
                logger.warning("gm_state: timeline entry 損毀，略過", exc_info=True)
        return out

    # ---------------------------------------------------------------- pending 決策

    def set_pending_decision(self, decision: GMDecision) -> None:
        self._data["pending_decision"] = decision.model_dump(mode="json")
        self.save()

    def get_pending_decision(self) -> GMDecision | None:
        raw = self._data.get("pending_decision")
        if raw is None:
            return None
        try:
            return GMDecision.model_validate(raw)
        except Exception:
            logger.warning("gm_state: pending_decision 損毀，當無決策處理", exc_info=True)
            return None

    def clear_pending_decision(self) -> None:
        self._data["pending_decision"] = None
        self.save()

    # ---------------------------------------------------------------- story pressure

    def add_unresolved_thread(self, thread: str) -> None:
        """Persist the newest dramatic question and raise pressure, capped at five."""
        text = (thread or "").strip()
        if not text:
            return
        threads = self._data.setdefault("unresolved_threads", [])
        threads.append(text)
        self._data["unresolved_threads"] = threads[-3:]
        self._data["dramatic_pressure"] = min(5, int(self._data.get("dramatic_pressure", 1)) + 1)
        self.save()

    def resolve_unresolved_thread(self) -> None:
        """A player intervention pays off one outstanding question and releases pressure."""
        threads = self._data.setdefault("unresolved_threads", [])
        if threads:
            threads.pop(0)
        self._data["dramatic_pressure"] = max(1, int(self._data.get("dramatic_pressure", 1)) - 1)
        self.save()

    # ---------------------------------------------------------------- injection log

    def log_injection(self, record: InjectionRecord) -> None:
        self._data["injection_log"].append(record.model_dump(mode="json"))
        self.save()

    # ---------------------------------------------------------------- round baseline

    def set_round_baseline(self, baseline) -> None:
        """baseline 係 delta.RoundBaseline（dataclass），呢度 duck-typing 存 dict。"""
        self._data["round_baseline"] = {
            "conversation_keys": list(baseline.conversation_keys),
            "memory_node_ids": {k: list(v) for k, v in baseline.memory_node_ids.items()},
            "sim_time": baseline.sim_time,
        }
        self.save()

    def get_round_baseline(self):
        """回傳 dict 或 None（director 自行重建 RoundBaseline，避免循環 import）。"""
        return self._data.get("round_baseline")

    # ---------------------------------------------------------------- finale

    def set_finale(self, finale: Finale) -> None:
        self._data["finale"] = finale.model_dump(mode="json")
        self.save()

    def get_finale(self) -> Finale | None:
        raw = self._data.get("finale")
        if raw is None:
            return None
        try:
            return Finale.model_validate(raw)
        except Exception:
            logger.warning("gm_state: finale 損毀，當未生成處理", exc_info=True)
            return None

    # ---------------------------------------------------------------- errors

    def _log_error(self, round_no: int, stage: str, error: str, at: str = "") -> None:
        self._data["errors"].append(
            {"round": round_no, "stage": stage, "error": str(error), "at": at}
        )

    def log_error(self, round_no: int, stage: str, error: str, at: str = "") -> None:
        self._log_error(round_no, stage, error, at)
        self.save()
