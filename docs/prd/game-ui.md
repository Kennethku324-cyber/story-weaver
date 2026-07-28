# PRD: 遊戲主 UI

> 系統：遊戲主 UI（Game Main UI）
> 專案：Story Weaver（基於 GenerativeAgentsCN，Python 3.12 + Flask + Phaser 3.55.2）
> 代碼基礎：`/Users/kenneth/Projects/story-weaver/generative_agents/replay.py`、`frontend/templates/index.html`、`frontend/templates/main_script.html`、`compress.py`
> 狀態：Mini-PRD v1（設計基於已驗證代碼，所有檔案路徑與函數名均為真實存在）

---

## 核心目的

**唯一必須做到的事：讓玩家「看住故事發生、在關鍵時刻插手」，而唔係睇回放。**

現有 `replay.py` 係一個離線回放器：玩家跑完 `start.py` → 跑 `compress.py` 生成 `results/compressed/<name>/movement.json` → 打開瀏覽器被動觀看。Story Weaver 需要將佢改造成**遊戲主介面**，必須同時滿足三個玩家需求：

1. **在場感**：地圖上半實時睇到 agents 移動、行動、對話（重用 Phaser 地圖渲染，但數據來源由「壓縮存檔」改為「每回合新鮮 checkpoint」）。
2. **敘事知情權**：右側事件/對話 feed 持續滾動，玩家任何時候都知道「邊個喺邊度做緊咩、講咗咩」（數據來自 checkpoint 嘅 `action.event.describe` 同 `conversation.json`）。
3. **介入權**：每回合結束彈出決策 modal —— 玩家必須能（a）睇返由故事開端到而家嘅完整時間線（保留對白原文）、（b）揀 GM 生成嘅 2-3 個選項、或者（c）輸入自訂命令直接命令某個角色 —— 然後先至開下一回合。冇呢個 modal，呢個系統就係失敗。

非目標（Out of scope）：唔做輸贏結算、唔做分數、唔做多人聯機、唔改動 agents 自主推演邏輯（`Agent.think` 嘅 percept → plan → reflect → chat 循環原封不動）。

---

## 玩家體驗

### 進入流程
1. 玩家喺 Setup 頁（另一系統）完成角色配置後，被導向 `GET /game?name=<session_name>`。
2. 後端確認 `results/checkpoints/<session_name>/` 存在（Setup 系統已生成初始 config 同第 0 步存檔），渲染遊戲頁。頁面沿用 `base.html`（Bootstrap 3 + jQuery 已載入）+ Phaser 3.55.2 CDN。
3. 玩家見到：左邊 2D 小鎮地圖（35 個 sector，agents 喺住所/咖啡館等位置待命）、右邊側欄（事件 feed + 角色好感度面板 + 回合控制）、頂部顯示「第 0 回合 · 故事未開始 · 遊戲內時間」。

### 一個回合嘅互動循環
1. 玩家撳「**開始推演**」（第一回合）或「**下一回合**」。前端 `POST /api/round/start`，按鈕即刻變 loading + disabled。
2. 後台線程跑 `SimulateServer.simulate(step=<每回合步數>, stride)`。前端每 2 秒 `GET /api/state` 輪詢：
   - 地圖：新產生嘅 frame 增量加入播放隊列，agents sprite 沿 `maze.find_path` 路徑移動（動畫邏輯直接搬 `main_script.html` 嘅 `update()`：四方向 walk anim、`execute_count` 步進、pronunciatio 氣泡顯示 `<名字>: <行動描述>`，瞓覺加 😴、對話加 💬）。
   - Feed：每條新事件一行 —— `🕐 09:45 · 伊莎贝拉 喺 霍布斯咖啡馆 冲緊咖啡`；對話則成段顯示 `伊莎贝拉：「……」`（格式照抄 `compress.py` 第 137-141 行嘅 `step_conversation` 組裝方式，但逐句結構化，唔再係一舊 string）。
3. 玩家呢段時間係**觀察者**：可以用方向鍵移鏡頭、撳角色頭像 focus 佢（沿用 `index.html` 嘅 `on_screen_det_trigger` 機制，顯示 currently / 當前行動 / 所在地址）、撳「暫停播放」（只停前端動畫，唔停後台推演）、睇好感度面板（GM 調整後即時反映）。
4. 回合結束（後台 simulate 跑完，寫咗新 `simulate-*.json` + `conversation.json`）→ 後端觸發 GM agent 生成分支選項 → `/api/state` 返回 `status: "waiting_decision"` + GM payload → 前端**強制彈出決策 modal**（唔可以撳空白位關閉）。

### 決策 modal（本系統嘅靈魂）
Modal 分三區，順序固定：

1. **故事回顧**（可滾動，置頂）：由「故事開端」到而家嘅完整時間線。每個節點 = 遊戲時間 + 事件描述 + 對白原文（`conversation.json` 入面嘅 `c[0]：c[1]` 逐句保留，唔准摘要改寫）。玩家撳節點可以摺疊/展開對白。
2. **GM 選項**：2-3 張選項卡，每張有標題 + 一段劇情走向描述。撳一張即選中（高亮）。
3. **自訂命令**：文字框 + 角色下拉（只列本局 4+ 個角色）。玩家可以代替選項，直接寫「命令卡洛斯聽日去酒館搵瑞恩道歉」。

玩家撳「**確認，繼續故事**」→ `POST /api/decision` → 後端交畀決策注入系統 → modal 關閉 → 「下一回合」按鈕重新亮起。**感覺**：玩家係導演，唔係玩家角色 —— 睇住一班自主角色生活，喺劇情分叉位落閘、出手、放行。

---

## 數據模型

### Inputs（玩家 / 其他系統輸入）

| 欄位 | 類型 | 來源 | 說明 |
|---|---|---|---|
| `session_name` | string | URL query / Setup 系統 | 對應 `results/checkpoints/<name>/`，規則同 `start.py` argparse `--name` |
| `round_command` | enum: `start` / `next` | 玩家撳掣 | `POST /api/round/start` |
| `decision.choice_id` | string \| null | 玩家 | GM 選項 ID（`opt_1`/`opt_2`/`opt_3`）；同 `custom_command` 互斥 |
| `decision.custom_command` | object \| null | 玩家 | `{target_agent: string, text: string(1..500字)}`，對應簡報「玩家可輸入自訂命令直接命令角色」 |
| `playback.speed` | int 0..5 | 玩家 | 沿用 replay.py 嘅 speed → `2 ** speed` |
| `playback.paused` | bool | 玩家 | 只影響前端動畫隊列 |

### Internal State（後端 session state，存喺 `results/checkpoints/<name>/game_ui_state.json`）

| 欄位 | 類型 | 說明 |
|---|---|---|
| `round` | int | 當前回合，0-based；上限 10（簡報：2-10 回合） |
| `status` | enum | `idle` / `simulating` / `playing` / `waiting_decision` / `finished` / `error` |
| `sim_step_cursor` | int | 已模擬嘅總 step 數（對應 `SimulateServer.start_step`） |
| `frame_cursor` | int | 前端已播放至第幾 frame（對應 `compress.py` 嘅 `(step-1)*frames_per_step+1+i`，`frames_per_step=60`） |
| `timeline` | list | 故事回顧數據，逐條：`{seq, sim_time, node_type: "opening"\|"event"\|"chat"\|"decision"\|"gm_note", title, body, dialogue: [{speaker, line}], poignancy}`；`decision` 節點記錄玩家每回合揀咗咩 |
| `pending_gm` | object \| null | GM 輸出：`{options: [{id, title, description}], affinity_deltas: [{from, to, delta}], generated_at}`；玩家未決策前唔會被清走（恢復用） |
| `affinity` | dict | 雙向好感度：`{"A->B": -100..100}`，A對B 同 B對A 獨立；初始值由 Setup 系統寫入，GM 每回合可改 |
| `error` | string \| null | 最近錯誤訊息（畀 UI 顯示） |

### Outputs（畀其他系統 / 前端）

| 輸出 | 格式 | 消費者 | 說明 |
|---|---|---|---|
| `GET /api/state` | JSON | 前端 | `{status, round, sim_time, new_frames: {step_key: {agent: {movement, location, action}}}, new_feed: [...], conversation_delta: {...}, pending_gm, affinity}`；frame 結構同 `compress.py generate_movement` 產出一致，前端動畫代碼可以原樣用 |
| `POST /api/decision` payload | JSON | 決策注入系統 | `{round, choice_id 或 custom_command, decided_at}`；注入系統負責調 `Associate.add_node(node_type="event", event=..., poignancy=<高值>)` 寫入相關 agents 記憶流 |
| GM 輸入快照 | checkpoint 檔案 | GM 系統 | GM 直接讀 `results/checkpoints/<name>/simulate-*.json`（含每個 agent 嘅 `action.event`、`currently`）+ `conversation.json`（`{sim_time: [{persons @ location: [[speaker, line], ...]}]}`），唔經本系統轉手 |
| `GET /api/timeline` | JSON | 前端 modal | 完整 `timeline` 陣列（故事回顧用） |
| `POST /api/round/start` 回應 | JSON | 前端 | `{accepted: bool, round, reason?}`；`simulating` 中再撳會回 `409 + {accepted: false, reason: "推演進行中"}` |

---

## 依賴

### 本系統直接重用 / 修改嘅現有代碼

| 檔案 / 函數 | 關係 | 點用 |
|---|---|---|
| `replay.py`（Flask app、`index()`） | **改造起點** | 新 `game_server.py` 沿用佢個 app 設定（`template_folder="frontend/templates"`、`static_folder="frontend/static"`），但 route 由單一 `GET /` 擴充為上表 5 個 endpoint；原本「讀 `results/compressed/<name>/movement.json`」改為「每回合增量壓縮」 |
| `compress.py` · `generate_movement()` | **邏輯重用** | 抽岀佢嘅 frame 生成邏輯（`maze.find_path` 插值、😴/💬 圖標、`step_conversation` 組裝）做增量版：每回合完結只處理新 checkpoint，append 入 `all_movement`，唔再全量重掃 |
| `compress.py` · `get_location()` / `insert_frame0()` | 直接調用 | 地址 `[world, sector, arena, game_object]` → 顯示字串；第 0 帧初始位置 |
| `frontend/templates/main_script.html` | **改造** | Phaser preload/create（tilemap、16 個 tileset、agent atlas 載入、`sprite.json` 動畫）原封保留；`update()` 由「本地 `all_movement` 逐帧播」改為「播放隊列：輪詢到嘅新 frame push 入隊」；`all_movement` 唔再係頁面載入時一次過 `tojson`，改為漸進注入 |
| `frontend/templates/index.html` + `base.html` | **擴充** | 保留角色頭像列同 `on_screen_det_trigger` 詳情面板；新增右側 feed 欄、好感度面板、回合控制列、決策 modal（Bootstrap 3 modal，`data-backdrop="static" data-keyboard="false"`） |
| `start.py` · `SimulateServer.simulate(step, stride)` | **被調用** | 每回合後台線程調一次；`step` = 每回合步數（建議 config 化，預設 8 步 × stride 10 分鐘）；佢每 step 寫 `simulate-<時間>.json` + `conversation.json`，係本系統同 GM 系統嘅共同數據源 |
| `start.py` · `get_config_from_log()` / `get_config()` | 恢復依賴 | 斷線恢復時用 `get_config_from_log` 讀最後 checkpoint 重建 `SimulateServer` |
| `modules/game.py` · `Game.agent_think()` | 唯讀 | 佢返回嘅 `info`（`currently`、`action.abstract()`、`chats`、`address`）係 feed 嘅素材來源 |
| `modules/memory/associate.py` · `Associate.add_node(node_type, event, poignancy, ...)` | **注入點（間接）** | 決策注入系統調佢；本系統只負責將玩家 decision 交過去，並將「玩家決定咗 X」本身作為 `timeline` 嘅 `decision` 節點 |

### 同其他 5 個系統嘅介面

| 系統 | 方向 | 介面 |
|---|---|---|
| Setup 角色配置系統 | 入 | 寫好初始 checkpoint + `game_ui_state.json`（含初始 `affinity`、故事開端作為 `timeline[0]`，`node_type="opening"`），再 302 去 `/game?name=...` |
| GM 敘事總監系統 | 出/入 | 出：回合完結時本系統喺 state 設 `status="waiting_gm"` 並提供 checkpoint 路徑；入：GM 寫返 `pending_gm`（選項 + `affinity_deltas`）落 `game_ui_state.json` 或經 `POST /api/gm/result` |
| 決策注入系統 | 出 | 收 `POST /api/decision` 嘅 payload，負責 `add_node` 高 poignancy 記憶 + 應用 GM 嘅 `affinity_deltas`（clamp ±100） |
| 提示詞繁體化 / LLM 配置系統 | 入 | `data/config.json` 嘅 `think.llm` 轉 `provider=openai`；UI 顯示嘅所有對白/行動描述靠 29 個 prompt 模板轉繁體後自然產出繁體，本系統**唔做**任何簡轉繁後處理 |
| 存檔與回放系統 | 出 | 本系統產生嘅 `game_ui_state.json` + 增量 movement 數據要兼容現有 `compress.py` 全量重跑（離線回放唔會壞）；`timeline` 可導出為故事紀錄 |

---

## 邊界情況

| 場景 | 預期行為 |
|---|---|
| 玩家未揀選項就閂 modal / 刷新頁 / 閂瀏覽器 | Modal 唔可以用 ESC/撳背景關閉（`data-backdrop="static"`）；`pending_gm` 持久化喺 `game_ui_state.json`，重新入 `/game` 時 `status="waiting_decision"` 即刻重彈同一 modal；回合數唔會偷偷前進 |
| GM / LLM 返回垃圾（選項少於 2 個、JSON 爛、超時） | 靠 `llm_model.py` 嘅 retry=10 + failsafe 先頂；仍然失敗 → 後端注入保底選項「讓故事自由發展」+「重試 GM 生成」，`error` 欄記低，玩家唔會卡死 |
| 推演中途進程被殺（checkpoint 寫到一半） | `simulate-*.json` 可能截斷 → 恢復時 `get_config_from_log` 掃檔案，跳過 JSON parse 失敗嘅檔，用最後一個完整 checkpoint；UI 顯示「已恢復至第 N 回合（M 步），部分進度遺失」 |
| 自訂命令：`target_agent` 唔存在 / `text` 空 / 超 500 字 / 含指令注入式內容 | 前端即時擋（下拉限制角色、字數計）；後端再驗證一次，回 `400 {reason}`，modal 保留玩家已輸入內容唔清走 |
| 好感度爆界（GM `affinity_deltas` 令數值超出 ±100） | 應用前 clamp 到 [-100, 100]；UI 面板顯示 clamp 後數值，feed 加一行系統訊息「GM 調整：A 對 B 好感 +35（已達上限）」 |
| 玩家喺 `simulating` 期間再撳「下一回合」 | 按鈕 disabled；就算繞過前端，`POST /api/round/start` 回 409，唔會起第二個 simulate 線程（後端以 session lock 保證單線程推演，因為 `GenerativeAgentsMap` 係全局單例，並行會撞 state） |
| `movement.json` / 增量 frame 未生成但玩家已開頁 | 唔學 replay.py 咁回一串錯誤 HTML；改為 JSON `503 {status: "preparing"}`，前端顯示 loading 動畫繼續輪詢 |
| 決策注入時目標角色正在瞓覺 | 記憶注入唔受睡眠影響（`add_node` 直接寫記憶流）；角色醒後下一個 `think()` 先反映；feed 提示「命令已送達，角色醒來後生效」 |
| 對話 feed 過長（10 回合、每回合幾十條） | Feed 只保留最近 200 條於 DOM，舊條目入 `timeline`；「故事回顧」modal 永遠有完整原文，feed 唔係唯一記錄 |
| 遊戲內時間跨日 / agents 全部瞓覺嘅回合 | Feed 顯示「夜深了，小鎮一片寂靜」；GM 仍然要出選項（允許「直接跳到朝早」類選項）；唔准 modal 唔彈令玩家以為 hang 機 |
| 多 tab / 多瀏覽器開同一 session | 第一個 tab 持有控制權（heartbeat）；第二個 tab 進入唯讀觀察模式（冇決策按鈕，banner 提示） |
| 回合數達到 10 上限 | `status="finished"`，地圖繼續可播最後回合動畫；modal 改為「故事完結」頁：完整時間線 + 導出故事按鈕；「下一回合」永久消失 |

---

## Done When

- [ ] `GET /game?name=<session>` 渲染三欄介面：Phaser 地圖（沿用現有 tilemap + 25 個 agent atlas 載入）、事件/對話 feed、好感度面板 + 回合控制；console 無 JS error
- [ ] 撳「開始推演」觸發後台 `SimulateServer.simulate()`，地圖喺 2 秒輪詢間隔內見到 agents 移動（四方向動畫、pronunciatio 氣泡、😴/💬 圖標同 replay 版一致）
- [ ] Feed 顯示嘅事件時間、地點、行動描述同當前 checkpoint `simulate-*.json` 嘅 `action.event` 完全一致；對話逐句顯示 `角色：對白`，同 `conversation.json` 原文一致
- [ ] 每回合結束必彈決策 modal：含完整「故事回顧」（由 opening 到而家、對白原文、未經摘要）、2-3 個 GM 選項、自訂命令輸入（角色下拉 + 500 字上限）；modal 唔可以無選擇關閉
- [ ] 玩家確認決策後，`POST /api/decision` payload 正確送到決策注入系統，且 `timeline` 新增一條 `decision` 節點（記錄揀咗咩）
- [ ] 好感度面板顯示雙向數值（A→B 同 B→A 獨立），GM 調整後下一個輪詢週期內更新，數值永遠喺 [-100, 100]
- [ ] `simulating` 中撳「下一回合」後端回 409；刷新頁面喺 `waiting_decision` 狀態會重彈同一個 modal（`pending_gm` 無丟失）
- [ ] 殺掉推演進程再重啟，系統用最後完整 checkpoint 恢復，UI 明確顯示恢復到第幾回合
- [ ] LLM 持續失敗時 modal 出現「讓故事自由發展」保底選項，玩家唔會卡死
- [ ] 第 10 回合後進入 `finished`：顯示完整故事時間線 + 導出功能，回合按鈕消失
- [ ] 全部 UI 靜態文案（按鈕、面板標題、錯誤提示、系統 feed 訊息）為繁體香港中文書面語；角色對白/行動描述經繁體化 prompt 自然產出繁體
- [ ] 現有離線流程無退化：`start.py` → `compress.py` → `replay.py` 全量回放照舊運作
