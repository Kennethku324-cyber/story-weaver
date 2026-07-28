"""story_weaver.affinity.models — 好感度系統數據模型（spec §2）。

存儲層直接用 dict[str, dict[str, {"value": int, "label": str}]] 嘅 JSON 形態
（config 頂層 "affinity" key，同 checkpoint 共享引用）；進出時用 AffinityEntry 校驗。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field

AFFINITY_MIN, AFFINITY_MAX = -100, 100
DELTA_CLAMP = 25  # 每回合單次調整上限
LABEL_MAX_LEN = 100

# 數值 → 描述 band（spec §2.5），順序由高分到低分
BANDS: list[tuple[int, int, str]] = [
    (61, 100, "摯愛/至交"),
    (21, 60, "友好"),
    (1, 20, "略有好感"),
    (0, 0, "陌生/中立"),
    (-20, -1, "略有反感"),
    (-60, -21, "敵對"),
    (-100, -61, "死敵/痛恨"),
]


class AffinityEntry(BaseModel):
    value: int = Field(default=0, ge=AFFINITY_MIN, le=AFFINITY_MAX)
    label: str = Field(default="", max_length=LABEL_MAX_LEN)


@dataclass
class AffinityChange:
    """單次好感度變動（GM 調整 / 絕對重置）嘅記錄。"""

    from_agent: str
    to_agent: str
    old: int
    new: int
    delta: int  # new - old（absolute 重置時都係實際差值）
    reason: str
    absolute: bool = False  # True = GM 敍事重置（set_absolute）
    round: int = 0  # 第幾回合（由 GM 系統填入）
    time: str = ""  # utils.get_timer().get_date("%Y%m%d-%H:%M")

    def to_dict(self) -> dict:
        return asdict(self)


class RelationInput(BaseModel):
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    # 超界由前端 clamp；後端 pydantic 拒收
    affinity: int = Field(ge=AFFINITY_MIN, le=AFFINITY_MAX)
    label: str = Field(default="", max_length=LABEL_MAX_LEN)

    model_config = {"populate_by_name": True}


class SetupAffinityPayload(BaseModel):
    agents: list[str] = Field(min_length=4)  # 已選角色白名單
    relations: list[RelationInput] = []


class SetupAffinityResult(BaseModel):
    # 補齊後嘅完整矩陣，可直接做 config["affinity"]
    affinity: dict[str, dict[str, AffinityEntry]]


class SetupErrorItem(BaseModel):
    """單格校驗錯誤，Setup 層映射返 UI 格仔用。"""

    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    message: str

    model_config = {"populate_by_name": True}
