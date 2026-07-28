# 技術規格：角色 Setup 系統

> 項目：Story Weaver（Python 3.12 + Flask，基於 GenerativeAgentsCN）
> 對應 PRD：`docs/prd/character-setup.md`（唯一事實來源）
> 版本：v0.1 ｜ 日期：2026-07-28
> 代碼根目錄：`/Users/kenneth/Projects/story-weaver/generative_agents/`

---

## 0. 已驗證代碼事實（含兩個 PRD 需要修正嘅位）

以下事實全部對照過源碼，係本 spec 嘅設計前提。

### 0.1 已確認嘅 PRD 事實

| 事實 | 出處 |
|---|---|
| `Agent.__init__` 讀取 `name / coord / currently / scratch / spatial / percept / think / chat_iter / schedule / associate`，可選 `status / plan / action / chats / path` | `modules/agent.py` L13-62 |
| `Agent.__init__` 無 `action` key 時行 else 分支：`tile_at(config["coord"])` 驗證 spawn tile，無效 coord 會 `IndexError` 直接炸 | `modules/agent.py` L50-56 |
| **agent.json 絕對唔可以帶 `action` key**——有嘅話 L46-49 會用 action address 隨機重抽 coord，我哋精心揀嘅 spawn 位會被覆蓋 | `modules/agent.py` L46-49 |
| `spatial.address.living_area` 會自動派生 `睡觉` 地址 = `living_area + ["床"]` | `modules/memory/spatial.py` L12-14 |
| `Maze.get_address_tiles(address)` 攞唔到地址時**唔會報錯，係靜靜雞 `random.choice` 返一格亂格**（L210）——所以 setup 一定要先自己驗證地址存在 | `modules/maze.py` L206-210 |
| `Maze.tile_at(coord)` 無 bounds check，負數 coord 會因 Python 負索引靜默 wrap | `modules/maze.py` L165-166 |
| maze.json 全部 40 格「床」tile 都係 **非 collision**，spawn 落床格係安全嘅 | 掃描 `frontend/static/assets/village/maze.json` 驗證 |
| Housing Registry 全鎮有床房間共 **23 間**（含莫雷诺「空卧室」、宿舍 4 房、共居空間 6 房等） | 同上掃描 |
| `utils.update_dict` 係遞歸 force-update，會保留多餘 key（如 `relationships`） | `modules/utils/arguments.py` L63-98 |
| `config_path` 拼接 = `assets/village/agents/<name.replace(" ", "_")>/agent.json` | `start.py` L132、L153-155 |
| `sprite.json` 係全局共用嘅 texture atlas frame 定義（32x32、20 frames、4 方向），放喺 `agents/` 根目錄，**新角色目錄唔需要複製** | `frontend/static/assets/village/agents/sprite.json` |
| `SimulateServer.__init__` 用 `os.makedirs(checkpoints_folder, exist_ok=True)`，setup 預先建立嘅 `results/checkpoints/<story>/` 唔會衝突 | `start.py` L33 |
| 記憶注入 `Associate.add_node(node_type, event, poignancy, ...)` 內部用 `utils.get_timer().get_date()`，timer 由 `create_game` 嘅 `utils.set_timer(**config["time"])` 初始化——**注入必須發生喺 Game 建立之後** | `modules/memory/associate.py` L166-194、`modules/game.py` L85、`modules/utils/timer.py` L92 |
| `add_node` 會即時調 embedding model（config.json `agent.associate.embedding`，支援 ollama / openai / hugging_face） | `modules/storage/index.py` L21-39 |

### 0.2 修正一：`story.json` 會被 `get_config_from_log` 誤當 checkpoint（PRD 盲點）

`start.py` L111-119 嘅 `get_config_from_log`：

```python
json_files = [f for f in files if f.endswith(".json") and f != "conversation.json"]
# sorted 之後攞最後一個
```

PRD 規定 `story.json` 放喺 `results/checkpoints/<story_name>/`。**問題**：字母序 `simulate-*` < `story.json`，`sorted()` 之後 `story.json` 排最尾，`--resume` 會載入 `story.json` 當 config，直接炸（無 `time`/`agents` key）。

**決策**：改 `start.py` L116 一行，過濾條件收緊做 `file_name.startswith("simulate-") and file_name.endswith(".json")`。呢個改動語義上更正確（checkpoint 檔名本來就係 `simulate-*.json`），唔影響任何現有行為，係最小侵入修法。`story.json` 位置維持 PRD 原定。

### 0.3 修正二：`relationships` 只寫入 agent.json 係**唔會**落盤 checkpoint 嘅（PRD 推論有誤）

PRD 話「`Game.__init__` 用 `update_dict` 合併，多餘 key 會保留喺 config 入面，所以好感度可以跟住 checkpoint 演變」。實際追蹤：

1. `Game.__init__`（`modules/game.py` L30-37）合併出嚟嘅 `agent_config` 係**局部變數**，餵俾 `Agent(...)` 之後就掉咗，**唔會寫返入 `config["agents"][name]`**。
2. `SimulateServer.simulate`（`start.py` L81）每 step 做 `self.config["agents"][name].update(agent.to_dict())`；`Agent.to_dict()`（L678-688）只返 `status / schedule / associate / chats / currently / action`，**無 `relationships`**。
3. 所以 checkpoint 嘅 `agents.<名>` 入面有咩 key，完全取決於 **sim config 生成時放咗咩入 `agents[name]`**。

**決策**：生成 sim config 時，`agents[name]` 除咗 `config_path` 之外**必須直接帶 `relationships` block**：

```jsonc
"agents": {
  "阿欣": {
    "config_path": "assets/village/agents/阿欣/agent.json",
    "relationships": { "阿強": { "score": 60, "desc": "欣賞佢但唔敢講" } }
  }
}
```

咁樣每個 `simulate-*.json` checkpoint 都會帶住 `relationships`；GM 系統改 live `server.config["agents"][name]["relationships"]`（或直接改 checkpoint 檔）就會隨下一步落盤，`to_dict()` 嘅 update 永遠唔會覆蓋佢。agent.json 入面同時保留一份 `relationships`（PRD 要求），作用係**源檔記錄 + resume 後 GM 可對照初始值**，引擎唔讀，無害。

---

## 1. 架構決策

### 決策：新開 `generative_agents/story_weaver/` 獨立 package，唔改 `modules/`

```
generative_agents/
├── modules/            # 模擬引擎（一字唔改）
├── story_weaver/       # 【新增】Setup 系統全部代碼
│   ├── __init__.py
│   ├── schemas.py      # pydantic 請求/回應模型
│   ├── templates.py    # Template Catalog
│   ├── housing.py      # Housing Registry
│   ├── builder.py      # StoryBuilder：校驗 → 生成 → rollback
│   ├── memory_seed.py  # 記憶注入（俾推演啟動系統 call）
│   └── routes.py       # Flask Blueprint：/setup + /api/setup/*
├── tests/              # 【新增】pytest
│   └── test_character_setup.py
├── frontend/
│   ├── templates/setup.html          # 【新增】
│   └── static/assets/setup/          # 【新增】setup.js / setup.css
```

**理由：**

1. **職責分層**：`modules/` 係「推演時」引擎（think loop、記憶、prompt）；Setup 係「創作時」生產器（Flask、pydantic、檔案生成）。兩者生命週期唔同——引擎每 step 跑，Setup 一個故事只跑一次。混埋一齊會令引擎 package 依賴 Flask/pydantic 請求模型。
2. **最小侵入**：成個系統對現有代碼只改兩處——`replay.py` 註冊 Blueprint（2 行）+ `start.py` L116 過濾條件（1 行）。`modules/` 零改動，將來 upstream（GenerativeAgentsCN）更新唔會撞 conflict。
3. **可獨立測試**：`StoryBuilder` / `TemplateCatalog` / `HousingRegistry` 全部係純 Python 類，唔經 Flask 都可以單測；Flask route 只係薄殼。
4. **點解唔放 `modules/` 入面**：`modules/` 每個子package（memory/prompt/model/storage）都係 agent 思考鏈嘅一環，由 `Agent` 直接 import。Setup 唔係思考鏈嘅一環，放入去會誤導後人。

### 輔助決策

- **校驗用 pydantic v2**（項目已經經 magentic 引入 pydantic，無新依賴）。業務規則（撞名、住所衝突、路徑安全）喺 `StoryBuilder.validate()` 做，pydantic 只負責形狀/型別/長度。全部錯一次過收集，唔逐個抛。
- **LLM 輔助擴寫（currently、learned 擴寫、角色視角開端）列為 v0.1 可選**：預設用**規則式拼接**（見 §5.4），LLM 版做一個 `TextExpander` protocol 後補。理由：Setup 發生喺 Game 建立之前，無 `Agent.completion` 可用；直接裸調 OpenAI 兼容 API 會同 `llm_model.py` 嘅 retry/failsafe 邏輯重複。規則式輸出已滿足 PRD 邊界情況 7 嘅兜底語義，PRD 嘅 `llm_fallback: true` 喺 v0.1 恒為 true（代表「用咗規則式」）。
- **繁體轉換**：本系統產出嘅所有文字（`currently`、`scratch.learned/innate` 改寫、關係 thought、UI 文案）直接以繁體生成。模板嘅 `innate/learned/lifestyle/daily_plan` 係簡體原文——**複製落新 agent.json 之前經 `story_weaver/textnorm.py` 嘅 `to_traditional(s: str) -> str` 過一次**（用 OpenCC `s2hk`；未裝 OpenCC 就原樣通過 + log warning，唔阻塞）。呢個係本系統對 PRD「語言依賴」章節嘅具體兌現：`data/prompts/` 29 個模板唔郁，但我哋寫入 agent 自我認知嘅字全部繁體。

---

## 2. 數據模型

### 2.1 請求模型（`story_weaver/schemas.py`，pydantic v2）

```python
from typing import Literal
from pydantic import BaseModel, Field, field_validator

class CharacterIn(BaseModel):
    template_id: str                              # 對應 assets/village/agents/<名>/
    display_name: str = Field(min_length=1, max_length=20)
    occupation: str = Field(min_length=1, max_length=200)
    personality: str = Field(min_length=1, max_length=200)
    home: list[str] = Field(min_length=3, max_length=3)   # [world, sector, arena]

    @field_validator("display_name")
    @classmethod
    def _name_safe(cls, v: str) -> str:
        # 拒絕 / \ .. 同控制字符；空格合法（會轉底線目錄名，response 提示）
        ...

class RelationshipIn(BaseModel):
    from_name: str = Field(alias="from")
    to: str
    score: int = 0                                # 超界會 clamp，唔係 reject
    desc: str = Field(default="", max_length=200)
    model_config = {"populate_by_name": True}

class SetupCreateRequest(BaseModel):
    story_name: str = Field(min_length=1, max_length=50)
    story_opening: str = Field(min_length=10, max_length=1000)
    characters: list[CharacterIn] = Field(min_length=4, max_length=10)
    relationships: list[RelationshipIn] = Field(default_factory=list)

class FieldError(BaseModel):
    field: str        # 例如 "characters[2].display_name" / "story_name" / "relationships[5].score"
    message: str      # 繁體中文錯誤訊息

class ClampNotice(BaseModel):
    field: str
    original: int | str
    clamped: int | str

class SetupCreateResponse(BaseModel):
    story_name: str
    story_dir: str                                  # results/checkpoints/<story_name>
    sim_config_path: str                            # results/checkpoints/<story_name>/sim_config.json
    characters: list[str]                           # display_name 列表（順序 = 玩家揀選順序）
    template_map: dict[str, str]
    clamped: list[ClampNotice] = []
    filled_relationships: int = 0                   # 補咗幾多對「陌生」關係
    llm_fallback: bool = True
    redirect: str                                   # 下游推演畫面 URL，帶 ?story=<name>
```

### 2.2 Template Catalog（`story_weaver/templates.py`）

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class CharacterTemplate:
    template_id: str                # 目錄名，例如「伊莎贝拉」
    name: str                       # agent.json["name"]
    age: int
    innate: str                     # 簡體原文（display 用，生成時先轉繁體）
    learned: str
    learned_first_line: str         # learned 第一句，表單預填
    lifestyle: str
    daily_plan: str
    living_area: list[str]          # [world, sector, arena]，由 spatial.address.living_area 讀
    spatial_tree: dict              # 完整 spatial.tree
    portrait_path: str              # 相對 static_root：assets/village/agents/<id>/portrait.png
    texture_path: str
    assets_complete: bool           # portrait.png + texture.png 都存在先係 True

class TemplateCatalog:
    def __init__(self, agents_root: str) -> None: ...
    def scan(self) -> None: ...                    # 掃全部 */agent.json；壞檔 log warning + 標 assets_complete=False
    def list(self) -> list[CharacterTemplate]: ... # 全部模板（含唔完整，前端灰顯）
    def get(self, template_id: str) -> CharacterTemplate: ...  # 唔存在 raise KeyError
```

啟動時掃一次並快取（Flask app startup 建立 singleton）；模板目錄係靜態資產，運行期唔會變。

### 2.3 Housing Registry（`story_weaver/housing.py`）

```python
@dataclass
class Room:
    world: str
    sector: str
    arena: str
    bed_tiles: int                  # 該房「床」game_object tile 數（≥1 先入冊）
    occupied_by: str | None = None  # display_name

class HousingConflict(Exception): ...

class HousingRegistry:
    def __init__(self, maze_path: str) -> None: ...  # 掃 tiles[].address[-1]=="床" 分組
    def rooms(self) -> list[Room]: ...
    def available(self) -> list[Room]: ...
    def is_valid_home(self, address: list[str]) -> bool: ...
    def assign(self, address: list[str], display_name: str) -> None: ...
        # 房唔存在 → ValueError；已佔用 → HousingConflict(附剩餘空房清單)
    def release_all(self) -> None: ...               # rollback 用
```

已驗證入冊房間共 23 間（`the Ville` world 下）：各角色自有公寓/房屋主人房、`塔玛拉和卡门的家` 兩房、`奥克山学院宿舍` 四房、`艺术家共居空间` 六房、`林氏家族的房子` 兩房、`莫雷诺家族的房子`「汤姆和简的卧室」+「空卧室」、`摩尔家族的房子`主人房、`山本百合子的房子`主人房、`瑞恩的公寓`主人房、`乔治/亚当/亚瑟/卡洛斯` 各自公寓主人房。

Registry 係**進程內狀態**（每次 POST /api/setup/create 開始時 `release_all()` 重置），唔落盤——落盤嘅事實係各 agent.json 嘅 `spatial.address.living_area`。同屋唔同房合法（不同 [sector, arena] 就係唔同房）。

### 2.4 輸出：agent.json（對齊 `Agent.__init__` 讀取嘅 key）

```jsonc
{
  "name": "阿欣",
  "portrait": "assets/village/agents/阿欣/portrait.png",
  "coord": [72, 14],
  "currently": "阿欣係茶餐廳老闆。年三十晚，阿欣嘅茶餐廳收到拆遷通知……",
  "scratch": {
    "age": 34,                       // 沿用模板
    "innate": "慢熱、念舊",           // ← personality（玩家輸入，已是繁體）
    "learned": "阿欣係茶餐廳老闆。",   // ← display_name + occupation 規則式擴寫
    "lifestyle": "……",               // 模板原文，經 to_traditional() 轉繁體
    "daily_plan": "……"               // 同上
  },
  "spatial": {
    "address": { "living_area": ["the Ville", "伊莎贝拉的公寓", "主人房"] },
    "tree": { /* 完整複製模板 spatial.tree */ }
  },
  "relationships": {                 // 擴展欄位：源檔記錄；checkpoint 持久化靠 sim config（§0.3）
    "阿強": { "score": 60, "desc": "欣賞佢但唔敢講" }
  }
}
```

**禁止出現嘅 key**：`action`（會觸發 coord 重抽，§0.1）、`schedule` / `associate` / `percept` / `think` / `chat_iter`（由 `data/config.json` 嘅 `agent_base` 提供，寫落 agent.json 會覆蓋全局設定）。

### 2.5 輸出：story.json —— `results/checkpoints/<story_name>/story.json`

```jsonc
{
  "story_name": "茶餐廳風雲",
  "story_opening": "年三十晚……",
  "created_at": "2026-07-28T15:00:00",
  "language": "zh-Hant-HK",
  "locked": true,
  "characters": ["阿欣", "阿強", "阿珍", "阿豪"],
  "template_map": { "阿欣": "伊莎贝拉" },
  "relationships": [
    { "from": "阿欣", "to": "阿強", "score": 60, "desc": "欣賞佢但唔敢講" },
    { "from": "阿強", "to": "阿欣", "score": 0, "desc": "" }   // 缺方向自動補嘅「陌生」
  ]
}
```

### 2.6 輸出：sim config —— `results/checkpoints/<story_name>/sim_config.json`

同 `start.py get_config()`（L138-157）輸出格式完全一致，外加 §0.3 嘅 `relationships`：

```jsonc
{
  "stride": 10,
  "time": { "start": "20240213-09:30" },
  "maze": { "path": "assets/village/maze.json" },
  "agent_base": { /* data/config.json 嘅 agent 原文 */ },
  "agents": {
    "阿欣": {
      "config_path": "assets/village/agents/阿欣/agent.json",
      "relationships": { "阿強": { "score": 60, "desc": "欣賞佢但唔敢講" } }
    }
  }
}
```

落多一份 `sim_config.json` 喺 story 目錄，係俾推演啟動系統直接 `json.load` 用（唔使重新砌），同時做埋「呢個故事係 Setup 系統生嘅」嘅標記。檔名 `sim_config.json` 唔係 `simulate-*` 開頭，修正後嘅 `get_config_from_log` 唔會誤載（§0.2）。

---

## 3. 公開 API（Python）

### 3.1 `story_weaver/builder.py`

```python
class SetupError(Exception):
    """業務校驗/生成失敗。routes 層按 status 映射 HTTP code。"""
    def __init__(self, status: int, errors: list[FieldError]) -> None: ...
    status: int          # 409 / 422 / 423 / 500
    errors: list[FieldError]

@dataclass
class BuildResult:
    story_name: str
    story_dir: str
    sim_config_path: str
    sim_config: dict
    agent_dirs: list[str]
    template_map: dict[str, str]
    clamped: list[ClampNotice]
    filled_relationships: int
    llm_fallback: bool

class StoryBuilder:
    def __init__(
        self,
        catalog: TemplateCatalog,
        housing: HousingRegistry,
        maze: "Maze",                 # 用現成 modules.maze.Maze，唔再解析 maze.json
        static_root: str,             # "frontend/static"
        checkpoints_root: str,        # "results/checkpoints"
    ) -> None: ...

    def validate(self, req: SetupCreateRequest) -> list[FieldError]:
        """純校驗，唔寫檔。返空 list = 合法。俾 /api/setup/validate 同 build() 共用。"""

    def build(self, req: SetupCreateRequest) -> BuildResult:
        """校驗 → 生成。任何一步失敗 rollback 今次新建嘅全部路徑，抛 SetupError(500)。"""

    # —— 內部步驟（私有，列出嚟係因為測試要直接打）——
    def _normalize_relationships(
        self, req: SetupCreateRequest
    ) -> tuple[dict[str, dict[str, dict]], list[ClampNotice], int]:
        """返 {from_name: {to: {score, desc}}}（雙向補齊）+ clamp 記錄 + 補咗幾多對。"""

    def _pick_spawn_coord(self, home: list[str]) -> list[int]:
        """get_address_tiles(home+["床"]) → 過濾 collision → random.choice；
        空集 fallback 同房（home[:3]）任意非 collision tile；再空 raise SetupError(500)。"""

    def _render_agent_json(
        self,
        char: CharacterIn,
        template: CharacterTemplate,
        relationships: dict,
        story_opening: str,
    ) -> dict: ...

    def _build_sim_config(self, req, relationships) -> dict: ...
    def _rollback(self, created_paths: list[str]) -> None: ...
```

`Maze` 實例由 routes 層喺 app startup 建立一次（`Maze(utils.load_dict("frontend/static/assets/village/maze.json"), logger)`），builder 同注入共用，避免每次 request 重parse 4201 個 tile。

### 3.2 `story_weaver/memory_seed.py`（推演啟動系統嘅契約接口）

```python
from modules.game import Game
from modules.memory.event import Event

OPENING_POIGNANCY: int = 9

def score_to_poignancy(score: int) -> int:
    """|score| 線性映射：0→1，100→8。int(1 + 7*abs(score)/100)"""

def opening_event(agent_name: str, story_opening: str, address: list[str]) -> Event:
    """Event(subject=agent_name, predicate="經歷", object="故事開端",
             describe=story_opening, address=address)"""

def relationship_thought(agent_name: str, to: str, score: int, desc: str) -> Event:
    """describe = f"我對{to}嘅好感係 {score:+d}：{desc or '陌生'}"。subject=agent_name。"""

def inject_story_memories(game: Game, story: dict) -> dict[str, int]:
    """對 story["characters"] 每個 agent：
       1) add_node("event", opening_event(...), poignancy=9)
       2) 每段以佢為 from 嘅關係 add_node("thought", relationship_thought(...),
          poignancy=score_to_poignancy(score))
       返 {agent_name: 注入咗幾多 node}。
       前提：game 已 create（timer 已 set，§0.1）；即 SimulateServer __init__ 之後、
       simulate(step=1) 之前。失敗唔静默——任一 agent 注入唔到 raise。"""

def inject_event(game: Game, agent_name: str, describe: str, poignancy: int) -> None:
    """通用單條注入——玩家指令注入系統（第 4 個下游）重用呢條路徑。"""
```

---

## 4. Flask Routes（`story_weaver/routes.py`，Blueprint）

```python
setup_bp = Blueprint("setup", __name__)
```

| Method | Path | 用途 |
|---|---|---|
| GET | `/setup` | 渲染 `setup.html`，template context 帶 `templates`（畫廊 JSON） |
| GET | `/api/setup/templates` | 模板畫廊數據 |
| GET | `/api/setup/housing` | 可揀住所清單（只列有床房） |
| POST | `/api/setup/validate` | 乾跑校驗，唔寫檔（前端即時反饋用） |
| POST | `/api/setup/create` | 生成故事（§2.5/2.6 + 角色目錄） |

### 4.1 `GET /api/setup/templates` → 200

```jsonc
{
  "templates": [
    {
      "template_id": "伊莎贝拉",
      "portrait_url": "/static/assets/village/agents/伊莎贝拉/portrait.png",
      "innate": "友好、外向、好客",
      "learned_first_line": "伊莎贝拉是霍布斯咖啡馆的老板……",
      "living_area": ["the Ville", "伊莎贝拉的公寓", "主人房"],
      "assets_complete": true
    }
  ]
}
```

### 4.2 `GET /api/setup/housing` → 200

```jsonc
{ "rooms": [ { "address": ["the Ville", "莫雷诺家族的房子", "空卧室"], "label": "莫雷诺家族的房子 · 空卧室" } ] }
```

### 4.3 `POST /api/setup/validate`

Request = `SetupCreateRequest` JSON。Response 200（永遠 200，校驗結果喺 body）：

```jsonc
{
  "ok": false,
  "errors": [ { "field": "characters[2].display_name", "message": "呢個名同模板角色撞咗，唔該改過" } ],
  "clamped": [ { "field": "relationships[3].score", "original": 150, "clamped": 100 } ],
  "filled_relationships": 6,
  "estimated_llm_calls_per_step": 28
}
```

### 4.4 `POST /api/setup/create`

Request = `SetupCreateRequest` JSON。

| HTTP | 情境 | Body |
|---|---|---|
| 201 | 成功 | `SetupCreateResponse`（§2.1），`redirect = "/?name=<story_name>"`（下游推演畫面接手；URL 參數以推演系統契約為準，見 §7.1） |
| 409 | 故事名撞 `results/checkpoints/` 現有目錄 | `{"errors":[{"field":"story_name","message":"故事名「茶餐廳風雲」已經用咗，建議改用「茶餐廳風雲-2」"}], "suggestion":"茶餐廳風雲-2"}` |
| 422 | 業務校驗失敗（撞名/住所衝突/路徑危險字符等，一次過返晒） | `{"errors":[FieldError...]}` |
| 423 | 故事已 `locked: true`（重複提交同一 story_name 且該故事已開局） | `{"errors":[{"field":"story_name","message":"呢個故事已經開始推演，唔可以再改設定"}]}` |
| 500 | 生成中途失敗（已 rollback） | `{"errors":[{"field":"_","message":"生成到第 3 個角色「阿珍」時寫檔失敗：……已經還原晒，唔該再試一次"}]}` |

pydantic 形狀錯（422 from Flask 層）：攞 `ValidationError.errors()` 轉做 `FieldError` 格式，訊息轉繁體。

### 4.5 鎖定語義

`story.json` 生成時已寫 `"locked": true`（PRD：故事開始後鎖定）。`/api/setup/create` 開頭檢查 `results/checkpoints/<story_name>/story.json` 存在且 locked → 423。v0.1 唔提供解鎖 API（故事開局後嘅演變只經 GM 改 checkpoint，PRD 邊界 9）。

---

## 5. 生成流程（`StoryBuilder.build` 嘅精確順序）

```
1. validate(req) → 有錯 raise SetupError(422/409/423)
2. housing.release_all()
3. 逐角色 housing.assign(char.home, char.display_name)        # 衝突 → 422 附剩餘空房
4. rel_map, clamped, filled = _normalize_relationships(req)
5. created = []
   try:
6.   mkdir results/checkpoints/<story_name>/                   # created.append
7.   寫 story.json（locked: true）
8.   寫 sim_config.json
9.   for char in req.characters:                              # PRD：先 story 目錄後角色目錄
10.      template = catalog.get(char.template_id)
11.      dir = static_root/assets/village/agents/<display_name.replace(" ","_")>/
12.      mkdir dir                                             # created.append
13.      copy portrait.png / texture.png（由模板目錄）
14.      agent_json = _render_agent_json(...)                  # coord 經 _pick_spawn_coord
15.      寫 dir/agent.json
16.  return BuildResult(...)
   except Exception as e:
17.  _rollback(created)  # 逆序 shutil.rmtree / unlink，best-effort，唔遮蔽原異常
18.  raise SetupError(500, ...)
```

**生成文字規則（v0.1 規則式，`llm_fallback=True`）：**

- `currently` = `f"{display_name}係{occupation}。{story_opening}"`（≤500 字天然成立，因 story_opening ≤1000 會截到 480 + 前綴）
- `scratch.learned` = `f"{display_name}係{occupation}。"`
- `scratch.innate` = `personality` 原文
- `lifestyle` / `daily_plan` = 模板原文經 `to_traditional()`
- 關係 thought describe = `f"我對{to}嘅好感係 {score:+d}：{desc or '陌生'}"`

`daily_plan` 入面嘅模板專有名詞（例如「霍布斯咖啡馆」）v0.1 **唔改寫**——改店名屬於 LLM 輔助功能，規則式替換會誤傷。呢個係已知限制，寫入 `BuildResult` 無需標記，但要喺 UI 「一鍵用返模板預設」按鈕旁提示「日程會沿用模板原本嘅地點名」。

---

## 6. 整合點（要改嘅現有文件，逐行講）

### 6.1 `generative_agents/replay.py`（+3 行）

L14 之後（`app = Flask(...)` 建立之後）：

```python
from story_weaver.routes import setup_bp
app.register_blueprint(setup_bp)
```

`index()`（L17-66）**唔改邏輯**；首頁「開始新故事」按鈕改 template（見 6.3）。

### 6.2 `generative_agents/start.py`（改 1 行）

L116：

```python
# 之前
if file_name.endswith(".json") and file_name != "conversation.json":
# 之後
if file_name.startswith("simulate-") and file_name.endswith(".json"):
```

理由見 §0.2（`story.json` / `sim_config.json` 會被誤當 resume checkpoint）。呢個係對現有 `--resume` 行為嘅純收緊，舊 checkpoint 目錄全部檔名本來就係 `simulate-*.json` + `conversation.json`，向後兼容。

### 6.3 `generative_agents/frontend/templates/index.html` 或 `base.html`（+幾行）

首頁（`?name=` 為空時而家係死路一條「Invalid name」）加「開始新故事」連結 `<a href="/setup">`。具體擺位由 UI 系統 PRD（game-ui.md）決定，本系統只保證 `/setup` 可達。

### 6.4 `modules/` —— 零改動

`agent.py` / `game.py` / `maze.py` / `memory/*` / `model/*` 全部唔郁。Setup 只係**消費**佢哋嘅公開行為（`Maze.get_address_tiles` / `Maze.tile_at` / `Associate.add_node` / `Game`）。

### 6.5 推演啟動嘅接入方式（唔改 `SimulateServer`）

推演啟動系統（另一個 deliverable）嘅標準啟動序列：

```python
from start import SimulateServer
from story_weaver.memory_seed import inject_story_memories
from modules import utils

sim_config = utils.load_dict(f"results/checkpoints/{story}/sim_config.json")
server = SimulateServer(story, "frontend/static",
                        f"results/checkpoints/{story}", sim_config)
story_meta = utils.load_dict(f"results/checkpoints/{story}/story.json")
injected = inject_story_memories(server.game, story_meta)   # ← 時機：init 後、simulate 前
server.simulate(step=1, stride=sim_config["stride"])
```

本系統喺 Done-when 驗收時就用呢個序列實跑一次 4 角色故事（對應 PRD「生成嘅 sim config 直接餵俾 SimulateServer 可以跑完第一個 step」）。

---

## 7. 同其他 5 個系統嘅契約

### 7.1 → 推演啟動系統

- **消費**：`results/checkpoints/<story>/sim_config.json`（格式 = `get_config()` 輸出 + `agents[name].relationships`）；角色目錄 `assets/village/agents/<display_name>/`。
- **必須履行嘅時序**：`SimulateServer(...)` → `inject_story_memories(server.game, story_meta)` → `simulate()`。唔注入就開跑係契約違反（agents 會失去共同開端記憶，劇情無共同起點）。
- **`agents[name]` 只會多 `relationships` 一個 key**，`SimulateServer.simulate` 嘅 `.update()` 語義唔受影響。
- 驗收接口：`story_weaver.bootstrap_smoke(story_name) -> dict`（tests 用，跑一次 step=1 返每 agent 嘅 plan）。

### 7.2 → GM 敘事總監系統

- **讀**：`story.json`（`story_opening` 原文、`characters`、`relationships` 初始值）。
- **讀寫**：每回合 checkpoint `simulate-*.json` 嘅 `agents.<display_name>.relationships`（schema：`{display_name: {score: int ∈ [-100,100], desc: str}}`）。GM 調整好感度**只準改呢個 block**；live 調整就改 `server.config["agents"][name]["relationships"]`，下一個 step 落盤自動帶上（§0.3 嘅機制保證）。
- **保證**：display_name 全故事唯一、無路徑分隔符，可以直接做 dict key 同地址拼接。
- 注入高 poignancy 事件（GM 選項落實時）行 `memory_seed.inject_event`（§3.2）。

### 7.3 → 決策 modal / 故事回顧系統

- `story.json.story_opening` = 時間線第一條（原文保留，唔會被改写）。
- 角色對白原文：`results/checkpoints/<story>/conversation.json`（`SimulateServer` 每 step 寫，`start.py` L100-101），格式 `{時間key: [{"A -> B @ 地址": [[speaker, text], ...]}]}`。
- `story.json.created_at` + checkpoint 嘅 `time` 欄位（`%Y%m%d-%H:%M`）可以做現實時間↔遊戲時間換算。

### 7.4 → 玩家指令注入系統

- 唯一注入路徑：`story_weaver.memory_seed.inject_event(game, agent_name, describe, poignancy)`（同開端注入同一條 `Associate.add_node` 路徑）。
- 建議玩家自訂命令用 `poignancy=9`（同開端同級）；契約上 poignancy ∈ [1,10]。

### 7.5 → 回放 / 前端系統

- `story.json.template_map`：`{display_name: template_id}`，攞原版 sprite 對照用。
- 新角色嘅 `portrait.png` / `texture.png` 已放喺 `assets/village/agents/<display_name 空格轉底線>/`，路徑慣例同 25 個模板完全一致，前端現有嘅 `<img src>` 拼接邏輯唔使改。
- `sprite.json`（texture atlas frame 定義）係全局共用，唔使為新角色做任何嘢（§0.1）。
- `setup.html` 掛現有 `template_folder="frontend/templates"`；靜態檔行現有 `static_url_path="/static"`，放 `frontend/static/assets/setup/`。

---

## 8. 文件計劃

### 新建

| 路徑 | 內容 |
|---|---|
| `generative_agents/story_weaver/__init__.py` | package 標記 + 版本 |
| `generative_agents/story_weaver/schemas.py` | §2.1 pydantic 模型 |
| `generative_agents/story_weaver/templates.py` | §2.2 TemplateCatalog |
| `generative_agents/story_weaver/housing.py` | §2.3 HousingRegistry + HousingConflict |
| `generative_agents/story_weaver/builder.py` | §3.1 SetupError / BuildResult / StoryBuilder |
| `generative_agents/story_weaver/memory_seed.py` | §3.2 注入函數 |
| `generative_agents/story_weaver/textnorm.py` | `to_traditional(s)`（OpenCC s2hk，無依賴時 passthrough + warning） |
| `generative_agents/story_weaver/routes.py` | §4 Blueprint；app startup 建立 catalog/housing/maze singleton |
| `generative_agents/frontend/templates/setup.html` | 四步表單（畫廊→角色卡→關係矩陣→開端），extends `base.html` |
| `generative_agents/frontend/static/assets/setup/setup.js` | 前端校驗、矩陣渲染、POST /api/setup/* |
| `generative_agents/frontend/static/assets/setup/setup.css` | 樣式 |
| `generative_agents/tests/__init__.py` | （空） |
| `generative_agents/tests/test_character_setup.py` | §9 測試 |
| `generative_agents/tests/conftest.py` | fixture：tmp 目錄嘅假 agents_root / maze.json 副本、catalog/builder 實例 |

### 修改

| 路徑 | 改動 |
|---|---|
| `generative_agents/replay.py` | +3 行註冊 Blueprint（§6.1） |
| `generative_agents/start.py` | L116 一行（§6.2） |
| `generative_agents/frontend/templates/index.html`（或 `base.html`） | +「開始新故事」入口連結（§6.3） |

---

## 9. 測試計劃（pytest，`cd generative_agents && ../.venv/bin/pytest tests/test_character_setup.py`）

| 測試 | 對應 PRD 邊界/Done-when |
|---|---|
| `test_schema_valid_request` | 合法 4 角色請求過 validate，errors 為空 |
| `test_schema_too_few_characters` | 邊界 1：<4 角色 → 422 |
| `test_required_fields_empty` | 邊界 2：空職業/性格/開端 → 逐欄位一次過返 |
| `test_display_name_duplicate_in_request` | 邊界 3：request 內撞名 → 422 |
| `test_display_name_collides_with_template` | 邊界 3：同 25 個模板目錄撞名 → 422 |
| `test_display_name_path_traversal` | 邊界 10：`../x`、`a/b`、純空格 → 422；空格名 → 通過 + response 提示底線目錄 |
| `test_story_name_conflict` | 邊界 3：`results/checkpoints/` 已有目錄 → 409 附 `-2` 建議 |
| `test_score_clamped` | 邊界 4：+150 → 100 且 `clamped` 有記錄 |
| `test_missing_reverse_relationship_filled` | 邊界 4：只得 A→B，B→A 補 0/"" 且 `filled_relationships` 正確 |
| `test_housing_conflict_same_room` | 邊界 5：兩角色同房 → 422 附剩餘空房 |
| `test_housing_same_house_different_room_ok` | 邊界 5：宿舍唔同房 → 通過 |
| `test_home_without_bed_rejected` | 邊界 5：`home` 指向廚房（唔喺 registry）→ 422 |
| `test_spawn_coord_non_collision` | Done-when：生成嘅 coord 經 `Maze.tile_at` 驗證非 collision、地址喺 `living_area` 房內 |
| `test_agent_json_schema` | Done-when：含 `name/coord/currently/scratch{5 keys}/spatial{address,tree}/relationships`，**且無 `action` key**（§0.1） |
| `test_agent_json_no_engine_keys` | 無 `schedule/associate/percept/think/chat_iter`（防覆蓋 agent_base） |
| `test_rollback_on_mid_write_failure` | 邊界 8：mock 第 3 個角色 copy 時 IOError → 已建目錄全部刪走，返 500 |
| `test_locked_story_returns_423` | 邊界 9：story.json locked → 再 POST 同名 → 423 |
| `test_sim_config_feeds_simulate_server` | Done-when 核心：tmp 環境生成 4 角色故事 → 砌 §6.5 序列 → `simulate(step=1)` 唔報錯（LLM mock：monkeypatch `create_llm_model` 返 stub；embedding 用 hugging_face 細模型或 stub） |
| `test_memory_injection` | Done-when：注入後 `agent.associate.abstract()` 含 ≥1 條開端 event（P.9）+ 每段關係 1 條 thought；`score_to_poignancy` 邊界（0→1, 100→8, -60→5） |
| `test_incomplete_assets_template_unselectable` | 邊界 11：刪走 texture.png 嘅假模板 `assets_complete=False` 且 `build` 拒絕揀佢（422） |
| `test_generated_text_is_traditional` | 邊界/語言：mock `to_traditional` 斷言 lifestyle/daily_plan 有經過轉換；currently 用繁體句式 |

`test_sim_config_feeds_simulate_server` 係最貴嘅測試：要喺 tmp_path 複製 maze.json + 4 個模板目錄，monkeypatch `story_weaver.routes` 嘅 singleton 同 `SimulateServer` 嘅 checkpoints/static 路徑。標 `@pytest.mark.slow`，預設跑但 CI 可以 `-m "not slow"` 跳過。

---

## 10. 已知限制（v0.1 明確認領）

1. `daily_plan` / `lifestyle` 嘅模板專有地名（霍布斯咖啡馆等）唔改寫（§5）；LLM 輔助擴寫係後續版本。
2. Housing Registry 係進程內狀態——兩個並發 POST 理論上可同時 assign 同一房。v0.1 單人本地使用，用一把 `threading.Lock` 包住 `build()` 即夠。
3. `get_address_tiles` 嘅靜默 random fallback（`maze.py` L210）喺引擎其他路徑仍然存在；Setup 已繞開（先驗證地址），但唔修引擎（§6.4 零改動原則）。
4. 好感度演變寫 checkpoint 嘅機制依賴 §0.3 嘅 sim config 擴展；若日後 `Agent.to_dict()` 加咗 `relationships` key 會覆蓋 GM 嘅調整——呢個係跨系統契約，GM 系統 spec 要認領呢條。
