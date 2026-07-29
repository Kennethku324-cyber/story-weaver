"""story_weaver.gm.injector — MemoryInjector：玩家意志 → agents 記憶流（spec §2.2、§1.5）。

行 `Agent._add_concept("event", ..., poignancy_override=...)`（agent.py:647，
跳過 LLM poignancy 評分），唔會被 1-10 隨機評分淹沒玩家意志。

describe 規範（違反 → ValueError，上層捕獲記 log）：
- 必須係完整、自包含、繁體書面語嘅第三人稱句子（embedding 質量 = 檢索質量）；
- 不得包含 keywords.py 保留字（睡覺/對話/空閒/待開始）——佢哋係 agent.py
  邏輯判斷依賴，混入記憶會令 LLM 生成嘅後續事件誤觸硬編碼分支。
"""

from __future__ import annotations

import logging

from modules.memory import Event
from modules.prompt.keywords import KW_CHAT, KW_IDLE, KW_PENDING, KW_SLEEPING

logger = logging.getLogger(__name__)

INJECT_OBJECT = "命運的提示"  # Event object 顯式非空值，避開 KW_IDLE fallback
PREDICATE_OPTION = "得知"
PREDICATE_CUSTOM = "被命運驅使"


class MemoryInjector:
    # 直接引用 keywords.py 常量（本地化 SSoT），唔自行 hardcode
    FORBIDDEN_TOKENS: tuple[str, ...] = (KW_SLEEPING, KW_CHAT, KW_IDLE, KW_PENDING)

    def validate_describe(self, describe: str) -> None:
        """命中禁詞或空字串 → ValueError。"""
        if not describe or not describe.strip():
            raise ValueError("注入 describe 唔可以係空字串")
        for token in self.FORBIDDEN_TOKENS:
            if token in describe:
                raise ValueError(f"注入 describe 命中保留字「{token}」：{describe[:50]}…")

    def inject(
        self,
        agent,
        describe: str,
        predicate: str,
        poignancy: int,
        poignancy_boost: int = 20,
    ) -> str:
        """注入一條 event concept + status["poignancy"] 助推，返 node_id。

        agent 瞓覺都照注（邊界 8）——記憶唔係行動，醒返自然檢索到。
        describe 違規拋 ValueError（上層捕獲並記 log，唔會 throw 出回合流程）。
        """
        self.validate_describe(describe)
        concept = agent._add_concept(
            "event",
            Event(
                subject=agent.name,
                predicate=predicate,
                object=INJECT_OBJECT,
                describe=describe,
            ),
            poignancy_override=poignancy,
        )
        try:
            agent.status["poignancy"] = agent.status.get("poignancy", 0) + poignancy_boost
        except Exception:
            logger.warning("gm injector: poignancy boost 失敗（%s）", agent.name, exc_info=True)
        node_id = getattr(concept, "node_id", "") or ""
        logger.info(
            "gm injector: 已注入「%s」→ %s（poignancy=%d, node=%s）",
            agent.name, describe[:30], poignancy, node_id,
        )
        return node_id
