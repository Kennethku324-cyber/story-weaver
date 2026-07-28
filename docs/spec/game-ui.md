# 技術 Spec：遊戲主 UI（Game Main UI）

> 系統：遊戲主 UI
> 對應 PRD：`docs/prd/game-ui.md`
> 代碼基礎已驗證：`generative_agents/replay.py`、`compress.py`、`start.py`、`frontend/templates/{base,index,main_script}.html`、`modules/game.py`、`modules/agent.py`、`modules/memory/associate.py`
> Python 3.12 + Flask + Phaser 3.55.2；pydantic v2 由 `magentic==0.41.0` 帶入，可直接用

---

## 1. 架構決策

### 1.1 新開 `generative_agents/story_weaver/` 套件，唔改 `modules/`

**決定**：所有新後端代碼放喺 `generative_agents/story_weaver/`（同 `modules/`、`compress.py`、`start.py` 平排，因為現有代碼用 cwd-relative import 同路徑，例如 `from modules.game import create_game`、`results/checkpoints/...`、`frontend/static`）。

**理由**：

1. **PRD 明確非目標**：「唔改動 agents 自主推演邏輯」。`Agent.think`（`modules/agent.py:107`）、`SimulateServer.simulate`（`start.py:71`）、`compress.py` 全部**零修改**——本系統只 import 佢哋。離線流程（`start.py` → `compress.py` → `replay.py`）物理上唔可能退化，因為冇一行被掂過。
2. `replay.py` 亦**保留原樣**。新 Flask app 寫喺 `story_weaver/game_server.py`，照抄佢嘅 app 設定（`template_folder="frontend/templates"`、`static_folder="frontend/static"`、`static_url_path="/static"`），兩個 server 可以並存（replay 用 port 5000，game server 用 5001，見 §6）。
3. 前端新增 `game.html` / `game_script.html` 兩個模板；`index.html` / `main_script.html` 原封不動（replay 繼續用）。`game_script.html` 係 `main_script.html` 嘅 fork，差異集中喺 `update()` 嘅數據來源（見 §4.3）。

### 1.2 狀態持久化：單一 JSON 檔 + 原子寫

`game_ui_state.json` 放喺 `results/checkpoints/<name>/`（同 simulate checkpoint 同一目錄），寫入用「臨時檔 + `os.replace`」保證原子性，避免進程被殺時寫出半截 JSON（同 PRD 邊界情況「checkpoint 寫到一半」對應嘅防禦）。

### 1.3 Frame 數據唔持久化，得 checkpoint 係事實來源

增量 frame（`all_movement` 格式）**只存 memory**（`FrameBuffer`）。Server 重啟時由 checkpoint 重新掃描重建。理由：checkpoint（`simulate-*.json` + `conversation.json`）已經係完整事實來源；多寫一份 movement 數據只會引入雙寫不一致風險。離線 `compress.py` 全量重跑因此天然兼容（PRD 對存檔回放系統嘅要求）。

### 1.4 單線程推演：全局鎖

`modules/game.py:86` 嘅 `GenerativeAgentsMap` 係全局單例（`create_game` set / `get_game` get），**一個進程同一時間只可以有一個 simulation**。`RoundRunner` 用一把 process-wide `threading.Lock`（唔係 per-session 鎖）保證：任何時刻只有一個 `SimulateServer.simulate` 線程。第二個 session 想推演會收到 409。

### 1.5 GM / 決策注入用「可註冊 callable + HTTP 兜底」雙軌

GM 系統同決策注入系統係獨立開發嘅系統。本系統定義 Python `Protocol`（`contracts.py`），開發期可以喺進程內 `register_*` 直接注入實作；同時提供 HTTP endpoint（`POST /api/gm/result`）畀外部進程用。兩條路殊途同歸，都係寫 `pending_gm` / 觸發注入。

### 1.6 已知技術債（唔喺本系統處理，記低）

- `start.py:162-170` 喺 **import 時**執行 `parser.parse_args()`。`replay.py:7` 已經係咁 import（`from start import personas`），所以跟佢模式 import 係安全嘅；但 `game_server.py` 啟動時**唔好多餘傳 CLI 參數**（`python -m story_weaver.game_server` 唔接 argument，配置用環境變數 / 常量）。
- checkpoint 檔名用 `simulate-<YYYYmmdd-HHMM>.json`（`start.py:97` 將 `:` 剝走），排序靠 `sorted(os.listdir(...))` 字串序（`compress.py:72`）——同日內係正確嘅；本系統用 `step` 欄位做權威排序，檔名只做後備。

---

## 2. 公開 API（Python）

全部位於 `generative_agents/story_weaver/`。型別用 pydantic v2（`BaseModel`）做邊界校驗，內部流轉用 dataclass。

### 2.1 `models.py` — 數據模型

```python
from __future__ import annotations
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class UIStatus(str, Enum):
    IDLE = "idle"
    SIMULATING = "simulating"
    PLAYING = "playing"            # 推演完、前端重播緊 frame，GM 未觸發
    WAITING_GM = "waiting_gm"      # 回合完、等 GM 出選項
    WAITING_DECISION = "waiting_decision"
    FINISHED = "finished"
    ERROR = "error"


class TimelineNodeType(str, Enum):
    OPENING = "opening"
    EVENT = "event"
    CHAT = "chat"
    DECISION = "decision"
    GM_NOTE = "gm_note"


class DialogueLine(BaseModel):
    speaker: str
    line: str                       # 對白原文，唔准摘要改寫


class TimelineNode(BaseModel):
    seq: int                        # 全局遞增，0 = opening
    sim_time: str                   # "YYYYmmdd-HH:MM"（同 checkpoint 嘅 "time" 格式）
    node_type: TimelineNodeType
    title: str                      # 時間線節點標題（例如「卡洛斯 喺 玫瑰酒吧」）
    body: str = ""                  # 事件描述（action.event.describe 原文）
    dialogue: list[DialogueLine] = Field(default_factory=list)
    poignancy: Optional[int] = None
    meta: dict = Field(default_factory=dict)   # decision 節點：{"choice_id"|"custom_command"}


class GMOption(BaseModel):
    id: str                         # "opt_1" / "opt_2" / "opt_3"
    title: str
    description: str


class AffinityDelta(BaseModel):
    from_agent: str = Field(alias="from")
    to_agent: str = Field(alias="to")
    delta: int                      # 應用前 clamp 到 [-100, 100]，見 §3 apply_affinity_deltas


class GMResult(BaseModel):
    options: list[GMOption] = Field(min_length=2, max_length=3)
    affinity_deltas: list[AffinityDelta] = Field(default_factory=list)
    generated_at: str               # ISO 8601


class CustomCommand(BaseModel):
    target_agent: str
    text: str = Field(min_length=1, max_length=500)


class Decision(BaseModel):
    round: int
    choice_id: Optional[str] = None
    custom_command: Optional[CustomCommand] = None
    decided_at: str                 # ISO 8601，由 server 填

    @model_validator(mode="after")
    def exactly_one(self) -> "Decision":
        if (self.choice_id is None) == (self.custom_command is None):
            raise ValueError("choice_id 同 custom_command 必須二擇一")
        return self


class FeedKind(str, Enum):
    EVENT = "event"
    CHAT = "chat"
    SYSTEM = "system"               # 「命令已送達」「GM 調整好感」等系統訊息


class FeedItem(BaseModel):
    seq: int                        # 全局遞增；前端用 since_feed 增量拉
    sim_time: str
    kind: FeedKind
    actor: Optional[str] = None
    location: Optional[str] = None  # get_location() 格式：「霍布斯咖啡馆，咖啡馆」
    text: str                       # event：行動描述；system：系統文案（繁體）
    dialogue: list[DialogueLine] = Field(default_factory=list)  # kind=chat 時逐句


class GameUIState(BaseModel):
    """持久化喺 results/checkpoints/<name>/game_ui_state.json"""
    version: int = 1
    session: str
    round: int = 0                  # 0-based；完成決策後 +1
    max_rounds: int = 10
    status: UIStatus = UIStatus.IDLE
    sim_step_cursor: int = 0        # 已模擬總 step 數（= SimulateServer.start_step + 已跑步數）
    steps_per_round: int = 8
    stride: int = 10                # 分鐘 / step
    agents: list[str]               # 本局角色（4+ 個，Setup 寫入）
    timeline: list[TimelineNode] = Field(default_factory=list)
    pending_gm: Optional[GMResult] = None
    affinity: dict[str, int] = Field(default_factory=dict)   # key: "A->B"，值 [-100,100]
    control_owner: Optional[str] = None        # 持有控制權嘅 client_id
    control_lease_until: float = 0.0           # epoch 秒
    feed_cursor: int = 0            # 已發出嘅 feed seq 上限（server 內部用）
    processed_steps: list[int] = Field(default_factory=list)  # 已壓縮嘅 checkpoint step
    error: Optional[str] = None
    updated_at: str = ""
```

### 2.2 `state_store.py` — 狀態存取

```python
import contextlib
from story_weaver.models import GameUIState, AffinityDelta


class GameUIStateStore:
    """game_ui_state.json 嘅唯一讀寫口。原子寫：tmp file + os.replace。"""

    def __init__(self, checkpoints_folder: str) -> None: ...

    @property
    def path(self) -> str:
        """<checkpoints_folder>/game_ui_state.json"""

    def exists(self) -> bool: ...

    def load(self) -> GameUIState:
        """檔案唔存在 → FileNotFoundError；JSON 爛 → StateCorruptError。"""

    def save(self, state: GameUIState) -> None:
        """更新 updated_at 後原子寫入。持有內部 threading.RLock。"""

    @contextlib.contextmanager
    def mutate(self) -> "Iterator[GameUIState]":
        """with store.mutate() as s: s.round += 1 —— 離開 block 自動 save。"""


def create_initial_state(
    checkpoints_folder: str,
    session: str,
    agents: list[str],
    affinity: dict[str, int],
    opening_text: str,
    opening_sim_time: str,
    steps_per_round: int = 8,
    stride: int = 10,
    max_rounds: int = 10,
) -> GameUIState:
    """【畀 Setup 系統用】建立初始 game_ui_state.json：
    status=idle, round=0, timeline[0]=TimelineNode(seq=0, node_type=OPENING,
    body=opening_text)。affinity 所有值 clamp 到 [-100, 100]。"""


def clamp_affinity(v: int) -> int:
    return max(-100, min(100, v))


def apply_affinity_deltas(
    affinity: dict[str, int], deltas: list[AffinityDelta]
) -> list[tuple[str, int, int, bool]]:
    """逐條 delta 應用落 affinity["from->to"]（雙向獨立，唔會自動改反向）。
    返回 [(key, old, new, clamped)] 畀 feed 生成系統訊息用。"""
```

### 2.3 `incremental.py` — 增量壓縮（重用 `compress.py` 邏輯）

```python
from modules.maze import Maze
from story_weaver.models import FeedItem


class CompressDelta(BaseModel):
    new_frames: dict[str, dict]        # key: step_key 字串，結構同 compress.py all_movement[step_key]
    new_feed: list[FeedItem]
    last_step: int                     # 今次處理到邊個 checkpoint step
    conversation_times: list[str]      # 新出現嘅 conversation key（sim_time）


class FrameBuffer:
    """每個 session 一個。memory-only；server 重啟由 checkpoint 全量重建。
    frame 結構同 compress.py generate_movement 產出完全一致：
    {"<step_key>": {"<agent>": {"location": str, "movement": [x, y], "action": str}}}
    """

    def __init__(self, checkpoints_folder: str, maze: Maze) -> None: ...

    def scan(self, processed_steps: list[int]) -> CompressDelta:
        """掃 checkpoints_folder 入面 step > max(processed_steps) 嘅 simulate-*.json：
        1. 逐檔 json.load，失敗（半截檔）→ 跳過並記 warning（PRD：殺進程恢復）。
        2. 對每個新 checkpoint，逐 agent：
           - source = self._last_location.get(name) 或第 0 帧位置
           - path = maze.find_path(source, target)（同 compress.py:127）
           - location = compress.get_location(agent_data["action"]["event"]["address"])
           - 展開 frames_per_step=60 帧，key = (step-1)*60+1+i（同 compress.py:174）
           - action 文字同 😴/💬 前綴規則照抄 compress.py:155-172
        3. 由 conversation.json 攞新 sim_time 嘅對話 → FeedItem(kind=CHAT)，
           逐句結構化（唔再係 compress.py:137-141 嘅一舊 string）。
        4. 由 action.event 生成 FeedItem(kind=EVENT)。"""

    def frames_since(self, frame_cursor: int) -> dict[str, dict]:
        """返回 key > frame_cursor 嘅全部 frame（畀 /api/state 增量拉取）。"""

    def latest_frame_key(self) -> int: ...


def load_maze(static_root: str = "frontend/static") -> Maze:
    """同 compress.py:97-100：讀 assets/village/maze.json 建 Maze(json_data, None)。"""
```

### 2.4 `round_runner.py` — 後台推演

```python
from start import SimulateServer, get_config, get_config_from_log
from story_weaver.state_store import GameUIStateStore
from story_weaver.incremental import FrameBuffer


class RoundBusyError(RuntimeError): ...   # 已有推演線程 / status 唔啱
class CheckpointCorruptError(RuntimeError): ...


class RecoveryInfo(BaseModel):
    recovered: bool
    last_complete_step: int
    skipped_files: list[str]         # JSON parse 失敗嘅 checkpoint
    message: str                     # 畀 UI 顯示：「已恢復至第 N 回合（M 步），部分進度遺失」


class RoundRunner:
    """包裝 SimulateServer，喺 daemon thread 跑 simulate(steps_per_round, stride)。
    全局單例 + process-wide threading.Lock（GenerativeAgentsMap 係全局單例，
    唔可以並行推演，見 §1.4）。"""

    def __init__(
        self,
        session: str,
        checkpoints_folder: str,
        static_root: str = "frontend/static",
    ) -> None: ...

    def recover(self) -> RecoveryInfo:
        """server 啟動 / 首次訪問時調：
        1. 掃 checkpoint 目錄，跳過 JSON 爛檔，搵最後完整 checkpoint（step 最大者）。
        2. 若 state.status == SIMULATING（即上次進程死喺推演中途）：
           回滾 status=IDLE，sim_step_cursor=last_complete_step，寫 error 訊息。
        3. 用 get_config_from_log 重建 SimulateServer（start.py:111）。
        4. 重建 FrameBuffer（全量 scan 一次）。"""

    def start_round(self) -> int:
        """status 必須係 IDLE 或 PLAYING；否則 raise RoundBusyError（route 轉 409）。
        - 首次（sim_step_cursor==0）：get_config(...) 建新 SimulateServer
          （start.py:138，config 由 Setup 寫好嘅第 0 步 checkpoint 恢復）。
        - 之後：get_config_from_log 由最後 checkpoint 恢復（start.py:111）。
        - status=SIMULATING，開 thread 跑 server.simulate(steps_per_round, stride)。
        返回 round 編號。"""

    def _on_round_complete(self) -> None:
        """線程結尾（finally）：
        1. FrameBuffer.scan() 壓縮新 checkpoint，更新 processed_steps。
        2. timeline 追加本回合嘅 event/chat 節點（對白原文）。
        3. sim_step_cursor += steps_per_round。
        4. status=WAITING_GM，調註冊咗嘅 GMProvider（冇就等 POST /api/gm/result）。
        5. GM 成功 → pending_gm 寫入 + status=WAITING_DECISION；
           GM 失敗（拋異常/超時）→ 注入保底選項（見 §5.4）+ status=WAITING_DECISION。
        6. 任何未捕獲異常 → status=ERROR, error=traceback 摘要。
        7. round >= max_rounds 時 → status=FINISHED（GM 唔再觸發）。"""

    def status(self) -> UIStatus: ...
```

### 2.5 `contracts.py` — 其他系統嘅注入點

```python
from typing import Protocol, Callable, Optional
from story_weaver.models import GMResult, Decision, GameUIState


class GMProvider(Protocol):
    """【GM 敘事總監系統實作】回合完結時被調。
    實作方自己讀 checkpoints_folder 嘅 simulate-*.json + conversation.json
    （本系統唔轉手數據，只俾路徑同上下文）。"""

    def generate(
        self,
        session: str,
        round: int,
        checkpoints_folder: str,
        timeline: list[TimelineNode],
        affinity: dict[str, int],
    ) -> GMResult: ...


class DecisionInjector(Protocol):
    """【決策注入系統實作】玩家確認決策後被調。
    職責：
    1. 對 choice_id：將 GM 選項描述寫入相關 agents 記憶流；
    2. 對 custom_command：向 target_agent 嘅 Associate 注入命令；
    3. 兩者都用 Associate.add_node(node_type="event", event=Event(...),
       poignancy=<高值，建議 9-10>)（modules/memory/associate.py:166，
       poignancy 上限由 config think.poignancy_max=150 約束，記憶流嘅
       importance 正規化喺 associate.py:104-119）。
    睡眠中角色唔使特判——add_node 直接寫記憶流，醒後下一個 think() 自然生效。"""

    def inject(self, session: str, decision: Decision, state: GameUIState) -> None: ...


# game_server 層嘅註冊口（開發期進程內直連；生產可留空，改用 HTTP endpoint）
def register_gm_provider(provider: Optional[GMProvider]) -> None: ...
def register_decision_injector(injector: Optional[DecisionInjector]) -> None: ...
def get_gm_provider() -> Optional[GMProvider]: ...
def get_decision_injector() -> Optional[DecisionInjector]: ...
```

---

## 3. 公開 API（Flask Routes）

全部喺 `story_weaver/game_server.py`。JSON 全部 `Content-Type: application/json; charset=utf-8`。錯誤統一 `{"error": str}` + 對應 HTTP code。

### 3.1 `GET /game?name=<session>`

渲染 `game.html`（三欄介面）。

- 200：渲染頁面。template context：`session`、`agents`（本局角色，**唔係** 25 個預設）、`persona_init_pos`（由 agent.json `coord` 讀，同 `compress.py:49-50`）、`zoom`、`readonly`。
- 404 `{"error": "session 不存在：results/checkpoints/<name>"}`：checkpoint 目錄或 `game_ui_state.json` 缺失。
- 首次訪問觸發 `RoundRunner.recover()`；若 `status==waiting_decision`，前端 load 完即刻重彈決策 modal（PRD 硬性要求）。

### 3.2 `GET /api/state?name=<session>&since_frame=<int>&since_feed=<int>`

前端 2 秒輪詢。回應：

```json
{
  "status": "simulating",
  "round": 2,
  "max_rounds": 10,
  "sim_time": "20240213-11:40",
  "sim_step_cursor": 16,
  "new_frames": {
    "901": {"伊莎贝拉": {"movement": [72, 14], "location": "霍布斯咖啡馆，咖啡馆", "action": "💬 与顾客聊天"}}
  },
  "frame_latest": 960,
  "new_feed": [
    {"seq": 41, "sim_time": "20240213-11:40", "kind": "event", "actor": "伊莎贝拉",
     "location": "霍布斯咖啡馆，咖啡馆", "text": "冲緊咖啡", "dialogue": []},
    {"seq": 42, "sim_time": "20240213-11:40", "kind": "chat", "actor": null,
     "location": "霍布斯咖啡馆，咖啡馆", "text": "",
     "dialogue": [{"speaker": "伊莎贝拉", "line": "……"}, {"speaker": "卡洛斯", "line": "……"}]}
  ],
  "feed_latest": 42,
  "agents_meta": {
    "伊莎贝拉": {"currently": "……", "action": "冲緊咖啡", "location": "霍布斯咖啡馆，咖啡馆"}
  },
  "pending_gm": null,
  "affinity": {"伊莎贝拉->卡洛斯": 20, "卡洛斯->伊莎贝拉": -5},
  "readonly": false,
  "recovered_message": null,
  "error": null
}
```

- `new_frames` key 係字串 frame key（同 `compress.py:174` 嘅 `"%d" % ((step-1)*60+1+i)`），前端直接 push 入播放隊列。
- `agents_meta` 由最新完整 checkpoint 嘅 `agents[name].currently / action.event.describe / action.event.address` 讀出（畀 `on_screen_det_trigger` 詳情面板）。
- `readonly`：heartbeat 判定本 tab 唔係控制者 → true（隱藏決策按鈕 + banner）。
- 503 `{"status": "preparing"}`：state 存在但 FrameBuffer 仲未就緒（首次 scan 中）——前端顯示 loading 繼續輪詢（PRD：唔學 replay.py 回錯誤 HTML）。

### 3.3 `POST /api/round/start`

Request：

```json
{"name": "<session>", "command": "start"}
```

`command`: `"start"` | `"next"`（語義相同，都係「跑一回合」；保留 enum 係為咗日後區分首次行為）。

- 200 `{"accepted": true, "round": 1}`
- 409 `{"accepted": false, "reason": "推演進行中"}`：`RoundBusyError`（status 係 SIMULATING / WAITING_GM，或全局鎖被持有）。
- 409 `{"accepted": false, "reason": "等待玩家決策"}`：status==WAITING_DECISION 時唔准開下一回合。
- 409 `{"accepted": false, "reason": "故事已完結"}`：status==FINISHED。
- 403 `{"error": "唯讀模式"}`：非控制 tab。

### 3.4 `POST /api/decision`

Request（`Decision` model，choice_id 與 custom_command 互斥）：

```json
{"name": "<session>", "choice_id": "opt_2"}
```
或
```json
{"name": "<session>", "custom_command": {"target_agent": "卡洛斯", "text": "聽日去酒館搵瑞恩道歉"}}
```

處理次序（全部喺 `store.mutate()` 內，原子）：

1. 校驗：status 必須 WAITING_DECISION（否則 409）；`choice_id` 必須喺 `pending_gm.options` 入面；`target_agent` 必須喺 `state.agents`；`text` 1–500 字（違規 → 400 `{"error": "...", "field": "..."}`，前端保留已輸入內容）。
2. 調 `DecisionInjector.inject(...)`（未註冊 → 500 `{"error": "決策注入系統未就緒"}`，state 唔變，玩家可以重試）。
3. 應用 `pending_gm.affinity_deltas`（`apply_affinity_deltas`，clamp ±100；被 clamp 嘅加 system feed「GM 調整：A 對 B 好感 +35（已達上限）」）。
4. `timeline` 追加 `TimelineNode(node_type=DECISION, meta={"choice_id"|"custom_command"})`。
5. 清 `pending_gm`，`round += 1`；`round >= max_rounds` → status=FINISHED，否則 status=IDLE。

- 200 `{"accepted": true, "round": 3, "status": "idle"}`

### 3.5 `GET /api/timeline?name=<session>`

- 200 `{"timeline": [TimelineNode...]}`——完整故事回顧數據（modal 用）。

### 3.6 `POST /api/gm/result`（GM 系統用）

Request：`GMResult` + `{"name": "<session>"}`。

- 僅當 status==WAITING_GM 接受：寫入 `pending_gm`，status→WAITING_DECISION → 200 `{"accepted": true}`。
- 校驗失敗（options <2 或 >3、JSON 爛）→ 400；呢個情況本系統**唔會**自動注入保底選項（保底邏輯只喺進程內 GMProvider 拋異常/超時時觸發，見 §5.4）；外部 GM 要自行重試或 POST 保底選項。
- 409：status 唔啱（例如已經有 pending_gm）。

### 3.7 `POST /api/heartbeat`

Request：`{"name": "<session>", "client_id": "<uuid>"}`

- 控制權租約 15 秒。無主 / 租約過期 → 該 client 成為 owner。200 `{"is_owner": true, "readonly": false}`。
- 前端每 5 秒發一次；第二個 tab 會持續收到 `is_owner: false`（唯讀觀察模式）。

### 3.8 `GET /api/export/story?name=<session>&format=md|json`

- status==FINISHED（或任何時刻）導出完整 timeline。`format=md` 生成類似 `compress.py generate_report` 嘅 markdown（章節 = 回合，保留對白原文引用塊）；`format=json` 原樣返回 timeline。`Content-Disposition: attachment`。

---

## 4. 整合點（現有文件具體改邊度）

原則：**現有 `.py` 零修改**；前端新增模板，舊模板唔掂。

### 4.1 `start.py` — 唔改，只 import

| 被用符號 | 位置 | 用法 |
|---|---|---|
| `SimulateServer` | `start.py:24` | `RoundRunner` 持有；每回合 `simulate(steps_per_round, stride)`（`start.py:71`）喺 daemon thread 跑。佢每 step 寫 `simulate-<time>.json`（`start.py:97`）同 `conversation.json`（`start.py:100`）——呢兩個 write 係本系統嘅數據源，**唔插手** |
| `get_config_from_log` | `start.py:111` | 斷線恢復 + 第 2 回合起重建 server。**注意**：佢直接 `json.load` 最後一個檔，冇容忍爛檔——`RoundRunner.recover` 要自己先掃描、跳過爛檔，再將「最後完整檔」嘅邏輯補喺外層（必要時喺 story_weaver 內實作一個 `get_config_from_log_tolerant()`，唔改原版） |
| `get_config` | `start.py:138` | 首次推演建 config（正常路徑係 Setup 已寫好第 0 步 checkpoint，所以實際多數行 `get_config_from_log`） |
| `personas` | `start.py:12` | **唔用**——本局角色由 `game_ui_state.json` 嘅 `agents` 欄位話事（4+ 個子集）；template 唔再收 25 人全列表 |

### 4.2 `compress.py` — 唔改，抽取重用

`story_weaver/incremental.py` import：

| 被用符號 | 位置 | 用法 |
|---|---|---|
| `frames_per_step` (=60) | `compress.py:12` | frame key 計算保持一致 |
| `get_location(address)` | `compress.py:27` | 地址 list → 顯示字串 |
| `insert_frame0(...)` | `compress.py:39` | 第 0 帧初始位置（首次 scan 用） |

frame 生成內循環（`compress.py:102-184`）嘅邏輯**複寫**入 `FrameBuffer.scan`（增量版），唔係調用 `generate_movement`——因為原版係全量重掃 + 寫檔，增量版要 (a) 跳過已處理 step、(b) 維護 `last_location` 跨 checkpoint 狀態、(c) 對話逐句結構化（原版 `compress.py:137-141` 係拼一舊 string，唔合用）、(d) 唔寫檔。複寫係有意嘅：保持離線版零風險。

### 4.3 `frontend/templates/main_script.html` → fork 出 `game_script.html`

保留唔變嘅部分（照抄）：

- Phaser config（`main_script.html:36-59`）
- `preload()` tilemap + 16 tileset + agent atlas 載入（`:67-98`）——agent 列表改用本局 `agents`
- `create()` 嘅圖層建立（`:101-143`）、camera + cursors（`:198-204`）、sprite + pronunciatio 建立（`:207-232`）、四方向動畫註冊（`:235-273`）

修改嘅部分：

| 位置 | 原版行為 | 新版行為 |
|---|---|---|
| `:12` `let all_movement = {{ all_movement|tojson }}` | 頁面載入時一次過注入全量 | 改為 `let all_movement = {}` + 輪詢 `GET /api/state` 將 `new_frames` `Object.assign(all_movement, new_frames)` |
| `:32` `let finished = false` | 播到尾即 finished | 改為 `queue_drained` 判斷：`step in all_movement` 為 false 時**唔再**設 `finished=true`（`:420-422`），而係停在當前 frame 等下一輪輪詢；只有 `/api/state` 返回 `status=="finished"` 且隊列乾先真正 finished |
| `:339-342` conversation 顯示 | 由 `all_movement["conversation"]` 一舊 string | 改由 feed 欄（DOM）負責；Phaser 內嘅 `textConversation` 保留 pronunciatio 氣泡功能，對話正文唔再喺地圖上顯示 |
| `:364-366` 詳情面板更新 | 讀 `all_movement["description"]` | 改讀 `/api/state` 嘅 `agents_meta` |
| `:176-190` 運行/暫停按鈕 | 控制回放 | 「暫停」保留（只停前端動畫隊列，`paused` 邏輯 `:328` 不變）；「運行」移除（推演由側欄「開始推演/下一回合」按鈕觸發 `POST /api/round/start`） |

新增（放 `game_script.html` 尾部或 `game.html` 嘅 `js_content` block）：

```text
pollState(): 每 2000ms GET /api/state?name&since_frame&since_feed
  → Object.assign(all_movement, resp.new_frames)
  → appendFeed(resp.new_feed)（DOM cap 200 條，舊條目移除——timeline 係完整記錄）
  → renderAffinity(resp.affinity)
  → if resp.status == "waiting_decision" && !modalShown: openDecisionModal()
  → if resp.status == "finished" && queue drained: showFinaleModal()
```

### 4.4 `frontend/templates/index.html` → 新建 `game.html`（extends `base.html`）

三欄 layout（Bootstrap 3 grid）：

```text
┌──────────────────────────────────────────────────────────┐
│ 頂欄：第 N 回合 · 狀態 · 遊戲內時間 · [開始推演/下一回合]      │
├───────────────────────────────┬──────────────────────────┤
│ #game-container（Phaser）      │ 右側欄：                  │
│ （col-md-8）                  │  - 事件/對話 feed #feed    │
│                               │  - 好感度面板 #affinity    │
│ 角色頭像列（沿用 index.html    │  - 角色下拉 + 狀態         │
│  :9-21 嘅 trigger 機制）       │    （col-md-4）            │
├───────────────────────────────┴──────────────────────────┤
│ #on_screen_det_content-* 詳情面板（沿用 index.html :29-43）│
└──────────────────────────────────────────────────────────┘
決策 modal（Bootstrap 3 modal，data-backdrop="static"
  data-keyboard="false"，三區：故事回顧 / GM 選項卡 / 自訂命令）
```

- 角色頭像列 + `on_screen_det_trigger` 機制**直接抄** `index.html:9-21` 同 `:52-83` 嘅 jQuery click handler，`persona_names` 改為本局 `agents`。
- 好感度面板：table 顯示每條 `A->B` 雙向數值（A對B 同 B對A 獨立兩行），輪詢更新。
- 決策 modal 細節：
  - 區一「故事回顧」：`GET /api/timeline` 渲染，每節點可摺疊對白（Bootstrap collapse）。
  - 區二「GM 選項」：`pending_gm.options` 渲染 2-3 張 card，點擊 `.active` 高亮。
  - 區三「自訂命令」：`<select>` 只列 `state.agents`；`<textarea maxlength=500>` + 字數計；揀選項同輸入命令互斥（輸入文字即取消選項高亮，反之亦然）。
  - 「確認，繼續故事」→ `POST /api/decision`；400 時 modal 唔閂、內容唔清，顯示 `error`。

### 4.5 `replay.py` — 唔改

`game_server.py` 照抄 `replay.py:9-14` 嘅 Flask app 設定。兩者並存；replay 繼續服務 `results/compressed/` 離線回放（Done When 最後一條）。

---

## 5. 關鍵流程

### 5.1 回合狀態機

```text
idle ──POST /round/start──▶ simulating ──simulate 完成──▶ waiting_gm
  ▲                                                        │
  │                                          GMResult 到達（provider 回調
  │                                          或 POST /api/gm/result；
  │                                          失敗→保底選項）
  │                                                        ▼
  │                                                  waiting_decision
  │                                                        │
  └────────────── POST /decision 成功（round+1） ◀─────────┘
                     round==max_rounds → finished
任何狀態異常 → error（error 欄有訊息；由 idle 可重試）
```

`playing` 係可選子態：simulating 完成、frame 隊列未播完、GM 仲未觸發時用。實作上可以直接 simulating→waiting_gm（GM 觸發唔等前端播完），`playing` 只係 UI 顯示用，唔影響狀態機正確性。

### 5.2 首次進入（Setup 系統交接後）

1. Setup 已寫：`results/checkpoints/<name>/` 第 0 步 config + `game_ui_state.json`（經 `create_initial_state`，timeline[0]=opening，affinity 初值）→ 302 去 `/game?name=<name>`。
2. `game_server` 首次見到 session：`RoundRunner.recover()` → 建 FrameBuffer（scan 出第 0 帧 `insert_frame0`）。
3. 玩家撳「開始推演」→ `start_round()` → `get_config_from_log` 恢復 → thread 跑 `simulate(8, 10)`。

### 5.3 斷線恢復

- Server 重啟 / 進程被殺後：第一次 `GET /game` 或 `GET /api/state` 觸發 `recover()`。
- 掃描時逐檔 `json.load`，失敗檔記入 `RecoveryInfo.skipped_files` 並跳過；以 `step` 欄位最大嘅完整檔為準。
- 若 `game_ui_state.json` 本身爛（理論上原子寫唔會）：由最後完整 checkpoint 重建一份默認 state（round 由 `step // steps_per_round` 推算，`recovered_message` 話畀玩家知）。
- status 死時係 SIMULATING → 回滾 IDLE；死時係 WAITING_DECISION → `pending_gm` 仲喺檔入面，直接重彈 modal（PRD 要求）。

### 5.4 GM 失敗保底

進程內 `GMProvider.generate` 拋異常 / 超時（60s）→ `_on_round_complete` catch，寫入：

```python
GMResult(
    options=[
        GMOption(id="opt_1", title="讓故事自由發展", description="唔干預，由角色繼續自主推演。"),
        GMOption(id="opt_2", title="重試 GM 生成", description="重新觸發一次分支選項生成。"),
    ],
    affinity_deltas=[],
    generated_at=...,
)
```

`error` 欄記低原因。玩家揀「重試 GM 生成」時 `POST /api/decision {"choice_id": "opt_2"}` 嘅特殊處理：唔注入記憶，重新調 GMProvider 一次（仍失敗再出保底），唔推進 round。外部 HTTP GM 嘅失敗由 GM 系統自行負責（§3.6）。

### 5.5 對話與事件嘅 feed 生成

每個新 checkpoint：

- **事件**：逐 agent 讀 `agents[name].action.event`，`describe` 為空時 fallback `predicate+object`（同 `compress.py:158-160`）→ `FeedItem(kind=EVENT, text=..., location=get_location(address))`；😴/💬 前綴規則同 `compress.py:168-172`。
- **對話**：`conversation.json` 新 sim_time key → 逐場 `{"persons @ location": [[speaker, line], ...]}`（`start.py:100` 寫入嘅結構）→ 一條 `FeedItem(kind=CHAT, dialogue=[DialogueLine...])`。**逐句結構化，唔准拼 string**（同離線版 `compress.py:137-141` 嘅差異點）。
- **timeline**：每回合結束時，將該回合嘅 event/chat 聚合成 timeline 節點（event 節點 body=describe；chat 節點 dialogue=原文逐句）。呢一步喺 `_on_round_complete` 做，唔係實時做——timeline 係「回合粒度」嘅故事回顧。

### 5.6 多 tab 控制

- 前端載入時 `crypto.randomUUID()` 生成 `client_id`，每 5s `POST /api/heartbeat`。
- 非 owner：`/api/state` 返回 `readonly: true` → 隱藏「開始推演」同 modal 確認按鈕，頂部 banner「觀察模式：另一個分頁正在主持此故事」。
- owner 租約 15s 自然過期（閂 tab 唔使主動釋放）。

---

## 6. 文件計劃

### 新建

| 路徑 | 內容 |
|---|---|
| `generative_agents/story_weaver/__init__.py` | 空 / package docstring |
| `generative_agents/story_weaver/models.py` | §2.1 全部 pydantic model |
| `generative_agents/story_weaver/state_store.py` | §2.2 `GameUIStateStore`、`create_initial_state`、`apply_affinity_deltas` |
| `generative_agents/story_weaver/incremental.py` | §2.3 `FrameBuffer`、`CompressDelta`、`load_maze` |
| `generative_agents/story_weaver/round_runner.py` | §2.4 `RoundRunner`、`RecoveryInfo`、`get_config_from_log_tolerant` |
| `generative_agents/story_weaver/contracts.py` | §2.5 `GMProvider` / `DecisionInjector` Protocol + registry |
| `generative_agents/story_weaver/game_server.py` | §3 全部 route；`if __name__ == "__main__": app.run(port=5001, threaded=True)` |
| `generative_agents/frontend/templates/game.html` | §4.4 三欄頁面 + 決策 modal + 輪詢 JS |
| `generative_agents/frontend/templates/game_script.html` | §4.3 Phaser fork |
| `docs/spec/game-ui.md` | 本文件 |

### 修改

| 路徑 | 改動 | 理由 |
|---|---|---|
| **無**（後端 `.py`） | — | 最小侵入原則，見 §1.1 |
| `generative_agents/data/config.json` | `think.llm` 轉 `provider=openai` + base_url/api_key/model | **由「提示詞繁體化 / LLM 配置系統」負責**，本系統只聲明依賴，唔喺本 spec 範圍內改 |

### 明確唔掂嘅文件

`start.py`、`compress.py`、`replay.py`、`modules/**`、`frontend/templates/{base,index,main_script}.html`、`frontend/static/assets/**`。

---

## 7. 同其他 5 個系統嘅契約

### 7.1 Setup 角色配置系統（入）

- **交付物**：`results/checkpoints/<name>/` 入面有 (a) 可供 `get_config_from_log` 恢復嘅初始 checkpoint（或可直接 `get_config` 嘅初始條件）、(b) `game_ui_state.json`。
- **調用口**：`story_weaver.state_store.create_initial_state(...)`（§2.2）——Setup 直接調呢個函數寫 state，唔准手砌 JSON。
- **必須保證**：`agents` ≥ 4 且全部喺 `frontend/static/assets/village/agents/<名>/` 有 `agent.json` + `texture.png`；`affinity` key 格式 `"A->B"`（雙向獨立兩條）；opening 文字寫入 `timeline[0]`（`node_type=OPENING`）。
- **交接**：302 redirect 去 `/game?name=<session>`。

### 7.2 GM 敘事總監系統（出/入）

- **觸發**：status 變 `waiting_gm`（可由 `/api/state` 輪詢觀察，或進程內 `register_gm_provider` 被直接調用）。
- **輸入**（GM 自己讀，本系統唔轉手）：`results/checkpoints/<name>/simulate-*.json`（每 agent 嘅 `action.event`、`currently`）+ `conversation.json`；另有 `timeline` 同 `affinity` 快照（進程內經 `GMProvider.generate` 參數；HTTP 模式 GM 自己 `GET /api/timeline` + `/api/state`）。
- **輸出**：`GMResult`（§2.1）——options 2-3 個、可選 `affinity_deltas`。進程內：return value；HTTP：`POST /api/gm/result`。
- **超時/失敗**：進程內 60s 超時 → 保底選項（§5.4）；HTTP 模式無超時兜底，GM 系統自己保證最終 POST（否則玩家停喺 waiting_gm——GM 系統嘅 spec 要處理呢個 case）。

### 7.3 決策注入系統（出）

- **調用口**：`DecisionInjector.inject(session, decision, state)`（§2.5），由 `POST /api/decision` 處理器同步調用（§3.4 步驟 2）。
- **記憶注入點**：`game.get_agent(name).associate.add_node(node_type="event", event=Event(subject=..., predicate=..., object=..., describe=..., address=[...]), poignancy=9)`（`modules/memory/associate.py:166`；`Event` 喺 `modules/memory/event.py`）。`Game` 實例經 `modules.game.get_game()` 攞（全局單例，此時一定係本 session 嘅 game，因為 §1.4 全局鎖）。
- **睡眠角色**：唔使特判，`add_node` 直接寫記憶流；本系統負責喺 feed 加 system 訊息「命令已送達，角色醒來後生效」（由 inject 返回值或 state 查睡眠態觸發）。
- **職責邊界**：affinity clamp 同 timeline decision 節點係**本系統**做（§3.4 步驟 3-4）；記憶流寫入係**注入系統**做。

### 7.4 提示詞繁體化 / LLM 配置系統（入）

- 本系統**零簡轉繁後處理**。UI 見到嘅對白/行動描述全部來自 checkpoint，繁體與否取決於 29 個 prompt 模板（`data/prompts/*.txt`）嘅繁體化。
- UI 靜態文案（按鈕、面板標題、system feed、錯誤訊息）喺 `game.html` / `game_server.py` 硬編碼繁體香港書面語，係本系統自己嘅責任。
- LLM 連接靠 `data/config.json` 嘅 `think.llm`（provider=openai 兼容）——本系統唔讀寫 LLM 配置。

### 7.5 存檔與回放系統（出）

- **兼容承諾**：checkpoint 目錄嘅檔案格式完全唔變（本系統只新增 `game_ui_state.json` 一個檔，離線工具會當佢唔存在——`compress.py:74-76` 同 `start.py:115-117` 都係用「`.json` 結尾且唔係 conversation.json」過濾。**注意**：`game_ui_state.json` 會被呢個過濾規則誤拾！所以離線兼容需要存檔回放系統喺掃描時排除 `game_ui_state.json`——呢個係**存檔回放系統 spec 要處理嘅改動**，本系統喺度聲明呢個交互事實。）
- **導出**：`GET /api/export/story?format=md|json`（§3.8）畀存檔系統做故事紀錄導出。
- **回放**：`results/compressed/<name>/movement.json` 仍由離線 `compress.py` 全量生成，`replay.py` 照舊可用。

---

## 8. 非功能約定

- **線程模型**：Flask `threaded=True`；推演喺單一 daemon thread；全局一把 `threading.Lock`（§1.4）；state 讀寫經 `GameUIStateStore` 內部 `RLock` + 原子檔案寫。
- **輪詢間隔**：`/api/state` 2s；heartbeat 5s；租約 15s。
- **Feed DOM 上限**：200 條；timeline 無上限（10 回合規模可控）。
- **錯誤呈現**：`state.error` 永遠喺 `/api/state` 返回，前端頂欄紅色 banner 顯示；`status==error` 時「下一回合」可用（由 idle 語義重試）。
- **Port**：game server 5001（replay.py 5000 唔變）。
