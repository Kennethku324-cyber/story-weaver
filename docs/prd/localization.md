# PRD: 繁體中文化

> 版本 v1.0 · 2026-07-28 · 負責系統：繁體中文化（Localization）
> 範圍：29 個 prompt 模板 + `scratch.py` 內嵌字串 + `agent.py`／memory 模組內中文邏輯判斷 + maze／agent 地名 + 前端介面文案，全部由簡體轉繁體香港書面語。

---

## 核心目的

**唯一必須做到嘅嘢：玩家（同 LLM）喺成個遊戲循環入面接觸到嘅每一隻中文字，都係繁體香港書面語，而且轉換之後 agent 行為邏輯唔可以壞。**

具體拆解：

1. **輸出面一致** — agents 嘅對白、計劃、反思、事件描述（即玩家喺決策 modal「故事回顧」、對話泡泡、角色面板見到嘅所有文字）全部係自然嘅繁體書面語，唔會出現「睡觉」「对话」「卫生间」呢啲簡體殘留，亦唔會係生硬嘅機械式逐字轉換（例如「小睡一会儿」要變「午睡一陣」／「瞓一陣」嘅書面語寫法「小睡片刻」，而非「小睡一會兒」）。
2. **邏輯面唔壞** — 代碼入面有一批**以中文字串做邏輯判斷**嘅硬耦合（詳見「依賴」），例如 `agent.py:111` 用 `"睡" in plan["describe"]` 判斷係咪瞓覺、`agent.py:301` 用 `event.fit(self.name, "对话")` 判斷 node_type、`_completion` 用 `"是"` 解析 boolean。轉語言之後呢啲 keyword 必須同 LLM 實際輸出嘅文字繼續對得上，否則 agents 會唔瞓覺、唔記得對話、判斷唔到話題結束。

服務嘅玩家需求：目標玩家係香港繁體中文使用者。佢哋揀咗角色、寫咗故事開端之後，成個 2-10 回合嘅體驗 —— 睇 agents 自主推演、讀對白原文、喺決策 modal 回顧劇情時間線、輸入自訂命令 —— 全部要係佢哋慣用嘅書面語。簡體殘留會直接打破沉浸感，令「共寫故事」呢個核心賣點失效。

**非目標（Out of Scope）：**
- 唔改遊戲邏輯、數值、prompt 結構（${} 佔位符、JSON schema 示例格式原封不動）。
- 唔做口語／粵語對白（已定決策：對白都用書面語）。
- 唔翻譯英文技術字串（log、exception message、代碼註釋可保留，但玩家可見註釋文字如 replay 參數說明不在 UI 範圍）。
- 唔處理 LLM provider 切換（另一系統負責 config.json 改 provider=openai）。

---

## 玩家體驗

玩家**唔會直接同「本地化系統」互動** —— 佢體驗到嘅係轉換完成後嘅結果。逐步描述：

1. **進入**：玩家打開 Setup 頁（Flask port 5000），見到 25 個可選角色。角色名（「伊莎贝拉」→「伊莎貝拉」）、住所名（「摩尔家族的房子」→「摩爾家族的房子」）、角色簡介（agent.json `currently` 欄位）全部係繁體。
2. **輸入**：玩家填職業、性格、關係、雙向好感度、故事開端 —— 全部用繁體書面語輸入。呢啲文字會原樣注入 agent 嘅 `scratch` 同記憶流，所以**輸入文字本身就係本地化數據嘅一部分**，系統唔會亦唔應該轉換玩家輸入。
3. **推演中**：玩家睇到嘅角色頭頂 emoji＋當前活動（如「伊莎貝拉 正在 沖調咖啡 ☕」）、對話泡泡、地址路徑（`the Ville > 霍布斯咖啡館 > 咖啡館顧客座位`）全部繁體。前端按鈕係「[運行]」「暫停」「[顯示對話]」（由 main_script.html 嘅「[运行]」「[显示对话]」轉嚟），字體用繁體兼容字體（現時寫死 `"24px 黑体"`，黑體係內地字體，香港玩家部機未必有，要改做 `PingFang TC, Microsoft JhengHei, Noto Sans TC, sans-serif` fallback chain）。
4. **決策時刻**：每回合結束 GM agent 出決策 modal，「故事回顧」時間線入面嘅角色對白原文、事件描述，全部源自 prompt 模板產出 —— 模板轉咗繁體，呢度先會係繁體。呢個係玩家感受本地化質素最直接嘅位。
5. **收到嘅反饋／感覺**：成條時間線讀落似一本繁體中文小說，冇簡體字、冇大陸用語（「视频」「软件」「小睡一会儿」），玩家感覺「呢個故事係為我寫嘅」。

---

## 數據模型

### Inputs（本系統消耗咩）

| 來源 | 內容 | 已驗證位置 |
|---|---|---|
| Prompt 模板 ×29 | 簡體 .txt，含 `${var}` 佔位符、內嵌 JSON schema 示例（如 schedule_daily.txt 嘅 `"6:00": "起床并完成早晨的例行工作"`） | `generative_agents/data/prompts/*.txt` |
| scratch.py 內嵌字串 | pydantic `Field(description=...)`（會隨 json_schema 送去 LLM）、failsafe 示例日程、模板化句子（如 `f"{a.name} 正去往 {event.get_describe(False)}"`、`chat_history` 句子） | `modules/prompt/scratch.py`（844 行，約 60+ 處中文） |
| 邏輯關鍵字 | `"睡"`、`"睡觉"`、`"对话"`、`"空闲"`、`"此时"`、`"待开始"`、`"被占用"`、`"床"`、`"是"`、`"living_area"` | `agent.py`、`memory/event.py`、`memory/schedule.py`、`memory/spatial.py`、`memory/associate.py`、`scratch.py` |
| 地名表 | maze.json 約 100 個唯一簡體地名（sector/arena/game_object 三層）；25 個 agent.json 嘅 `spatial.address` / `spatial.tree` 以**字串精確引用** maze 地名 | `frontend/static/assets/village/maze.json`、`agents/<名>/agent.json` |
| 角色檔案 | `name`、`currently`、`scratch.{innate,learned,lifestyle,daily_plan}` 全部簡體；目錄名＝角色名（main_script.html:91 用目錄名載入 texture.png） | `agents/<名>/agent.json` |
| 前端文案 | 按鈕、標籤、`font: "24px 黑体"` | `frontend/templates/{index,main_script,base}.html` |
| 時間格式 | `星期一`…`星期日`、`%Y年%m月%d日`、`daily_format_cn()` | `modules/utils/timer.py:58-76` |

### Internal State

| 狀態 | 說明 |
|---|---|
| 術語對照表（glossary） | 一個 checked-in 嘅 `docs/prd/localization-glossary.md`（或 CSV）：簡體→繁體香港書面語嘅逐項映射，特別係**地名**（「约翰逊公园」→「約翰遜公園」）同**邏輯關鍵字**（「对话」→「對話」）。呢個表係轉換嘅 single source of truth，亦係日後回歸測試嘅基準。 |
| 關鍵字常量化 | 建議將散落各處嘅邏輯字串收斂做 `modules/prompt/keywords.py`（或類似）一組常量：`KW_SLEEP="睡"`、`KW_SLEEPING="睡覺"`、`KW_CHAT="對話"`、`KW_IDLE="空閒"`、`KW_AT_THIS_TIME="此時"`、`KW_PENDING="待開始"`、`KW_OCCUPIED="被佔用"`、`KW_BED="床"`、`KW_TRUE=("true","yes","是","1")`。咁樣 keyword 同 prompt 語言嘅一致性有一個單點可以改。（若另一系統已做類似重構，本系統配合遷入。） |
| 記憶流文字 | `Concept`（event/chat/thought）嘅 `describe`、地址、embedding 文本全部會係繁體 —— **呢個係輸出唔係本系統儲存嘅嘢**，但檢索 query（`associate.py:221` `"对话 " + name`）必須同儲存文字同語言，否則向量相似度跌。 |

### Outputs（其他系統需要知道咩）

| 輸出 | 消費者 | 欄位級說明 |
|---|---|---|
| 繁體 prompt 模板 ×29 | Agent think loop（`Agent.think` → `scratch.py` → `completion()`） | 檔名、`${}` 佔位符名、JSON schema 結構完全不變；只改中文內容 |
| 繁體關鍵字集合 | GM agent 系統、玩家命令注入系統 | GM 生成選項、解析玩家自訂命令時要 inject 高 poignancy event，`Concept(node_type="chat"/"event")` 嘅 predicate/object 用字要同 keywords.py 一致 |
| 繁體地名表 | Setup 系統（住所分配 UI）、GM（事件描述）、前端渲染 | maze.json + 25 個 agent.json **原子性一齊改**；地址格式 `[world, sector, arena, game_object]` 不變 |
| 繁體角色檔案 | Setup 系統（角色選擇頁）、記憶注入 | `name` 改咗嘅話目錄名都要改（main_script.html 用目錄名載入 sprite）；**建議角色名維持原文音譯只轉字形**（伊莎贝拉→伊莎貝拉），避免連鎖改動 |
| 繁體前端文案 | 前端／回放系統 | 按鈕文字、index.html 標籤、字體 fallback chain |
| Glossary | 全部其他系統 | 新增字串時嘅用字基準（例如 GM 系統寫新 prompt 要用「對話」唔係「对话」） |

---

## 依賴

### 對 GenerativeAgentsCN 現有模塊（檔案＋函數級）

| 檔案 | 依賴點 | 風險 |
|---|---|---|
| `modules/agent.py` | `Agent.think()` line 111 `"睡" in plan["describe"]`；line 113 `find_address("睡觉")`；line 118 `Event(self.name, "正在", "睡觉", ...)`；line 121 `"被占用"`；line 208 `seed = [(h, "睡觉")]`；line 301 `event.fit(self.name, "对话")`；line 326-327 strip `"此时"` 前綴；line 461 `ignore_words=["空闲"]`；line 489 `"睡觉" in event.get_describe(False)`；line 491 `predicate == "待开始"`；line 509 `fit(predicate="对话")`；line 642 `event.fit(None, "此时", "空闲")`；line 669 `fit(self.name, "正在", "睡觉")` | **最高風險檔案**。「睡」「床」「空」等字簡繁同形唔使驚，但「对话→對話」「睡觉→睡覺」「待开始→待開始」「被占用→被佔用」全部斷裂。 |
| `modules/prompt/scratch.py` | `completion()` 嘅 pydantic `Field(description=...)` 隨 `model_json_schema()` 送去 LLM（llm_model.py:125-133）；failsafe 示例（line 115-163）；line 447/473 boolean 解析 `in ("true","yes","是","1")`；line 425 chat_history 模板句；line 408 failsafe `"空闲"`；line 370-373 `"此时"` 前綴處理 | description 用咩語言直接影響 LLM 輸出語言；failsafe 值必須係合法繁體狀態字串 |
| `modules/memory/event.py` | line 17-18, 53-54 預設 `predicate="此时"`、`object="空闲"` | 預設值要同 keywords.py 一致 |
| `modules/memory/associate.py` | line 221 `retrieve_chats()` 用 `"对话 " + name` 做 embedding 檢索 query | 儲存落嚟嘅 chat concept 係繁體，query 必須改做 `"對話 "`，否則檢索質素跌 |
| `modules/memory/schedule.py` | line 80-88 `"睡" in describe or "床" in describe` | 「睡」「床」簡繁同形，**唔使改但要回歸測試確認** |
| `modules/memory/spatial.py` | line 12-14 `"睡觉"`、`"living_area"`、`"床"` 地址鍵 | `"睡觉"` 鍵名要改「睡覺」，同 agent.py:113 一致 |
| `modules/utils/timer.py` | `daily_format_cn()`、`星期一…星期日`、`%Y年%m月%d日` | 星期字串簡繁同形（「星期一」兩邊一樣）；**唔使改** |
| `modules/model/llm_model.py` | `_completion()` 用 `return_type.model_json_schema()` 做 structured output；解析失敗時 return raw text | schema 內中文 description 來自 scratch.py；failsafe 路徑返回嘅 raw text 可能混入簡體（LLM 自由發揮時）→ 需要喺模板指令明確「一律使用繁體中文」 |
| `data/prompts/*.txt` | 29 個模板經 `compress.py`／scratch.py 載入 | 模板內 JSON 示例嘅 key（如 `"6:00"`）唔郁，只改 value |
| `frontend/static/assets/village/maze.json` + `agents/*/agent.json` | 地址以字串精確互相引用 | 必須單次原子轉換，半改狀態會令 `find_address` 搵唔到路 |
| `frontend/templates/main_script.html` | line 91 用角色目錄名載入 texture.png；line 155 字體 `黑体`；line 176-301 按鈕字串 | 角色目錄改名係連鎖操作 |

### 對其他 5 個系統

| 系統 | 本系統提供 | 本系統需要 |
|---|---|---|
| Setup／角色配置 | 繁體角色檔案＋地名表＋住所名（「空臥室」「主人房」已係繁體可用字形） | Setup 頁所有 label 用 glossary 用字；玩家輸入唔轉換 |
| Agent 推演引擎 | 繁體 prompt＋keywords.py；保證 `Agent.think` 行為不變 | 唔該佢哋唔好再 hardcode 新中文字串，新字串一律入 keywords.py |
| GM／決策 modal | 「故事回顧」時間線顯示嘅文字語言保證；`Concept` predicate/object 用字規範 | GM 寫入記憶流嘅事件描述要用繁體；GM 調整好感度嘅數值邏輯同語言無關 |
| 記憶注入（玩家選擇→記憶流） | 注入文本嘅語言規範（玩家自訂命令原樣注入，唔轉） | 注入時 `node_type`／poignancy 欄位用現有 `"chat"/"event"` 英文鍵，唔受語言影響 |
| Checkpoint／回放 | 回放 UI 文案 | **版本分界**：舊 checkpoint（簡體記憶）恢復後會簡繁混雜，需要 checkpoint 版本標記或遷移脚本（見邊界情況） |

---

## 邊界情況

| 場景 | 預期行為 |
|---|---|
| 1. LLM 唔跟指示，輸出簡體（例如 OpenAI 模型對「起床時間」嘅預設傾向） | 29 個模板尾部統一加指令：「一律使用繁體中文（香港書面語）回答」；關鍵字判斷（`"睡" in describe`）因「睡」簡繁同形仍有容錯；「對話」類斷裂關鍵字喺 `_completion` 層做 normalize：回傳前用 mapping（如 OpenCC `s2hk` 或自建 dict）將輸出標準化做繁體，先至進入 `event.fit()` 判斷。**呢個 normalize 層係本系統最重要嘅防線。** |
| 2. 簡繁同形字誤判（「睡」「床」「空閒／空闲」差異） | 「睡」「床」唔使改但要寫回歸測試：LLM 輸出「睡覺」（繁）時 `"睡" in describe` 仍然命中；「空闲」→「空閒」要連 `event.py` 預設值、`agent.py:295/461/642`、`scratch.py:408` 四處一齊改，測試覆蓋每處 |
| 3. 舊 checkpoint 恢復（`results/checkpoints/<name>/simulate-*.json` 內係簡體記憶） | 提供一次性遷移脚本：讀 checkpoint → 對所有 `describe`/address/scratch 文字跑 s2hk 轉換 → 寫返；未遷移嘅舊檔喺載入時 log warning「偵測到簡體 checkpoint，建議運行 migrate_checkpoint.py」；唔做靜默轉換以免破壞原始存檔 |
| 4. maze.json 同 agent.json 改咗一半（原子性失敗） | 轉換脚本單次 transaction 寫晒 26 個檔案（1 maze + 25 agents），寫入前逐個驗證：agent.json `spatial.tree` 嘅每個地名喺新 maze.json 搵到；腳本失敗時全部 rollback（先寫 temp dir 再 move）。CI 加一個 `validate_addresses.py` 檢查 |
| 5. 角色目錄改名連鎖斷裂 | 角色名只轉字形（伊莎贝拉→伊莎貝拉），`name` 欄位、目錄名、`start.py` personas 列表、main_script.html 引用四處用同一個 mapping 表生成；任何一處漏改，`find_address`/sprite 載入即刻斷 → 用脚本做，唔准手改 |
| 6. embedding 檢索語言不一致 | `associate.py:221` query 改「對話 」；新增概念全部繁體；混合狀態（舊記憶未遷移）靠邊界 3 嘅遷移脚本消除。qwen3-embedding 對簡繁混合有耐受，但唔依賴呢點 |
| 7. LLM structured output 解析失敗，failsafe 路徑返回 raw text | failsafe 字串本身要係繁體（`scratch.py:408` 嘅 `"空闲"`→「空閒」、示例日程 line 115-163）；raw text 落入 `plan["describe"]` 後經邊界 1 嘅 normalize 層，保證後續 keyword 判斷唔斷 |
| 8. 玩家自訂命令混雜簡體或廣東話口語 | 玩家輸入**原樣注入**記憶流，唔轉換（保留玩家原聲）；但 GM prompt 要註明「玩家命令可能係口語，請以書面語演繹角色反應」 |
| 9. 字體缺字（玩家部機冇黑體） | main_script.html `font` 改 `24px "PingFang TC","Microsoft JhengHei","Noto Sans TC",sans-serif`；Phaser text 渲染唔再依賴單一內地字體 |
| 10. 模板內 JSON 示例被當內容翻譯 | 轉換規則明確：JSON key（時間字串 `"6:00"`、schema 結構）唔郁，只翻 value 同自然語言句子；轉換後逐個模板跑 `json.loads` 驗證示例段仍可解析 |

---

## Done When

- [ ] `docs/prd/localization-glossary.md` 存在，列出全部簡→繁映射，至少覆蓋：29 個模板內全部詞彙、約 100 個 maze 地名、25 個角色名、全部邏輯關鍵字（對話／睡覺／空閒／此時／待開始／被佔用／床／是）
- [ ] 29 個 `data/prompts/*.txt` 全部繁體化，`${}` 佔位符名零改動，每個模板尾部有「一律使用繁體中文（香港書面語）」指令；每個含 JSON 示例嘅模板示例段 `json.loads` 可解析
- [ ] `scratch.py` 所有 `Field(description=...)`、failsafe 字串、模板句轉繁體；boolean 解析保留 `"是"`（繁體 LLM 都會答「是」）並加「係」做容錯
- [ ] 邏輯關鍵字收斂入單一模組（`keywords.py` 或等價），`agent.py`／`event.py`／`schedule.py`／`spatial.py`／`associate.py`／`scratch.py` 全部引用常量，代碼內零 hardcode 中文判斷字串（`grep -nP '[\x{4e00}-\x{9fff}]' modules/` 只剩註釋＋keywords.py）
- [ ] `_completion` 輸出有 s2hk normalize 層（或等價機制），單測：輸入「对话记录」「睡觉」→ 內部狀態為「對話」「睡覺」
- [ ] maze.json + 25 個 agent.json 由腳本原子轉換，`validate_addresses.py` 通過（每個 agent 嘅 living_area/睡覺地址喺 maze 搵到）；角色目錄、start.py personas、main_script.html 引用同步
- [ ] `frontend/templates/` 三個檔案無簡體；字體 fallback chain 已改；按鈕顯示「[運行]」「暫停」「[顯示對話]」
- [ ] checkpoint 遷移脚本存在並對一份真實舊 checkpoint 測試通過；載入未遷移檔案有 warning
- [ ] 端到端冒煙測試：用 config.json（OpenAI provider）跑 2 個回合、4 個角色，檢查 `results/checkpoints/*/conversation.json` 同 simulate-*.json：(a) 無簡體字（用簡體字表掃描，如「话觉闲时厅卫个們這學習體現」黑名單）；(b) agents 有正常瞓覺行為（事件流出現「睡覺」且 `find_address` 成功）；(c) 對話 node_type 正確標記為 "chat"
- [ ] `timer.py` 星期／日期格式確認無需改動並有測試鎖定（「星期一」等字簡繁同形）
- [ ] Glossary 移交其他 5 個系統負責人確認用字（特別係 GM 系統新增 prompt 嘅用字）
