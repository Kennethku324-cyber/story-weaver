"""story_weaver.gameui.models — 遊戲主 UI 數據模型（spec §2.1，已按整合現實瘦身）。

同 spec 嘅差異（drift 決策）：
- timeline / pending_gm / affinity **唔喺呢度存**——佢哋分別由 gm_state.json
  （GMDirector）同 config["affinity"]（AffinityStore）持有，避免三重複寫。
- GameUIState 只係薄 session 狀態：回合數、狀態機、cursor、控制權、error。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class UIStatus(str, Enum):
    IDLE = "idle"                      # 等玩家撳「開始推演 / 下一回合」
    SIMULATING = "simulating"          # 後台 thread 推演中
    WAITING_DECISION = "waiting_decision"  # GM 決策已出，等玩家揀（pending 喺 gm_state）
    FINISHED = "finished"
    ERROR = "error"


class FeedKind(str, Enum):
    EVENT = "event"
    CHAT = "chat"
    SYSTEM = "system"  # 「命令已送達」「GM 調整好感」等系統訊息


class DialogueLine(BaseModel):
    speaker: str
    line: str  # 對白原文，唔准摘要改寫


class FeedItem(BaseModel):
    seq: int  # 全局遞增；前端用 since_feed 增量拉
    sim_time: str = ""
    kind: FeedKind
    actor: Optional[str] = None
    location: Optional[str] = None
    text: str = ""
    dialogue: list[DialogueLine] = Field(default_factory=list)  # kind=chat 時逐句


class GameUIState(BaseModel):
    """持久化喺 results/checkpoints/<name>/game_ui_state.json（原子寫）。"""

    version: int = 1
    session: str
    round: int = 0  # 已完成決策嘅回合數（下一回合 = round + 1）
    max_rounds: int = 10
    min_rounds_to_finish: int = 2
    status: UIStatus = UIStatus.IDLE
    sim_step_cursor: int = 0  # 已模擬總 step 數
    steps_per_round: int = 6  # 對齊 SimulateServer 預設
    stride: int = 10  # 分鐘 / step
    agents: list[str] = Field(default_factory=list)
    control_owner: Optional[str] = None  # 持有控制權嘅 client_id
    control_lease_until: float = 0.0  # epoch 秒
    processed_steps: list[int] = Field(default_factory=list)  # 已壓縮嘅 checkpoint step
    error: Optional[str] = None
    updated_at: str = ""
