"""邏輯判斷關鍵字常量 — 繁體香港書面語 SSoT。

所有以中文字串做邏輯判斷嘅代碼必須引用呢度嘅常量，
唔准 hardcode。改語言時只改呢個檔。
"""

# --- 事件三元組（Event.predicate / Event.object）---
KW_AT_THIS_TIME: str = "此時"      # Event 預設 predicate；agent.py:326-327、scratch.py:370-373 前綴剝離
KW_IDLE: str = "空閒"              # Event 預設 object；agent.py:295/461/642、scratch.py:408
KW_ONGOING: str = "正在"           # agent.py:118/669
KW_PENDING: str = "待開始"          # agent.py:491
KW_CHAT: str = "對話"              # agent.py:301/509/624；associate.py:221 檢索 query 前綴
KW_OCCUPIED: str = "被佔用"         # agent.py:121

# --- 睡眠相關 ---
KW_SLEEP: str = "睡"               # 子串判斷：agent.py:111、schedule.py:80-88（簡繁同形，勿改）
KW_SLEEPING: str = "睡覺"          # 地址鍵／事件 object：agent.py:113/118/208/489/669、spatial.py:12-14
KW_BED: str = "床"                 # 子串判斷：schedule.py:80-88；地址尾段：spatial.py:14（簡繁同形，勿改）
KW_SLEEPING_EN: str = "sleeping"   # 英文容錯：agent.py:111/489、spatial.py:12
KW_LIVING_AREA: str = "living_area"  # spatial.py:12 地址鍵（英文，不翻譯）

# --- Boolean 解析 ---
KW_TRUE_TOKENS: tuple[str, ...] = ("true", "yes", "是", "係", "1")
# scratch.py:447/473 嘅解析邏輯改用：str(response).strip().lower() in KW_TRUE_TOKENS
# 注意「是」「係」需喺 lower() 後比對，中文無大小寫，直接命中

# --- 模板尾部統一指令（29 個 prompt 共用）---
TRADITIONAL_CHINESE_DIRECTIVE: str = "一律使用繁體中文（香港書面語）回答。"
