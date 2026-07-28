# PRD: 角色 Setup 系統

> 項目：Story Weaver（互動敘事遊戲，Python 3.12 + Flask，基於 GenerativeAgentsCN）
> 狀態：首個 deliverable ｜ 版本：v0.1 ｜ 日期：2026-07-28
> 代碼根目錄：`/Users/kenneth/Projects/story-weaver/generative_agents/`

---

## 核心目的

**呢個系統唯一必須做到嘅嘢：將玩家嘅故事意圖（邊幾個角色、佢哋係邊個、互相點睇對方、故事由邊度開始）轉化成一組 GenerativeAgentsCN 可以直接載入嘅合法 agent 配置檔（`agent.json`）+ 一份故事元數據（`story.json`），令 `SimulateServer` 可以無修改噉跑第一個 step。**

服務嘅玩家需求：

1. **低門檻開局**——玩家唔使識 JSON、唔使識斯坦福小鎮嘅文件結構，用表單就可以喺 5 分鐘內由「我想講一個咩故事」去到「故事開始推演」。
2. **創作控制權**——角色嘅職業、性格、關係、雙向好感度、故事開端全部係玩家話事；agents 之後自主推演，但起點係玩家寫嘅。
3. **重用現成資產**——玩家揀嘅係「模板」（現成 25 個 persona 嘅 sprite + 住所模板），唔使自己整圖，保證視覺質素一致，亦保證 maze 地址一定合法。

**非目標（明確劃界）：** 本系統唔負責回合推演、GM 分支偵測、決策 modal、好感度隨劇情演變、回放。佢只負責「開局前」嘅數據生產，同埋保證產出物俾下游系統用得著。

---

## 玩家體驗

### 進入路徑

玩家打開 Flask server（現有 `replay.py`，port 5000）→ 首頁新增「開始新故事」按鈕 → 進入 `/setup`。URL 無需參數。

### 互動流程（逐步）

1. **步驟一：揀角色（模板畫廊）**
   - 畫面展示 25 張角色卡（嚟自 `frontend/static/assets/village/agents/<名>/` 嘅 `portrait.png` + 模板預設性格一句話）。
   - 玩家點擊卡片揀選，卡片亮起並標記順序（1、2、3…）。已揀少於 4 個時，頂部進度條顯示「已揀 2/4」，「下一步」按鈕灰色唔撳得。
   - 揀滿 4 個後可繼續加，上限 10 個（超過會令每回合 LLM 開支同時間過長，且 `think_interval` 會被拖慢）。
   - 感覺：似揀角色卡開局，輕快、有收藏感。

2. **步驟二：設定每個角色（逐張卡片展開）**
   每個已揀角色一張設定卡，欄位如下（* = 必填）：
   - **角色名***：預填模板名，可改（例如將「伊莎贝拉」改成「阿欣」）。
   - **職業***：一行文字（例如「茶餐廳老闆」）。預填模板 `scratch.learned` 嘅第一句，玩家可改。
   - **性格***：一行文字（例如「慢熱、念舊、口硬心軟」）。預填模板 `scratch.innate`。
   - **住所**：下拉選單，預設係模板自己嘅 `living_area`；可改揀其他未佔用房間（見「數據模型」住所清單）。兩個角色可以住同一間屋唔同房；同房唔允許。
   - 提供「一鍵用返模板預設」按鈕，趕時間嘅玩家可以跳過微調。

3. **步驟三：關係同好感度（矩陣表格）**
   - N×N 表格（N = 角色數），對角線灰色停用。
   - 每格兩部分：**好感度滑桿**（-100 ~ +100，預設 0，標籤：-100 仇視 / 0 陌生 / +100 摯愛）+ **關係描述**（短文字，例如「前夫，仲有啲嬲」）。
   - **A 對 B 同 B 對 A 係兩格獨立欄位**，UI 用三角形提示「阿欣對阿強 +60」同「阿強對阿欣 -20」可以好唔同——呢個係戲劇張力嚟源。
   - 留空嘅格自動當 0「陌生」，提交前彈一次確認：「有 6 對關係未設定，佢哋會以陌生人開局，繼續？」

4. **步驟四：故事開端**
   - 一個大文本框（*必填，建議 50-300 字），例如：「年三十晚，阿欣嘅茶餐廳收到拆遷通知，佢決定瞞住啲街坊，但阿強已經收到風……」
   - 下方提示：呢段文字會注入所有角色嘅記憶，成為佢哋共同嘅「今日發生嘅大事」。

5. **提交同反饋**
   - 撳「開始故事」→ 前端逐欄位校驗，有錯即時紅框標示，唔會整甩已填內容。
   - 校驗通過 → POST `/api/setup/create` → 後端生成文件（見「數據模型 Outputs」）→ 顯示進度「正在為角色安頓住所…寫入記憶…」。
   - 成功 → 跳轉去推演畫面（下游系統），URL 帶 `?story=<story_name>`。
   - 失敗（例如故事名撞名）→ 模態框講明原因同點改，已填資料保留喺頁面。

### 整體感覺目標

由「揀卡」到「開局」不超過 5 分鐘；每一步都有即時反饋；玩家覺得自己係「編劇」而唔係「填表機」——關係矩陣同故事開端係成個流程嘅情感高潮位。

---

## 數據模型

### Inputs（玩家經 UI 提供）

```jsonc
// POST /api/setup/create 嘅 request body
{
  "story_name": "茶餐廳風雲",            // string, 必填, 唯一, 用作 checkpoints 目錄名
  "story_opening": "年三十晚……",        // string, 必填, 10-1000 字
  "characters": [                        // array, 長度 4-10
    {
      "template_id": "伊莎贝拉",          // string, 必填, 對應 assets/village/agents/<名>/
      "display_name": "阿欣",             // string, 必填, 全故事唯一
      "occupation": "茶餐廳老闆",         // string, 必填 → scratch.learned
      "personality": "慢熱、念舊",        // string, 必填 → scratch.innate
      "home": ["the Ville", "伊莎贝拉的公寓", "主人房"]  // [world, sector, arena], 必填, 可改
    }
  ],
  "relationships": [                     // array, 可空（空 = 全部陌生）
    {
      "from": "阿欣",                    // string, 必填, 必須係 display_name
      "to": "阿強",                      // string, 必填, ≠ from
      "score": 60,                       // int, -100..+100, clamp
      "desc": "欣賞佢但唔敢講"            // string, 可空
    }
  ]
}
```

### Internal State（後端生成過程）

- **模板目錄（Template Catalog）**：啟動時掃描 `frontend/static/assets/village/agents/*/agent.json`，緩存 `{template_id: {name, portrait_path, texture_path, innate, learned_first_line, living_area, spatial_tree}}`。來源：現有 25 個目錄（`start.py` 嘅 `personas` 列表同目錄一一對應）。
- **住所登記表（Housing Registry）**：由 `frontend/static/assets/village/maze.json` 嘅 `tiles[].address` 掃出全部 `[sector, arena]` 有房間嘅組合。已驗證嘅可用住所包括：各角色自有公寓/房屋、`莫雷诺家族的房子` 嘅「空卧室」、`奥克山学院宿舍` 四間房、`艺术家共居空间` 六間房、`摩尔家族的房子`「主人房」等。分配時標記 `occupied_by`，保證一間房（含「床」game_object）最多一個角色。
- **校驗錯誤列表**：`[{field, message}]`，全部錯一次過返晒，唔好逐個彈。
- **生成順序**：先寫 story 目錄 → 再逐角色寫 agent 目錄 → 最後寫 sim config。任何一步失敗要 rollback 已寫嘅目錄（避免半殘故事檔）。

### Outputs（其他系統消費嘅產物）

**1. 每個角色一個目錄**（路徑跟現有慣例，令 `start.py` 嘅 `get_config` / `get_config_from_log` 嘅 `config_path` 拼接邏輯唔使改）：

```
frontend/static/assets/village/agents/<display_name>/
├── agent.json       # 新生成
├── portrait.png     # 由模板目錄複製
└── texture.png      # 由模板目錄複製（32x32, 20 frames, 4 方向）
```

`agent.json` 結構（對齊 `modules/agent.py` `Agent.__init__` 讀取嘅 key，加一個擴展 block）：

```jsonc
{
  "name": "阿欣",
  "portrait": "assets/village/agents/阿欣/portrait.png",
  "coord": [72, 14],                    // 由 maze.get_address_tiles(home+["床"]) 隨機揀一格非 collision tile
  "currently": "阿欣剛收到茶餐廳嘅拆遷通知……",  // 由故事開端改寫嘅角色視角一句話
  "scratch": {
    "age": 34,                          // 沿用模板
    "innate": "慢熱、念舊",              // ← personality
    "learned": "阿欣係茶餐廳老闆……",      // ← occupation 擴寫
    "lifestyle": "阿欣晚上11點左右上床睡覺，早上6點左右醒來。",  // 沿用模板
    "daily_plan": "阿欣每天早上8點開茶餐廳……"                  // 沿用模板改店名
  },
  "spatial": {
    "address": { "living_area": ["the Ville", "伊莎贝拉的公寓", "主人房"] },
    "tree": { /* 完整複製模板嘅 spatial.tree（全鎮認知地圖）*/ }
  },
  "relationships": {                    // 【擴展欄位】Agent.__init__ 唔讀，無害；
    "阿強": { "score": 60, "desc": "欣賞佢但唔敢講" }   // GM 系統同好感度演變系統讀寫
  }
}
```

關鍵約束（已對照代碼驗證）：
- `spatial.address.living_area` 必須指向有「床」嘅房間，因為 `modules/memory/spatial.py` 第 14 行會自動派生 `"睡觉"/"睡覺"` 地址 = `living_area + ["床"]`。
- `coord` 必須係 maze 入面有效 tile——`Agent.__init__` 會 call `self.maze.tile_at(config["coord"])`，無效會直接炸。
- `relationships` 係額外 key：`Game.__init__` 用 `utils.update_dict` 合併配置，多餘 key 會保留喺 config 入面；`SimulateServer.simulate` 每 step 做 `config["agents"][name].update(agent.to_dict())`，`to_dict()` 唔會覆蓋 `relationships`，所以好感度可以一路跟住 checkpoint 演變。

**2. 故事元數據**：`results/checkpoints/<story_name>/story.json`

```jsonc
{
  "story_name": "茶餐廳風雲",
  "story_opening": "年三十晚……",        // 原文保留，俾「故事回顧」時間線做第一條
  "created_at": "2026-07-28T15:00:00",
  "characters": ["阿欣", "阿強", ...],
  "template_map": { "阿欣": "伊莎贝拉" }, // 回放/前端攞 sprite 用
  "relationships": [ /* 同 input, 已 clamp 同補預設 */ ],
  "language": "zh-Hant-HK"
}
```

**3. 記憶注入（開局時執行，唔係文件）**：故事創建後、第一個 step 前，對每個 agent call `agent.associate.add_node(...)`（`modules/memory/associate.py` 第 166 行）：
- 故事開端：`node_type="event"`，`poignancy=9`（滿分 10，確保 reflect 觸發——`config.json` 嘅 `poignancy_max: 150` 係累計閾值），subject = 角色自己。
- 每段關係：`node_type="thought"`，描述如「我對阿強嘅好感係 +60：欣賞佢但唔敢講」，poignancy 按 |score| 映射（0→1，100→8）。

**4. Sim config**：同 `start.py` 嘅 `get_config()` 輸出格式完全一致（`stride` / `time.start` / `maze.path` / `agent_base` / `agents{name: {config_path}}`），但 `agents` 只含玩家揀嘅角色，交俾推演啟動系統餵 `SimulateServer`。

---

## 依賴

### 對內（本系統直接用嘅現有模塊）

| 依賴 | 文件 / 函數 | 用途 |
|---|---|---|
| 模板資產 | `frontend/static/assets/village/agents/<名>/{agent.json, portrait.png, texture.png}` | 模板畫廊 + 複製 sprite |
| Maze | `modules/maze.py` — `Maze.get_address_tiles(address)`（L206）、`Maze.tile_at(coord)`（L165） | 住所清單掃描、spawn coord 合法性 |
| Agent schema | `modules/agent.py` — `Agent.__init__`（L13）讀 `name/coord/currently/scratch/spatial` | 定義 agent.json 必填 key |
| Spatial 派生 | `modules/memory/spatial.py` L12-14（`living_area` → `睡覺` 地址） | 住所必須有「床」嘅約束來源 |
| 記憶注入 | `modules/memory/associate.py` — `Associate.add_node(node_type, event, poignancy, ...)`（L166） | 注入開端 event + 關係 thought |
| Flask host | `replay.py` — `app = Flask(..., template_folder="frontend/templates", static_folder="frontend/static")` | 掛 `/setup` 同 `/api/setup/*` 路由（建議用 Blueprint 唔好直接改 `index()`） |
| Config 格式 | `start.py` — `get_config()`（L138）、`get_config_from_log()`（L111）、`SimulateServer.__init__`（L25） | 輸出格式對齊；`config_path` 拼接 = `assets/village/agents/<name.replace(" ","_")>/agent.json`，所以 **display_name 唔准含空格以外嘅路徑分隔符，空格會轉底線**，命名要兼容 |
| LLM（可選輔助） | `modules/model/llm_model.py` — `create_llm_model`（provider=openai/ollama，retry=10 + failsafe） | 可選嘅「自動擴寫性格/開端」功能；失敗時用模板原文兜底 |

### 對外（本系統服務嘅其他 5 個系統）

1. **推演啟動系統**：消費 sim config + agent 目錄，起 `SimulateServer` 跑 think loop（`Agent.think` → percept/plan/reflect/chat）。
2. **GM 敘事總監系統**：讀 `story.json`（開端原文、角色表）+ 每回合 checkpoint 嘅 `agents.<名>.relationships`，負責好感度調整時直接改 checkpoint config 嘅 `relationships` block（merge 機制保證落盤）。
3. **決策 modal / 故事回顧系統**：`story.json` 嘅 `story_opening` 係時間線第一條；角色對白原文嚟自 `results/checkpoints/<story>/conversation.json`（`SimulateServer` 每 step 寫）。
4. **玩家指令注入系統**：玩家自訂命令同 GM 選項，同本系統注入開端用同一條路徑 `add_node`（高 poignancy event）。
5. **回放 / 前端系統**：`template_map` 搵 sprite；`frontend/templates/` 加 `setup.html`，靜態資產行現有 `static_url_path="/static"`。

### 語言依賴

`data/prompts/` 29 個簡體模板（`base_desc.txt` 等）**唔係本系統改**，但本系統生成嘅 `currently`、`scratch`、關係描述必須寫繁體，否則簡繁混雜會污染 agent 自我認知。繁體化係獨立任務，本 PRD 只保證自己產出嘅文字係繁體。

---

## 邊界情況

| # | 場景 | 預期行為 |
|---|---|---|
| 1 | 玩家揀少於 4 個角色就提交 | 前端 disable 提交按鈕 + 進度條提示；即使繞過前端，後端都返 `422 {"field": "characters", "message": "最少要揀 4 個角色"}` |
| 2 | 必填項留空（職業 / 性格 / 故事開端 / 角色名） | 後端逐欄位驗證，一次過返晒所有錯；前端紅框標示，已填內容唔清甩；每張角色卡提供「用返模板預設」一鍵補填 |
| 3 | 兩個角色 display_name 撞名，或同已有 25 個模板目錄撞名，或故事名同 `results/checkpoints/` 現有目錄撞名 | display_name 全庫唯一性校驗（掃 `agents/` 目錄 + 本故事內）；故事名撞名返 `409`，建議「茶餐廳風雲-2」（對齊 `start.py` L185 嘅撞名處理語義） |
| 4 | 好感度輸入超界（+150）、非數字、或得 A對B 冇 B對A | score 一律 `int()` 後 clamp 去 [-100, +100] 並喺 response 標註 `clamped: true`；缺方向嘅關係補 `score: 0, desc: ""`（陌生），提交前 UI 已彈過一次確認 |
| 5 | 住所衝突：兩個角色揀同一間房；或揀咗冇「床」嘅房（例如「廚房」） | Housing Registry 只列出含「床」game_object 嘅房間俾玩家揀（保證 `spatial.py` 嘅 `睡覺` 地址派生唔會炸）；同房第二個角色提交時後端返錯並列出剩餘空房；同屋唔同房（例如宿舍唔同房間）合法 |
| 6 | spawn coord 落喺 collision tile 或房間搵唔到 tile | 用 `maze.get_address_tiles(home + ["床"])` 攞候选，`tile_at(coord)` 驗證非 collision 先落筆；候選為空（數據異常）時 fallback 去同房任意非 collision tile，再無就返 500 並指明邊個角色邊間房 |
| 7 | LLM 輔助擴寫（性格→currently、開端→角色視角）返回垃圾 / timeout / 空串 | `llm_model.py` 已有 retry=10 + failsafe；本系統加多一層：輸出為空或超長（>500 字）就用規則式拼接兜底（「{name}係{occupation}。{story_opening}」），並喺 response 標 `llm_fallback: true`，唔阻塞開局 |
| 8 | 生成中途失敗（寫到第三個角色目錄時 disk error / permission error） | 生成順序有 rollback：任何一步失敗，刪返今次新建嘅角色目錄同 `results/checkpoints/<story>/`，返 500 附原因；唔留半殘檔俾 `get_config_from_log` 誤載 |
| 9 | checkpoint 恢復（`--resume`）撞正 setup 改咗嘢 | Setup 系統喺故事開始後**鎖定**：`story.json` 加 `"locked": true`，`/api/setup/*` 對已鎖故事返 423；角色嘅演變（好感度等）只準經 GM 改 checkpoint，唔準返轉頭改 setup 源檔。`get_config_from_log`（`start.py` L111）行最新 checkpoint，語義一致 |
| 10 | display_name 含路徑危險字符（`/`、`\`、`..`）或純空格 | 拒絕並提示；空格合法但要知會玩家會存做底線目錄名（因為 `config_path` 拼接用 `name.replace(" ", "_")`）；長度限 1-20 字 |
| 11 | 模板目錄唔見咗 `portrait.png` / `texture.png` | 掃描時標記該模板「素材不完整」，畫廊仲顯示但唔俾揀（tooltip 講明），log warning；唔好開局先至炸 |
| 12 | 玩家揀足 10 個角色，`think_interval` 同 LLM 開支爆錶 | 上限 10 寫死喺前後端；提交時顯示預估「每回合約 N 次 LLM 調用」，提醒開支；唔阻塞但要有心理預期 |

---

## Done When

- [ ] `GET /setup` 渲染 `setup.html`，畫廊列出全部 25 個模板（portrait + 預設性格），素材不完整嘅模板唔俾揀
- [ ] 揀唔夠 4 個角色時提交按鈕 disabled；揀 4-10 個可以進入下一步
- [ ] 關係矩陣 N×N 正確渲染，A對B 同 B對A 係獨立欄位，滑桿限 -100~+100
- [ ] 提交合法表單後，`frontend/static/assets/village/agents/<display_name>/` 生成 `agent.json` + `portrait.png` + `texture.png`；`agent.json` 通過 schema 校驗（含 `name/coord/currently/scratch{age,innate,learned,lifestyle,daily_plan}/spatial{address,tree}/relationships`）
- [ ] 每個角色嘅 `coord` 經 `Maze.tile_at()` 驗證有效且非 collision；`living_area` 指向含「床」嘅房
- [ ] `results/checkpoints/<story_name>/story.json` 寫成，含 `story_opening` 原文、`characters`、`template_map`、`relationships`、`locked: true`
- [ ] 生成嘅 sim config 直接餵俾 `SimulateServer` 可以跑完第一個 step 唔報錯（用 4 角色測試故事實跑一次 `simulate(step=1)` 驗證）
- [ ] 第一個 step 前，每個 agent 嘅記憶流含 ≥1 條故事開端 event（poignancy 9）+ 每段關係一條 thought node（可經 `agent.associate.abstract()` 查證）
- [ ] 好感度超界輸入被 clamp 並喺 API response 標註；缺方向關係補 0
- [ ] 撞名（角色 / 故事）、住所衝突、生成中途失敗 rollback，全部有對應 HTTP code 同中文錯誤訊息
- [ ] 全部 UI 文案同生成文字係繁體香港中文書面語
- [ ] 單元測試覆蓋：schema 校驗、好感度 clamp、住所分配（含衝突）、撞名、rollback；測試用 pytest，行 `cd generative_agents && ../.venv/bin/pytest tests/test_character_setup.py`
