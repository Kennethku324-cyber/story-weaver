"""story_weaver.affinity.store — AffinityStore：集中式雙向好感度矩陣。

持有 config["affinity"] dict 嘅引用，所有寫入都係 in-place 修改，
令 start.py 每 step dump config 時自動係最新值（零同步代碼）。
"""

from __future__ import annotations

import logging

from .models import (
    AFFINITY_MAX,
    AFFINITY_MIN,
    BANDS,
    DELTA_CLAMP,
    LABEL_MAX_LEN,
    AffinityChange,
    AffinityEntry,
    RelationInput,
    SetupAffinityPayload,
    SetupAffinityResult,
    SetupErrorItem,
)

logger = logging.getLogger(__name__)


def _clamp(value: int) -> int:
    return max(AFFINITY_MIN, min(AFFINITY_MAX, int(value)))


class UnknownAgentError(Exception):
    """角色唔喺白名單入面（Setup 層捕獲轉 400）。"""


class SetupValidationError(Exception):
    """Setup 校驗失敗，errors 逐格指出，絕唔靜默丟棄。"""

    def __init__(self, errors: list[SetupErrorItem]) -> None:
        super().__init__("; ".join(e.message for e in errors))
        self.errors = errors


class AffinityStore:
    """集中式雙向好感度矩陣。持有 config["affinity"] dict 嘅引用，in-place 修改。"""

    def __init__(self, data: dict, agent_names: list[str]) -> None:
        # data 即 config.setdefault("affinity", {})；舊 checkpoint 傳入 {} 都唔會出事
        self._data = data
        self._agents = list(agent_names)
        self.ensure_pairs()

    def ensure_pairs(self) -> None:
        """補齊所有缺漏有序對為 {value: 0, label: ""}；清理唔喺 agent_names 嘅殘留 key。"""
        known = set(self._agents)
        for a in list(self._data.keys()):
            if a not in known:
                logger.warning("affinity: 移除未知角色「%s」嘅殘留關係", a)
                del self._data[a]
                continue
            for b in list(self._data[a].keys()):
                if b not in known or b == a:
                    logger.warning("affinity: 移除「%s」對未知角色「%s」嘅殘留關係", a, b)
                    del self._data[a][b]
        for a in self._agents:
            row = self._data.setdefault(a, {})
            for b in self._agents:
                if a == b:
                    continue
                row.setdefault(b, {"value": 0, "label": ""})

    def get(self, from_agent: str, to_agent: str) -> AffinityEntry:
        """未知角色對返回 AffinityEntry(value=0, label="")，唔 raise。"""
        raw = self._data.get(from_agent, {}).get(to_agent)
        if not isinstance(raw, dict):
            return AffinityEntry()
        try:
            return AffinityEntry.model_validate(raw)
        except Exception:
            logger.warning("affinity: 「%s」對「%s」嘅數據異常，當 0 處理", from_agent, to_agent)
            return AffinityEntry()

    def set_affinity(self, from_agent: str, to_agent: str, value: int, label: str = "") -> None:
        """Setup 寫入接口。白名單校驗 + clamp [-100, 100] + label 截 100 字。"""
        if from_agent not in self._agents or to_agent not in self._agents:
            raise UnknownAgentError(f"角色「{from_agent}」或「{to_agent}」唔喺已選角色名單入面")
        self._data.setdefault(from_agent, {})[to_agent] = {
            "value": _clamp(value),
            "label": (label or "")[:LABEL_MAX_LEN],
        }

    def adjust(
        self,
        from_agent: str,
        to_agent: str,
        delta: int,
        reason: str,
        absolute: bool = False,
        absolute_value: int | None = None,
    ) -> AffinityChange | None:
        """GM 調整接口。delta clamp [-25, +25]；absolute 重置唔受限。delta 為 0 返回 None。"""
        if from_agent not in self._agents or to_agent not in self._agents:
            raise UnknownAgentError(f"角色「{from_agent}」或「{to_agent}」唔喺已選角色名單入面")
        old = self.get(from_agent, to_agent).value
        if absolute:
            new = _clamp(absolute_value if absolute_value is not None else old)
        else:
            new = _clamp(old + max(-DELTA_CLAMP, min(DELTA_CLAMP, int(delta))))
        actual = new - old
        if actual == 0:
            return None
        entry = self._data.setdefault(from_agent, {}).setdefault(
            to_agent, {"value": 0, "label": ""}
        )
        entry["value"] = new
        return AffinityChange(
            from_agent=from_agent,
            to_agent=to_agent,
            old=old,
            new=new,
            delta=actual,
            reason=reason or "",
            absolute=absolute,
        )

    def relation_line(self, from_agent: str, to_agent: str) -> str:
        """繁中一句描述（spec §2.5 模板），prompt 注入用。任何異常返回 "" 唔 raise。"""
        try:
            entry = self.get(from_agent, to_agent)
            band = self.band_of(entry.value)
            if entry.value == 0:
                return f"「{from_agent}與{to_agent}並不相識（陌生/中立）。」"
            if entry.label:
                return (
                    f"「{from_agent}對{to_agent}的好感度為{entry.value}"
                    f"（{band}）：{entry.label}。」"
                )
            return f"「{from_agent}對{to_agent}的好感度為{entry.value}（{band}）。」"
        except Exception:
            logger.exception("affinity: relation_line 失敗（%s → %s）", from_agent, to_agent)
            return ""

    @staticmethod
    def band_of(value: int) -> str:
        for lo, hi, label in BANDS:
            if lo <= value <= hi:
                return label
        return BANDS[3][2]  # 陌生/中立（理論上到唔到）

    def to_dict(self) -> dict:
        """返回持有嘅 dict 本身（唔係 copy）——checkpoint 共享引用嘅關鍵。"""
        return self._data

    @property
    def agent_names(self) -> list[str]:
        return list(self._agents)

    def full_matrix_text(self) -> str:
        """GM prompt 用：成個矩陣嘅人讀文字版，每行一條非對角關係。"""
        lines = []
        for a in self._agents:
            for b in self._agents:
                if a == b:
                    continue
                entry = self.get(a, b)
                band = self.band_of(entry.value)
                line = f"- {a} 對 {b}：{entry.value}（{band}）"
                if entry.label:
                    line += f"：{entry.label}"
                lines.append(line)
        return "\n".join(lines)


def validate_setup(payload: SetupAffinityPayload) -> SetupAffinityResult:
    """校驗 Setup 關係輸入，通過後補齊所有缺漏有序對。失敗 raise SetupValidationError。"""
    agents = list(payload.agents)
    known = set(agents)
    errors: list[SetupErrorItem] = []
    matrix: dict[str, dict[str, dict]] = {}
    for rel in payload.relations:
        bad = False
        for name in (rel.from_agent, rel.to_agent):
            if name not in known:
                errors.append(
                    SetupErrorItem(
                        **{
                            "from": rel.from_agent,
                            "to": rel.to_agent,
                            "message": f"角色「{name}」唔喺已選角色名單入面",
                        }
                    )
                )
                bad = True
        if bad:
            continue
        if rel.from_agent == rel.to_agent:
            errors.append(
                SetupErrorItem(
                    **{
                        "from": rel.from_agent,
                        "to": rel.to_agent,
                        "message": "唔可以設定角色對自己嘅關係",
                    }
                )
            )
            continue
        # 同一 (from, to) 重複提交 → 後者覆蓋前者（視為玩家改咗主意）
        matrix.setdefault(rel.from_agent, {})[rel.to_agent] = {
            "value": rel.affinity,
            "label": rel.label,
        }
    if errors:
        raise SetupValidationError(errors)
    for a in agents:
        row = matrix.setdefault(a, {})
        for b in agents:
            if a != b:
                row.setdefault(b, {"value": 0, "label": ""})
    return SetupAffinityResult.model_validate({"affinity": matrix})


def build_matrix_from_setup(agent_names: list[str], rel_map: dict) -> dict:
    """由 Setup builder 嘅 rel_map（{from: {to: {"score", "desc"}}}，已 clamp）
    產生 config 頂層 affinity 矩陣（plain dict）。重用 validate_setup 嘅校驗同補齊邏輯。
    """
    relations = [
        RelationInput(
            **{"from": a, "to": b, "affinity": rel["score"], "label": rel.get("desc", "")}
        )
        for a, targets in rel_map.items()
        for b, rel in targets.items()
        if rel["score"] != 0 or rel.get("desc")
    ]
    result = validate_setup(SetupAffinityPayload(agents=list(agent_names), relations=relations))
    return result.model_dump()["affinity"]
