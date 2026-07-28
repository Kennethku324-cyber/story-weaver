# 技術 Spec：繁體中文化（Localization）

> 版本 v1.0 · 2026-07-28 · 對應 PRD：`docs/prd/localization.md`（繁體中文化 v1.0）
> 代碼根目錄：`/Users/kenneth/Projects/story-weaver`（Python 3.12 + Flask，venv `.venv`）
> 本文所有行號已對照實際代碼驗證（2026-07-28）。

---

## 1. 總覽

本系統將 Story Weaver 全鏈路（29 個 prompt 模板 → `scratch.py` 內嵌字串 → 邏輯關鍵字 → maze／agent 地名 → 前端文案）由簡體轉為繁體香港書面語，同時保證以中文字串做邏輯判斷嘅硬耦合唔斷裂。

核心設計三支柱：

1. **關鍵字常量化** — 所有邏輯判斷字串收斂入單一模組 `keywords.py`，代碼內零 hardcode 中文判斷字串。
2. **LLM 輸出 normalize 層** — 喺 `LLMModel.completion()` 統一出口做 s2hk 標準化，係防禦 LLM 輸出簡體嘅最後防線。
3. **腳本化原子轉換** — maze.json + 25 個 agent.json + 角色目錄 + personas 列表由腳本單次 transaction 轉換，禁止手改。

---

## 2. 架構決策

### 2.1 新代碼放邊

**決策：唔開新頂層目錄，直接喺 `generative_agents/modules/` 內新增兩個檔案＋一個 `scripts/localization/` 腳本目錄。**

| 新增 | 路徑 | 理由 |
|---|---|---|
| 關鍵字常量 | `generative_agents/modules/prompt/keywords.py` | 所有消費者（`agent.py`、`event.py`、`schedule.py`、`spatial.py`、`associate.py`、`scratch.py`）都喺 `modules/` 內，放 `modules/prompt/` 同 `scratch.py` 同層，import 路徑最短（`from .keywords import ...` / `from ..prompt.keywords import ...`）。開新頂層 package 反而要郁 `sys.path` 或相對 import 層級。 |
| Normalize 層 | `generative_agents/modules/model/text_normalize.py` | Normalize 嘅唯一注入點係 `llm_model.py` 嘅 `completion()`，放同目錄 `modules/model/` 最內聚；以純函數暴露，其他系統（GM、記憶注入）都可以 `from ..model.text_normalize import normalize_text` 重用。 |
| 轉換／驗證腳本 | `scripts/localization/*.py`（新建頂層 `scripts/`） | 一次性工具唔屬於 runtime package；同 `generative_agents/` 分開，CI 直接 `python scripts/localization/validate_addresses.py`。 |
| Glossary | `docs/prd/localization-glossary.md`（人讀）＋ `generative_agents/data/glossary_s2hk.json`（機讀） | Markdown 係畀人 review 嘅 SSoT 文件；JSON 係腳本同 normalize fallback dict 嘅數據源，由 MD 生成（單向，腳本 `build_glossary_json.py`），避免兩份手維護漂移。 |
| 測試 | `tests/localization/*.py`（新建頂層 `tests/`） | 項目現時無 tests 目錄，本系統順手建立；用 pytest。 |

**唔採用 `story_weaver/` 新目錄嘅理由**：本系統 90% 工作係改現有檔案嘅字串，新運行時代碼只有兩個細模塊；開平行目錄會製造「邊啲嘢喺邊」嘅認知負擔，違反最小侵入原則。

### 2.2 Normalize 技術選型

**決策：主路徑用 `opencc-python-reimplemented`（純 Python，無原生編譯，`s2hk` config），fallback 用自建 dict（glossary JSON）。**

- 官方 `opencc` 需要 C++ 編譯，macOS 上安裝唔穩定；`opencc-python-reimplemented` API 兼容（`OpenCC('s2hk').convert(text)`），加入 `requirements.txt`。
- Normalize 層唔依賴 OpenCC 一定得：若 import 失敗，退回 glossary dict 嘅逐詞替換＋常見簡繁字對表，功能降級但唔會 crash（LLM 輸出路徑係熱路徑，唔可以因為缺 dependency 而斷）。
- 只喺 **LLM 輸出** 同 **checkpoint 遷移** 用呢個層；玩家輸入**永不**經過 normalize（PRD 邊界 8：原樣注入）。

### 2.3 關鍵字處理原則

- 簡繁同形字（「睡」「床」「是」「正在」）：**唔改字形，照樣入 constants**，並加回歸測試鎖定「LLM 輸出繁體『睡覺』時 `KW_SLEEP in describe` 仍然命中」。
- 簡繁異形字（对话→對話、睡觉→睡覺、空闲→空閒、此时→此時、待开始→待開始、被占用→被佔用）：全部改繁體，所有引用點同一 commit 切換。
- `"living_area"`、`"sleeping"` 呢啲**英文鍵**保留唔郁（`spatial.py:12`、`agent.py:111` 嘅 `"sleeping"` 容錯分支保留，係對 LLM 輸出英文嘅兼容）。
- Boolean 解析 `("true","yes","是","1")`：保留「是」，新增「係」容錯（PRD Done When 明確要求）。

### 2.4 地名原子轉換

maze.json 嘅地址存喺 `tiles[].address` 陣列（已驗證：頂層 keys 係 `world/tile_size/size/map/camera/tile_address_keys/tiles`，`tile_address_keys = ["world","sector","arena","game_object"]`，約 100 個唯一地名）；25 個 agent.json 嘅 `spatial.address` / `spatial.tree` 以字串精確引用。轉換腳本一次過寫 26 個檔案（1 maze + 25 agents），先寫 temp dir、驗證通過先 atomic move，失敗全部 rollback。

### 2.5 角色名只轉字形

「伊莎贝拉」→「伊莎貝拉」：音譯不變，只轉字形。`name` 欄位、目錄名、`start.py:12-17` personas 列表、`main_script.html` 動態路徑（`"static/assets/village/agents/" + p + "/texture.png"`）四處由同一個 mapping（glossary JSON 嘅 `agent_names` 段）生成，腳本執行，唔准手改。

---

## 3. 公開 API

### 3.1 `generative_agents/modules/prompt/keywords.py`（新建）

```python
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
```

### 3.2 `generative_agents/modules/model/text_normalize.py`（新建）

```python
"""LLM 輸出繁體標準化層 — 防禦 LLM 輸出簡體嘅最後防線。"""
from __future__ import annotations
from typing import Any

def normalize_text(text: str) -> str:
    """將字串中嘅簡體中文轉為繁體香港字形（s2hk）。

    - 主路徑：OpenCC('s2hk')（惰性初始化，模塊級單例）
    - Fallback：glossary dict 逐詞替換 + 內置高頻簡繁字對表
    - 空串／純英文／純數字原樣返回（fast path：無 CJK 字符直接 return）
    - 永不拋異常：任何內部錯誤 log warning 後返回原文
    """

def normalize_llm_output(output: Any, return_type: type | None = None) -> Any:
    """對 LLMModel._completion 嘅輸出做遞歸 normalize。

    - str → normalize_text
    - list / tuple → 逐項遞歸（保持容器型別；pydantic 驗證後嘅 Tuple 變 list 都接受）
    - dict → 只 normalize value，**key 不郁**（JSON schema 嘅 key 如 "6:00" 唔係自然語言）
    - 其他型別（int/bool/None）原樣返回
    喺 LLMModel.completion() 內、callback 之前調用。
    """

def contains_simplified(text: str) -> bool:
    """黑名單掃描：text 含常見簡體字（约/贝/见/话/觉/间/时/占/闲…約 200 字表）即返回 True。
    供 checkpoint 載入 warning 同端到端冒煙測試用。"""

def build_fallback_dict(glossary_path: str = "data/glossary_s2hk.json") -> dict[str, str]:
    """由機讀 glossary JSON 建立 fallback 替換表。"""
```

### 3.3 腳本 CLI（`scripts/localization/`）

全部腳本用 `argparse`，exit code 0 = 成功，非 0 = 失敗（CI 可用）。

```python
# convert_assets.py — maze + agents + 目錄 + personas 原子轉換
# usage: python scripts/localization/convert_assets.py --glossary generative_agents/data/glossary_s2hk.json --dry-run
def main() -> int
# 流程：load glossary → 建 mapping → 轉 maze.json tiles[].address →
#       轉 25 個 agent.json（spatial.address / spatial.tree / name / currently / scratch.*）→
#       驗證（每個 agent tree 地名喺新 maze 搵到）→ temp dir 寫晒 → atomic rename →
#       git mv 角色目錄 → 更新 start.py personas 列表
# --dry-run：只輸出 diff 報告，唔寫檔

# validate_addresses.py — CI 檢查
# usage: python scripts/localization/validate_addresses.py
def validate() -> list[str]  # 返回錯誤列表，空 = 通過
# 檢查：每個 agent.json 嘅 spatial.tree 葉路徑、spatial.address["living_area"]、
#       spatial.py 推導出嘅「睡覺」地址，全部喺 maze.json tiles[].address 搵到

# migrate_checkpoint.py — 舊 checkpoint 簡→繁遷移
# usage: python scripts/localization/migrate_checkpoint.py results/checkpoints/<name> [--in-place]
def migrate(checkpoint_dir: str, in_place: bool = False) -> MigrateReport
# 對 simulate-*.json + conversation.json 內所有 describe/address/scratch/event 文字跑
# normalize_text；預設寫去 <name>-zhHK/，--in-place 先覆寫（會先備份 .bak）

# convert_prompts.py — 輔助生成 29 個繁體模板初稿（人工潤飾前嘅機轉底稿）
# usage: python scripts/localization/convert_prompts.py --check
# --check 模式（CI）：驗證每個模板 (a) 無簡體黑名單字 (b) ${} 佔位符集契約不變
# (c) 尾部含 TRADITIONAL_CHINESE_DIRECTIVE (d) 內嵌 JSON 示例段 json.loads 可解析

# scan_simplified.py — 全 repo 簡體掃描（CI gate）
# usage: python scripts/localization/scan_simplified.py
# 掃描範圍：data/prompts/*.txt、modules/（豁免 keywords.py 註釋同 timer.py 白名單）、
#           frontend/templates/、frontend/static/assets/village/*.json

# build_glossary_json.py — 由 docs/prd/localization-glossary.md 生成機讀 JSON（單向）
```

### 3.4 Flask routes

**本系統不新增任何 Flask route。** 理由：本地化係 build-time／data-time 關注點，runtime 無需動態切換語言（已定決策：只有繁體一種）。`replay.py` 唯一相關改動係 templates 文案（見 §5）。Checkpoint 遷移係 CLI 操作，唔經 HTTP。

如日後 Setup 系統需要喺瀏覽器觸發地址驗證，由 Setup 系統自己加 route 並調用 `validate_addresses.validate()` 函數（§6 契約）。

---

## 4. 數據模型

### 4.1 機讀 Glossary（`generative_agents/data/glossary_s2hk.json`）

```json
{
  "version": "1.0",
  "generated_from": "docs/prd/localization-glossary.md",
  "keywords": {
    "对话": "對話",
    "睡觉": "睡覺",
    "空闲": "空閒",
    "此时": "此時",
    "待开始": "待開始",
    "被占用": "被佔用"
  },
  "place_names": {
    "约翰逊公园": "約翰遜公園",
    "霍布斯咖啡馆": "霍布斯咖啡館",
    "咖啡馆顾客座位": "咖啡館顧客座位"
  },
  "agent_names": {
    "伊莎贝拉": "伊莎貝拉",
    "克劳斯": "克勞斯"
  },
  "vocabulary": {
    "小睡一会儿": "小睡片刻",
    "视频": "影片",
    "软件": "軟件"
  },
  "protected_tokens": ["${", "living_area", "sleeping", "the Ville"]
}
```

- `place_names`：全量覆蓋 maze.json `tiles[].address` 出現嘅 ~100 個唯一地名（已用腳本核實數量 = 100）。
- `agent_names`：25 個角色，只轉字形。
- `protected_tokens`：轉換時唔准觸碰嘅 token（佔位符前綴、英文鍵、world 名）。
- 人讀版 `docs/prd/localization-glossary.md` 用表格呈現同樣四段，`build_glossary_json.py` 單向生成此 JSON。

### 4.2 腳本報告（dataclass）

```python
# scripts/localization/_report.py
from dataclasses import dataclass, field

@dataclass
class ConvertReport:
    files_scanned: int = 0
    files_changed: int = 0
    replacements: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

@dataclass
class MigrateReport:
    checkpoint_dir: str = ""
    output_dir: str = ""
    files_migrated: int = 0
    concepts_normalized: int = 0   # Concept.describe / address 被改動嘅數量
    warnings: list[str] = field(default_factory=list)
```

### 4.3 內部狀態變更

| 既有結構 | 變更 |
|---|---|
| `Event`（`memory/event.py`） | 無 schema 變更；預設值 `"此时"/"空闲"` → `KW_AT_THIS_TIME`/`KW_IDLE`（值為「此時」「空閒」）。`to_dict()`／`fit()` 簽名不變。 |
| `Concept`（`memory/associate.py`） | 無 schema 變更；`node_type` 繼續用英文 `"event"/"chat"/"thought"`（PRD 明確：不受語言影響）。 |
| maze.json | 結構不變；`tiles[].address` 字串值轉繁體。`tile_address_keys` 係英文，不郁。 |
| agent.json | 結構不變；`name`/`currently`/`scratch.{innate,learned,lifestyle,daily_plan}`/`spatial.*` 值轉繁體；目錄名同步。 |
| checkpoint（simulate-*.json） | 結構不變；遷移脚本改文字值。檔名沿用時間戳格式，唔加語言後綴（新跑嘅 checkpoint 天然繁體）。 |

---

## 5. 整合點（具體檔案＋行號）

行號已對照 2026-07-28 嘅代碼。原則：**每處只改字串／改引用常量，唔改邏輯結構**。

### 5.1 `generative_agents/modules/prompt/keywords.py`（新建，見 §3.1）

### 5.2 `generative_agents/modules/model/text_normalize.py`（新建，見 §3.2）

### 5.3 `generative_agents/modules/model/llm_model.py`

| 位置 | 改動 |
|---|---|
| `LLMModel.completion()`（line 24-54）retry 迴圈內，`output = self._completion(...)` 之後、`callback` 之前 | 插入一行：`output = normalize_llm_output(output, return_type)`（`from .text_normalize import normalize_llm_output`）。**呢個係全系統唯一 normalize 注入點**，OllamaLLMModel 同 OpenAILLMModel 都經呢度，唔使逐個子類改。 |
| line 141 註釋（「从输出结果中过滤掉…」） | 註釋可保留簡體（PRD：代碼註釋可保留），唔郁。 |

注意：normalize 放 `callback` **之前**——`scratch.py` 嘅 callback（如 boolean 解析、三元組驗證）收到嘅必須係已標準化文字。

### 5.4 `generative_agents/modules/agent.py`（最高風險檔案，逐行）

| 行 | 現狀 | 改為 |
|---|---|---|
| 111 | `"睡" in plan["describe"]` | `KW_SLEEP in plan["describe"]`（值不變，只常量化；`"sleeping"` 容錯保留） |
| 113 | `find_address("睡觉", ...)` | `find_address(KW_SLEEPING, ...)` |
| 118 | `Event(self.name, "正在", "睡觉", ...)` | `Event(self.name, KW_ONGOING, KW_SLEEPING, ...)` |
| 121 | `"被占用"` | `KW_OCCUPIED` |
| 188-189 | f-string「在…的计划。」「在…的生活中，重要的近期事件。」 | 轉繁體（「的」不變，其餘用字對照 glossary；呢啲係送入 prompt 嘅自然語言） |
| 208 | `seed = [(h, "睡觉") ...]` | `KW_SLEEPING` |
| 228 / 233 | 「这是 {} 在 {} 的计划：{}」「计划」 | 「呢度係…」唔用——書面語：「這是 {} 在 {} 的計劃：{}」「計劃」 |
| 295 | `event.object == "空闲"` | `event.object == KW_IDLE`（`"idle"` 英文容錯保留） |
| 301 | `event.fit(self.name, "对话")` | `event.fit(self.name, KW_CHAT)` |
| 326-327 | strip `"此时"` 前綴 | `KW_AT_THIS_TIME`（邏輯不變） |
| 380 | f"对于 {self.name} 的计划：{thought}" | 「對於 {self.name} 的計劃：{thought}」 |
| 461 | `ignore_words=["空闲"]` | `ignore_words=[KW_IDLE]` |
| 489 | `"睡觉" in event.get_describe(False)` | `KW_SLEEPING in ...`（`"sleeping"` 保留） |
| 491 | `event.predicate == "待开始"` | `KW_PENDING` |
| 509 | `fit(predicate="对话")` ×2 | `fit(predicate=KW_CHAT)` |
| 539-569 | 中文註釋（复读/话题） | 保留（註釋豁免） |
| 624 | `"对话"` | `KW_CHAT` |
| 642 | `event.fit(None, "此时", "空闲")` | `event.fit(None, KW_AT_THIS_TIME, KW_IDLE)` |
| 669 | `fit(self.name, "正在", "睡觉")` | `fit(self.name, KW_ONGOING, KW_SLEEPING)` |
| 頂部 | — | `from .prompt.keywords import (...)` |

### 5.5 `generative_agents/modules/prompt/scratch.py`（844 行，約 60+ 處）

| 行 | 改動 |
|---|---|
| 56, 71, 86, 108, 143, 197, 252, 301, 333, 354, 376, 399, 442, 468 | pydantic `Field(description=...)` 全部轉繁體（description 會隨 `model_json_schema()` 送去 LLM，直接影響輸出語言）。例：line 56「事件的情感强度评分，整数，范围1到10」→「事件的情感強度評分，整數，範圍1到10」 |
| 115-121, 128, 130 | failsafe 示例日程轉繁體：「小睡一会儿」→「小睡片刻」（書面語，唔係「小睡一會兒」）；「睡觉」→ KW_SLEEPING 字面值「睡覺」（failsafe 係字串拼接，直接用常量） |
| 146-163 | failsafe 24 小時日程 dict：key（`"6:00"` 等）**不郁**，value 轉繁體 |
| 175, 219 | 模板句「至」「计划」→「計劃」 |
| 370-373 | `"此时"` 前綴處理 → `KW_AT_THIS_TIME` |
| 408 | `failsafe = "空闲"` → `failsafe = KW_IDLE` |
| 415 | f"{a.name} 正去往 …" 保留（「正去往」簡繁同形， glossary 確認） |
| 425 | chat_history 句「上次在…聊过关于…的话题」→「上次在…傾過關於…嘅」**唔用口語**——書面語：「上次在…聊過關於…的話題」 |
| 447, 473 | `in ("true","yes","是","1")` → `in KW_TRUE_TOKENS`（加咗「係」容錯） |
| 455 | `"[对话尚未开始]"` →「[對話尚未開始]」 |
| 482-506 | decide_wait 嘅兩個 few-shot 示例（context/agent/status/reason/answer）全段轉繁體；「答案：<选项A>」→「答案：<選項A>」——`<选项A>` 係佔位標記，「选项」轉「選項」，尖括號結構不郁 |
| 513-515 | 地址拼接 f-string 邏輯不郁（值已隨 maze 轉繁體） |
| 19-22 | `template_path = "data/prompts"` 載入邏輯不郁 |

### 5.6 `generative_agents/modules/memory/event.py`

- line 17-18、53-54：`"此时"/"空闲"` → `KW_AT_THIS_TIME`/`KW_IDLE`（`from ..prompt.keywords import ...`）

### 5.7 `generative_agents/modules/memory/associate.py`

- line 221：`("对话 " + name)` → `(KW_CHAT + " " + name)`——embedding 檢索 query 必須同儲存文字同語言。

### 5.8 `generative_agents/modules/memory/schedule.py`

- line 80-88：`"睡"`、`"床"` 簡繁同形，**字面值不改**，但改引用 `KW_SLEEP`/`KW_BED` 常量，並加回歸測試（見 §8）。

### 5.9 `generative_agents/modules/memory/spatial.py`

- line 12：`"睡觉" not in self.address` → `KW_SLEEPING not in ...`（`"sleeping"`、`KW_LIVING_AREA` 保留）
- line 14：`self.address["睡觉"] = ... + ["床"]` → `self.address[KW_SLEEPING] = ... + [KW_BED]`

### 5.10 `generative_agents/modules/utils/timer.py`

- line 58-76：**唔改**（「星期一」…「星期日」、`%Y年%m月%d日` 簡繁同形）。加測試鎖定（§8），`scan_simplified.py` 白名單豁免。

### 5.11 `generative_agents/data/prompts/*.txt`（29 個）

逐個模板：中文內容轉繁體書面語（機轉底稿＋人工潤飾）；`${}` 佔位符名零改動；內嵌 JSON 示例 key 不郁只轉 value；尾部統一追加 `TRADITIONAL_CHINESE_DIRECTIVE`（「一律使用繁體中文（香港書面語）回答。」）。檔名不變（`scratch.py:19-22` 以檔名載入）。

### 5.12 前端（`generative_agents/frontend/`）

| 檔案 | 行 | 改動 |
|---|---|---|
| `templates/main_script.html` | 155 | `font: "24px 黑体"` → `font: '24px "PingFang TC","Microsoft JhengHei","Noto Sans TC",sans-serif'` |
| 同上 | 176, 180, 184, 281-282, 288-289, 294, 300 | 按鈕字串：「[运行]」→「[運行]」、「 暂停 」→「 暫停 」、「[显示对话]」→「[顯示對話]」（注意兩態切換邏輯 281-300 行成對改） |
| 同上 | ~91 | sprite 載入路徑用目錄名拼接（`"static/assets/village/agents/" + p + "/texture.png"`），目錄名由 convert_assets.py 統一改，template 代碼本身**唔使改**（動態拼接） |
| `templates/index.html`、`templates/base.html` | — | 全量掃描簡體標籤轉繁體（由 scan_simplified.py 把關） |
| `static/assets/village/maze.json` | `tiles[].address` | 腳本原子轉換（§2.4） |
| `static/assets/village/agents/<名>/` ×25 | agent.json + 目錄名 | 腳本原子轉換＋目錄 rename |

### 5.13 `generative_agents/start.py`

| 行 | 改動 |
|---|---|
| 12-17 | `personas` 列表 25 個名由 convert_assets.py 同步更新（唔手改） |
| 37-41（`SimulateServer.__init__` 載入 conversation.json 處） | 插入 checkpoint 簡體偵測：載入後對全部文字跑 `contains_simplified()`，命中即 `logger.warning("偵測到簡體 checkpoint，建議運行 python scripts/localization/migrate_checkpoint.py %s", checkpoints_folder)`。**唔做靜默轉換**（PRD 邊界 3）。 |

---

## 6. 同其他 5 個系統嘅契約

| 消費系統 | 本系統暴露嘅接口 | 約定 |
|---|---|---|
| **Setup／角色配置** | `scripts/localization/validate_addresses.py::validate() -> list[str]`；`data/glossary_s2hk.json` 嘅 `place_names`/`agent_names` | Setup 頁 label／住所名用字以 glossary 為準；玩家輸入永不經 normalize；角色目錄名＝glossary `agent_names` 嘅 value |
| **Agent 推演引擎** | `modules/prompt/keywords.py` 全部常量；`modules/model/text_normalize.py::normalize_text` | 新代碼唔准 hardcode 中文判斷字串，一律入 keywords.py；CI 用 `grep -nP '[\x{4e00}-\x{9fff}]' modules/` 把關（豁免註釋＋keywords.py＋timer.py） |
| **GM／決策 modal** | `keywords.KW_CHAT`/`KW_ONGOING` 等；`normalize_text()`；`TRADITIONAL_CHINESE_DIRECTIVE` | GM 寫入記憶流嘅 `Concept.predicate/object` 用字必須同 keywords.py 一致（`node_type` 用英文 `"chat"/"event"`）；GM 新增 prompt 模板尾部須加同一指令；「故事回顧」時間線文字語言由 prompt 層保證 |
| **記憶注入（玩家選擇→記憶流）** | `normalize_text()`（**僅供內部文字用**）；glossary | 玩家自訂命令**原樣注入，唔 normalize**（邊界 8）；注入用嘅 `node_type`／poignancy 欄位係英文鍵，同語言解耦 |
| **Checkpoint／回放** | `migrate_checkpoint.py` CLI；`contains_simplified()`；start.py 載入 warning | 舊簡體 checkpoint 要遷先遷移；回放 UI 文案隨 templates 轉繁體；回放讀取唔做 runtime 轉換 |
| **全部** | `docs/prd/localization-glossary.md`（人讀 SSoT） | 任何系統新增中文 UI 字串／prompt 用字，先對照 glossary；新詞彙入 glossary 再使用 |

---

## 7. 文件計劃

### 新建

| 路徑 | 說明 |
|---|---|
| `generative_agents/modules/prompt/keywords.py` | 關鍵字常量（§3.1） |
| `generative_agents/modules/model/text_normalize.py` | s2hk normalize 層（§3.2） |
| `generative_agents/data/glossary_s2hk.json` | 機讀 glossary（§4.1） |
| `docs/prd/localization-glossary.md` | 人讀 glossary SSoT |
| `scripts/localization/__init__.py` | — |
| `scripts/localization/_report.py` | ConvertReport / MigrateReport dataclass |
| `scripts/localization/build_glossary_json.py` | MD → JSON 單向生成 |
| `scripts/localization/convert_prompts.py` | 模板機轉底稿＋`--check` CI 驗證 |
| `scripts/localization/convert_assets.py` | maze+agents+目錄+personas 原子轉換 |
| `scripts/localization/validate_addresses.py` | 地址引用完整性 CI 檢查 |
| `scripts/localization/migrate_checkpoint.py` | 舊 checkpoint 遷移 |
| `scripts/localization/scan_simplified.py` | 全 repo 簡體黑名單掃描（CI gate） |
| `tests/localization/test_keywords.py` | 關鍵字命中回歸（「睡覺」命中 KW_SLEEP 等） |
| `tests/localization/test_normalize.py` | normalize 單測（「对话记录」→「對話記錄」、「睡觉」→「睡覺」、dict key 不郁、空串 fast path） |
| `tests/localization/test_timer.py` | timer.py 星期／日期格式鎖定 |
| `tests/localization/test_event_defaults.py` | Event 預設值＝keywords 常量 |
| `docs/spec/localization.md` | 本文件 |

### 修改

| 路徑 | 改動摘要 |
|---|---|
| `generative_agents/modules/agent.py` | §5.4 全部行；import keywords |
| `generative_agents/modules/prompt/scratch.py` | §5.5 全部行；import keywords |
| `generative_agents/modules/memory/event.py` | 預設值改常量 |
| `generative_agents/modules/memory/associate.py` | line 221 檢索 query |
| `generative_agents/modules/memory/schedule.py` | 引用常量（值不變） |
| `generative_agents/modules/memory/spatial.py` | 地址鍵改常量 |
| `generative_agents/modules/model/llm_model.py` | `completion()` 插入 normalize 一行＋import |
| `generative_agents/data/prompts/*.txt` ×29 | 全量繁體化＋尾部指令 |
| `generative_agents/frontend/templates/{index,main_script,base}.html` | 文案＋字體 fallback chain |
| `generative_agents/frontend/static/assets/village/maze.json` | 腳本轉換 |
| `generative_agents/frontend/static/assets/village/agents/*` ×25 | agent.json 轉換＋目錄 rename |
| `generative_agents/start.py` | personas 列表（腳本改）；載入 warning |
| `requirements.txt` | 加 `opencc-python-reimplemented` |

---

## 8. 測試與驗收（對應 PRD Done When）

| 測試 | 形式 | 通過條件 |
|---|---|---|
| 關鍵字回歸 | `test_keywords.py` | LLM 風格輸入「睡覺」→ `KW_SLEEP in "睡覺"` 為 True；`Event(...).fit(predicate=KW_CHAT)` 命中；`"待開始" == KW_PENDING` |
| Normalize 單測 | `test_normalize.py` | 輸入「对话记录」「睡觉」→ 輸出「對話記錄」「睡覺」；dict `{"6:00": "睡觉"}` → key 不郁、value「睡覺」；OpenCC 缺裝時 fallback dict 仍工作 |
| 模板 CI | `convert_prompts.py --check` | 29 個模板：無簡體黑名單字、`${}` 佔位符集契約不變、尾部有指令、JSON 示例段可 `json.loads` |
| 地址完整性 | `validate_addresses.py` | 每個 agent 嘅 living_area／睡覺地址喺 maze 搵到；exit 0 |
| 簡體掃描 | `scan_simplified.py` | `data/prompts/`、`modules/`（豁免註釋/keywords/timer）、`frontend/templates/`、village JSON 零命中 |
| 代碼 hardcode 掃描 | CI grep | `grep -nP '[\x{4e00}-\x{9fff}]' generative_agents/modules/` 只剩註釋＋keywords.py＋timer.py |
| Checkpoint 遷移 | 對一份真實舊 checkpoint 跑 migrate | 輸出檔無簡體；未遷移檔載入時 start.py 有 warning |
| timer 鎖定 | `test_timer.py` | `daily_format_cn()` 輸出含「星期一」…「星期日」其中一個＋「年…月…日」格式 |
| 端到端冒煙 | config.json（OpenAI provider）跑 2 回合 4 角色 | (a) `results/checkpoints/*/conversation.json`＋simulate-*.json 黑名單掃描零簡體；(b) 事件流出現「睡覺」且 `find_address(KW_SLEEPING)` 成功；(c) 對話 `node_type == "chat"` |

---

## 9. 風險與緩解

| 風險 | 緩解 |
|---|---|
| LLM 唔跟指令輸出簡體，「對話」類斷裂關鍵字令 agents 唔記得對話 | normalize 層喺 `LLMModel.completion()` 統一出口（§5.3），callback 之前標準化；呢個係最重要防線，有獨立單測 |
| maze/agent 半改狀態令 `find_address` 搵唔到路 | convert_assets.py temp-dir＋atomic move＋寫入前全量驗證；失敗 rollback；CI 常駐 validate_addresses.py |
| 角色目錄改名漏一處（start.py personas／agent.json name／目錄名） | 三處由 glossary `agent_names` 單一 mapping 腳本生成，禁止手改 |
| OpenCC 喺玩家環境裝唔到 | `opencc-python-reimplemented` 純 Python；再兜底 glossary fallback dict，normalize 永不拋異常 |
| 舊 checkpoint 簡繁混雜拖累 embedding 檢索 | 遷移腳本＋載入 warning；qwen3-embedding 對簡繁混合有耐受但唔依賴 |
| 模板 JSON 示例被當內容翻譯 | 轉換規則＋`--check` 嘅 `json.loads` 驗證；protected_tokens 清單 |
