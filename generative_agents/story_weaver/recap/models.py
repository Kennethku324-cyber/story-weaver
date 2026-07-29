"""story_weaver.recap.models — 故事回顧數據模型（spec §3.1、§3.3）。

全部 dataclass（同代碼庫 Event/Concept 嘅 plain-class 風格一致）；
pydantic 只留俾 LLM 輸出校驗（見 prompts.py）。
對白 text 位元級保留，永不經 LLM 改寫。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

RecapStatus = Literal["ok", "fallback", "pending"]
EventType = Literal["action", "gm_note", "player_intervention"]
DecisionType = Literal["option", "custom"]
DialogueHealth = Literal["ok", "degraded", "missing"]
SimTime = str  # "%Y%m%d-%H:%M"，同 start.py timer.get_date 一致
ISOTime = str


@dataclass
class AgentProfile:
    """Setup 傳入嘅角色設定快照（init 時凝固；之後 GM 改動行 gm_note 事件）。"""

    name: str
    occupation: str = ""
    personality: str = ""
    relations: dict[str, str] = field(default_factory=dict)
    affinity: dict[str, int] = field(default_factory=dict)
    age: int = 0
    innate: str = ""
    learned: str = ""
    lifestyle: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentProfile":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class TimelineEvent:
    sim_time: SimTime
    agent: str  # gm_note 時為 "GM"；player_intervention 時為 "PLAYER"
    type: EventType
    location: str  # "，".join(address[1:])，對齊 compress.py get_location
    describe: str
    poignancy: int = 5
    step: int = 0  # 來自邊個 checkpoint step（resume 去重用）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TimelineEvent":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class DialogueLine:
    speaker: str
    text: str  # 對白原文，位元級保留，永不經 LLM

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DialogueLine":
        return cls(speaker=d["speaker"], text=d["text"])


@dataclass
class DialogueBlock:
    sim_time: SimTime  # conversation.json 嘅分鐘級 key
    participants: list[str]
    location: str  # " @ " 後段
    lines: list[DialogueLine] = field(default_factory=list)
    degraded: bool = False  # True = conversation.json 損毀時由記憶摘要重建（無原文）

    def to_dict(self) -> dict:
        d = asdict(self)
        d["lines"] = [l.to_dict() for l in self.lines]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "DialogueBlock":
        return cls(
            sim_time=d.get("sim_time", ""),
            participants=list(d.get("participants", [])),
            location=d.get("location", ""),
            lines=[DialogueLine.from_dict(l) for l in d.get("lines", [])],
            degraded=d.get("degraded", False),
        )


@dataclass
class PlayerDecision:
    type: DecisionType
    text: str  # 選項文本或自訂命令原文
    chosen_at: ISOTime
    round: int = 0  # 寫入時由 store 強制對齊 round_no

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerDecision":
        return cls(
            type=d.get("type", "option"),
            text=d.get("text", ""),
            chosen_at=d.get("chosen_at", ""),
            round=d.get("round", 0),
        )


@dataclass
class RoundRecap:
    round: int
    sim_time_start: SimTime
    sim_time_end: SimTime  # checkpoint 截斷時如實反映最後完整 step
    step_range: tuple[int, int] = (0, 0)  # [start_step, end_step]，含頭含尾
    events: list[TimelineEvent] = field(default_factory=list)
    dialogues: list[DialogueBlock] = field(default_factory=list)
    player_decision: Optional[PlayerDecision] = None  # 驅動本回合嘅上一回合決策
    round_recap: str = ""
    recap_status: RecapStatus = "pending"
    dialogue_health: DialogueHealth = "ok"
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "round": self.round,
            "sim_time_start": self.sim_time_start,
            "sim_time_end": self.sim_time_end,
            "step_range": list(self.step_range),
            "events": [e.to_dict() for e in self.events],
            "dialogues": [d.to_dict() for d in self.dialogues],
            "player_decision": self.player_decision.to_dict() if self.player_decision else None,
            "round_recap": self.round_recap,
            "recap_status": self.recap_status,
            "dialogue_health": self.dialogue_health,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RoundRecap":
        pd = d.get("player_decision")
        return cls(
            round=d.get("round", 0),
            sim_time_start=d.get("sim_time_start", ""),
            sim_time_end=d.get("sim_time_end", ""),
            step_range=tuple(d.get("step_range", (0, 0))),
            events=[TimelineEvent.from_dict(e) for e in d.get("events", [])],
            dialogues=[DialogueBlock.from_dict(b) for b in d.get("dialogues", [])],
            player_decision=PlayerDecision.from_dict(pd) if pd else None,
            round_recap=d.get("round_recap", ""),
            recap_status=d.get("recap_status", "pending"),
            dialogue_health=d.get("dialogue_health", "ok"),
            warnings=list(d.get("warnings", [])),
        )


@dataclass
class CumulativeRecap:
    text: str = ""
    generated_at_round: int = 0
    status: RecapStatus = "pending"
    model: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CumulativeRecap":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class StoryRecap:
    sim_name: str
    opening: str
    created_at: ISOTime
    agents: list[AgentProfile] = field(default_factory=list)
    rounds: list[RoundRecap] = field(default_factory=list)
    cumulative_recap: CumulativeRecap = field(default_factory=CumulativeRecap)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "sim_name": self.sim_name,
            "opening": self.opening,
            "created_at": self.created_at,
            "agents": [a.to_dict() for a in self.agents],
            "rounds": [r.to_dict() for r in self.rounds],
            "cumulative_recap": self.cumulative_recap.to_dict(),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StoryRecap":
        return cls(
            sim_name=d.get("sim_name", ""),
            opening=d.get("opening", ""),
            created_at=d.get("created_at", ""),
            agents=[AgentProfile.from_dict(a) for a in d.get("agents", [])],
            rounds=[RoundRecap.from_dict(r) for r in d.get("rounds", [])],
            cumulative_recap=CumulativeRecap.from_dict(d.get("cumulative_recap", {})),
            schema_version=d.get("schema_version", 1),
        )


@dataclass
class GMContext:
    """俾 GM prompt 嘅壓縮時間線（spec §3.3）。結構穩定，加欄位只加唔改。"""

    sim_name: str
    opening: str
    round_count: int
    round_summaries: list[dict]  # [{"round": 1, "recap": "...", "status": "ok"}, ...]
    latest_round: Optional[RoundRecap]
    agents: list[AgentProfile]
    generated_at: ISOTime

    def to_dict(self) -> dict:
        return {
            "sim_name": self.sim_name,
            "opening": self.opening,
            "round_count": self.round_count,
            "round_summaries": list(self.round_summaries),
            "latest_round": self.latest_round.to_dict() if self.latest_round else None,
            "agents": [a.to_dict() for a in self.agents],
            "generated_at": self.generated_at,
        }
