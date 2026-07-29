# 技術 Spec：故事回顧系統（Story Recap）

> 依據 PRD：`docs/prd/story-recap.md`（唯一事實來源）
> 代碼庫：`/Users/kenneth/Projects/story-weaver`（Python 3.12 + Flask，GenerativeAgentsCN 重構版）
> 本文所有行號以 2026-07-28 嘅代碼為準。

---

## 1. 架構決策

### 1.1 新開 `generative_agents/story_weaver/` 套件，唔改 `modules/`

**決定**：本系統所有新代碼放入獨立套件 `generative_agents/story_weaver/`（同 `modules/` 平級），`modules/` 下面**一行都唔改**。

理由：
1. **層級清晰**：`modules/` 係 agent 級（percept / plan / reflect / chat 嘅單個 agent 行為）；故事回顧係**遊戲級**嘅旁觀者系統，佢唔參與任何 agent 嘅 think loop，只消費 think loop 嘅產物（checkpoints + conversation.json）。放入 `modules/` 會令人誤以為佢係 agent 行為嘅一部分。
2. **保留上游 diff 能力**：`modules/` 係 GenerativeAgentsCN 嘅重構代碼，日後要對照上游或者再重構，保持零改動可以直接 diff。本系統唯一需要嘅「上游行為」（checkpoint 寫入、對話記錄、poignancy 評分、LLM 調用層）全部已有公開接口，唔使侵入。
3. **純消費者定位**：PRD 明確「唔改動寫入邏輯，只做消費者」。消費者唔應該住喺生產者屋企。

### 1.2 觸發方式：回合管理系統調用，`start.py` 零改動

`SimulateServer.simulate()`（`start.py` 第 71-104 行）每 step 寫完 checkpoint 後乜都唔做就入下一 step。本系統**唔喺 loop 入面插 hook**，而係由上游「回合管理 / 模擬循環系統」喺每次 `server.simulate(step=N, stride)` 返回之後調用 `RecapService.on_round_end(...)`。

理由：「回合」係遊戲概念（2-10 回合、決策 modal 暫停），唔係模擬器概念。模擬器只識 step。將 round ↔ step range 映射嘅責任交返俾回合管理系統，`start.py` 保持對遊戲邏輯無知。咁樣 `--resume` 對齊亦自然成立：回合管理系統本身就讀 `sim_config["step"]`（`start.py` 第 196 行 `start_step = sim_config["step"]`）決定下回合由邊個 step 開始，佢將同一對數字傳入 `on_round_end` 即可。

**唯一可選嘅 `start.py` 改動**（唔係必須）：若日後想喺無回合管理系統嘅情況下（純 CLI 跑模擬）都自動生成回顧，可以加一個可選 callback 參數；本 spec 唔要求，預設唔改。

### 1.3 存儲：append-only JSON 檔，同 checkpoints 同目錄，原子寫入

`results/checkpoints/<sim_name>/story_recap.json` 由本系統獨家讀寫。

- **原子性**：所有寫入行「臨時檔 + `os.replace()`」——先寫 `story_recap.json.tmp.<pid>`，`os.replace()` 係 POSIX 原子操作，kill -9 最多留低一個 `.tmp` 殘檔，主檔永遠係合法 JSON。啟動時清理殘留 `.tmp.*`。
- **唔用 SQLite / 唔入 LlamaIndex**：呢個係遊戲狀態記錄，唔係語義檢索；append-only JSON 同現有 checkpoint 嘅檔案哲學一致，`get_config_from_log()`（`start.py` 第 111-134 行）只認 `simulate-*.json`，新檔案對 resume 邏輯零影響（第 116 行嘅 filter 只排除 `conversation.json`，但佢用 `endswith(".json")` + 排序後取**最後一個**做 resume 來源 —— 注意：`story_recap.json` 會被第 115-117 行嘅 filter 收埋入 `json_files`！

> **重要修正（已過時）**：`get_config_from_log()` 已於 affinity commit 白名單化（`start.py:146`：`file_name.startswith("simulate-")`），`story_recap.json` 及任何新 metadata json 都唔會被誤收。**本系統對 `start.py` 零改動**，§2.2 嘅一行防禦性修改唔需要做。

### 1.4 LLM 調用：獨立 `LLMModel` 實例，複用 `create_llm_model`

`RecapGenerator` 唔借 agent 嘅 `_llm`，而係自己用 `modules.model.llm_model.create_llm_model()` 由 `data/config.json` 嘅 `agent.think.llm` 段建一個獨立實例。好處：
- `completion(retry=10, failsafe=None, callback=validator, caller="story_recap")` 嘅 retry / failsafe / token 統計（`get_summary()`，第 65-69 行）全部免費攞到；
- `caller="story_recap"` 令 token 用量喺 summary 入面獨立成項，方便驗證 PRD 嘅「context ≤60%」要求；
- 獨立實例意味住 recap 生成嘅失敗 / disable 唔會影響 agents 嘅 think loop。

Prompt 模板**唔入 `Scratch`**（嗰個係 agent 專用，模板函數全部綁死 agent state），改用獨立嘅 `RecapPrompt` loader；但模板**檔案**放返 `generative_agents/data/prompts/`（PRD 指定嘅第 30、31 個模板），同現有 29 個模板同一目錄、同一 `string.Template` + `$var` 語法（對齊 `scratch.py` 第 19-26 行 `build_prompt`）。

### 1.5 背景生成：threading，唔阻塞模擬

`on_round_end()` 同步完成**提取**（讀 checkpoints、去重、寫 `recap_status="pending"`），然後開 `threading.Thread` 做 LLM 敘事生成，完成後再原子寫入更新 status。Flask 喺 threaded mode 下讀取永遠讀到最新檔案。唔用 Celery / 唔用進程池 —— 單機單遊戲進程，thread + 檔案鎖（`threading.Lock`，寫入側先攞鎖）已足夠。

### 1.6 命名事實：角色名係繁體鍵（已更新）

代碼庫 25 個 persona 名已於 localization commit 全部轉為**繁體**（`start.py`：`"阿伊莎"`, `"瑪麗亞"`…），所有 checkpoint / conversation.json / agent.json 嘅 key 都用呢啲名。本系統內部一律沿用**原鍵**。另注：寫作模板同文案時要用 HK 標準字形（`幹預` 唔係 `干預`、`敍事` 唔係 `敘事`——OpenCC s2hk 會改動嘅字都被 CI gate `scan_simplified.py` 攔截）。

---

## 2. 模塊結構與文件計劃

### 2.1 新建文件

| 路徑 | 職責 |
|---|---|
| `generative_agents/story_weaver/recap/__init__.py` | 套件入口，re-export `RecapService`、`recap_bp`（**實際落地為 `story_weaver/recap/` 子套件**，同 `gm/`、`affinity/` 對稱；spec 原本嘅平鋪路徑 `story_weaver/models.py` 等全部對應落入 `recap/`） |
| `generative_agents/story_weaver/recap/models.py` | 全部 dataclass（§3）+ JSON 序列化 |
| `generative_agents/story_weaver/recap/store.py` | `StoryRecapStore`：原子讀寫 `story_recap.json`、upsert、執行緒鎖、tmp 殘檔清理 |
| `generative_agents/story_weaver/recap/extractors.py` | `EventExtractor`、`DialogueExtractor`、`MemoryFallbackExtractor`（§4） |
| `generative_agents/story_weaver/recap/prompts.py` | `RecapPrompt` loader + LLM 輸出嘅 pydantic 校驗模型 |
| `generative_agents/story_weaver/recap/generator.py` | `RecapGenerator`：分層摘要、LLM 調用、validator callback、模板降級（§5） |
| `generative_agents/story_weaver/recap/service.py` | `RecapService` 門面：init / on_round_end / record_decision / build_gm_context / get_recap（§7） |
| `generative_agents/story_weaver/recap/markdown_export.py` | `export_markdown()`：完整故事 markdown 導出（`compress.py` `generate_report()` 嘅敘事化升級） |
| `generative_agents/story_weaver/recap/api.py` | Flask Blueprint `recap_bp`（§8） |
| `generative_agents/data/prompts/story_recap_round.txt` | 單回合敍事摘要模板（繁體香港書面語，HK 標準字形） |
| `generative_agents/data/prompts/story_recap_cumulative.txt` | 累積回顧模板（繁體香港書面語，HK 標準字形） |
| `tests/story_weaver/test_recap_extractors.py` | 事件去重、對話三層解析、位元級對白比對、損毀 checkpoint |
| `tests/story_weaver/test_recap_store.py` | 原子寫入、upsert、tmp 殘檔清理 |
| `tests/story_weaver/test_recap_generator.py` | mock LLM 全失敗 → fallback；垃圾輸出 → validator 拒收；分層摘要 token 預算 |
| `tests/story_weaver/test_recap_api.py` | route schema、分頁、markdown 導出、降級提示欄位 |
| `docs/spec/story-recap.md` | 本文件 |

### 2.2 修改現有文件

**無。** `start.py` 白名單已由 affinity commit 完成（見 §1.3 修正），`replay.py`、`modules/`、`compress.py` 全部零改動。

---

## 3. 數據模型

全部用 `@dataclass`（同代碼庫 `Event` / `Concept` 嘅 plain-class 風格一致；pydantic 只留俾 LLM 輸出校驗，對齊 `scratch.py` 嘅用法）。每個 dataclass 提供 `to_dict() -> dict` 同 `from_dict(d: dict)`。

### 3.1 核心模型（`story_weaver/models.py`）

```python
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal, Optional

# ---- 型別別名 ----
RecapStatus = Literal["ok", "fallback", "pending"]
EventType   = Literal["action", "gm_note", "player_intervention"]
DecisionType = Literal["option", "custom"]
SimTime = str   # 格式 "%Y%m%d-%H:%M"，同 start.py 第 89 行 timer.get_date 一致
ISOTime = str   # 真實世界時間，datetime.isoformat()

@dataclass
class AgentProfile:
    """Setup 系統傳入嘅角色設定快照（init 時凝固，之後 GM 改動行 gm_note 事件）。"""
    name: str                 # 角色原鍵（簡體，如 "玛丽亚"）
    occupation: str           # 玩家填嘅職業
    personality: str          # 玩家填嘅性格
    relations: dict[str, str] # {其他角色名: 關係描述}
    affinity: dict[str, int]  # {其他角色名: -100..+100}，雙向獨立（A對B 同 B對A 分開記）
    age: int = 0
    innate: str = ""          # 由 frontend/static/assets/village/agents/<名>/agent.json scratch 補
    learned: str = ""
    lifestyle: str = ""

@dataclass
class TimelineEvent:
    sim_time: SimTime
    agent: str                          # gm_note 時為 "GM"；player_intervention 時為 "PLAYER"
    type: EventType
    location: str                       # "，".join(address[1:])，對齊 compress.py get_location 第 27-35 行
    describe: str
    poignancy: int = 5                  # 1-10；來源見 §4.1
    step: int = 0                       # 來自邊個 checkpoint step（resume 去重用）

@dataclass
class DialogueLine:
    speaker: str
    text: str          # 對白原文，位元級保留，永不經 LLM

@dataclass
class DialogueBlock:
    sim_time: SimTime                   # conversation.json 嘅分鐘級 key
    participants: list[str]             # [發起者, 對象]，由 "A -> B @ ..." 解析
    location: str                       # " @ " 後段（"，" 連接嘅地址）
    lines: list[DialogueLine]
    degraded: bool = False              # True = conversation.json 損毀時由記憶摘要重建（無原文）

@dataclass
class PlayerDecision:
    type: DecisionType
    text: str                           # 選項文本或自訂命令原文
    chosen_at: ISOTime
    round: int = 0                      # 冗餘欄位，寫入時由 store 強制對齊 round_no

@dataclass
class RoundRecap:
    round: int
    sim_time_start: SimTime
    sim_time_end: SimTime               # 若 checkpoint 截斷，如實反映最後完整 step
    step_range: tuple[int, int]         # [start_step, end_step]，含頭含尾
    events: list[TimelineEvent] = field(default_factory=list)
    dialogues: list[DialogueBlock] = field(default_factory=list)
    player_decision: Optional[PlayerDecision] = None   # 「上一回合」嘅決策（驅動本回合）
    round_recap: str = ""
    recap_status: RecapStatus = "pending"
    dialogue_health: Literal["ok", "degraded", "missing"] = "ok"
    warnings: list[str] = field(default_factory=list)  # 如 "step 7 checkpoint 損毀已剔除"

@dataclass
class CumulativeRecap:
    text: str = ""
    generated_at_round: int = 0
    status: RecapStatus = "pending"
    model: str = ""                     # 生成用嘅模型名，由 LLMModel.get_summary()["model"]

@dataclass
class StoryRecap:
    sim_name: str
    opening: str
    created_at: ISOTime
    agents: list[AgentProfile] = field(default_factory=list)
    rounds: list[RoundRecap] = field(default_factory=list)
    cumulative_recap: CumulativeRecap = field(default_factory=CumulativeRecap)
    schema_version: int = 1             # 日後遷移用
```

### 3.2 `story_recap.json` 落盤格式

即 `StoryRecap.to_dict()`。欄位約束（對齊 PRD）：

- `recap_status` / `cumulative_recap.status` 三值：`ok` / `fallback` / `pending`。
- `dialogues[].lines[].text` 由 `conversation.json` 直接複製，**位元級等同**（測試用 sha256 比對，見 §9）。
- `events[].poignancy < 3` 嘅瑣碎事件唔入時間線（仍喺原始 checkpoint）。
- `player_decision` 按 `round` upsert：同一回合只保留最新一次提交。
- `opening` 為空字串或純空白 → `init_story()` 拒絕（見 §7）。

### 3.3 GM 上下文模型

```python
@dataclass
class GMContext:
    sim_name: str
    opening: str
    round_count: int
    round_summaries: list[dict]        # [{"round": 1, "recap": "...", "status": "ok"}, ...]
    latest_round: Optional[RoundRecap] # 最新一回合完整事件 + 對話原文
    agents: list[AgentProfile]         # 含初始好感度快照
    generated_at: ISOTime
```

GM 系統將佢渲染入自己嘅 prompt；本系統唔保證 GM 點用，只保證呢個結構穩定。

---

## 4. 提取器（`story_weaver/extractors.py`）

### 4.1 `EventExtractor`

```python
class EventExtractor:
    def __init__(self, static_root: str = "frontend/static"): ...

    def extract(self, sim_dir: str, step_range: tuple[int, int]) -> ExtractionResult: ...

@dataclass
class ExtractionResult:
    events: list[TimelineEvent]
    sim_time_start: SimTime
    sim_time_end: SimTime
    warnings: list[str]                # 損毀 checkpoint 嘅 step 列表
    truncated: bool                    # 有冇因損毀而提前終止
```

**算法**（重用 `compress.py` `generate_report()` 第 217-245 行嘅 `last_state` 去重模式，改為輸出結構化數據）：

1. `sorted(os.listdir(sim_dir))`，篩 `simulate-*.json`（用 regex `^simulate-\d{12}\.json$`，唔會誤中 `story_recap.json`）。
2. 逐檔 `json.load`；任何一檔 `JSONDecodeError` 或缺 `agents` / `time` / `step` 頂層鍵 → 記 warning（`"step N checkpoint 損毀已剔除"`）並 **continue**（唔中斷，除非全部爛晒）。`sim_time_end` 取最後一個**合法**檔嘅 `time`，如實反映截斷。
3. 只收 `step_range` 範圍內嘅 step（用檔內 `json_data["step"]` 判斷，唔用檔名排序推斷 —— resume 後檔名時間可能唔連續）。
4. 每個 checkpoint 逐 agent 讀 `agents[name].action.event`（六欄位對齊 `event.py` `to_dict()` 第 63-71 行）：`describe` 為空時 fallback 用 `predicate + object`（對齊 `Event.get_describe()`）。
5. **瑣碎過濾**：`predicate/object` 命中 `{"此时","空闲","is","idle","waiting to start"}` 或 `describe` 含「睡觉/空闲」且 poignancy < 3 → 唔入時間線。
6. **poignancy 來源**（按優先序）：
   - 首選：輕量掃描 `results/checkpoints/<sim>/storage/<agent>/associate` 嘅 LlamaIndex docstore JSON，按 event describe 文本匹配 metadata 嘅 `poignancy`（`associate.py` `add_node` 第 178-183 行將 `poignancy` 寫入 node metadata）。注意**唔實例化** LlamaIndex（避免 embedding 模型依賴），直接讀 docstore 檔。
   - 後備：heuristic —— 瑣碎類 = 1（會被過濾），其餘 = 5。
   - 每 agent 嘅 poignancy map 喺 `extract()` 開頭建一次，唔逐事件重掃。
7. **去重**：維護 `last_state[agent] = (location, describe)`；相鄰 checkpoint 同 agent 同 (location, describe) → 合併，保留首次出現嘅 `sim_time` 同 `step`（驗證標準：同 agent 連續 3 個 step 同 describe + 地點 → 時間線只出 1 條）。
8. 事件按 `sim_time` 再按 `step` 排序輸出。

**API 層唔會讀取 associate 嘅語義檢索功能**；嗰個係 agent 內部嘢。

### 4.2 `DialogueExtractor`

```python
class DialogueExtractor:
    def extract(self, sim_dir: str, sim_time_start: SimTime,
                sim_time_end: SimTime) -> DialogueResult: ...

@dataclass
class DialogueResult:
    blocks: list[DialogueBlock]
    health: Literal["ok", "degraded", "missing"]   # 供 RoundRecap.dialogue_health
    warnings: list[str]
```

**算法**：
1. 讀 `<sim_dir>/conversation.json`。檔案唔存在或 `JSONDecodeError` → 進入降級（步驟 4）。
2. 結構係**三層**（`agent.py` `_chat_with` 第 576-579 行）：
   ```
   {"YYYYmmdd-HH:MM": [ {"阿伊莎 -> 玛丽亚 @ 地址1，地址2": [[speaker, text], ...]}, ... ]}
   ```
   分鐘級 key 之內係 **list of dict**（同一分鐘多組對話會 append）。逐組拆開成獨立 `DialogueBlock`，**唔合併唔覆蓋**；key 解析用 `title.split(" -> ")` 同 `.split(" @ ", 1)`。
3. 只收 `sim_time_start <= key <= sim_time_end` 嘅 key（字串比較即時間比較，格式固定 13 字符）。
   `lines` 逐條 `[speaker, text]` 複製 —— **唔做** strip / 唔做簡繁轉換 / 唔做標點修整。位元級保留。
4. **降級**（`health="degraded"`）：conversation.json 損毀時，由 `MemoryFallbackExtractor` 掃各 agent associate storage 中 `node_type="chat"` 嘅 concept（`associate.py` 第 178 行 metadata），重建 `DialogueBlock(degraded=True)`，`lines` 得返一條 `{speaker: "（回憶）", text: summarize_chats 摘要}` —— 呢個**唔係原文**，UI 據 `degraded` 旗標顯示「對話原文已散佚，以下為角色回憶」。完全救唔返 → `health="missing"`，blocks 為空。

### 4.3 `MemoryFallbackExtractor`

```python
class MemoryFallbackExtractor:
    def scan_chat_concepts(self, sim_dir: str, agents: list[str],
                           sim_time_start: SimTime, sim_time_end: SimTime
                           ) -> list[DialogueBlock]: ...
```

直接讀 `storage/<agent>/associate` docstore JSON 嘅 node metadata（`node_type == "chat"`、`create` 喺時間範圍內），按 `subject/object` 推 participants，按 `address` 推地點。輸出嘅 block 全部 `degraded=True`。

---

## 5. 敘事生成與降級（`story_weaver/generator.py` + `prompts.py`）

### 5.1 `RecapPrompt`（`prompts.py`）

```python
class RecapPrompt:
    def __init__(self, template_path: str = "data/prompts"): ...

    def build(self, template: str, data: dict) -> str:
        """同 scratch.py 第 19-26 行一致：string.Template + substitute。"""

    def round_input(self, opening: str, round_recap: RoundRecap,
                    prev_recaps: list[str]) -> str: ...
        """渲染 story_recap_round.txt"""

    def cumulative_input(self, opening: str, round_summaries: list[dict],
                         latest_round: RoundRecap) -> str: ...
        """渲染 story_recap_cumulative.txt（分層摘要，見 §5.3）"""

class RoundRecapResponse(BaseModel):
    res: str = Field(description="本回合敘事摘要，繁體中文書面語，150-250字")

class CumulativeRecapResponse(BaseModel):
    res: str = Field(description="由開端到而家嘅完整敘事回顧，繁體中文書面語，300-600字")
```

兩個模板直接以**繁體香港書面語**撰寫（本系統係新嘢，唔等 29 個簡體模板嘅轉換工程）。模板必須明確指示：「對白引號內容必須逐字引用下方提供嘅原文，不得改寫」同「若玩家干預無明顯效果，須如實敘述，不得隱瞞」（對齊 PRD 邊界情況）。

### 5.2 `RecapGenerator`

```python
class RecapGenerator:
    def __init__(self, llm_config: dict, template_path: str = "data/prompts"):
        self._llm: LLMModel = create_llm_model(llm_config)   # 獨立實例，§1.4
        self._prompts = RecapPrompt(template_path)

    def generate_round_recap(self, opening: str, round_recap: RoundRecap,
                             prev_recaps: list[str],
                             agent_names: list[str]) -> tuple[str, RecapStatus]: ...

    def generate_cumulative(self, opening: str, rounds: list[RoundRecap],
                            agent_names: list[str]) -> tuple[str, RecapStatus]: ...

    def estimate_tokens(self, text: str) -> int:
        """粗略計數：len(text)（中文 1 字 ≈ 1 token 嘅保守上界）；
        驗證 cumulative prompt ≤ 模型 context 60% 時用呢個 + get_summary() 對帳。"""
```

**LLM 調用**（兩個函數內部一致）：

```python
result = self._llm.completion(
    prompt,
    retry=10,                                   # 用盡內建 retry
    callback=make_validator(agent_names),       # 校驗唔過 → return None → 觸發重試
    failsafe=None,                              # 10 次全敗 → 返回 None → 落 fallback
    return_type=RoundRecapResponse,             # magentic/pydantic structured output
    caller="story_recap",
)
if result is None:
    return (build_fallback_text(...), "fallback")
return (result, "ok")
```

**validator 校驗規則**（PRD 指定）：非空、`len ≥ 50`、唔含 `{` 或 `$` 開頭嘅未渲染 placeholder、至少提及一個 `agent_names` 入面嘅角色名。任何一條唔過 → callback 返回 `None` → 計入 retry。

**捷徑（唔調 LLM）**：
- 第 1 回合決策前（rounds 為空）：cumulative 直接設 `status="ok"`，text 由模板出「故事即將展開」句式，**唔調 LLM**。
- 某回合零對話零重要事件：`round_recap` 用模板「本回合風平浪靜：……」，`recap_status="ok"`，唔調 LLM。

### 5.3 分層摘要（防 context 爆）

cumulative recap 嘅 prompt input 永遠係：

```
開端原文
+ 各回合 round_recap（每段 ≤200 字，生成 round recap 時已限制 150-250 字，
  渲染前再截斷至 200 字保底）
+ 最新一回合嘅完整事件列表 + 對話原文
```

**絕對唔將** N 回合嘅全部原文一次過塞入。第 10 回合時 prompt ≈ 開端 + 9×200 字 + 1 回合原文，本地驗證 `estimate_tokens(prompt) ≤ 0.6 × context_window`（context_window 由 config 或模型預設表查，寫入 warning log 如果超）。

### 5.4 模板降級文本（`build_fallback_text`）

LLM 全敗時嘅降級輸出 = 純模板拼接：

```
【故事摘要暫時不可用，以下為原始記錄】
第 N 回合（2024-02-13 09:30 – 12:00）
· 09:45 阿伊莎 @ 奧克山學院，圖書館：正在查閱莎士比亞嘅資料
【對話】阿伊莎 -> 玛丽亚 @ 奧克山學院，圖書館
阿伊莎：「……（原文）」
```

事件同對白原文齊全，只係冇敘事文。呢段文本直接入 `round_recap` / `cumulative_recap.text`，status 記 `fallback`。

---

## 6. 整合點（現有代碼改動清單）

| # | 文件 | 位置 | 改動 | 理由 |
|---|---|---|---|---|
| 1 | ~~`generative_agents/start.py`~~ | — | ✅ **唔使改**：白名單已實施（`start.py:146`），見 §1.3 修正 | — |
| 2 | `generative_agents/data/prompts/` | 新增 2 檔 | `story_recap_round.txt`、`story_recap_cumulative.txt` | 第 30、31 個模板，繁體撰寫（HK 標準字形），唔入 Scratch。 |
| 3 | 遊戲主 server（新，由 game-ui / 回合管理系統擁有） | app 初始化 | `app.register_blueprint(recap_bp)` | 掛 API（§8）。**唔掛 `replay.py`** —— replay 係唯讀回放器，決策 modal 唔喺嗰度。 |

**`SimulateServer.simulate()`（第 71-104 行）零改動。** 回合觸發序列（由回合管理系統執行）：

```python
# 回合管理系統嘅偽代碼（佢嘅實作，本系統只定義契約）
prev_step = sim_config["step"]                 # resume 時即 start.py 第 196 行嘅 start_step
server.simulate(step=steps_this_round, stride=stride)
end_step = server.config["step"]               # simulate loop 第 93 行更新
recap_service.on_round_end(
    sim_name, round_no,
    step_range=(prev_step + 1, end_step),      # 含頭含尾；resume 天然對齊
    player_decision=decision_from_prev_round,  # 上一回合 modal 提交嘅決策（可為 None）
)
```

模擬器寫 checkpoint（第 97-101 行）→ 回合結束 → 本系統讀同一批 checkpoint。生產者/消費者完全經檔案系統解耦。

---

## 7. 公開 Python API（`story_weaver/service.py`）

```python
class OpeningMissingError(ValueError):
    """opening 為空或純空白時拋出；Setup 系統捕獲後返回佢自己嘅 400。"""

class RecapService:
    def __init__(
        self,
        checkpoints_root: str = "results/checkpoints",
        static_root: str = "frontend/static",
        llm_config: Optional[dict] = None,   # None → 自動讀 data/config.json 嘅 agent.think.llm
    ): ...

    # ---- Setup 系統 ----
    def init_story(self, sim_name: str, opening: str,
                   agents: list[AgentProfile]) -> StoryRecap:
        """故事開始時調用。opening 空白 → 拋 OpeningMissingError。
        已存在 story_recap.json → 直接返回現有（冪等，唔覆蓋）。"""

    # ---- 回合管理系統 ----
    def on_round_end(self, sim_name: str, round_no: int,
                     step_range: tuple[int, int],
                     player_decision: Optional[PlayerDecision] = None,
                     background: bool = True) -> RoundRecap:
        """同步：提取事件+對話、append RoundRecap(recap_status="pending")、原子寫入。
        背景（background=True）：thread 生成 round_recap 同 cumulative_recap，
        完成後再原子寫入更新 status。返回嘅係提取完成時嘅快照。"""

    # ---- 決策 modal 系統（或注入系統代寫） ----
    def record_player_decision(self, sim_name: str, round_no: int,
                               decision: PlayerDecision) -> None:
        """按 round upsert（同 round 只留最新）。寫入用臨時檔 + rename。"""

    # ---- GM 系統 ----
    def record_gm_note(self, sim_name: str, round_no: int, note: str,
                       sim_time: Optional[SimTime] = None) -> None:
        """GM 調整好感度/設定後調用，append 一條 type="gm_note" 事件
        入最新（或指定）回合時間線，如「阿伊莎對玛丽亚嘅好感降至 -40」。"""

    def build_gm_context(self, sim_name: str) -> GMContext:
        """壓縮時間線俾 GM prompt（§3.3）。同步、純讀、可隨時調用。"""

    # ---- 決策 modal UI / 導出 ----
    def get_recap(self, sim_name: str,
                  round_no: Optional[int] = None) -> StoryRecap:
        """純讀。round_no 指定時只返回該回合（分頁用），但 opening /
        cumulative_recap / agents 永遠附上。"""

    def get_player_decision(self, sim_name: str,
                            round_no: int) -> Optional[PlayerDecision]:
        """注入系統用：確認上一回合注入咗咩，避免重複注入。"""

    def export_markdown(self, sim_name: str) -> str:
        """完整故事 markdown（§8.2 嘅 format=markdown 後盾）。"""
```

所有寫方法（`init_story` / `on_round_end` / `record_*`）內部經 `StoryRecapStore`（`threading.Lock` + tmp+rename）。所有讀方法唔上鎖（讀到嘅最多係上一個原子版本，永遠合法 JSON）。

---

## 8. Flask API（`story_weaver/api.py`，Blueprint `recap_bp`）

掛喺遊戲主 server（決策 modal 系統同一個 app），url_prefix `/api/story`。

### 8.1 `GET /api/story/<sim_name>/recap`

Query params：
| 參數 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `round` | int | 全部 | 只返回第 N 回合（分頁） |
| `format` | `json` \| `markdown` | `json` | markdown = 完整故事導出 |

**200 JSON response**（`format=json`）：

```json
{
  "sim_name": "my-story",
  "opening": "……",
  "agents": [ {"name": "阿伊莎", "occupation": "……", "affinity": {"玛丽亚": 40}, "……": "……"} ],
  "cumulative_recap": {"text": "……", "generated_at_round": 3, "status": "ok", "model": "gpt-4o-mini"},
  "rounds": [ {
      "round": 1,
      "sim_time_start": "20240213-09:30", "sim_time_end": "20240213-12:00",
      "step_range": [1, 12],
      "events": [ {"sim_time": "……", "agent": "……", "type": "action",
                   "location": "……", "describe": "……", "poignancy": 4, "step": 3} ],
      "dialogues": [ {"sim_time": "……", "participants": ["阿伊莎", "玛丽亚"],
                      "location": "……", "degraded": false,
                      "lines": [{"speaker": "阿伊莎", "text": "原文"}]} ],
      "player_decision": {"type": "option", "text": "……", "chosen_at": "……", "round": 0},
      "round_recap": "……", "recap_status": "ok",
      "dialogue_health": "ok", "warnings": []
  } ],
  "ui_hints": {
    "fallback_banner": "故事摘要暫時不可用，以下為原始記錄",
    "show_fallback_banner": false,
    "generating": false,
    "generating_message": "GM 正在整理故事……"
  }
}
```

`ui_hints.show_fallback_banner = true` 當 `cumulative_recap.status == "fallback"` 或最新 round `recap_status == "fallback"`；`generating = true` 當最新 round `recap_status == "pending"`（前端據此顯示 loading 同 30 秒超時降級輪詢）。**文案全部繁體香港書面語，由後端出**，前端唔硬編碼。

**`format=markdown`**：`200 text/markdown; charset=utf-8`，`Content-Disposition: attachment; filename="<sim_name>-story.md"`。

**錯誤**：
| 情況 | 碼 | body |
|---|---|---|
| sim_name 唔存在 / 未 init | 404 | `{"error": "story_not_found", "sim_name": "..."}` |
| `round=N` 超範圍 | 400 | `{"error": "round_out_of_range", "round": N, "round_count": M}` |

永遠唔會因 LLM 失敗返回 500 —— LLM 失敗只反映喺 `status` 欄位。

### 8.2 `POST /api/story/<sim_name>/recap/decision`

決策 modal 提交後回寫（modal 系統自己嘅提交 endpoint 處理完佢嘅邏輯後調呢個，或直接當佢係記錄 endpoint）。

Request：
```json
{"round": 2, "type": "option", "text": "讓阿伊莎主動約玛丽亚去咖啡館"}
```
- `round`：int ≥ 1，必填（指「呢個決策驅動第 N 回合」，即寫入第 N 回合嘅 `player_decision`）。
- `type`：`"option" | "custom"`。
- `text`：非空字串，≤500 字。

Response：`200 {"ok": true, "round": 2, "upserted": true}`（`upserted=false` 表示首次寫入）。
錯誤：`400 {"error": "invalid_decision", "detail": "..."}`；`404 story_not_found`。
重複 POST / 網絡重試 → upsert 語義，同 round 只留最新，冪等安全。

---

## 9. 同其他五個系統嘅契約

| 系統 | 方向 | 契約 | 失敗語義 |
|---|---|---|---|
| **Setup 系統** | 佢 → 我 | 故事開始時調 `RecapService.init_story(sim_name, opening, agents)`。`agents` 用 §3.1 `AgentProfile`，含玩家填嘅職業/性格/關係/雙向好感度 + 由 agent.json 補嘅 scratch。 | `opening` 空白 → 我拋 `OpeningMissingError`，Setup 必須攔截並返回佢自己嘅錯誤頁；我保證唔會寫出半個 `story_recap.json`。 |
| **回合管理系統** | 佢 → 我 | 每回合 `simulate()` 返回後調 `on_round_end(sim_name, round_no, step_range, player_decision=上一回合決策)`；`step_range` 由佢嘅 `sim_config["step"]` 簿記提供（§6 偽代碼）。`--resume` 對齊係**佢嘅責任**（佢簿記 start_step），我保證唔重複收錄 `step_range` 以外嘅 step。 | 我嘅提取永遠唔拋未捕獲異常（損毀 checkpoint → warnings）；若我整體失敗，佢嘅模擬循環唔應被我阻塞 —— `background=True` 下 LLM 部分本來就係 thread。 |
| **GM agent 系統** | 我 → 佢 | `build_gm_context(sim_name) -> GMContext`（§3.3）。佢每次寫分支偵測 prompt 前調用。結構穩定，加欄位只加唔改。 | 若最新回合 `recap_status="pending"`，`round_summaries` 照舊返回（該回合 recap 為空字串），GM 可自行決定等唔等；我唔阻塞佢。 |
| **GM agent 系統** | 佢 → 我 | 佢調整好感度/角色設定後調 `record_gm_note(sim_name, round_no, note)`，我寫 `type="gm_note"` 事件。佢**唔可以**直接改寫時間線其他任何欄位。 | note 空白 → `ValueError`。 |
| **決策 modal UI** | 我 → 佢 | `GET /api/story/<sim>/recap`（§8.1）。佢渲染 `opening`（頂部固定）、`cumulative_recap`、`rounds[]`；對話區塊沿用 `compress.py` 第 255 行嘅 `> 引用` 排版；`player_decision` 渲染為「✦ 你嘅決定：……」；`degraded=true` 嘅對話區塊加「對話原文已散佚，以下為角色回憶」。 | 佢打開 modal 時若 `ui_hints.generating=true`，顯示 loading 並輪詢（建議 2s 間隔），30s 超時後以當前狀態渲染（永遠有原始時間線兜底）。 |
| **決策 modal UI** | 佢 → 我 | 玩家提交後 `POST .../recap/decision`（§8.2）。 | upsert 冪等；重複提交安全。 |
| **玩家指令注入系統** | 我 → 佢 | `get_player_decision(sim_name, round_no)` 確認上一回合注入咗咩，避免重複注入。 | 返回 `None` = 該回合無決策（玩家直接跳過），佢唔應注入任何嘢。 |
| **玩家指令注入系統** | （無） | 注入邏輯（`Agent._add_concept()` 高 poignancy 注入）**完全係佢嘅事**，我唔參與、唔提供注入 API。時間線上嘅 ✦ 標記數據來自 `player_decision` 欄位，唔係來自注入結果。 | — |

---

## 10. 邊界情況 → 實作對照

| PRD 場景 | 實作位置 |
|---|---|
| 第 1 回合決策（冇事件） | `RecapGenerator` 捷徑（§5.2）：cumulative 模板直出，`status="ok"`，唔調 LLM；`rounds=[]` |
| LLM 返回垃圾 | validator callback（§5.2）四條規則；10 次用盡 → `fallback` + §5.4 模板文本 |
| 零對話零事件回合 | 捷徑模板「本回合風平浪靜」，`recap_status="ok"` |
| checkpoint 寫到一半 | `EventExtractor` 步驟 2：壞檔剔除 + warning；`sim_time_end` 如實截斷 |
| `conversation.json` 損毀/缺失 | `DialogueExtractor` 步驟 4 + `MemoryFallbackExtractor`；`dialogue_health` 三態 |
| 同分鐘多組對話 | `DialogueExtractor` 步驟 2：list-of-dict 逐組拆開，唔合併 |
| 玩家干預無反應 | 干預條目照記（`player_decision`）；cumulative 模板明文要求如實敘述（§5.1） |
| 第 10 回合爆 context | 分層摘要（§5.3）+ `estimate_tokens` 60% 紅線 + `?round=N` 分頁 |
| 對白係簡體 | `lines[].text` 位元級保留（§4.2 步驟 3），敘事文一律繁體 —— 已知過渡狀態 |
| 決策重複提交 | store upsert by round + tmp+rename（§8.2） |
| `--resume` | step_range 由回合管理系統簿記（§6）；提取按檔內 `step` 欄位篩選（§4.1 步驟 3），唔信檔名 |
| GM 調整好感度 | `record_gm_note` → `type="gm_note"` 事件 |
| kill -9 中途 | store 全部寫入行 tmp+`os.replace`；啟動時清 `.tmp.*` 殘檔 |

---

## 11. 測試計劃（對應 PRD「Done When」）

| 測試 | 驗證項 |
|---|---|
| `test_store.py::test_atomic_write_kill` | 子進程寫到一半被 kill → 主檔仍係合法 JSON；`.tmp` 殘檔被清理 |
| `test_store.py::test_decision_upsert` | 同 round 連寫兩次 → 只留最新 |
| `test_extractors.py::test_event_dedup` | 同 agent 連續 3 step 同 describe+地點 → 1 條事件 |
| `test_extractors.py::test_dialogue_bitexact` | 提取後 `lines[].text` 與 `conversation.json` 原文 sha256 逐條比對一致 |
| `test_extractors.py::test_multi_chat_same_minute` | 同一分鐘 key 兩組對話 → 兩個獨立 block |
| `test_extractors.py::test_corrupt_checkpoint` | 截斷 JSON → warnings 記錄、時間線到最後完整 step |
| `test_extractors.py::test_corrupt_conversation_fallback` | conversation.json 損毀 → degraded blocks / missing |
| `test_generator.py::test_llm_total_failure` | mock `LLMModel.completion` 永遠返 None → `recap_status="fallback"`，模板文本可讀 |
| `test_generator.py::test_validator_rejects_garbage` | 空字串 / 含 `{placeholder}` / 無角色名 → callback 返 None 觸發 retry |
| `test_generator.py::test_layered_token_budget` | 10 回合假數據 → cumulative prompt `estimate_tokens ≤ 0.6 × context` |
| `test_generator.py::test_no_llm_shortcuts` | 第 1 回合 / 風平浪靜回合 → 唔發 LLM 調用（mock assert_not_called） |
| `test_api.py::test_recap_schema` | response 含全部 §8.1 欄位；`ui_hints` 三態正確 |
| `test_api.py::test_pagination_and_markdown` | `?round=N`、`?format=markdown` |
| `test_api.py::test_no_500_on_llm_failure` | mock 全失敗 → 200 + fallback banner |
| 整合：resume 對齊 | 跑 5 step → kill → `--resume` 再 5 step → 兩回合 step_range 無重疊無遺漏 |
| 整合：就緒率 | 本地 20 回合，modal 打開時 `recap_status != "pending"` ≥ 95%（背景生成喺回合間隙完成） |

---

## 12. 開放問題（需其他系統確認，唔阻塞本 spec 實作）

1. **遊戲主 server 未存在**：`recap_bp` 嘅註冊目標（game-ui / 回合管理系統嘅 Flask app）仲未起。本系統先以 `RecapService` 純 Python 形式可測，blueprint 註冊係一行嘢。
2. **`AgentProfile` 嘅職業/性格/好感度來源**：Setup 系統嘅輸出格式未定，§3.1 係本系統嘅**需求側定義**；若 Setup 出唔同結構，由佢做 adapter，唔改本模型。
3. **模型 context window 查表**：`data/config.json` 冇 context 長度欄位；60% 紅線需要 config 加一個可選嘅 `agent.think.llm.context_window`（讀唔到就用保守預設 8192）。呢個係加 config 欄位，唔改代碼邏輯。
