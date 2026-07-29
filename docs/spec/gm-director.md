# 技術 Spec：GM/敘事總監系統（GMDirector）

> 對應 PRD：`docs/prd/gm-director.md`（唯一事實來源）。
> 本文所有行號、簽名均對照已驗證代碼（2026-07-28 驗證）：`generative_agents/start.py`、`modules/game.py`、`modules/agent.py`、`modules/memory/associate.py`、`modules/memory/event.py`、`modules/model/llm_model.py`、`modules/prompt/scratch.py`、`data/config.json`。

---

## 0. 設計總覽

```
┌────────────────────────────────────────────────────────────┐
│ 回合引擎系統（Flask routes，另一系統擁有）                      │
│   POST /api/round/start ──┐                                │
│   POST /api/round/decide ─┤                                │
│   GET  /api/gm/pending ───┤                                │
│   POST /api/gm/finale ────┘                                │
└──────────────┬─────────────────────────────────────────────┘
               │ 呼叫（唯二入口）
               ▼
┌────────────────────────────────────────────────────────────┐
│ story_weaver.gm.GMDirector                                 │
│  ├─ RoundDeltaCollector  讀 conversation/config/associate  │
│  ├─ GMPrompter           3 個繁體模板 → LLM structured out │
│  ├─ MemoryInjector       associate.add_node 直注記憶        │
│  ├─ AffinityStore        affinity.json 讀寫 + clamp        │
│  └─ GMStateStore         gm_state.json 讀寫 + pending 決策  │
└──────────────┬─────────────────────────────────────────────┘
               │ 只讀（零侵入）           │ 只寫（兩個槓桿）
               ▼                          ▼
   SimulateServer.game.conversation   agent.associate.add_node()
   SimulateServer.config["agents"]    agent.status["poignancy"] += N
   agent.associate.memory（node ids）  scratch.currently 前綴（回合開始）
```

核心原則：**GM 不是 Agent，不改 `simulate()`，不碰 agent 的 action/schedule**。GM 是一個獨立 LLM 客戶端 + 一組狀態存儲，透過「記憶注入」與「好感度快照」兩個槓桿影響 agents。

---

## 1. 架構決策

### 1.1 新模塊位置：`generative_agents/story_weaver/`（新開，不改 `modules/`）

**決定**：在代碼根 `generative_agents/` 下新開 `story_weaver/` package，GM 相關代碼全部放 `story_weaver/gm/`。

理由：
1. `start.py`、`replay.py` 都以 `generative_agents/` 為 cwd 運行（`from modules.game import ...`），`story_weaver/` 放同層可自然 `from modules.memory.event import Event`，無需改 `sys.path`。
2. `modules/` 是 GenerativeAgentsCN 上游代碼，保持原樣方便日後 merge 上游更新；`story_weaver/` 是我方新增層，邊界清晰。
3. 後續 5 個系統（Setup、回合引擎等）同樣落腳 `story_weaver/`，形成 `story_weaver/{gm,setup,rounds,server,...}` 的統一結構。

### 1.2 對現有代碼的修改：`start.py` 白名單（✅ 已實施）

`get_config_from_log()` 的白名單化**已於 affinity commit 完成**（`start.py` 第 146 行）：

```python
if file_name.startswith("simulate-") and file_name.endswith(".json"):
```

`gm_state.json` 放入 checkpoints 目錄後不會被誤認為 checkpoint。本系統對 `start.py` **零改動**。

另注：affinity commit 已在 `SimulateServer` 加入 `gm_hook` + `steps_per_round` seam（`start.py` 第 93-94、130-131 行），每回合尾於 simulate loop 內呼叫 `gm_hook(self.game, step)`。此 hook 供 affinity 自動調整使用；**GMDirector 不註冊此 hook**，保持外置 API（`on_round_start`/`on_round_end`），由回合引擎在 `simulate()` 之外呼叫——GM 的 LLM 決策不應阻塞模擬 loop 內部。

### 1.3 GM 的 LLM 客戶端：復用 `create_llm_model`，獨立實例

`modules/model/llm_model.py` 第 170-182 行 `create_llm_model(llm_config)` 已支援 `provider: "openai"`（magentic `OpenaiChatModel`，支援自訂 `base_url`）。GM 直接建立自己的實例，不借用任何 agent 的 `_llm`：

- 好處一：GM 可用不同 model（例如 agents 用平價模型、GM 用強模型），配置獨立。
- 好處二：`LLMModel.completion(retry=10, failsafe=...)`（第 24-55 行）自帶 retry + failsafe，GM 的「永不阻塞」保證直接繼承，無需重寫。
- 好處三：`get_summary()` 可分開統計 GM 的 token 消耗。

Structured output 路徑：`OpenAILLMModel._completion` 第 81-88 行用 magentic `@prompt(...) -> return_type`，`return_type` 為 pydantic model 時自動走 structured output。注意 magentic 要求 `return_type` 是**函數返回註解**，且現有代碼習慣用 `class XxxResponse(BaseModel): res: ...` 包一層（見 `scratch.py` 第 55-58 行），GM 模板沿用此慣例。

### 1.4 記憶注入：`_add_concept(poignancy_override=...)`（✅ seam 已由 affinity 建好）

affinity commit 已為 `Agent._add_concept()`（`agent.py` 第 647-673 行）加入 `poignancy_override` 參數：傳入即跳過 LLM poignancy 評分（`completion("poignancy_event")` 會讓玩家意志被評低分而淹沒）。GM 注入與 affinity 注入走同一條路徑：

```python
agent._add_concept(
    "event",               # 永遠用 "event"（GM 意志是「發生過的事」；affinity 的 "thought" 是內心態度，語義不同，不統一）
    Event(...),
    poignancy_override=8,  # 選項=8，自訂命令=10（邊界情況 7）
)
```

`_add_concept` 內部調 `Associate.add_node()`（`associate.py` 第 166-194 行），以 `event.get_describe()` 做 embedding（第 188 行），所以 **`describe` 的文字質量直接決定檢索質量**，必須是完整、自包含、繁體書面語的第三人稱句子。

另加 `agent.status["poignancy"] += 20`，推 agent 盡快跨過 `reflect()` 閾值（`agent.py` 第 346 行：`status["poignancy"] < think_config["poignancy_max"]` 則 skip；`config.json` 的 `think.poignancy_max = 150`）。注意 `status` dict 由 `SimulateServer.simulate()` 外層持有並傳入 `agent.think(status, agents)`——但 `Agent.__init__` 第 38 行 `status = {"poignancy": 0}` 是 agent 自身的實例屬性，checkpoint 裡 `config["agents"][名]["status"]` 由 `agent.to_dict()`（第 678-688 行）寫出、resume 時還原。GM 改的是 **agent 實例的 `self.status`**，下一 step checkpoint 自然持久化，無需額外處理。

### 1.5 注入記憶的語言與謂語規範（防誤觸硬編碼）

本地化 commit 已把 `agent.py` 全部硬編碼中文字串常量化到 `modules/prompt/keywords.py`（**繁體香港書面語 SSoT**）。已驗證的關鍵字判斷：

| 位置 | 判斷 | 影響 |
|---|---|---|
| `agent.py` 第 121 行 | `plan["describe"] == "sleeping"` or `KW_SLEEP in plan["describe"]`（`"睡"`，簡繁同形） | 只作用於 schedule plan，不作用於記憶 concept，注入不受影響 |
| `agent.py` 第 305 行 | `event.object == "idle"` or `KW_IDLE`（`"空閒"`） | 只作用於 `percept()` 的 tile event，注入繞過 percept，不受影響 |
| `agent.py` 第 311 行 | `event.fit(self.name, KW_CHAT)`（`"對話"`）→ node_type="chat" | 同上，只在 percept 內 |
| `agent.py` 第 658-661 行 | `event.fit(None, "is", "idle")` / `fit(None, KW_AT_THIS_TIME, KW_IDLE)` → poignancy=1 | `poignancy_override` 分支在最前，傳了 override 就不會落到此判斷；但為防日後有人改走無 override 路徑，注入 event 的 `predicate/object` **禁用** `is/idle/此時/空閒` 組合 |
| `agent.py` `is_awake()` | `fit(name, "is", "sleeping")` / `fit(name, KW_ONGOING, KW_SLEEPING)`（`"睡覺"`） | 只作用於 `self.action.event`，注入記憶不進 action，不受影響 |

規範（寫入 `MemoryInjector` 的 docstring 與 GM prompt）：
- `subject` = 目標 agent 名（繁體，與 `config["agents"]` key 逐字相同）。
- `predicate`：選項注入用 `"得知"`；自訂命令用 `"被命運驅使"`。
- `object` = 顯式傳 `"命運的提示"`（`Event.__init__` 對空 object 會 fallback 成 `KW_IDLE`，必須避免）。
- `describe`：完整第三人稱句子，**繁體書面語**，自包含（含相關角色名），**不得包含** `keywords.py` 的保留字：`KW_SLEEPING`（睡覺）、`KW_CHAT`（對話）、`KW_IDLE`（空閒）、`KW_PENDING`（待開始）。`MemoryInjector.FORBIDDEN_TOKENS` **直接引用 `modules.prompt.keywords` 常量**，不自行 hardcode（本地化時跟住改）。命中則拒絕注入並記 log。

### 1.6 好感度可見化：`scratch.currently` 前綴（不改 `scratch.py`）

PRD 依賴第 4 點給了兩個選項（改 `base_desc` 模板 / 前綴 `currently`）。**選擇前綴 `currently`**，理由：

1. `Scratch._base_desc()`（`scratch.py` 第 30-43 行）已把 `currently` 代入 `base_desc.txt` 模板，前綴方式**零代碼改動**——`scratch.currently` 是普通實例屬性（第 18 行 `self.currently = currently`），外部直接賦值即可。
2. 改模板需動 `base_desc.txt`（屬 Prompt 本地化系統的 29 模板範疇），跨系統改動增加協調成本。
3. 每回合開始時由回合引擎呼叫 `GMDirector.on_round_start()` → 內部 `apply_relations_prefix(game.agents)` 重設前綴，覆蓋 agents 自己對 `currently` 的更新（`retrieve_currently` 會改寫它，屬預期——前綴是「回合級快照」，回合內被自然演替覆蓋可接受，下回合再注入）。

**實作位置**：`story_weaver/gm/relations.py`（GM 層新模塊），**復用 `story_weaver.affinity.store.AffinityStore`** 讀矩陣（見 2.3），不重寫存儲。前綴堆疊防護：剝離上回合前綴再套新前綴（上回合前綴存 `gm_state.json` 的 `last_relations_prefix` 欄位）。

前綴格式（繁體書面語，band 標籤用 `affinity.models.BANDS`）：

```
【人際關係】你對約翰的好感度為 65（友好）；約翰對你的好感度為 45。你對埃迪的好感度為 80（摯愛/至交）；埃迪對你的好感度為 70。
```

### 1.7 回合增量採集：node_id 快照 diff（不改 simulate loop）

`Agent.concepts` 每 step 被 `percept()` 重置（`agent.py` 第 288 行 `self.concepts, valid_num = [], 0`），無法在回合結束時回溯整回合事件。但 `Associate.memory` 是 `{node_type: [node_id, ...]}`，新節點 `insert(0)`（`associate.py` 第 190 行），且 node_id 由 LlamaIndex 生成、單調唯一。

故 `RoundDeltaCollector` 做法：
- **回合開始**：對每個 agent 記錄 `set(associate.memory["event"] + memory["chat"] + memory["thought"])`，及 `game.conversation` 的 key 集合。
- **回合結束**：diff 出新 node_id → `associate.find_concept(node_id)` 得 `Concept`（`associate.py` 第 199-200 行）；diff 出新 conversation key → 取原文對話。

零侵入、支援 resume（快照存 `gm_state.json` 的 `round_baseline` 欄位，中途斷線可重建）。

---

## 2. 公開 API（Python）

### 2.1 `story_weaver/gm/director.py`

```python
class GMDirector:
    """敘事總監。非 Agent，獨立 LLM 客戶端。所有方法均不會拋異常出回合流程（內部 failsafe）。"""

    def __init__(
        self,
        sim_name: str,
        checkpoints_folder: str,
        llm_config: dict,          # {"provider": "openai", "model": ..., "base_url": ..., "api_key": ...}
        agent_names: list[str],
        prompts_dir: str = "data/prompts_gm",
    ) -> None: ...

    # ---- 回合生命週期（由回合引擎呼叫）----

    def on_round_start(self, server: "SimulateServer") -> None:
        """拍增量基線快照（conversation keys + 各 agent memory node_ids）；
        把好感度前綴注入各 agent 的 scratch.currently。"""

    def on_round_end(self, server: "SimulateServer", round_no: int) -> "GMDecision":
        """採增量 → 判斷靜默回合（邊界 9）→ LLM 生成摘要/分支/選項 →
        持久化 pending_decision → 回傳 GMDecision。LLM 全敗時回傳 failsafe 決策（邊界 2）。"""

    def apply_player_choice(
        self,
        server: "SimulateServer",
        round_no: int,
        choice: "PlayerChoice",
    ) -> "InjectionReport":
        """依 choice 注入記憶 + 寫 affinity.json + 更新 gm_state.json timeline +
        清除 pending_decision。type=="skip" 時只更新 timeline、零注入（邊界 1）。"""

    def parse_custom_command(self, text: str) -> "CustomCommandParse":
        """POST /api/round/decide 前先呼叫（前端即時驗證用，亦可由 decide 內部呼叫）。
        feasible=False 不消耗回合（邊界 6）。"""

    # ---- 終章 ----

    def generate_finale(self, server: "SimulateServer") -> "Finale":
        """用累積 timeline 跑 gm_finale.txt，寫入 gm_state.json["finale"]。可重複呼叫（冪等，已生成則直接回傳）。"""

    # ---- 恢復 ----

    def get_pending_decision(self) -> "GMDecision | None":
        """resume 時前端重開 modal 用（邊界 3）。選項逐字取自已持久化的 pending_decision，不重跑 LLM。"""

    @classmethod
    def resume(
        cls,
        sim_name: str,
        checkpoints_folder: str,
        llm_config: dict,
    ) -> "GMDirector":
        """從 gm_state.json + affinity.json 重建。檔案缺失時視為新遊戲（邊界 4）。"""
```

### 2.2 `story_weaver/gm/injector.py`

```python
class MemoryInjector:
    # 直接引用 modules/prompt/keywords.py 常量（本地化 SSoT），不自行 hardcode
    FORBIDDEN_TOKENS: tuple[str, ...] = (KW_SLEEPING, KW_CHAT, KW_IDLE, KW_PENDING)

    def inject(
        self,
        agent: "Agent",
        describe: str,
        predicate: str,          # "得知" | "被命運驅使"
        poignancy: int,          # 8（選項）| 10（自訂命令）
        poignancy_boost: int = 20,
    ) -> str:                    # 回傳 node_id
        """調 agent._add_concept("event", event, poignancy_override=poignancy)
        （agent.py:647，跳過 LLM 評分），再 agent.status["poignancy"] += poignancy_boost。
        describe 含 FORBIDDEN_TOKENS 時拋 ValueError（上層捕獲並記 log）；
        agent 瞓覺都照注（邊界 8）。"""
```

### 2.3 好感度：復用 `story_weaver/affinity/store.py`（✅ 已存在，不新建）

affinity 系統已實作 `AffinityStore`（`story_weaver/affinity/store.py`），存儲於 **`config["affinity"]` 頂層 key**（in-place 修改，隨 `SimulateServer.simulate()` 每 step 的 checkpoint dump 自動持久化，`get_config_from_log()` resume 自動還原）。**GM 不建獨立 `affinity.json`**——雙寫會造成 checkpoint 與 json 檔不同步。

GM 消費方式：

- **讀**：`AffinityStore(config["affinity"], agent_names)` 包住 server config 的引用；`store.get(from, to).value`、`store.to_dict()`。
- **寫（GM 調整）**：`GMRoundAnalysis.suggested_affinity_changes` → 轉 `GMAdjustmentItem` → 經 `story_weaver.affinity.gm.apply_gm_response()` 落地（白名單過濾、雙重 clamp、變動記憶注入、rounds log 全套已有，**接入不重寫**）。
- **寫（玩家 slider）**：`apply_player_choice` 的 `affinity_overrides` 同樣轉 `GMAdjustmentItem` 行 `apply_gm_response`。
- **前綴**：`story_weaver/gm/relations.py` 新增 `render_relations_block(store, agent_name)` 與 `apply_relations_prefix(store, agents, state)`（含剝離上回合前綴防堆疊，見 1.6）。

```python
# story_weaver/gm/relations.py
def render_relations_block(store: "AffinityStore", agent_name: str) -> str:
    """生成 1.6 節的【人際關係】前綴文字。全部關係皆 0 時回空字串。"""

def apply_relations_prefix(
    store: "AffinityStore",
    agents: dict[str, "Agent"],
    last_prefixes: dict[str, str],   # gm_state["last_relations_prefix"]，用於剝離
) -> dict[str, str]:                 # 回傳本回合前綴（落 gm_state 供下回合剝離）
    """對每個 agent：剝離 last_prefixes[name]（若 currently 以它開頭），
    再 scratch.currently = 新前綴 + "\\n" + 剩餘 currently。"""
```

### 2.4 `story_weaver/gm/state.py`

```python
class GMStateStore:
    def __init__(self, path: str) -> None: ...          # path = <checkpoints>/gm_state.json
    def init_new(self, story_seed: str, agent_names: list[str]) -> None: ...
    def append_timeline(self, entry: "TimelineEntry") -> None: ...
    def set_pending_decision(self, decision: "GMDecision") -> None: ...
    def get_pending_decision(self) -> "GMDecision | None": ...
    def clear_pending_decision(self) -> None: ...
    def log_injection(self, record: "InjectionRecord") -> None: ...
    def set_round_baseline(self, baseline: "RoundBaseline") -> None: ...
    def get_round_baseline(self) -> "RoundBaseline | None": ...
    def set_finale(self, finale: "Finale") -> None: ...
    def build_story_timeline(self) -> list["TimelineEntry"]:
        """故事回顧輸出：story_seed 作第 0 項 + 全部 timeline entries，對白保留原文。"""
    def save(self) -> None:
        """原子寫入：先寫 .tmp 再 os.replace()，防寫到一半斷電（邊界 4）。"""
    @classmethod
    def load(cls, path: str) -> "GMStateStore":
        """JSON 損毀時嘗試 .tmp 備份；再敗則從零開始並記 error（timeline 歸零但流程不斷）。"""
```

### 2.5 `story_weaver/gm/delta.py`

```python
@dataclass
class RoundBaseline:
    conversation_keys: list[str]
    memory_node_ids: dict[str, list[str]]   # agent_name -> node_ids
    sim_time: str                            # config["time"]，例 "20240213-14:30"

@dataclass
class RoundDelta:
    conversations_delta: dict    # {時間key: [{"A -> B @ 地址": [[名, 對白], ...]}]}，原文
    events_delta: list[dict]     # Concept.abstract() 展開 + agent_name 欄位
    agent_states: dict           # config["agents"][名] 的 currently/status/action/schedule 摘要
    is_quiet: bool               # 邊界 9：無對話且全部新 event poignancy<=1

class RoundDeltaCollector:
    def snapshot(self, server: "SimulateServer") -> RoundBaseline: ...
    def collect(self, server: "SimulateServer", baseline: RoundBaseline) -> RoundDelta: ...
```

### 2.6 Flask Routes（GM 相關；route 函數體由回合引擎系統實作，本節為契約）

| Method | Path | Request JSON | Response JSON | 說明 |
|---|---|---|---|---|
| POST | `/api/round/start` | `{"name": str, "steps": int = 12}` | `GMDecision`（見 3.2）| 跑 `simulate(steps)` → `on_round_end()`。整段同步阻塞（N 步推演耗時以分鐘計，前端顯示「推演中…」；如回合引擎改用 background thread + polling，此 route 回 `{"status": "running"}` 而改由 `GET /api/round/result` 取——**此為回合引擎的決定，GM 無所謂**） |
| POST | `/api/round/decide` | `{"name": str, "round_no": int, "player_choice": PlayerChoice}` | `InjectionReport` | `apply_player_choice()`。`player_choice.type=="custom"` 或含 `text` 時先內部 `parse_custom_command()`，`feasible=false` 回 HTTP 200 + `InjectionReport.refused`（不消耗回合，邊界 6） |
| POST | `/api/gm/parse_command` | `{"name": str, "text": str}` | `CustomCommandParse` | 前端輸入框即時驗證（debounce），不推進任何狀態 |
| GET | `/api/gm/pending?name=<sim>` | — | `GMDecision \| {"pending": null}` | resume 後前端重開 modal（邊界 3） |
| POST | `/api/gm/finale` | `{"name": str}` | `Finale` | 第 10 回合後或玩家提早完結（邊界 10） |

錯誤約定：GM 內部 LLM 失敗**永不**回 5xx；5xx 只保留給「simulation 不存在」（404）與「request body 驗證失敗」（422）。

---

## 3. 數據模型（pydantic，繁體 description 供 LLM schema 用）

### 3.1 LLM structured output 模型（`models.py`）

```python
from pydantic import BaseModel, Field
from typing import Literal

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
    """gm_round_summary.txt 的 structured output（一次 LLM call 完成摘要+分支+選項）"""
    summary: str = Field(description="本回合摘要，3-5 句繁體中文敘事")
    branch_point: str = Field(description="本回合最重要的劇情分支點，一句話")
    options: list[GMOption] = Field(description="2-3 個分支選項", min_length=2, max_length=3)
    suggested_affinity_changes: list[AffinitySuggestion] = Field(description="好感度建議變動，可為空列表")

class GMRoundAnalysisResponse(BaseModel):   # 沿用 scratch.py 的 res 包裝慣例
    res: GMRoundAnalysis

class CustomCommandParse(BaseModel):
    targets: list[str] = Field(description="命令涉及的角色名，必須在角色名單內，可為空")
    command_event_describe: str = Field(description="改寫為第三人稱完整事件描述，繁體書面語")
    feasible: bool = Field(description="命令是否可執行（角色存在、語義明確、內容恰當）")
    refuse_reason: str | None = Field(description="不可執行時的拒絕理由，繁體中文，可執行時為 null")

class CustomCommandParseResponse(BaseModel):
    res: CustomCommandParse

class FinaleNarrative(BaseModel):
    ending: str = Field(description="故事終章敘事，繁體中文，200-400字")
    character_epilogues: list[dict] = Field(description="每個角色的結局一段話：[{name, epilogue}]")

class FinaleNarrativeResponse(BaseModel):
    res: FinaleNarrative
```

### 3.2 持久化 / API 模型（`models.py`）

```python
class AffinityChange(BaseModel):
    from_agent: str
    to_agent: str
    delta: int
    new_value: int                       # clamp 後的值
    reason: str

class DialogueBlock(BaseModel):
    speakers: str                        # "梅 -> 約翰"（conversation.json 原 key 格式）
    address: str
    lines: list[tuple[str, str]]         # [[名, 對白原文], ...]，不經 LLM 改寫

class TimelineEntry(BaseModel):
    round: int                           # 0 = story_seed
    summary: str
    key_events: list[str]
    dialogues: list[DialogueBlock]
    branch_point: str | None
    options_offered: list[GMOption]
    player_choice: "PlayerChoice | None"
    affinity_changes: list[AffinityChange]
    is_quiet: bool = False               # 邊界 9 標記
    had_error: bool = False              # 邊界 2/4 標記

class PlayerChoice(BaseModel):
    type: Literal["option", "custom", "option+custom", "skip", "finish"]
    option_id: str | None = None         # "A" | "B" | "C"
    text: str | None = None              # 自訂命令原文
    affinity_overrides: list[AffinitySuggestion] = []   # 玩家手動 slider 值（delta 語義）

class GMDecision(BaseModel):
    """on_round_end 回傳 + pending_decision 持久化 + /api/round/start 回應"""
    round_no: int
    summary: str
    branch_point: str | None             # 靜默回合 / failsafe 時為 None
    options: list[GMOption]              # 靜默回合 / failsafe 時為空 list
    suggested_affinity_changes: list[AffinitySuggestion]
    story_timeline: list[TimelineEntry]  # 故事回顧完整內容（含 story_seed 第 0 項）
    is_failsafe: bool = False            # 邊界 2：前端顯示「命運之線暫時模糊…」
    is_quiet: bool = False               # 邊界 9：前端顯示「平靜的一日」
    affinity_snapshot: dict[str, dict[str, int]]   # 渲染 slider 用當前值
    can_finish: bool                     # round_no >= 2

class InjectionRecord(BaseModel):
    round: int
    targets: list[str]
    content: str
    node_ids: dict[str, str]             # agent_name -> node_id
    poignancy: int
    source: Literal["option", "custom"]
    error: str | None = None

class InjectionReport(BaseModel):
    """POST /api/round/decide 回應"""
    ok: bool
    message: str                         # 「你的意志已注入小鎮」等繁體文案
    injected: list[InjectionRecord]
    affinity_changes: list[AffinityChange]
    refused: CustomCommandParse | None = None   # feasible=false 時填入，ok=false

class Finale(BaseModel):
    timeline: list[TimelineEntry]
    narrative: FinaleNarrative
    affinity_table: list[AffinityChange]  # 全劇好感度變化總表（初值→終值 diff）
```

### 3.3 JSON 檔案結構

**`results/checkpoints/<name>/gm_state.json`**：

```json
{
  "version": 1,
  "story_seed": "玩家填的開端原文",
  "agent_names": ["梅", "約翰", "埃迪", "簡"],
  "timeline": [ "<TimelineEntry JSON>" ],
  "pending_decision": null,
  "injection_log": [ "<InjectionRecord JSON>" ],
  "branch_point_history": ["..."],
  "round_baseline": null,
  "last_relations_prefix": {"梅": "【人際關係】..."},
  "finale": null,
  "errors": [{"round": 3, "stage": "round_summary", "error": "...", "at": "20260728-21:04"}]
}
```

**好感度存儲**：`config["affinity"]`（`simulate-*.json` checkpoint 內），由 affinity 系統持有，格式見 `docs/spec/affinity.md`。缺邊語義 = 0（中性）。

---

## 4. 整合點（具體到文件與行）

| # | 文件 | 位置 | 改動 | 侵入度 |
|---|---|---|---|---|
| 1 | `generative_agents/start.py` | 第 146 行 `get_config_from_log()` | ✅ 已白名單化（affinity commit），本系統零改動 | 零 |
| 2 | `generative_agents/start.py` | 無 | `simulate()` 零改動。GM 只在其**外**被回合引擎呼叫：`server.simulate(N, stride)` 前 `gm.on_round_start(server)`，後 `gm.on_round_end(server, round_no)`。已有的 `gm_hook` seam（第 93-94、130-131 行）歸 affinity 自動調整用，GMDirector 不註冊 | 零 |
| 3 | `generative_agents/modules/agent.py` | 無 | 零改動。注入走實例方法：`agent._add_concept("event", ..., poignancy_override=N)`（第 647-673 行）、`agent.status["poignancy"] += 20`、`agent.scratch.currently = ...` | 零 |
| 4 | `generative_agents/modules/prompt/scratch.py` | 無 | 零改動（選了 currently 前綴方案，見 1.6） | 零 |
| 5 | `generative_agents/modules/model/llm_model.py` | 無 | 零改動，`create_llm_model()` 直接用 | 零 |
| 6 | `results/checkpoints/<name>/` | 目錄 | 新增 `gm_state.json` 一個檔案（好感度存 `config["affinity"]`，隨 `simulate-*.json` 走） | 新增檔案 |
| 7 | `generative_agents/data/prompts_gm/` | 新目錄 | 3 個繁體模板，GM 自己讀取，不經 `Scratch.build_prompt`（Scratch 寫死 `data/prompts`，`scratch.py` 第 19 行）。GM 專屬模板集中此處；`data/prompts/gm_adjust_affinity.txt` 屬 affinity 系統，不搬 | 新增目錄 |

### 回合引擎呼叫時序（契約，實作屬回合引擎系統）

```python
gm = GMDirector.resume(name, ckpt_folder, gm_llm_config)  # 或新遊戲時 GMDirector(...)
server = SimulateServer(name, static_root, ckpt_folder, sim_config, start_step, ...)

# 每回合：
gm.on_round_start(server)              # 拍基線 + 好感度前綴
server.simulate(steps_per_round, stride)
decision = gm.on_round_end(server, round_no)   # → 回前端 modal
# ...玩家輸入...
report = gm.apply_player_choice(server, round_no, choice)
# round_no += 1，循環；round_no > 10 或 choice.type=="finish" → gm.generate_finale(server)
```

---

## 5. 文件計劃

### 新建

| 路徑 | 內容 |
|---|---|
| `generative_agents/story_weaver/gm/__init__.py` | re-export `GMDirector`, `GMDecision`, `PlayerChoice`（`story_weaver/__init__.py` 已存在） |
| `generative_agents/story_weaver/gm/director.py` | `GMDirector`（2.1） |
| `generative_agents/story_weaver/gm/models.py` | 全部 pydantic 模型（3.1、3.2） |
| `generative_agents/story_weaver/gm/state.py` | `GMStateStore`（2.4） |
| `generative_agents/story_weaver/gm/relations.py` | `render_relations_block` / `apply_relations_prefix`（1.6、2.3；復用 affinity 的 AffinityStore） |
| `generative_agents/story_weaver/gm/injector.py` | `MemoryInjector`（2.2、1.5） |
| `generative_agents/story_weaver/gm/delta.py` | `RoundDeltaCollector`, `RoundBaseline`, `RoundDelta`（2.5） |
| `generative_agents/story_weaver/gm/prompter.py` | 模板載入 + prompt 組裝（string.Template，同 `scratch.py` 手法），輸出 `LLMModel.completion()` 所需的 prompt + return_type |
| `generative_agents/data/prompts_gm/gm_round_summary.txt` | 繁體模板：回合摘要 + 分支點 + 2-3 選項 + 好感度建議（一次 call） |
| `generative_agents/data/prompts_gm/gm_custom_command.txt` | 繁體模板：自訂命令解析（約束：當前角色名單、feasible 判斷） |
| `generative_agents/data/prompts_gm/gm_finale.txt` | 繁體模板：終章敘事 + 角色結局 |
| `generative_agents/data/gm_config.json` | `{"llm": {...openai 兼容配置...}, "option_poignancy": 8, "custom_poignancy": 10, "poignancy_boost": 20, "max_rounds": 10, "min_rounds_to_finish": 2}` |
| `tests/story_weaver/test_gm_director.py` | Done When 各項測試（mock LLM）。測試目錄已存在（affinity tests 同層），沿用 repo root `tests/` |
| `tests/story_weaver/test_gm_relations.py` | 前綴渲染、剝離防堆疊、空矩陣 |
| `tests/story_weaver/test_gm_state.py` | 原子寫入、損毀恢復、pending 決策逐字重現 |
| `tests/story_weaver/test_gm_injector.py` | 注入後 `retrieve_events` 可檢索、FORBIDDEN_TOKENS 攔截 |
| `docs/spec/gm-director.md` | 本文件 |

### 修改

**無現有文件需要修改。**（`start.py` 白名單已由 affinity commit 完成）

---

## 6. 與其他 5 個系統的契約

### 6.1 ← Setup/角色配置系統（GM 消費）

| 契約 | 內容 |
|---|---|
| `GMStateStore.init_new(story_seed, agent_names)` | Setup 完成後呼叫一次，寫入故事開端與角色名單 |
| `config["affinity"]` 初始值 | Setup 依玩家填的雙向好感度寫入模擬 config（affinity 系統 `SetupAffinityResult`）；之後所有權移交 GM（經 `apply_gm_response`） |
| 角色名單即真相 | `agent_names` 用於 `gm_custom_command` 的 targets 約束與 `AffinityStore` 邊校驗；GM 假設回合中角色不增不減 |
| GM 只讀 `agent.json` 的 `scratch`（職業/性格經 `base_desc` 自然進入 agents 的 LLM context），永不寫 | scratch 是「角色原點」 |

### 6.2 ← 回合/推演引擎系統（GM 被呼叫，最強耦合）

| 契約 | 內容 |
|---|---|
| 呼叫時序 | 見第 4 節時序碼。`on_round_start` → `simulate(N)` → `on_round_end` →（玩家）→ `apply_player_choice` |
| 回合數判斷 | 引擎持有 `round_no`；`round_no > max_rounds` 或 `choice.type=="finish"`（且 `round_no >= min_rounds_to_finish`）時呼叫 `generate_finale()` |
| `SimulateServer` 傳入 | GM 只讀 `server.game`（`conversation`、`agents`）、`server.config`（`agents`、`time`、`step`），保證不呼叫任何會改變模擬狀態的方法 |
| 阻塞模型 | GM 不管同步/異步（見 2.6 `/api/round/start` 說明），兩種模式下 API 不變 |

### 6.3 → 前端 UI 系統（GM 產出 JSON）

| 契約 | 內容 |
|---|---|
| `GMDecision` schema | 見 3.2。modal 四區塊全部由此供數：`story_timeline`（故事回顧）、`summary`、`options`、`suggested_affinity_changes` + `affinity_snapshot`（slider 初值） |
| 旗標驅動文案 | `is_failsafe` → 「命運之線暫時模糊…」+ 只有「任由發展」；`is_quiet` → 「平靜的一日」+ 無選項卡；`can_finish` → 顯示「完結故事」按鈕 |
| `PlayerChoice` 提交 | 見 3.2；`affinity_overrides` 永遠是 delta 語義（前端把 slider 終值減 snapshot 現值） |
| `InjectionReport.refused` | `feasible=false` 時前端原框保留文字 + 顯示 `refuse_reason`，不推進回合 |
| 對白原文保證 | `TimelineEntry.dialogues[].lines` 逐字來自 `conversation.json`，前端可原樣渲染 |

### 6.4 ⇄ 存檔/恢復系統（互相依賴）

| 契約 | 內容 |
|---|---|
| 檔案所有權 | `gm_state.json` 歸 GM 系統寫、存檔系統負責備份/搬運（如壓縮歸檔）；GM 提供 `GMStateStore.load/save`。好感度隨 `simulate-*.json` checkpoint 走（`config["affinity"]`），無獨立檔案 |
| resume 流程 | 存檔系統載入 `simulate-*.json`（經 `get_config_from_log`）後，由回合引擎 `GMDirector.resume()` 載入 GM 狀態；`get_pending_decision()` 非空 → 通知前端重開 modal |
| 損毀降級 | `gm_state.json` 損毀 → 從零開始 + errors 記錄；LlamaIndex storage 損毀 → 由存檔系統重建空索引，GM 的 timeline 不受影響，下一回合 `had_error=True` 並在摘要提示「小鎮的記憶出現了裂縫」（邊界 4） |
| `start.py` 白名單 | ✅ 已實施（`start.py:146`），存檔系統任何新 metadata json 都不需再動 `get_config_from_log` |

### 6.5 ← Prompt 本地化系統（前置依賴）

| 契約 | 內容 |
|---|---|
| GM 3 模板直接以繁體撰寫 | 不依賴本地化系統，可先行 |
| 29 模板簡轉繁必須先於或同步上線 | GM 注入的繁體 describe 會進入 `retrieve_focus` 檢索結果，餵給簡體模板會簡繁混雜（PRD 依賴第 6 點） |
| 禁詞清單共享 | `MemoryInjector.FORBIDDEN_TOKENS` 直接引用 `modules/prompt/keywords.py` 的 `KW_SLEEPING/KW_CHAT/KW_IDLE/KW_PENDING`（本地化 SSoT，✅ 29 模板簡轉繁已完成）——這些保留字是 agent.py 邏輯判斷依賴，**永不得翻譯或改寫** |

---

## 7. 測試對應（Done When → 測試）

| Done When | 測試 |
|---|---|
| 回合掛鉤零侵入 | `test_gm_director.py::test_on_round_end_reads_only`（mock server，assert 無寫入呼叫） |
| 故事回顧正確 | `test_gm_state.py::test_timeline_accumulates_10_rounds`（對白逐字比對 conversation.json） |
| 記憶注入生效 | `test_gm_injector.py::test_inject_retrievable`（注入後 `retrieve_events("關鍵字")` 命中 + `status["poignancy"]` +20） |
| 自訂命令 F1 | `test_gm_director.py::test_parse_command_20_cases`（mock LLM 回固定 parse，校驗 targets 抽取邏輯與名單約束） |
| 好感度 clamp | `test_affinity_gm.py`（✅ affinity 已有，雙重 clamp + 白名單）+ `test_gm_relations.py::test_render_and_prefix`（前綴渲染/剝離） |
| 失敗安全 | `test_gm_director.py::test_llm_total_failure_failsafe`（mock `completion` 全回 None → `is_failsafe=True`、流程完成、error 入 log） |
| 決策持久化 | `test_gm_state.py::test_pending_decision_roundtrip`（set → save → load → 逐字相等） |
| 語言 | `test_gm_injector.py::test_forbidden_tokens_rejected` + prompt 模板人工 review checklist |
| 終章 | `test_gm_director.py::test_finale_idempotent` |
| 無輸贏語義 | repo-wide `grep -ri "win\|lose\|score" story_weaver/` 為空（CI 檢查） |

---

## 8. 風險與緩解

| 風險 | 緩解 |
|---|---|
| `scratch.currently` 前綴被 agents 自身邏輯覆寫，好感度「時隱時現」 | 每回合開始重注（1.6）；接受回合內被覆寫——agents 本來就該把關係內化成自己的敘述 |
| LlamaIndex node_id 在 resume 後是否穩定（快照 diff 依賴） | node_id 存於 `storage/` 持久化（`Associate.to_dict` → `_index.save()`，`associate.py` 第 256-258 行），resume 後一致；baseline 亦落盤 `gm_state.json`，雙保險 |
| magentic structured output 對複雜巢狀 model 相容性 | `GMRoundAnalysis` 巢狀三層，屬 magentic 支援範圍；若實測不穩，降級為單層 `res: str`（JSON 字串）+ GM 內部 `model_validate_json` 自行解析（`OllamaLLMModel._completion` 第 144-165 行已有此 fallback 手法可參考） |
| 同步阻塞的 `/api/round/start` 令 Flask 單線程卡死其他請求 | 由回合引擎決定 background thread 化；GM API 兩種模式兼容（2.6） |
| 玩家注入與模板語言混雜 | ✅ 已解除：29 模板簡轉繁完成（localization commit），注入的繁體 describe 與模板語境一致 |
