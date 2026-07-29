"""story_weaver.gameui.state_store — game_ui_state.json 原子讀寫（spec §2.2 瘦身版）。"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import threading

from .models import GameUIState

logger = logging.getLogger(__name__)


class StateCorruptError(RuntimeError):
    """game_ui_state.json 損毀（理論上原子寫唔會；出現即外部干預）。"""


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class GameUIStateStore:
    """game_ui_state.json 嘅唯一讀寫口。tmp + os.replace 原子寫；內部 RLock。"""

    def __init__(self, checkpoints_folder: str) -> None:
        self._folder = checkpoints_folder
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return os.path.join(self._folder, "game_ui_state.json")

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> GameUIState:
        if not os.path.exists(self.path):
            raise FileNotFoundError(self.path)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return GameUIState.model_validate(json.load(f))
        except json.JSONDecodeError as e:
            raise StateCorruptError(f"game_ui_state.json 損毀：{e}") from e

    def save(self, state: GameUIState) -> None:
        state.updated_at = _now_iso()
        tmp_path = f"{self.path}.tmp.{os.getpid()}"
        with self._lock:
            os.makedirs(self._folder, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(state.model_dump_json())
            os.replace(tmp_path, self.path)

    @contextlib.contextmanager
    def mutate(self):
        """with store.mutate() as s: s.round += 1 —— 離開 block 自動 save。"""
        with self._lock:
            state = self.load()
            yield state
            self.save(state)


def create_initial_state(
    checkpoints_folder: str,
    session: str,
    agents: list[str],
    steps_per_round: int = 6,
    stride: int = 10,
    max_rounds: int = 10,
    min_rounds_to_finish: int = 2,
) -> GameUIState:
    """建立初始 game_ui_state.json（冪等：已存在直接返回）。"""
    store = GameUIStateStore(checkpoints_folder)
    if store.exists():
        return store.load()
    state = GameUIState(
        session=session,
        agents=list(agents),
        steps_per_round=steps_per_round,
        stride=stride,
        max_rounds=max_rounds,
        min_rounds_to_finish=min_rounds_to_finish,
    )
    store.save(state)
    return state
