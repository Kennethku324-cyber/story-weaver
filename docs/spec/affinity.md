# 技術規格：關係／好感度系統（Affinity System）

> 版本：v1.0 ｜ 日期：2026-07-28
> 對應 PRD：`docs/prd/affinity.md`（關係/好感度系統）
> 代碼基礎：`/Users/kenneth/Projects/story-weaver`（GenerativeAgentsCN，Python 3.12 + Flask）
> 語言決策：所有新增／修改嘅 prompt 模板同埋注入記憶嘅文字，一律用繁體香港書面語。

---

## 0. 已驗證代碼事實（Spec 嘅地基）

以下行號以而家 repo 為準，實作前再 grep 一次確認：

| 事實 | 位置 |
|---|---|
| `SimulateServer.simulate()` 每 step 尾將成個 `self.config` dump 落 `simulate-<time>.json` | `generative_agents/start.py:97-98` |
| 每 step 逐個 agent 行 `game.agent_think(name, status)`，然後 `config["agents"][name].update(agent.to_dict())` | `start.py:76-87` |
| `get_config()` 組新遊戲 config（`stride/time/maze/agent_base/agents`） | `start.py:138-157` |
| `get_config_from_log()` resume 時讀最新 checkpoint 成個 config 返嚟 | `start.py:111-134` |
| `Game.__init__` 由 config 建 agents；`create_game()` 將 Game 放入全域 `GenerativeAgentsMap` | `modules/game.py:15-37, 82-87` |
| `Agent.completion(func_hint, *args)` → `scratch.prompt_<func_hint>(*args)` → `Result(prompt, callback, failsafe, return_type)` | `modules/agent.py:92-105` |
| `_chat_with`：`decide_chat`（L523）→ `summarize_relation` ×2（L528-531）→ `generate_chat` 循環（L533-574） | `modules/agent.py:501-594` |
| `_wait_other` 用 `decide_wait`（L603） | `modules/agent.py:596-618` |
| `_add_concept(e_type, event, create, expire, filling)` 內部用 LLM 評 poignancy（`poignancy_chat` / `poignancy_event`） | `modules/agent.py:632-656` |
| `Scratch.build_prompt(template, data)` 用 `string.Template.substitute`：**data 多畀 key 唔會出事**，模板無 `${var}` 就唔 render——向後兼容 | `modules/prompt/scratch.py:21-28` |
| `prompt_decide_chat`（L411）、`prompt_decide_wait`（L478）、`prompt_summarize_relation`（L557）、`prompt_generate_chat`（L577） | `modules/prompt/scratch.py` |
| `Associate.add_node(node_type, event, poignancy, ...)` 直接收 poignancy int，入 LlamaIndex metadata | `modules/memory/associate.py:166-194` |
| `Event(subject, predicate, object, address, describe, emoji)`；`describe` 非空時 `get_describe()` 直接返回 describe | `modules/memory/event.py:5-32` |
| `LLMModel.completion(prompt, retry=10, callback, failsafe, return_type)`：pydantic structured output，失敗 retry 10 次，最終返 `failsafe` | `modules/model/llm_model.py:24-55` |
| 現有 Flask server 係 `replay.py`（`app = Flask(...)`, port 5000），template/static 指住 `frontend/` | `generative_agents/replay.py:7-13` |
| 現有 29 個模板喺 `generative_agents/data/prompts/*.txt`，簡體 | `data/prompts/` |

---

## 1. 架構決策

### 1.1 新代碼放邊：`generative_agents/story_weaver/` 新 package

**決定：新開 `generative_agents/story_weaver/affinity/` package 裝晒本系統嘅領域邏輯；對 `modules/` 只做 4 處最小縫合（seam）修改。**

```
generative_agents/
├── modules/                  # GenerativeAgentsCN 上游引擎（盡量唔郁）
├── story_weaver/             # 【新】Story Weaver 專屬層
│   ├── __init__.py
│   └── affinity/
│       ├── __init__.py       # 公開 API re-export
│       ├── models.py         # pydantic / dataclass 數據模型
│       ├── store.py          # AffinityStore（矩陣持有 + clamp + band + relation_line）
│       ├── memory.py         # 記憶流投影（初始注入 + 變動注入）
│       ├── gm.py             # GM 調整：prompt 構建 + structured output 解析 + apply
│       └── api.py            # Flask Blueprint（俾 Setup / modal 用）
└── data/prompts/
    └── gm_adjust_affinity.txt  # 【新】
```

**理由：**

1. **上游可同步**：`modules/` 保持接近 GenerativeAgentsCN 原版，將來拉上游 bugfix（例如 LLM retry、schedule 修正）merge 衝突最細。本系統對 `modules/` 嘅改動壓到 4 個文件、每處 ≤ 10 行。
2. **cwd 兼容**：成個項目嘅相對路徑（`data/prompts`、`frontend/static`、`results/checkpoints`）都假設 cwd 係 `generative_agents/`。新 package 放喺 `generative_agents/story_weaver/` 可以直接 `from story_weaver.affinity import AffinityStore`，唔使郁 `sys.path`。
3. **後續 5 個系統（Setup、GM、modal、命令注入、記憶注入）都會落喺 `story_weaver/` 之下**，今次開咗個位，之後唔使再爭。

### 1.2 狀態持有：集中式矩陣，同 config 共享同一個 dict 對象

`Game.__init__` 入面：

```python
self.affinity = AffinityStore(config.setdefault("affinity", {}), list(self.agents.keys()))
```

`AffinityStore` **直接持有 `config["affinity"]` 呢個 dict 嘅引用**，所有 `set_affinity` / `adjust` 都係 in-place 修改佢。咁樣 `start.py:97` 每 step dump `self.config` 時，`affinity` 自動係最新值——**零同步代碼、零雙寫風險**，resume 經 `get_config_from_log()` 自動還原。呢個係 PRD「集中式存儲」嘅具體落地方式。

變動歷史（`affinity_changes`）放另一個頂層 key `config["affinity_rounds"]`（見 §2.3），同樣共享引用、自動入 checkpoint。

### 1.3 Prompt 注入：經 Scratch 加可選變數，唔改 completion() 呼叫鏈

PRD 提到「Scratch 唔識 Game，需喺 completion() 呼叫鏈注入」。**決定唔改鏈**：因為 `build_prompt` 用 `Template.substitute`，data 多畀 key 係安全嘅（模板無該變數就忽略），所以最細侵入做法係——喺 `scratch.py` 加一個私有 helper `_relation_line(a_name, b_name)`，佢 **lazy import** `get_game()` 攞 `game.affinity.relation_line(...)`；四個 prompt 函數嘅 data dict 各加一個 key `relation_line`。

- 舊模板（無 `${relation_line}`）行為完全唔變 → 原版 25 人小鎮模式零退化。
- 無 circular import：`game.py → agent.py → prompt/scratch.py` 係 import-time 鏈；`scratch.py` 對 `game.py` 嘅 import 放喺函數體內（runtime），嗰陣 Game 已經建好。
- Game 未建立（例如單元測試 Scratch）時 `_relation_line` 返回 `""`，模板 render 空行，唔 crash。

### 1.4 極端關係「偏置唔鎖死」

`decide_chat` 低好感度時 LLM 大概率返「否」，但系統**唔加硬性規則禁止對話**（PRD 邊界情況第 6 條）。所有影響都經 prompt 層嘅 `relation_line` 文字達成。

---

## 2. 數據模型

### 2.1 存儲結構（config 頂層 `affinity` key）

```jsonc
{
  "affinity": {
    "阿珍": {
      "阿強": { "value": -65, "label": "舊情人，分手時鬧得好僵" }
    },
    "阿強": {
      "阿珍": { "value": -40, "label": "想挽回" }
    }
  }
}
```

- key 永遠係角色**原名**（同 `config["agents"]` 嘅 key 一致），唔做 `replace(" ", "_")` 路徑轉換（嗰個只用喺 asset 路徑，`start.py:132`）。
- 雙向獨立，唔保證對稱。
- 缺漏嘅有序對視為 `{value: 0, label: ""}`，由 `AffinityStore.ensure_pairs()` 喺 Game 初始化時補齊（補齊後矩陣係 full N×(N-1)）。

Python 表示（`story_weaver/affinity/models.py`）：

```python
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
from pydantic import BaseModel, Field, field_validator

AFFINITY_MIN, AFFINITY_MAX = -100, 100
DELTA_CLAMP = 25            # 每回合單次調整上限
LABEL_MAX_LEN = 100

class AffinityEntry(BaseModel):
    value: int = Field(default=0, ge=AFFINITY_MIN, le=AFFINITY_MAX)
    label: str = Field(default="", max_length=LABEL_MAX_LEN)

# 存儲層直接用 dict[str, dict[str, AffinityEntry]] 嘅 JSON 形態，
# AffinityStore 內部操作 dict，進出時用 AffinityEntry 校驗。

@dataclass
class AffinityChange:
    """單次好感度變動（GM 調整 / 絕對重置）嘅記錄。"""
    from_agent: str
    to_agent: str
    old: int
    new: int
    delta: int               # new - old（absolute 重置時都係實際差值）
    reason: str
    absolute: bool = False   # True = GM 敘事重置（set_absolute）
    round: int = 0           # 第幾回合（由 GM 系統填入）
    time: str = ""           # utils.get_timer().get_date("%Y%m%d-%H:%M")

    def to_dict(self) -> dict:
        return asdict(self)
```

### 2.2 Setup 輸入模型（Flask / Python 共用）

```python
class RelationInput(BaseModel):
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    affinity: int = Field(ge=AFFINITY_MIN, le=AFFINITY_MAX)   # 超界由前端 clamp；後端 pydantic 拒收
    label: str = Field(default="", max_length=LABEL_MAX_LEN)

    model_config = {"populate_by_name": True}

class SetupAffinityPayload(BaseModel):
    agents: list[str] = Field(min_length=4)        # 已選角色白名單
    relations: list[RelationInput] = []

class SetupAffinityResult(BaseModel):
    affinity: dict[str, dict[str, AffinityEntry]]  # 補齊後嘅完整矩陣，可直接做 config["affinity"]
```

校驗規則（`store.validate_setup(payload) -> SetupAffinityResult | list[SetupError]`）：

1. `from_agent` / `to_agent` 必須 ∈ `agents`（白名單），唔 match → 收集 error `{from, to, message: "角色「阿強強」唔喺已選角色名單入面"}`，**逐格指出，絕唔靜默丟棄**（PRD 邊界情況 2）。
2. `from_agent == to_agent` → error（對角線停用）。
3. 同一 `(from, to)` 重複提交 → 後者覆蓋前者（視為玩家改咗主意），唔係 error。
4. 通過校驗後，對所有未出現嘅有序對補 `{value: 0, label: ""}`。

### 2.3 變動歷史（config 頂層 `affinity_rounds` key）

```jsonc
{
  "affinity_rounds": [
    {
      "round": 1,
      "step": 6,
      "time": "20240213-11:00",
      "changes": [
        { "from_agent": "阿珍", "to_agent": "阿強", "old": -65, "new": -50,
          "delta": 15, "reason": "阿強幫阿珍解圍", "absolute": false }
      ]
    }
  ]
}
```

- 由 GM 系統每回合尾 append；`AffinityStore` 持有同一個 list 引用，自動入 checkpoint。
- 決策 modal 讀最後一個 element；故事回顧系統過濾 `abs(delta) >= 20 or absolute == True` 做時間線節點。
- 冇變動嘅回合都要 append `{"round": N, "changes": []}`，modal 顯示「本回合無關係變動」。

### 2.4 GM structured output（`gm.py`）

```python
class GMAdjustmentItem(BaseModel):
    from_agent: str
    to_agent: str
    delta: int = Field(default=0, ge=-DELTA_CLAMP, le=DELTA_CLAMP)
    reason: str = Field(default="", max_length=200)
    set_absolute: bool = False
    absolute_value: Optional[int] = Field(default=None, ge=AFFINITY_MIN, le=AFFINITY_MAX)

class GMAdjustmentResponse(BaseModel):
    adjustments: list[GMAdjustmentItem] = []
```

Failsafe：`GMAdjustmentResponse(adjustments=[])`——LLM 連續 10 次返垃圾時，本回合靜默唔調整 + warning log（`llm_model.py:24-55` 嘅 retry/failsafe 機制直接做到，唔使自己寫 retry）。

### 2.5 數值 → 描述 band（注入 prompt 用）

`store.band_of(value: int) -> str`：

| 範圍 | 標籤 |
|---|---|
| 61 ~ 100 | 摯愛/至交 |
| 21 ~ 60 | 友好 |
| 1 ~ 20 | 略有好感 |
| 0 | 陌生/中立 |
| -20 ~ -1 | 略有反感 |
| -60 ~ -21 | 敵對 |
| -100 ~ -61 | 死敵/痛恨 |

`relation_line` 文案模板（繁體書面語）：

- 有 label：`「{a}對{b}的好感度為{value}（{band}）：{label}。」`
- 無 label 且 value ≠ 0：`「{a}對{b}的好感度為{value}（{band}）。」`
- value == 0：`「{a}與{b}並不相識（陌生/中立）。」`

---

## 3. 公開 API

### 3.1 Python API（`story_weaver/affinity/__init__.py` re-export）

```python
class AffinityStore:
    """集中式雙向好感度矩陣。持有 config["affinity"] dict 嘅引用，in-place 修改。"""

    def __init__(self, data: dict, agent_names: list[str]) -> None: ...
        # data 即 config.setdefault("affinity", {})；舊 checkpoint 傳入 {} 都唔會出事
        # 內部呼叫 self.ensure_pairs()

    def ensure_pairs(self) -> None:
        """補齊所有缺漏有序對為 {value: 0, label: ""}；清理唔喺 agent_names 嘅殘留 key（log warning）。"""

    def get(self, from_agent: str, to_agent: str) -> AffinityEntry: ...
        # 未知角色對返回 AffinityEntry(value=0, label="")，唔 raise

    def set_affinity(self, from_agent: str, to_agent: str, value: int, label: str = "") -> None:
        """Setup 寫入接口。白名單校驗 + clamp [-100, 100] + label 截 100 字。
        未知角色 → raise UnknownAgentError（Setup 層捕獲轉 400）。"""

    def adjust(self, from_agent: str, to_agent: str, delta: int,
               reason: str, absolute: bool = False, absolute_value: int | None = None) -> AffinityChange | None:
        """GM 調整接口。
        - absolute=False：delta clamp 到 [-25, +25]，new = clamp(old + delta)
        - absolute=True：new = clamp(absolute_value)，唔受 ±25 限制（敘事重置）
        - delta 最終為 0 → 返回 None（唔記錄、唔注入記憶）
        - 否則返回 AffinityChange，由呼叫方（gm.py）決定注入記憶同埋 append 去 affinity_rounds
        """

    def relation_line(self, from_agent: str, to_agent: str) -> str:
        """繁中一句描述（§2.5 模板），prompt 注入用。任何異常返回 "" 唔 raise。"""

    @staticmethod
    def band_of(value: int) -> str: ...

    def to_dict(self) -> dict:
        """返回持有嘅 dict 本身（唔係 copy）——checkpoint 共享引用嘅關鍵。"""

    def full_matrix_text(self) -> str:
        """GM prompt 用：成個矩陣嘅人讀文字版，每行一條非對角關係。"""


# ---- Setup 校驗 ----
def validate_setup(payload: SetupAffinityPayload) -> SetupAffinityResult: ...
    # 失敗 raise SetupValidationError(errors: list[SetupError])


# ---- 記憶流投影（memory.py） ----
def initial_poignancy(value: int) -> int:
    """|v| <= 60 → 8；61~80 → 9；81~100 → 10。"""

def inject_initial(store: AffinityStore, agents: dict[str, "Agent"],
                   meta: dict, logger) -> int:
    """遊戲開始時，對每條 value != 0 嘅關係，向 from 角色記憶流注入 thought concept：
      Agent._add_concept("thought",
          Event(subject=from_agent, predicate="對", object=to_agent,
                describe=f"{from_agent}與{to_agent}的關係：{label or band}（好感度{value}，{band}）"),
          poignancy_override=initial_poignancy(value))
    - 冧等（idempotent）：meta["affinity_initialized"] == True 就 skip（resume 唔會重複注入）
    - 注入後設 meta["affinity_initialized"] = True（meta 係 config.setdefault("affinity_meta", {})，跟 checkpoint 持久化）
    返回注入數量。"""

def inject_change(agent: "Agent", change: AffinityChange, logger) -> None:
    """|delta| >= 10 或 absolute 時注入 thought：
      一般：describe=f"{change.reason}，{from_agent}對{to_agent}的態度轉變了（好感度{old}→{new}）", poignancy 8
      absolute 重置：poignancy 10
    """


# ---- GM 接口（gm.py） ----
def build_gm_prompt(store: AffinityStore, round_events: list[str],
                    round_conversations: dict, logger) -> "Result":
    """讀 data/prompts/gm_adjust_affinity.txt，填入：
    ${matrix_text}（store.full_matrix_text()）、${events}、${conversations}
    返回 modules.prompt.scratch.Result 同構嘅 namedtuple，
    return_type = GMAdjustmentResponse，failsafe = GMAdjustmentResponse(adjustments=[])。"""

def apply_gm_response(store: AffinityStore, response: GMAdjustmentResponse,
                      agents: dict[str, "Agent"], rounds_log: list,
                      round_no: int, logger) -> list[AffinityChange]:
    """逐條 GMAdjustmentItem：
    1. 角色白名單過濾（LLM 幻覺角色名 → skip + warning）
    2. store.adjust(...)（內部雙重 clamp）
    3. 有變動 → inject_change()（|delta|>=10 或 absolute）
    4. 最後 append {"round": round_no, "step": ..., "time": ..., "changes": [...]} 落 rounds_log
    返回本回合實際生效嘅 AffinityChange 列表。"""


# ---- Flask Blueprint（api.py） ----
affinity_bp = Blueprint("affinity", __name__, url_prefix="/api/affinity")
```

### 3.2 Flask Routes

Blueprint 註冊喺現有 `replay.py` 嘅 Flask app（見 §4.6），俾 Setup 前端同決策 modal 用。

#### `GET /api/affinity/bands`

Response `200`：

```json
{
  "bands": [
    {"min": 61,  "max": 100, "label": "摯愛/至交"},
    {"min": 21,  "max": 60,  "label": "友好"},
    {"min": 1,   "max": 20,  "label": "略有好感"},
    {"min": 0,   "max": 0,   "label": "陌生/中立"},
    {"min": -20, "max": -1,  "label": "略有反感"},
    {"min": -60, "max": -21, "label": "敵對"},
    {"min": -100,"max": -61, "label": "死敵/痛恨"}
  ],
  "default": 0, "min": -100, "max": 100
}
```

用途：Setup 頁滑桿即時等級標籤（PRD 玩家體驗 §Setup.4）。純靜態，無狀態。

#### `POST /api/affinity/validate`

Request：

```json
{
  "agents": ["阿珍", "阿強", "小美", "阿明"],
  "relations": [
    {"from": "阿珍", "to": "阿強", "affinity": -65, "label": "舊情人，分手時鬧得好僵"}
  ]
}
```

Response `200`（可直接做 `config["affinity"]`）：

```json
{
  "affinity": {
    "阿珍": {"阿強": {"value": -65, "label": "舊情人，分手時鬧得好僵"},
             "小美": {"value": 0, "label": ""}, "...": "..."},
    "...": "..."
  }
}
```

Response `400`（白名單 / 對角線錯誤）：

```json
{
  "errors": [
    {"from": "阿珍", "to": "阿強強", "message": "角色「阿強強」唔喺已選角色名單入面"}
  ]
}
```

#### `GET /api/affinity/<sim_name>`

由 `results/checkpoints/<sim_name>/` 最新 `simulate-*.json` 讀 `affinity` key。

Response `200`：`{"affinity": {...}, "step": 6, "time": "20240213-11:00"}`
Response `404`：`{"error": "simulation「xxx」唔存在或無 checkpoint"}`；舊 checkpoint 無 `affinity` key → 返回 `{"affinity": {}, "legacy": true}`（唔係 error）。

#### `GET /api/affinity/<sim_name>/changes?round=<n>`

讀最新 checkpoint 嘅 `affinity_rounds`。無 query param → 返回最後一回合。

Response `200`：

```json
{
  "round": 3,
  "changes": [
    {"from_agent": "阿珍", "to_agent": "阿強", "old": -65, "new": -50,
     "delta": 15, "reason": "阿強幫阿珍解圍", "absolute": false}
  ],
  "display": ["阿珍 → 阿強：-65 → -50（+15）：阿強幫阿珍解圍"]
}
```

`display` 欄位係後端預先 format 好嘅 modal 摘要字串（PRD Done When 第 9 條嘅格式）。

---

## 4. 整合點（對現有文件嘅具體修改）

> 全部修改加註 `# [story-weaver:affinity]` 註解，方便將來上游同步時識別。

### 4.1 `generative_agents/start.py`

**(a) `get_config()`（L138-157）** — 加可選參數同埋新 key：

```python
def get_config(start_time="20240213-09:30", stride=15, agents=None,
               affinity=None):                      # [story-weaver:affinity]
    ...
    config = {
        "stride": stride,
        "time": {"start": start_time},
        "maze": {"path": os.path.join(assets_root, "maze.json")},
        "agent_base": agent_config,
        "agents": {},
        "affinity": affinity or {},                 # [story-weaver:affinity] Setup 校驗後嘅矩陣
        "affinity_rounds": [],                      # [story-weaver:affinity] 變動歷史
        "affinity_meta": {},                        # [story-weaver:affinity] 冧等標記等
    }
```

CLI 入口（L198）維持 `personas` 原版行為：`affinity=None` → 空矩陣 → 功能退化為原版，零退化。

**(b) `simulate()` 回合決策點（L97-101 checkpoint 寫入之後、L103 stride 之前）** — GM 系統嘅掛鉤位置，本系統只定義契約：

```python
            # 保存对话数据（L100-101 之後插入）
            # [story-weaver:affinity] 回合尾掛鉤：由 GM 系統注入，
            # 佢會呼叫 story_weaver.affinity.gm.apply_gm_response(...)。
            # 本系統保證：apply_gm_response 任何異常都唔會 throw 上嚟（內部 try/except + failsafe）。
            if self.gm_hook is not None and (i + 1) % self.steps_per_round == 0:
                self.gm_hook(self.game, i + 1)
```

`SimulateServer.__init__` 加 `self.gm_hook = None; self.steps_per_round = <由 config 或 Setup 決定>`。掛鉤本體係 GM 系統嘅責任；本系統只保證 `apply_gm_response` 嘅簽名同容错。

### 4.2 `generative_agents/modules/game.py`

**`Game.__init__`（L37，`for` loop 建完 agents 之後）** 加 3 行：

```python
        # [story-weaver:affinity]
        from story_weaver.affinity import AffinityStore
        self.affinity = AffinityStore(config.setdefault("affinity", {}), list(self.agents.keys()))
        for _a in self.agents.values():
            _a.affinity = self.affinity   # 俾 agent._chat_with 用（唔使 import game）
        self.affinity_rounds = config.setdefault("affinity_rounds", [])
```

舊 checkpoint 無 `affinity` key → `setdefault` 畀 `{}` → `ensure_pairs()` 補 0 → resume 唔 crash（PRD 邊界情況 4）。

**`reset_game()`（L75-79，`for` loop 之後）** 加初始記憶注入：

```python
        # [story-weaver:affinity] agent.reset() 之後 LLM/embedding 先 ready
        from story_weaver.affinity.memory import inject_initial
        inject_initial(self.affinity, self.agents,
                       self.config_meta if hasattr(self, "config_meta") else {}, self.logger)
```

注意：`Game.__init__` 而家**冇**持有成個 config，只有局部變數——需要喺 `__init__` 順手 `self._config = config`（1 行），等 `reset_game` 攞到 `config.setdefault("affinity_meta", {})`。呢個係對 game.py 嘅第 4 行改動。

### 4.3 `generative_agents/modules/agent.py`

**(a) `_add_concept`（L632-647）** — 加可選參數，跳過 LLM poignancy 評分：

```python
    def _add_concept(
        self,
        e_type,
        event,
        create=None,
        expire=None,
        filling=None,
        poignancy_override=None,   # [story-weaver:affinity]
    ):
        if poignancy_override is not None:              # [story-weaver:affinity]
            poignancy = int(poignancy_override)         # [story-weaver:affinity]
        elif event.fit(None, "is", "idle"):
            ...
```

呢個參數同時係**玩家選項注入系統**嘅共用路徑（PRD 依賴表最後一行），一次改動兩個系統用。

**(b) `_chat_with`（L523 同 L528-531）** — decide_chat 同 relations 都攞結構化錨點：

```python
        # [story-weaver:affinity] L523 之前唔使改——relation_line 經 scratch 內部 helper 注入（§1.3）
        relations = [
            self.completion("summarize_relation", self, other.name),
            other.completion("summarize_relation", other, self.name),
        ]
        # [story-weaver:affinity] prepend 結構化好感度錨點
        _aff = getattr(self, "affinity", None)
        if _aff is not None:
            relations[0] = _aff.relation_line(self.name, other.name) + relations[0]
            relations[1] = _aff.relation_line(other.name, self.name) + relations[1]
```

`summarize_relation` 嘅 LLM 總結仍然保留（記憶流細節），`relation_line` 喺前面做數值錨——咁 `generate_chat` 嘅 `focus = [relation, ...]`（scratch.py:578）會將好感度句子送入向量檢索 query，一舉兩得。

**(c) `_wait_other`（L603）** — 唔使改 agent.py；`decide_wait` 嘅 `relation_line` 喺 scratch 層注入（§4.4）。

### 4.4 `generative_agents/modules/prompt/scratch.py`

`Scratch` class 加 helper（放喺 `_base_desc` 隔籬）：

```python
    # [story-weaver:affinity]
    def _relation_line(self, a_name: str, b_name: str) -> str:
        try:
            from modules.game import get_game   # lazy import，避免 circular import
            game = get_game()
            if game is not None and getattr(game, "affinity", None) is not None:
                return game.affinity.relation_line(a_name, b_name)
        except Exception:
            pass
        return ""
```

四個 prompt 函數嘅 `build_prompt` data dict 各加一個 key（其他行唔郁）：

| 函數（行號） | 加嘅 key |
|---|---|
| `prompt_decide_chat`（L428-438） | `"relation_line": self._relation_line(agent.name, other.name)` |
| `prompt_decide_wait`（L523-537，`task` 嘅 dict） | 同上 |
| `prompt_summarize_relation`（L560-567） | `"relation_line": self._relation_line(agent.name, other_name)` |
| `prompt_generate_chat`（L605-618） | `"relation_line": self._relation_line(agent.name, other.name)` |

向後兼容：模板無 `${relation_line}` 時呢啲 key 被忽略；`get_game()` 未設置時返回 `""`。

### 4.5 `generative_agents/data/prompts/` 模板

| 模板 | 修改 |
|---|---|
| `decide_chat.txt` | 轉繁體；背景段 `${context}` 之後加一行 `${relation_line}`；判斷句改為「根據上述背景及兩人關係判斷」 |
| `generate_chat.txt` | 轉繁體；`${base_desc}` 段後加 `${relation_line}`；`<對話原則>` 加一條「- 說話態度必須符合上述關係描述所反映的好感度（數值越低態度越冷淡、帶敵意；越高越親切）」 |
| `decide_wait_example.txt` | 轉繁體；加可選 `${relation_line}` 行（example 呼叫位傳固定字串，task 呼叫位傳真值） |
| `summarize_relation.txt` | 轉繁體；開頭加「已知結構化關係：${relation_line}」，令 LLM 總結以數值為錨、以記憶為細節 |
| `gm_adjust_affinity.txt` | **【新檔】**，繁體。輸入 `${matrix_text}`、`${events}`、`${conversations}`；指示 GM 以 JSON 返回 `[{from_agent, to_agent, delta, reason, set_absolute, absolute_value}]`，講明 ±25/回合上限、只有劇情重大轉折先好用 `set_absolute`、無互動唔好硬調 |

其餘 24 個簡體模板嘅繁體化係另一個工作項（語言決策），唔屬本系統範圍，但本系統郁過嘅 4 個模板順手轉埋。

### 4.6 `generative_agents/replay.py`

L13 `app = Flask(...)` 之後註冊 blueprint（2 行）：

```python
from story_weaver.affinity.api import affinity_bp   # [story-weaver:affinity]
app.register_blueprint(affinity_bp)                # [story-weaver:affinity]
```

長遠 Setup 頁會有自己嘅 server（Setup 系統嘅責任）；blueprint 無狀態，搬去邊個 Flask app 都得。

---

## 5. 文件計劃

### 新建

| 路徑 | 內容 |
|---|---|
| `/Users/kenneth/Projects/story-weaver/generative_agents/story_weaver/__init__.py` | 空 package 標記 |
| `/Users/kenneth/Projects/story-weaver/generative_agents/story_weaver/affinity/__init__.py` | re-export `AffinityStore`, `validate_setup`, `inject_initial`, `inject_change`, `build_gm_prompt`, `apply_gm_response`, `affinity_bp`, models |
| `/Users/kenneth/Projects/story-weaver/generative_agents/story_weaver/affinity/models.py` | §2 全部 pydantic / dataclass |
| `/Users/kenneth/Projects/story-weaver/generative_agents/story_weaver/affinity/store.py` | `AffinityStore`、`band_of`、`validate_setup`、`SetupValidationError`、`UnknownAgentError` |
| `/Users/kenneth/Projects/story-weaver/generative_agents/story_weaver/affinity/memory.py` | `initial_poignancy`、`inject_initial`、`inject_change` |
| `/Users/kenneth/Projects/story-weaver/generative_agents/story_weaver/affinity/gm.py` | `GMAdjustmentItem/Response`、`build_gm_prompt`、`apply_gm_response` |
| `/Users/kenneth/Projects/story-weaver/generative_agents/story_weaver/affinity/api.py` | Flask blueprint，4 個 route（§3.2） |
| `/Users/kenneth/Projects/story-weaver/generative_agents/data/prompts/gm_adjust_affinity.txt` | GM 調整 prompt（繁體） |
| `/Users/kenneth/Projects/story-weaver/tests/story_weaver/test_affinity_store.py` | clamp、band、ensure_pairs、validate_setup 錯字拒收、舊 checkpoint `{}` 補 0 |
| `/Users/kenneth/Projects/story-weaver/tests/story_weaver/test_affinity_gm.py` | apply_gm_response：垃圾 item 過濾、±25 clamp、absolute 重置、空 response、rounds_log append |
| `/Users/kenneth/Projects/story-weaver/tests/story_weaver/test_affinity_api.py` | Flask test client 4 routes，含 400/404/legacy 分支 |
| `/Users/kenneth/Projects/story-weaver/docs/spec/affinity.md` | 本文件 |

### 修改

| 路徑 | 位置 | 改動量 |
|---|---|---|
| `generative_agents/start.py` | `get_config()` L138-157；`SimulateServer.__init__` L25-69；`simulate()` L100-103 | ~10 行 |
| `generative_agents/modules/game.py` | `__init__` L37 後 + `self._config = config`；`reset_game()` L79 後 | ~8 行 |
| `generative_agents/modules/agent.py` | `_add_concept` L632-647；`_chat_with` L528-531 | ~8 行 |
| `generative_agents/modules/prompt/scratch.py` | 新增 `_relation_line` helper；4 個 prompt 函數 data dict 各 +1 key | ~15 行 |
| `generative_agents/data/prompts/decide_chat.txt` | 繁體化 + `${relation_line}` | 全檔 |
| `generative_agents/data/prompts/generate_chat.txt` | 繁體化 + `${relation_line}` + 對話原則加一條 | 全檔 |
| `generative_agents/data/prompts/decide_wait_example.txt` | 繁體化 + `${relation_line}` | 全檔 |
| `generative_agents/data/prompts/summarize_relation.txt` | 繁體化 + 錨點句 | 全檔 |
| `generative_agents/replay.py` | L13 後註冊 blueprint | 2 行 |

---

## 6. 同其他 5 個系統嘅契約

| 系統 | 方向 | 契約 |
|---|---|---|
| **Setup／角色建立系統** | 佢 → 我 | 佢 call `POST /api/affinity/validate`（或直接 call `validate_setup()`），攞到 `SetupAffinityResult.affinity` 之後**原封不動**放入 `get_config(..., affinity=result.affinity)`。Setup 保證 `agents` 係最終已選角色（≥4）；打錯字由本系統 400 拒收、Setup 負責將 `errors[].from/to` 映射返 UI 格仔。 |
| **GM／敘事總監系統** | 雙向 | **讀**：`game.affinity.full_matrix_text()`（每回合尾組 prompt 用）、`game.affinity.get(a, b)`。**寫**：GM **唔准直接改數值**，必須經 `apply_gm_response(store, response, agents, game.affinity_rounds, round_no, logger)`；佢負責 clamp、記憶注入、rounds log。玩家自訂命令涉及關係（「佢哋和好咗」）→ GM 自己翻譯成 `GMAdjustmentItem`（可帶 `set_absolute=True`）交畀同一條路徑。掛鉤點：`SimulateServer.gm_hook`（§4.1b）。 |
| **決策 modal／故事回顧系統** | 我 → 佢 | 讀 `GET /api/affinity/<sim>/changes`（或 checkpoint 嘅 `affinity_rounds[-1]`），直接 render `display` 字串。故事回顧時間線：過濾 `abs(delta) >= 20 or absolute == True` 嘅 change 做事件節點。空 `changes` → 顯示「本回合無關係變動」。 |
| **玩家命令注入系統** | 間接（經 GM） | 本系統**唔 parse 玩家文字**。玩家命令嘅記憶注入同關係調整嘅記憶注入共用 `Agent._add_concept(poignancy_override=...)`（§4.3a），poignancy 量級對齊：玩家命令 8-10、關係變動 8、關係重置 10，避免任何一方被向量檢索淹沒。 |
| **記憶注入系統（玩家選項注入）** | 共用機制 | 同一條 `_add_concept(poignancy_override=...)` 路徑。本系統嘅 `inject_change` 係佢嘅參考實現：thought concept、`Event(subject, predicate="對", object=...)`、describe 繁體書面語。 |

### 契約上嘅硬性保證（本系統對外承諾）

1. **任何輸入都唔會令模擬 crash**：LLM 垃圾 → 空調整；未知角色 → exception 喺 Setup 層擋、GM 層 skip；舊 checkpoint → 補 0 退化。
2. **數值永遠 ∈ [-100, 100]**：寫入層（`set_affinity`）同調整層（`adjust`）雙重 clamp，prompt 同 checkpoint 永遠見唔到超界值。
3. **delta 永遠 ∈ [-25, +25]**，除非 GM 明確 `set_absolute=True`。
4. **零退化**：`affinity` 全空時，`relation_line` 得「陌生/中立」句，`decide_chat` 行為同原版一致（prompt 只多一句中性描述，無硬性規則改動）。

---

## 7. 驗收映射（PRD Done When → 測試）

| PRD 條件 | 驗證方式 |
|---|---|
| Setup `relations` 入 config 頂層 `affinity` 兼入第一個 checkpoint | `test_affinity_api.py::test_validate_persist` + 手動：4 角色新遊戲 step=1，打開 `simulate-*.json` 檢查 |
| 非零關係注入 poignancy ≥ 8 thought | `test_affinity_gm.py` + 檢查 `results/checkpoints/<name>/storage/<agent>/associate` index 或 log |
| 模板含 `relation_line` 且 `<PROMPT>` log 見到 | `grep relation_line data/prompts/*.txt` + debug log（`agent.py:102`）目視 |
| 對照測試 A→B=-80 vs C→D=0 各 3 回合 | 手動實驗腳本（唔入 CI，依賴 LLM），記錄對話次數同人工抽查對白 |
| GM 調整 pydantic 校驗 + 雙 clamp + 寫 `affinity_changes` | `test_affinity_gm.py` 全覆蓋 |
| LLM 連續垃圾 10 次 → 空操作 + warning，模擬唔斷 | mock LLM raise，`test_affinity_gm.py::test_failsafe` |
| resume 一致 + 舊 checkpoint 補 0 唔 crash | `test_affinity_store.py::test_legacy_checkpoint` + 手動 `--resume` |
| 新模板繁體香港書面語 | code review + `grep` 殘留簡體字 |
| modal 顯示 `-65 → -50（+15）` 格式 | `test_affinity_api.py::test_changes_display_format` |
