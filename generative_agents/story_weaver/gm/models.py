"""story_weaver.gm.models — GM/敘事總監系統數據模型（spec §3.1、§3.2）。

LLM structured output 模型（res 包裝慣例，同 modules/prompt/scratch.py）+
持久化 / API 模型。繁體 description 會進 LLM schema，唔好改簡體。

好感度變動記錄直接復用 story_weaver.affinity.models.AffinityChange（dataclass），
pydantic v2 原生支援 stdlib dataclass 做 field，唔重複定義。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from story_weaver.affinity.models import AffinityChange

__all__ = [
    "GMOption",
    "AffinitySuggestion",
    "GMRoundAnalysis",
    "GMRoundAnalysisResponse",
    "CustomCommandParse",
    "CustomCommandParseResponse",
    "FinaleNarrative",
    "FinaleNarrativeResponse",
    "DialogueBlock",
    "TimelineEntry",
    "PlayerChoice",
    "GMDecision",
    "InjectionRecord",
    "InjectionReport",
    "Finale",
    "AffinityChange",
]


# ---------------------------------------------------------------- LLM structured output（§3.1）


class GMOption(BaseModel):
    id: str = Field(description="選項代號，A/B/C")
    title: str = Field(description="選項標題，一句話，繁體中文，15字內")
    predicted: str = Field(description="預期走向，一句話，繁體中文，30字內")


class AffinitySuggestion(BaseModel):
    from_agent: str = Field(description="好感度來源角色名，必須在角色名單內")
    to_agent: str = Field(description="好感度目標角色名，必須在角色名單內")
    delta: int = Field(description="好感度變化，-30 到 +30 之間的整數")
    reason: str = Field(description="變化原因，一句話，繁體中文")


class GMRoundAnalysis(BaseModel):
    """gm_round_summary.txt 的 structured output（一次 LLM call 完成摘要+分支+選項）。"""

    summary: str = Field(description="本回合摘要，3-5 句繁體中文敘事")
    branch_point: str = Field(description="本回合最重要的劇情分支點，一句話")
    options: list[GMOption] = Field(description="2-3 個分支選項", min_length=2, max_length=3)
    suggested_affinity_changes: list[AffinitySuggestion] = Field(
        default_factory=list, description="好感度建議變動，可為空列表"
    )


class GMRoundAnalysisResponse(BaseModel):  # 沿用 scratch.py 的 res 包裝慣例
    res: GMRoundAnalysis


class CustomCommandParse(BaseModel):
    targets: list[str] = Field(
        default_factory=list, description="命令涉及的角色名，必須在角色名單內，可為空"
    )
    command_event_describe: str = Field(
        default="", description="改寫為第三人稱完整事件描述，繁體書面語"
    )
    feasible: bool = Field(description="命令是否可執行（角色存在、語義明確、內容恰當）")
    refuse_reason: Optional[str] = Field(
        default=None, description="不可執行時的拒絕理由，繁體中文，可執行時為 null"
    )


class CustomCommandParseResponse(BaseModel):
    res: CustomCommandParse


class FinaleNarrative(BaseModel):
    ending: str = Field(description="故事終章敘事，繁體中文，200-400字")
    character_epilogues: list[dict] = Field(
        default_factory=list, description="每個角色的結局一段話：[{name, epilogue}]"
    )


class FinaleNarrativeResponse(BaseModel):
    res: FinaleNarrative


# ---------------------------------------------------------------- 持久化 / API（§3.2）


class DialogueBlock(BaseModel):
    speakers: str  # "梅 -> 約翰"（conversation.json 原 key 嘅 speakers 部分）
    address: str
    lines: list[tuple[str, str]] = Field(default_factory=list)  # [[名, 對白原文], ...]，不經 LLM 改寫


class TimelineEntry(BaseModel):
    round: int  # 0 = story_seed
    summary: str = ""
    key_events: list[str] = Field(default_factory=list)
    dialogues: list[DialogueBlock] = Field(default_factory=list)
    branch_point: Optional[str] = None
    options_offered: list[GMOption] = Field(default_factory=list)
    player_choice: Optional["PlayerChoice"] = None
    affinity_changes: list[AffinityChange] = Field(default_factory=list)
    is_quiet: bool = False  # 邊界 9 標記
    had_error: bool = False  # 邊界 2/4 標記


class PlayerChoice(BaseModel):
    type: Literal["option", "custom", "option+custom", "skip", "finish"]
    option_id: Optional[str] = None  # "A" | "B" | "C"
    text: Optional[str] = None  # 自訂命令原文
    affinity_overrides: list[AffinitySuggestion] = Field(default_factory=list)  # 玩家手動 slider 值（delta 語義）


class GMDecision(BaseModel):
    """on_round_end 回傳 + pending_decision 持久化 + /api/round/start 回應。"""

    round_no: int
    summary: str = ""
    branch_point: Optional[str] = None  # 靜默回合 / failsafe 時為 None
    options: list[GMOption] = Field(default_factory=list)  # 靜默回合 / failsafe 時為空 list
    suggested_affinity_changes: list[AffinitySuggestion] = Field(default_factory=list)
    story_timeline: list[TimelineEntry] = Field(default_factory=list)  # 故事回顧完整內容（含 story_seed 第 0 項）
    is_failsafe: bool = False  # 邊界 2：前端顯示「命運之線暫時模糊…」
    is_quiet: bool = False  # 邊界 9：前端顯示「平靜的一日」
    affinity_snapshot: dict = Field(default_factory=dict)  # store.to_dict() 原樣（{from: {to: {value, label}}}）
    can_finish: bool = False  # round_no >= min_rounds_to_finish


class InjectionRecord(BaseModel):
    round: int
    targets: list[str] = Field(default_factory=list)
    content: str = ""
    node_ids: dict[str, str] = Field(default_factory=dict)  # agent_name -> node_id
    poignancy: int = 0
    source: Literal["option", "custom"]
    error: Optional[str] = None


class InjectionReport(BaseModel):
    """POST /api/round/decide 回應。"""

    ok: bool
    message: str = ""  # 「你的意志已注入小鎮」等繁體文案
    injected: list[InjectionRecord] = Field(default_factory=list)
    affinity_changes: list[AffinityChange] = Field(default_factory=list)
    refused: Optional[CustomCommandParse] = None  # feasible=false 時填入，ok=false


class Finale(BaseModel):
    timeline: list[TimelineEntry] = Field(default_factory=list)
    narrative: FinaleNarrative
    affinity_table: list[AffinityChange] = Field(default_factory=list)  # 全劇好感度變化總表（初值→終值）
